"use strict";

// Presentation and selection state stay separate from the listing editor.
let aiOriginalWorkbenchRunning = false;

function aiOriginalListingChecks(row) {
  const prepared = row.ai_original || {};
  return [
    {label: "美客多类目", ok: /^CBT\d+$/.test(row.category_id || "")},
    {label: "商品属性", ok: aiOriginalAttributes(prepared.attributes).some(a => !["BRAND", "ITEM_CONDITION"].includes(String(a.id || "").toUpperCase()))},
    {label: "双语标题", ok: [row.title_es, row.title_pt].every(s => s?.trim() && [...s].length <= 60)},
    {label: "双语描述", ok: [row.description_es, row.description_pt].every(s => s?.trim())},
    {label: "AI 白底主图", ok: prepared.image_generation_method === "ai_image_edit" && String(row.main_image_url || "").includes("-ai-white.jpg")},
    {label: "重量与收益", ok: Number(row.weight_g) > 0 && Number(row.net_proceeds_usd) > 0 && !["calculated_volumetric", "legacy_unknown", "plugin_volumetric_fallback"].includes(row.weight_basis)}
  ];
}

function aiOriginalListingReady(row) {
  return row.ai_status === "completed" && row.review_status === "approved" && aiOriginalListingChecks(row).every(check => check.ok);
}

function decorateAiOriginalWorkbench() {
  const rows = typeof aiOriginalRows !== "undefined" ? aiOriginalRows : [];
  const counts = {
    total: rows.length,
    pending: rows.filter(row => !aiOriginalListingReady(row)).length,
    generated: rows.filter(row => row.ai_status === "completed").length,
    ready: rows.filter(aiOriginalListingReady).length
  };
  Object.entries(counts).forEach(([key, value]) => {
    const node = document.getElementById(`ai-original-count-${key}`);
    if (node) node.textContent = value;
  });
  document.querySelectorAll("#ai-original-grid .ai-original-card").forEach(card => {
    const checkbox = card.querySelector('input[type="checkbox"]');
    const row = rows.find(item => Number(item.id) === Number(checkbox?.value));
    if (!row || card.dataset.decorated) return;
    card.dataset.decorated = "1";
    checkbox.setAttribute("aria-label", `选择商品 ${row.original_1688?.title || row.title || row.id}`);
    const checks = aiOriginalListingChecks(row);
    const ready = aiOriginalListingReady(row);
    const status = ["pending", "processing", "completed", "failed"].includes(row.ai_status) ? row.ai_status : "pending";
    const header = document.createElement("header");
    header.className = "ai-original-card-head";
    header.innerHTML = `<label><span>1688 · ${aiOriginalEscape(row.original_1688?.source_1688_item_id || row.source_item_id)}</span></label><span class="ai-original-badge ${status}">${ready ? "已审核 · 待上架校验" : status === "completed" ? "AI 已生成 · 待完善审核" : aiOriginalStateLabel(status)}</span>`;
    header.querySelector("label").prepend(checkbox);
    card.prepend(header);
    const fields = card.querySelector(".ai-original-fields");
    if (fields) {
      const checklist = document.createElement("div");
      checklist.className = "ai-original-check-panel";
      checklist.innerHTML = `<strong>上架资料检查 <small>${checks.filter(check => check.ok).length}/${checks.length}</small></strong><ul>${checks.map(check => `<li class="${check.ok ? "ok" : "pending"}"><span>${check.ok ? "✓" : "○"}</span>${check.label}<small>${check.ok ? "已填写" : "待补齐"}</small></li>`).join("")}</ul><p>必填属性以美客多当前类目规则为准</p>`;
      fields.prepend(checklist);
    }
    // A collected supplier image is not an AI result.
    const generatedFigure = card.querySelectorAll(".ai-original-image-pair figure")[1];
    if (generatedFigure && !row.ai_original?.main_image_url) {
      const image = generatedFigure.querySelector("img");
      if (image) image.replaceWith(Object.assign(document.createElement("div"), {className: "ai-original-image-empty", textContent: "等待生成"}));
    }
    card.querySelectorAll(".ai-original-image-pair img").forEach(image => {
      const fallback = () => {
        image.replaceWith(Object.assign(document.createElement("div"), {className: "ai-original-image-empty", textContent: "图片暂不可用"}));
      };
      if (!image.getAttribute("src") || (image.complete && !image.naturalWidth)) fallback();
      else image.addEventListener("error", fallback, {once: true});
    });
    // Placeholder text must not count towards the title limit.
    card.querySelectorAll(".ai-original-copy > strong").forEach((label, index) => {
      if (index < 2) label.textContent = `${index ? "PT · 葡萄牙语" : "ES · 西班牙语"} ${[...(index ? row.title_pt : row.title_es) || ""].length}/60`;
    });
  });
  syncAiOriginalWorkbenchSelection();
}

function syncAiOriginalWorkbenchSelection() {
  const cards = [...document.querySelectorAll("#ai-original-grid .ai-original-card")];
  const selected = cards.filter(card => card.querySelector('input[type="checkbox"]')?.checked);
  cards.forEach(card => card.classList.toggle("selected", Boolean(card.querySelector('input[type="checkbox"]')?.checked)));
  const all = document.getElementById("ai-original-select-all");
  if (all) { all.checked = cards.length > 0 && cards.length === selected.length; all.indeterminate = selected.length > 0 && selected.length < cards.length; }
  const count = typeof aiOriginalSelected !== "undefined" ? aiOriginalSelected.size : 0;
  const selectedRows = aiOriginalRows.filter(row => aiOriginalSelected.has(Number(row.id)));
  const publish = document.getElementById("ai-original-publish");
  if (publish) {
    publish.disabled = !count || !selectedRows.every(aiOriginalListingReady);
    publish.title = count && !selectedRows.every(aiOriginalListingReady) ? "所选产品仍有缺失资料或尚未审核" : "";
  }
  const process = document.getElementById("ai-original-process");
  if (process && aiOriginalWorkbenchRunning) process.disabled = true;
  const hint = document.getElementById("ai-original-selection-hint");
  if (hint) hint.textContent = count ? `所选 ${count} 件 · ${selectedRows.filter(aiOriginalListingReady).length} 件已完善并审核` : "选择商品后，批量生成刊登内容";
}

document.addEventListener("DOMContentLoaded", () => {
  const page = document.getElementById("tab-ai-original-products");
  if (!page) return;
  const settings = page.querySelector(".ai-original-ai-settings");
  if (settings) {
    const advanced = document.createElement("details");
    advanced.className = "ai-original-settings-disclosure";
    advanced.innerHTML = '<summary>AI 模型配置 <small>文案与白底图生成服务</small></summary>';
    settings.before(advanced);
    advanced.append(settings);
    const actions = document.createElement("div");
    actions.className = "ai-original-bulk-actions";
    actions.innerHTML = '<span id="ai-original-selection-hint">选择商品后，批量生成刊登内容</span>';
    const process = document.getElementById("ai-original-process");
    if (process) actions.append(process);
    advanced.after(actions);
  }
  const grid = document.getElementById("ai-original-grid");
  if (grid) new MutationObserver(decorateAiOriginalWorkbench).observe(grid, {childList: true});
  page.addEventListener("change", syncAiOriginalWorkbenchSelection);
  // Existing polling owns the task; preserve its running state on selection changes.
  const originalStatusRenderer = renderAiOriginalTaskStatus;
  renderAiOriginalTaskStatus = data => { aiOriginalWorkbenchRunning = Boolean(data.running); originalStatusRenderer(data); syncAiOriginalWorkbenchSelection(); };
  decorateAiOriginalWorkbench();
});
