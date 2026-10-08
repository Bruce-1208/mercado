"use strict";

// Presentation and selection state stay separate from the listing editor.
let aiOriginalWorkbenchRunning = false;

function aiOriginalListingReady(row) {
  return aiOriginalProductStatus(row) === "approved";
}

function aiOriginalManualCheckItems(row, card) {
  const draft = {...row};
  const dirty = new Set();
  for (const [field, prefix, label] of [["net_proceeds_usd", "net", "净收益"], ["weight_g", "weight", "实重"]]) {
    const input = card.querySelector(`#ai-original-${prefix}-${Number(row.id)}`);
    if (!input) continue;
    draft[field] = input.value;
    const invalid = !(Number(input.value) > 0) || !Number.isFinite(Number(input.value)) ||
      (field === "weight_g" && Number(input.value) === Number(row[field]) &&
       ["calculated_volumetric", "legacy_unknown", "plugin_volumetric_fallback"].includes(row.weight_basis));
    aiOriginalMarkField(input, invalid ? (field === "weight_g" ? "请填写真实实重" : "请填写有效净收益") : "");
    if (Number(input.value) !== Number(row[field])) {
      dirty.add(label);
      if (field === "weight_g") draft.weight_basis = "ai_original_manual";
    }
  }
  return aiOriginalManualListingChecks(draft).map(check => {
    const pendingSave = check.ok && dirty.has(check.label);
    const saved = check.ok && !pendingSave;
    return `<li class="${saved ? "ok" : "pending"}"><span>${saved ? "✓" : "○"}</span>${check.label}<small>${pendingSave ? "已填写待保存" : saved ? "已填写" : "待补齐"}</small></li>`;
  }).join("");
}

function updateAiOriginalManualChecks(event) {
  if (!/^ai-original-(net|weight)-\d+$/.test(event.target?.id || "")) return;
  const card = event.target.closest(".ai-original-card");
  const row = aiOriginalRows.find(item => Number(item.id) === Number(event.target.id.split("-").pop()));
  const list = card?.querySelector(".ai-original-manual-checks");
  if (row && list) list.innerHTML = aiOriginalManualCheckItems(row, card);
}


// One viewport tooltip avoids clipping inside fixed cards and offscreen containment.
function setupAiOriginalCardTooltip(grid) {
  const tooltip = document.createElement("div");
  tooltip.id = "ai-original-card-tooltip";
  tooltip.className = "ai-original-card-tooltip";
  tooltip.setAttribute("role", "tooltip");
  tooltip.hidden = true;
  document.body.append(tooltip);
  let active = null;
  const hide = () => {
    active?.removeAttribute("aria-describedby");
    active = null;
    tooltip.hidden = true;
  };
  const position = () => {
    const bounds = active.getBoundingClientRect();
    const box = tooltip.getBoundingClientRect();
    tooltip.style.left = `${Math.max(12, Math.min(bounds.right - box.width, innerWidth - box.width - 12))}px`;
    const below = bounds.bottom + 8;
    const top = below + box.height <= innerHeight - 12 ? below : bounds.top - box.height - 8;
    tooltip.style.top = `${Math.max(12, top)}px`;
  };
  const show = event => {
    const badge = event.target.closest("[data-card-notice]");
    if (!badge) return;
    hide();
    active = badge;
    tooltip.textContent = badge.dataset.cardNotice;
    tooltip.hidden = false;
    badge.setAttribute("aria-describedby", tooltip.id);
    position();
  };
  grid.addEventListener("mouseover", show);
  grid.addEventListener("focusin", show);
  grid.addEventListener("mouseout", event => {
    if (active && !active.contains(event.relatedTarget) && !tooltip.contains(event.relatedTarget)) hide();
  });
  tooltip.addEventListener("mouseleave", hide);
  grid.addEventListener("focusout", hide);
  grid.addEventListener("keydown", event => { if (event.key === "Escape") hide(); });
  window.addEventListener("scroll", event => {
    if (!active || event.target === tooltip) return;
    if (active.matches(":hover, :focus")) position();
    else hide();
  }, true);
  window.addEventListener("resize", hide);
  new MutationObserver(hide).observe(grid, {childList: true});
}

function decorateAiOriginalWorkbench() {
  const rows = typeof aiOriginalRows !== "undefined" ? aiOriginalRows : [];
  const counts = {total: rows.length};
  const rowsById = new Map();
  Object.keys(aiOriginalProductStatusLabels).forEach(status => { counts[status] = 0; });
  rows.forEach(row => {
    rowsById.set(Number(row.id), row);
    const status = aiOriginalProductStatus(row);
    if (Object.hasOwn(counts, status)) counts[status] += 1;
  });
  Object.entries(counts).forEach(([key, value]) => {
    const node = document.getElementById(`ai-original-count-${key}`);
    if (node) node.textContent = value;
  });
  document.querySelectorAll("#ai-original-grid .ai-original-card").forEach(card => {
    const checkbox = card.querySelector('input[type="checkbox"]');
    const row = rowsById.get(Number(checkbox?.value));
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
    const notices = [...card.querySelectorAll(".ai-original-source > p.bad, .ai-original-source > p.pending, .ai-original-copy > p.bad:not(.ai-original-title-text)")];
    const messages = [...notices].sort((a, b) => Number(b.classList.contains("bad")) - Number(a.classList.contains("bad")))
      .map(node => node.textContent.trim()).filter(Boolean);
    if (!isMercado) {
      const draft = aiOriginalPlatformDraft(row);
      messages.push(...aiOriginalValidationMessages(draft, "errors"), ...aiOriginalValidationMessages(draft, "warnings"));
    }
    const missingChecks = checks.filter(check => !check.ok).map(check => `${check.label}待修正`);
    if (isMercado) {
      missingChecks.push(...aiOriginalManualListingChecks(row).filter(check => !check.ok).map(check => `${check.label}待补齐`));
      if (aiOriginalProductStatus(row) === "suspected") messages.push("疑似侵权，请核实商品和刊登资料");
    }
    messages.push(...missingChecks);
    const issues = [...new Set(messages)];
    if (issues.length) {
      card.classList.add("has-issues");
      const panel = document.createElement("details");
      panel.className = "ai-original-issues";
      panel.open = true;
      panel.innerHTML = `<summary>⚠ 待修正 / 核实（${issues.length}）</summary><ul>${issues.map(message => `<li>${aiOriginalEscape(message)}</li>`).join("")}</ul>`;
      card.querySelector(".ai-original-copy")?.prepend(panel);
    }
    if (messages.length) {
      const badge = header.querySelector(".ai-original-badge");
      badge.dataset.cardNotice = [...new Set(messages)].join("\n\n");
      badge.tabIndex = 0;
      badge.setAttribute("aria-label", `${badgeLabel}，悬浮或聚焦查看详情`);
      notices.forEach(node => node.remove());
    }
    const sourceTitle = card.querySelector(".ai-original-source h4");
    if (sourceTitle) sourceTitle.title = sourceTitle.textContent;
    const fields = card.querySelector(".ai-original-fields");
    if (fields) {
      const checklist = document.createElement("details");
      checklist.className = "ai-original-check-panel";
      const checkItems = items => items.map(check => `<li class="${check.ok ? "ok" : "pending"}"><span>${check.ok ? "✓" : "○"}</span>${check.label}<small>${check.ok ? "已填写" : "待补齐"}</small></li>`).join("");
      checklist.innerHTML = `<summary>刊登检查 · ${checks.filter(check => check.ok).length}/${checks.length} 项完成</summary><ul>${checkItems(checks)}</ul>${isMercado ? `<strong>人工填写的上架资料</strong><ul class="ai-original-manual-checks">${aiOriginalManualCheckItems(row, card)}</ul><p>包装尺寸、实重、净收益必须填写后才能上架。净收益按已采集的 1688 价格自动计算并保存；修改金额或实重后需点击“保存参数并审核通过”，不参与 AI 生成成败判断。</p>` : "<p>提交前仍需完善平台要求的字段</p>"}`;

      fields.prepend(checklist);
      if (isMercado) {
        aiOriginalMarkField(card.querySelector(`#ai-original-category-${row.id}`), /^CBT\d+$/.test(row.category_id || "") ? "" : "请填写有效 CBT 类目");
        const dimensions = fields.querySelector(".ai-original-package-dimensions");
        dimensions?.classList.toggle("ai-original-field-issue", !aiOriginalManualListingChecks(row)[0].ok);
      }
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
    card.querySelectorAll(".ai-original-copy > .ai-original-title-label").forEach((label, index) => {
      if (isMercado && index < 2) label.textContent = `${index ? "PT · 葡萄牙语" : "ES · 西班牙语"} ${[...(index ? row.title_pt : row.title_es) || ""].length}/60`;
    });
    // Keep editable amounts in view and expand the full draft only when needed.
    const copy = card.querySelector(".ai-original-copy");
    if (copy) {
      const details = document.createElement("details");
      details.className = "ai-original-copy-details";
      const summary = document.createElement("summary");
      summary.textContent = "刊登文案与属性";
      details.append(summary);
      const visible = [...copy.querySelectorAll(":scope > .ai-original-title-label, :scope > .ai-original-title-text, :scope > .ai-original-issues, :scope > .ai-original-edit-button, :scope > .ai-original-draft-actions")];
      [...copy.childNodes].forEach(node => { if (!visible.includes(node)) details.append(node); });
      const actions = copy.querySelector(":scope > .ai-original-edit-button, :scope > .ai-original-draft-actions");
      if (actions) actions.before(details);
      else copy.append(details);
    }
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
  if (grid) {
    setupAiOriginalCardTooltip(grid);
    new MutationObserver(decorateAiOriginalWorkbench).observe(grid, {childList: true});
  }
  page.addEventListener("change", syncAiOriginalWorkbenchSelection);
  page.addEventListener("input", updateAiOriginalManualChecks);
  page.addEventListener("change", updateAiOriginalManualChecks);
  // Existing polling owns the task; preserve its running state on selection changes.
  const originalStatusRenderer = renderAiOriginalTaskStatus;
  renderAiOriginalTaskStatus = data => { aiOriginalWorkbenchRunning = Boolean(data.running); originalStatusRenderer(data); syncAiOriginalWorkbenchSelection(); };
  decorateAiOriginalWorkbench();
});
