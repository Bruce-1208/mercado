"use strict";

// Presentation and selection state stay separate from the listing editor.
let aiOriginalWorkbenchRunning = false;

function aiOriginalListingChecks(row) {
  if (aiOriginalTargetPlatform() !== "mercadolibre") {
    const draft = aiOriginalPlatformDraft(row);
    const profile = aiOriginalPlatformProfile();
    return [
      {label: "本平台草稿", ok: Boolean(draft) && draft.status === "completed"},
      {label: `${profile.language_label || profile.language}标题`, ok: Boolean(draft?.title?.trim()) && [...draft.title].length <= Number(profile.title_limit)},
      {label: "商品描述", ok: Boolean(draft?.description?.trim()) && (!profile.description_limit || [...draft.description].length <= Number(profile.description_limit))},
      {label: "商品主图", ok: Boolean(draft?.main_image_url)},
      {label: "平台基础规则", ok: Boolean(draft?.validation) && !aiOriginalValidationMessages(draft, "errors").length}
    ];
  }
  const prepared = row.ai_original || {};
  return [
    {label: "美客多类目", ok: /^CBT\d+$/.test(row.category_id || "")},
    {label: "商品属性", ok: aiOriginalAttributes(prepared.attributes).some(a => !["BRAND", "ITEM_CONDITION"].includes(String(a.id || "").toUpperCase()))},
    {label: "双语标题", ok: [row.title_es, row.title_pt].every(s => s?.trim() && [...s].length <= 60)},
    {label: "双语描述", ok: [row.description_es, row.description_pt].every(s => s?.trim())},
    {label: "AI 白底主图", ok: ["ai_image_edit", "local_background_removal"].includes(prepared.image_generation_method) && String(row.main_image_url || "").includes("-ai-white.jpg")},
    {label: "重量与收益", ok: Number(row.weight_g) > 0 && Number(row.net_proceeds_usd) > 0 && !["calculated_volumetric", "legacy_unknown", "plugin_volumetric_fallback"].includes(row.weight_basis)}
  ];
}

function aiOriginalListingReady(row) {
  return aiOriginalPlatformStatus(row) === "completed" && (aiOriginalTargetPlatform() !== "mercadolibre" || row.review_status === "approved") && aiOriginalListingChecks(row).every(check => check.ok);
}

function decorateAiOriginalWorkbench() {
  const rows = typeof aiOriginalRows !== "undefined" ? aiOriginalRows : [];
  const counts = {
    total: rows.length,
    pending: rows.filter(row => !aiOriginalListingReady(row)).length,
    generated: rows.filter(row => aiOriginalPlatformStatus(row) === "completed").length,
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
    const platform = aiOriginalTargetPlatform();
    const isMercado = platform === "mercadolibre";
    const currentStatus = aiOriginalPlatformStatus(row);
    const status = ["pending", "processing", "completed", "failed"].includes(currentStatus) ? currentStatus : "pending";
    const header = document.createElement("header");
    header.className = "ai-original-card-head";
    const reviewStatus = String(row.review_status || "unreviewed");
    const reviewLabel = {unreviewed: "待审核", approved: "审核通过", suspected: "疑似风险", infringing: "侵权", risk: "待复核"}[reviewStatus] || reviewStatus;
    const badgeLabel = isMercado ? (ready ? "已审核 · 待上架校验" : status === "completed" ? `AI 已生成 · ${aiOriginalEscape(reviewLabel)}` : aiOriginalStateLabel(status)) : (ready ? "草稿已生成 · 基础检查通过" : status === "completed" ? "草稿已生成 · 待完善" : aiOriginalStateLabel(status));
    header.innerHTML = `<label><span>1688 · ${aiOriginalEscape(row.original_1688?.source_1688_item_id || row.source_item_id)}</span></label><span class="ai-original-badge ${status}">${badgeLabel}</span>`;
    header.querySelector("label").prepend(checkbox);
    card.prepend(header);
    const fields = card.querySelector(".ai-original-fields");
    if (fields) {
      const checklist = document.createElement("div");
      checklist.className = "ai-original-check-panel";
      checklist.innerHTML = `<strong>上架资料检查 <small>${checks.filter(check => check.ok).length}/${checks.length}</small></strong><ul>${checks.map(check => `<li class="${check.ok ? "ok" : "pending"}"><span>${check.ok ? "✓" : "○"}</span>${check.label}<small>${check.ok ? "已填写" : "待补齐"}</small></li>`).join("")}</ul><p>${isMercado ? "必填属性以美客多当前类目规则为准" : "基础检查不等于平台审核通过；提交前仍需完善平台要求的字段"}</p>`;
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
    publish.title = count ? `审核通过 ${readyCount} 件，将自动忽略 ${skippedCount} 件未审核通过的产品` : "";
  }
  const process = document.getElementById("ai-original-process");
  if (process && (aiOriginalWorkbenchRunning || aiOriginalSubmitting)) process.disabled = true;
  const hint = document.getElementById("ai-original-selection-hint");
  if (hint) hint.textContent = aiOriginalTargetPlatform() === "mercadolibre"
    ? (count ? `所选 ${count} 件 · 审核通过 ${readyCount} 件 · 将忽略 ${skippedCount} 件` : "选择商品后，批量生成刊登内容")
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
