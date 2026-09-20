"use strict";

let aiOriginalRows = [];
let aiOriginalSelected = new Set();
let aiOriginalTaskTimer = null;
let aiOriginalPublishTimer = null;

function aiOriginalEscape(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function aiOriginalStatus(message, kind = "") {
  const node = document.getElementById("ai-original-task-status");
  if (node) { node.textContent = message; node.className = `ai-original-status ${kind}`.trim(); }
}

function aiOriginalStateLabel(status) {
  return {pending: "待处理", processing: "处理中", completed: "已完成", failed: "失败"}[status] || status || "待处理";
}

function renderAiOriginalProducts() {
  const grid = document.getElementById("ai-original-grid");
  if (!grid) return;
  const filter = document.getElementById("ai-original-status-filter")?.value || "";
  const rows = filter ? aiOriginalRows.filter(row => row.ai_status === filter) : aiOriginalRows;
  document.getElementById("ai-original-total").textContent = `共 ${rows.length} 件`;
  grid.innerHTML = rows.length ? rows.map(row => {
    const original = row.original_1688 || {};
    const status = String(row.ai_status || "pending");
    const image = row.main_image_url || original.main_image_url || "";
    const titleEs = row.title_es || "等待 AI 生成西班牙语标题";
    const titlePt = row.title_pt || "等待 AI 生成葡萄牙语标题";
    return `<article class="ai-original-card">
      <input type="checkbox" value="${Number(row.id)}" ${aiOriginalSelected.has(Number(row.id)) ? "checked" : ""} onchange="toggleAiOriginalProduct(${Number(row.id)}, this.checked)">
      <img src="${aiOriginalEscape(image)}" alt="1688 商品首图" loading="lazy">
      <div class="ai-original-source"><span class="ai-original-badge ${aiOriginalEscape(status)}">${aiOriginalEscape(aiOriginalStateLabel(status))}</span><h4>${aiOriginalEscape(original.title || row.title)}</h4><p>1688：${aiOriginalEscape(original.source_1688_item_id || row.source_item_id)}</p><p>采购价：${aiOriginalEscape(original.price ?? row.price ?? "-")} CNY</p>${row.ai_error ? `<p class="bad">${aiOriginalEscape(row.ai_error)}</p>` : ""}</div>
      <div class="ai-original-copy"><strong>西语 ${titleEs.length}/60</strong><p class="${titleEs.length > 60 ? "bad" : ""}">${aiOriginalEscape(titleEs)}</p><strong>葡语 ${titlePt.length}/60</strong><p class="${titlePt.length > 60 ? "bad" : ""}">${aiOriginalEscape(titlePt)}</p><p>详情：${row.description_es ? "已生成西语 / 葡语新详情" : "待生成"}</p></div>
      <div class="ai-original-fields">
        <label>实重(g)<input id="ai-original-weight-${Number(row.id)}" type="number" min="1" step="1" value="${aiOriginalEscape(row.weight_g || "")}"></label>
        <label>净收益USD<input id="ai-original-net-${Number(row.id)}" type="number" min="0.01" step="0.01" value="${aiOriginalEscape(row.net_proceeds_usd || "")}"></label>
        <label class="wide">CBT 类目（可留空自动预测）<input id="ai-original-category-${Number(row.id)}" type="text" value="${aiOriginalEscape(row.category_id || "")}" placeholder="例如 CBT1234"></label>
        <button class="secondary" type="button" onclick="saveAiOriginalProduct(${Number(row.id)})">保存参数并审核通过</button>
      </div>
    </article>`;
  }).join("") : '<div class="empty-state">暂无符合条件的 AI 原创产品；请先用泽顺插件采集 1688 商品。</div>';
  updateAiOriginalSelection();
}

async function loadAiOriginalProducts() {
  const search = document.getElementById("ai-original-search")?.value?.trim() || "";
  aiOriginalStatus("正在读取 1688 产品...");
  try {
    const response = await fetch(`/api/ai-original-products?limit=500&search=${encodeURIComponent(search)}`, {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    aiOriginalRows = payload.data?.rows || [];
    const validIds = new Set(aiOriginalRows.map(row => Number(row.id)));
    aiOriginalSelected = new Set([...aiOriginalSelected].filter(id => validIds.has(id)));
    renderAiOriginalProducts();
    aiOriginalStatus(`已加载 ${aiOriginalRows.length} 件 1688 产品`, "success");
  } catch (error) { aiOriginalStatus(`读取失败：${error.message || error}`, "error"); }
}

function toggleAiOriginalProduct(id, checked) {
  if (checked) aiOriginalSelected.add(Number(id)); else aiOriginalSelected.delete(Number(id));
  updateAiOriginalSelection();
}

function toggleAllAiOriginalProducts(checked) {
  const filter = document.getElementById("ai-original-status-filter")?.value || "";
  aiOriginalRows.filter(row => !filter || row.ai_status === filter).forEach(row => {
    if (checked) aiOriginalSelected.add(Number(row.id)); else aiOriginalSelected.delete(Number(row.id));
  });
  renderAiOriginalProducts();
}

function updateAiOriginalSelection() {
  const count = aiOriginalSelected.size;
  const summary = document.getElementById("ai-original-selection");
  if (summary) summary.textContent = `已选择 ${count} 件`;
  const process = document.getElementById("ai-original-process");
  const publish = document.getElementById("ai-original-publish");
  if (process) process.disabled = !count;
  if (publish) publish.disabled = !count;
}

async function processSelectedAiOriginalProducts() {
  const ids = [...aiOriginalSelected];
  if (!ids.length) return;
  const button = document.getElementById("ai-original-process");
  button.disabled = true;
  aiOriginalStatus(`正在创建 ${ids.length} 件 AI 原创任务...`);
  try {
    const response = await fetch("/api/ai-original-products/process", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      product_item_ids: ids,
      api_key: document.getElementById("ai-original-api-key")?.value || "",
      base_url: document.getElementById("ai-original-base-url")?.value || "",
      model: document.getElementById("ai-original-model")?.value || "",
      workers: Number(document.getElementById("ai-original-workers")?.value || 3)
    })});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    renderAiOriginalTaskStatus(payload.data || {});
    loadAiOriginalTaskStatus();
  } catch (error) { aiOriginalStatus(`启动失败：${error.message || error}`, "error"); button.disabled = false; }
}

function renderAiOriginalTaskStatus(data) {
  const text = `${data.message || "AI 原创任务"} · ${Number(data.processed_count || 0)}/${Number(data.requested_count || 0)} · 成功 ${Number(data.completed_count || 0)} · 失败 ${Number(data.failed_count || 0)}`;
  aiOriginalStatus(text, data.status === "error" ? "error" : (!data.running && data.status !== "idle" ? "success" : ""));
  const button = document.getElementById("ai-original-process");
  if (button) { button.textContent = data.running ? "AI 任务执行中..." : "执行所选 AI 任务"; button.disabled = Boolean(data.running) || !aiOriginalSelected.size; }
}

async function loadAiOriginalTaskStatus() {
  if (aiOriginalTaskTimer) clearTimeout(aiOriginalTaskTimer);
  try {
    const response = await fetch("/api/ai-original-products/process/status", {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    renderAiOriginalTaskStatus(payload.data || {});
    if (payload.data?.running) aiOriginalTaskTimer = setTimeout(loadAiOriginalTaskStatus, 1500);
    else if (payload.data?.status !== "idle") await loadAiOriginalProducts();
  } catch (error) { aiOriginalStatus(`任务状态读取失败：${error.message || error}`, "error"); }
}

async function saveAiOriginalProduct(id) {
  const weight = Number(document.getElementById(`ai-original-weight-${id}`)?.value || 0);
  const netProceeds = Number(document.getElementById(`ai-original-net-${id}`)?.value || 0);
  const row = aiOriginalRows.find(item => Number(item.id) === Number(id));
  if (row?.ai_status !== "completed") {
    aiOriginalStatus("请先完成该产品的 AI 原创任务，再审核通过", "error");
    return;
  }
  if (!(weight > 0) || !(netProceeds > 0)) {
    aiOriginalStatus("审核通过前必须填写大于 0 的实重和净收益 USD", "error");
    return;
  }
  try {
    const response = await fetch(`/api/ai-original-products/${id}`, {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      weight_g: weight,
      net_proceeds_usd: netProceeds,
      category_id: document.getElementById(`ai-original-category-${id}`)?.value?.trim() || "",
      review_status: "approved"
    })});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    aiOriginalStatus(`产品 ${id} 已保存并审核通过`, "success");
    await loadAiOriginalProducts();
  } catch (error) { aiOriginalStatus(`保存失败：${error.message || error}`, "error"); }
}

async function loadAiOriginalStores() {
  const select = document.getElementById("ai-original-stores");
  if (!select || select.dataset.loaded === "1") return;
  try {
    const response = await fetch("/api/mercado-tokens", {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    const rows = (payload.data?.rows || []).filter(row => Number(row.id) > 0 && row.enabled !== false && String(row.site_id || "").toUpperCase() === "CBT" && (row.status !== "expired" || row.has_refresh_token));
    select.innerHTML = rows.map(row => `<option value="${Number(row.id)}">${aiOriginalEscape(row.display_name || row.nickname || `账号 ${row.id}`)} · ${aiOriginalEscape(row.site_id || "CBT")}</option>`).join("") || '<option value="">暂无可用店铺</option>';
    select.dataset.loaded = "1";
  } catch (error) { select.innerHTML = '<option value="">店铺加载失败</option>'; }
}

async function publishSelectedAiOriginalProducts() {
  const ids = [...aiOriginalSelected];
  const tokenIds = [...(document.getElementById("ai-original-stores")?.selectedOptions || [])].map(option => Number(option.value)).filter(Boolean);
  const siteIds = [...document.querySelectorAll('input[name="ai-original-site"]:checked')].map(input => input.value);
  const status = document.getElementById("ai-original-publish-status");
  if (!ids.length || !tokenIds.length || !siteIds.length) { status.textContent = "请选择产品、对应店铺和至少一个目标站点"; status.className = "ai-original-status error"; return; }
  if (!window.confirm(`确定把 ${ids.length} 件产品上架到 ${tokenIds.length} 个店铺、${siteIds.length} 个站点吗？`)) return;
  try {
    const response = await fetch("/api/mercado-products/publish", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      product_item_ids: ids, selection_mode: "accounts", token_ids: tokenIds, site_ids: siteIds,
      quantity: Number(document.getElementById("ai-original-quantity")?.value || 500),
      worker_count: Number(document.getElementById("ai-original-publish-workers")?.value || 8)
    })});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    status.textContent = payload.data?.message || "已启动批量上架"; status.className = "ai-original-status success";
    loadAiOriginalPublishStatus();
  } catch (error) { status.textContent = `上架失败：${error.message || error}`; status.className = "ai-original-status error"; }
}

async function loadAiOriginalPublishStatus() {
  if (aiOriginalPublishTimer) clearTimeout(aiOriginalPublishTimer);
  const status = document.getElementById("ai-original-publish-status");
  try {
    const response = await fetch("/api/mercado-products/publish/status", {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") return;
    const data = payload.data || {};
    if (data.status && data.status !== "idle") status.textContent = `${data.message || "批量上架"} · ${Number(data.processed_count || 0)}/${Number(data.requested_count || 0)} · 成功 ${Number(data.published_count || 0)} · 失败 ${Number(data.failed_count || 0)}`;
    if (data.running) aiOriginalPublishTimer = setTimeout(loadAiOriginalPublishStatus, 1600);
    else if (data.status && data.status !== "idle") await loadAiOriginalProducts();
  } catch (_) {}
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("ai-original-search")?.addEventListener("keydown", event => { if (event.key === "Enter") loadAiOriginalProducts(); });
  document.getElementById("ai-original-status-filter")?.addEventListener("change", renderAiOriginalProducts);
  if (new URLSearchParams(location.search).get("tab") === "ai-original-products" && typeof switchTab === "function") switchTab("ai-original-products");
});
