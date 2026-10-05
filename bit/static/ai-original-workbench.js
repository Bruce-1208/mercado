"use strict";

// Presentation and selection state stay separate from the listing editor.
let aiOriginalWorkbenchRunning = false;

function aiOriginalListingReady(row) {
  return aiOriginalProductStatus(row) === "approved";
}

function decorateAiOriginalWorkbench() {
  const rows = typeof aiOriginalRows !== "undefined" ? aiOriginalRows : [];
  const counts = {total: rows.length};
  Object.keys(aiOriginalProductStatusLabels).forEach(status => {
    counts[status] = rows.filter(row => aiOriginalProductStatus(row) === status).length;
  });
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
    const platform = aiOriginalTargetPlatform();
    const isMercado = platform === "mercadolibre";
    const status = aiOriginalProductStatus(row);
    const header = document.createElement("header");
    header.className = "ai-original-card-head";
    const badgeLabel = aiOriginalProductStatusLabels[status];
    header.innerHTML = `<label><span>1688 · ${aiOriginalEscape(row.original_1688?.source_1688_item_id || row.source_item_id)}</span></label><span class="ai-original-badge ${status}">${badgeLabel}</span>`;
    header.querySelector("label").prepend(checkbox);
    card.prepend(header);
    const fields = card.querySelector(".ai-original-fields");
    if (fields) {
      const checklist = document.createElement("div");
      checklist.className = "ai-original-check-panel";
      const manualChecks = isMercado ? aiOriginalManualListingChecks(row) : [];
      const checkItems = items => items.map(check => `<li class="${check.ok ? "ok" : "pending"}"><span>${check.ok ? "✓" : "○"}</span>${check.label}<small>${check.ok ? "已填写" : "待补齐"}</small></li>`).join("");
      checklist.innerHTML = `<strong>刊登内容检查 <small>${checks.filter(check => check.ok).length}/${checks.length}</small></strong><ul>${checkItems(checks)}</ul>${isMercado ? `<strong>人工填写的上架资料</strong><ul>${checkItems(manualChecks)}</ul><p>包装尺寸、实重、净收益必须填写后才能上架，由人工填写，不参与 AI 生成成败判断。</p>` : "<p>提交前仍需完善平台要求的字段</p>"}`;

      fields.prepend(checklist);
    }
    // A collected supplier image is not an AI result.
    const generatedFigure = card.querySelectorAll(".ai-original-image-pair figure")[1];
    if (generatedFigure && !aiOriginalPlatformDraft(row)?.main_image_url) {
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
      if (isMercado && index < 2) label.textContent = `${index ? "PT · 葡萄牙语" : "ES · 西班牙语"} ${[...(index ? row.title_pt : row.title_es) || ""].length}/60`;
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
  const readyCount = aiOriginalPublishableSelection().length;
  const skippedCount = count - readyCount;
  const publish = document.getElementById("ai-original-publish");
  if (publish) {
    publish.disabled = aiOriginalTargetPlatform() !== "mercadolibre" || !count || aiOriginalSubmitting;
    publish.title = count ? `通过 ${readyCount} 件，将自动忽略 ${skippedCount} 件非“通过”状态的产品` : "";
  }
  const process = document.getElementById("ai-original-process");
  if (process && (aiOriginalWorkbenchRunning || aiOriginalSubmitting)) process.disabled = true;
  const hint = document.getElementById("ai-original-selection-hint");
  if (hint) hint.textContent = aiOriginalTargetPlatform() === "mercadolibre"
    ? (count ? `所选 ${count} 件 · 通过 ${readyCount} 件 · 将忽略 ${skippedCount} 件` : "选择商品后，批量生成刊登内容")
    : (count ? `所选 ${count} 件 · 将生成 ${aiOriginalPlatformProfile().label} 草稿` : `选择商品后，批量生成 ${aiOriginalPlatformProfile().label} 草稿`);
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
