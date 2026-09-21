"use strict";

let products1688Rows = [];
let products1688Selected = new Set();
let products1688Page = 1;
const PRODUCTS_1688_PAGE_SIZE = 24;

function products1688Escape(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function products1688Original(row) {
  return row?.original_1688 && typeof row.original_1688 === "object" ? row.original_1688 : {};
}

function products1688Images(row) {
  const original = products1688Original(row);
  const values = [original.main_image_url, ...(Array.isArray(original.images) ? original.images : [])];
  return [...new Set(values.map(value => String(value || "").trim()).filter(value => /^https?:\/\//i.test(value)))].slice(0, 20);
}

function products1688Status(row) {
  const status = String(row?.ai_status || "pending").toLowerCase();
  return {pending: "待处理", processing: "AI处理中", completed: "AI已完成", failed: "处理失败"}[status] || "待处理";
}

function products1688ReviewStatus(row) {
  return {
    unreviewed: "未审核",
    approved: "已审核",
    suspected: "疑似风险",
    infringing: "侵权",
    risk: "待复核"
  }[String(row?.review_status || "unreviewed").toLowerCase()] || "未审核";
}

function products1688Category(row) {
  const original = products1688Original(row);
  return original.category_name || original.category_id || "1688货源";
}

function products1688Money(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? `￥${number.toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : products1688Escape(value);
}

function products1688Date(value) {
  if (!value) return "—";
  const date = new Date(String(value).replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return products1688Escape(value);
  return new Intl.DateTimeFormat("zh-CN", {year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"}).format(date);
}

function products1688Weight(row) {
  const original = products1688Original(row);
  const weight = Number(original.weight_g ?? row.weight_g);
  return Number.isFinite(weight) && weight > 0 ? `${weight.toLocaleString("zh-CN")} g` : "—";
}

function products1688Dimensions(row) {
  const original = products1688Original(row);
  const values = [original.package_length_cm ?? row.package_length_cm, original.package_width_cm ?? row.package_width_cm, original.package_height_cm ?? row.package_height_cm];
  const present = values.map(value => Number(value)).filter(value => Number.isFinite(value) && value > 0);
  return present.length === 3 ? `${present.map(value => value.toLocaleString("zh-CN")).join(" × ")} cm` : "—";
}

function products1688SetStatus(message, kind = "") {
  const node = document.getElementById("products-1688-status");
  if (node) {
    node.textContent = message;
    node.className = `p1688-toolbar-note ${kind}`.trim();
  }
}

function products1688UpdateSummary(total = products1688Rows.length) {
  const completed = products1688Rows.filter(row => row.ai_status === "completed").length;
  const pending = products1688Rows.filter(row => !row.ai_status || row.ai_status === "pending").length;
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const todayCount = products1688Rows.filter(row => String(products1688Original(row).collected_at || row.added_at || "").slice(0, 10) === today).length;
  const values = {
    "products-1688-total": Number(total || 0).toLocaleString("zh-CN"),
    "products-1688-today": todayCount.toLocaleString("zh-CN"),
    "products-1688-pending": pending.toLocaleString("zh-CN"),
    "products-1688-completed": completed.toLocaleString("zh-CN")
  };
  Object.entries(values).forEach(([id, value]) => { const node = document.getElementById(id); if (node) node.textContent = value; });
}

function products1688SelectedRows() {
  return products1688Rows.filter(row => products1688Selected.has(Number(row.id)));
}

function products1688UpdateSelection() {
  const count = products1688Selected.size;
  const summary = document.getElementById("products-1688-selection");
  if (summary) summary.textContent = `已选择 ${count} 件`;
  const selectAll = document.getElementById("products-1688-select-all");
  if (selectAll) {
    const visible = products1688Rows.length;
    selectAll.checked = visible > 0 && products1688Rows.every(row => products1688Selected.has(Number(row.id)));
    selectAll.indeterminate = count > 0 && !selectAll.checked;
  }
}

function products1688RenderRows() {
  const body = document.getElementById("products-1688-body");
  if (!body) return;
  const rows = products1688Rows;
  body.innerHTML = rows.length ? rows.map(row => {
    const original = products1688Original(row);
    const images = products1688Images(row);
    const image = images[0] || "";
    const sourceId = original.source_1688_item_id || String(original.source_item_id || row.source_item_id || "").replace(/^1688/, "");
    const title = original.title || row.title || "未命名商品";
    const status = String(row.ai_status || "pending").toLowerCase();
    return `<tr>
      <td class="p1688-check-cell"><input type="checkbox" aria-label="选择 ${products1688Escape(sourceId)}" ${products1688Selected.has(Number(row.id)) ? "checked" : ""} onchange="products1688Toggle(${Number(row.id)}, this.checked)"></td>
      <td><div class="p1688-product-cell">${image ? `<img class="p1688-thumb" src="${products1688Escape(image)}" alt="1688 商品主图" loading="lazy" onerror="this.style.visibility='hidden'">` : `<span class="p1688-thumb"></span>`}<div class="p1688-product-copy"><strong title="${products1688Escape(title)}">${products1688Escape(title)}</strong><small>1688 商品编号 <span class="p1688-product-id">${products1688Escape(sourceId || "—")}</span></small></div></div></td>
      <td title="${products1688Escape(products1688Category(row))}">${products1688Escape(products1688Category(row))}</td>
      <td><span class="p1688-price">${products1688Money(original.price ?? row.price)}</span><small class="p1688-muted"> CNY</small></td>
      <td>${row.net_proceeds_usd === null || row.net_proceeds_usd === undefined || row.net_proceeds_usd === "" ? '<span class="p1688-muted">—</span>' : `<span class="p1688-price">$${products1688Escape(Number(row.net_proceeds_usd).toFixed(2))}</span>`}</td>
      <td>${products1688Escape(products1688Weight(row))}</td>
      <td>${products1688Escape(products1688Dimensions(row))}</td>
      <td><span class="p1688-status ${products1688Escape(String(row.review_status || "unreviewed"))}">${products1688Escape(products1688ReviewStatus(row))}</span></td>
      <td>${products1688Status(row) ? `<span class="p1688-status ${products1688Escape(status)}">${products1688Escape(products1688Status(row))}</span>` : "—"}</td>
      <td>${products1688Escape(products1688Date(original.collected_at || row.added_at))}</td>
      <td><div class="p1688-row-actions"><button class="secondary" type="button" onclick="products1688OpenDetail(${Number(row.id)})">查看详情</button><button class="secondary" type="button" onclick="products1688OpenAiEditor(${Number(row.id)})">编辑刊登内容</button>${original.source_url ? `<a href="${products1688Escape(original.source_url)}" target="_blank" rel="noopener">打开1688</a>` : ""}</div></td>
    </tr>`;
  }).join("") : '<tr><td class="p1688-empty" colspan="11">暂无 1688 商品；请先在 1688 商品页使用泽顺插件采集。</td></tr>';
  const total = Number(document.getElementById("products-1688-total-value")?.dataset.total || products1688Rows.length);
  const totalPages = Math.max(1, Math.ceil(total / PRODUCTS_1688_PAGE_SIZE));
  const indicator = document.getElementById("products-1688-page-indicator");
  if (indicator) indicator.textContent = `${products1688Page} / ${totalPages}`;
  const summary = document.getElementById("products-1688-list-summary");
  if (summary) summary.textContent = `当前 ${products1688Rows.length} 件 · 共 ${total.toLocaleString("zh-CN")} 件`;
  const previous = document.getElementById("products-1688-prev");
  const next = document.getElementById("products-1688-next");
  if (previous) previous.disabled = products1688Page <= 1;
  if (next) next.disabled = products1688Page >= totalPages;
  products1688UpdateSelection();
}

function products1688Toggle(id, checked) {
  const value = Number(id);
  if (checked) products1688Selected.add(value); else products1688Selected.delete(value);
  products1688UpdateSelection();
}

function products1688ToggleAll(checked) {
  products1688Rows.forEach(row => {
    const id = Number(row.id);
    if (checked) products1688Selected.add(id); else products1688Selected.delete(id);
  });
  products1688RenderRows();
}

function products1688PageMove(delta) {
  const total = Number(document.getElementById("products-1688-total-value")?.dataset.total || 0);
  const totalPages = Math.max(1, Math.ceil(total / PRODUCTS_1688_PAGE_SIZE));
  products1688Page = Math.min(totalPages, Math.max(1, products1688Page + Number(delta || 0)));
  load1688Products(false);
}

function products1688Query() {
  const status = document.getElementById("products-1688-status-filter")?.value?.trim() || "";
  const params = new URLSearchParams({limit: status ? "1000" : String(PRODUCTS_1688_PAGE_SIZE), offset: status ? "0" : String((products1688Page - 1) * PRODUCTS_1688_PAGE_SIZE)});
  const fields = {
    search: "products-1688-search",
    ai_status: "products-1688-status-filter",
    price_min: "products-1688-price-min",
    price_max: "products-1688-price-max",
    date_from: "products-1688-date-from",
    date_to: "products-1688-date-to"
  };
  Object.entries(fields).forEach(([name, id]) => {
    const value = document.getElementById(id)?.value?.trim() || "";
    if (value) params.set(name, value);
  });
  return params;
}

async function load1688Products(resetPage = true) {
  if (resetPage) products1688Page = 1;
  products1688SetStatus("正在读取泽顺插件采集的 1688 商品…");
  const body = document.getElementById("products-1688-body");
  if (body && !products1688Rows.length) body.innerHTML = '<tr><td class="p1688-empty" colspan="11">正在加载…</td></tr>';
  try {
    const response = await fetch(`/api/1688-products?${products1688Query().toString()}`, {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    const data = payload.data || {};
    products1688Rows = Array.isArray(data.rows) ? data.rows : [];
    products1688Selected = new Set([...products1688Selected].filter(id => products1688Rows.some(row => Number(row.id) === id)));
    const totalValue = document.getElementById("products-1688-total-value");
    if (totalValue) { totalValue.dataset.total = String(Number(data.total || 0)); totalValue.textContent = Number(data.total || 0).toLocaleString("zh-CN"); }
    products1688UpdateSummary(Number(data.total || 0));
    products1688RenderRows();
    products1688SetStatus(`已加载 ${products1688Rows.length} 件，数据来源：泽顺插件`, "success");
  } catch (error) {
    products1688Rows = [];
    products1688RenderRows();
    products1688SetStatus(`读取失败：${error.message || error}`, "error");
  }
}

function products1688OpenAiWorkflow() {
  if (typeof switchTab === "function") switchTab("ai-original-products");
}

async function products1688OpenAiEditor(id) {
  products1688OpenAiWorkflow();
  if (typeof loadAiOriginalProducts === "function") await loadAiOriginalProducts();
  const source = products1688Rows.find(item => Number(item.id) === Number(id));
  const match = typeof aiOriginalRows !== "undefined" && aiOriginalRows.find(item => Number(item.id) === Number(id))
    || (typeof aiOriginalRows !== "undefined" && aiOriginalRows.find(item => String(item.source_item_id || "") === String(source?.source_item_id || "")));
  if (match && typeof openAiOriginalEditor === "function") openAiOriginalEditor(match.id);
}

function products1688PropertyRows(properties) {
  const rows = Array.isArray(properties) ? properties.filter(item => item && (item.name || item.key) && (item.value !== undefined || item.value_name !== undefined)).slice(0, 100) : [];
  return rows.map(item => `<div class="p1688-property"><b>${products1688Escape(item.name || item.key || "属性")}</b><span>${products1688Escape(item.value ?? item.value_name ?? "—")}</span></div>`).join("");
}

function products1688OpenDetail(id) {
  const row = products1688Rows.find(item => Number(item.id) === Number(id));
  if (!row) return;
  const original = products1688Original(row);
  const images = products1688Images(row);
  const dialog = document.getElementById("products-1688-detail-dialog");
  if (!dialog) return;
  const sourceId = original.source_1688_item_id || String(original.source_item_id || row.source_item_id || "").replace(/^1688/, "");
  const title = original.title || row.title || "未命名商品";
  const mainImage = images[0] || "";
  document.getElementById("products-1688-detail-title").textContent = title;
  document.getElementById("products-1688-detail-subtitle").textContent = `1688 商品编号 ${sourceId || "—"} · 采集于 ${products1688Date(original.collected_at || row.added_at)}`;
  document.getElementById("products-1688-detail-content").innerHTML = `<div class="p1688-detail-top">
    <div class="p1688-gallery">${mainImage ? `<img class="p1688-detail-main-image" src="${products1688Escape(mainImage)}" alt="1688 商品主图">` : '<div class="p1688-empty">暂无商品图片</div>'}${images.slice(1).map(image => `<img src="${products1688Escape(image)}" alt="1688 商品图片" loading="lazy">`).join("")}</div>
    <div class="p1688-detail-facts">
      <div class="p1688-detail-fact"><label>商品标题</label><span>${products1688Escape(title)}</span></div>
      <div class="p1688-detail-fact"><label>1688 编号</label><span class="p1688-product-id">${products1688Escape(sourceId || "—")}</span></div>
      <div class="p1688-detail-fact"><label>产品分类</label><span>${products1688Escape(products1688Category(row))}</span></div>
      <div class="p1688-detail-fact"><label>采购价</label><span class="p1688-price">${products1688Money(original.price ?? row.price)} CNY</span></div>
      <div class="p1688-detail-fact"><label>净收益</label><span>${row.net_proceeds_usd === null || row.net_proceeds_usd === undefined || row.net_proceeds_usd === "" ? "—" : `$${products1688Escape(Number(row.net_proceeds_usd).toFixed(2))} USD`}</span></div>
      <div class="p1688-detail-fact"><label>实重</label><span>${products1688Escape(products1688Weight(row))}</span></div>
      <div class="p1688-detail-fact"><label>包装尺寸</label><span>${products1688Escape(products1688Dimensions(row))}</span></div>
      <div class="p1688-detail-fact"><label>审核状态</label><span><span class="p1688-status ${products1688Escape(String(row.review_status || "unreviewed"))}">${products1688Escape(products1688ReviewStatus(row))}</span></span></div>
      <div class="p1688-detail-fact"><label>AI 状态</label><span><span class="p1688-status ${products1688Escape(String(row.ai_status || "pending"))}">${products1688Escape(products1688Status(row))}</span></span></div>
      <div class="p1688-detail-fact"><label>来源链接</label><span>${original.source_url ? `<a href="${products1688Escape(original.source_url)}" target="_blank" rel="noopener">打开 1688 商品页 ↗</a>` : "—"}</span></div>
    </div>
  </div>
  <section class="p1688-detail-section"><h4>商品属性（${Array.isArray(original.properties) ? original.properties.length : 0}）</h4><div class="p1688-property-grid">${products1688PropertyRows(original.properties) || '<span class="p1688-muted">页面未采集到规格属性</span>'}</div></section>
  <section class="p1688-detail-section"><h4>1688 商品详情</h4><p class="p1688-description">${products1688Escape(original.description_text || "页面未采集到详情描述")}</p></section>`;
  dialog.showModal();
}

function products1688CloseDetail() {
  document.getElementById("products-1688-detail-dialog")?.close();
}

function products1688SendSelectedToWorkflow() {
  if (!products1688Selected.size) return;
  products1688OpenAiWorkflow();
}

document.addEventListener("DOMContentLoaded", () => {
  ["products-1688-search", "products-1688-price-min", "products-1688-price-max", "products-1688-date-from", "products-1688-date-to"].forEach(id => {
    document.getElementById(id)?.addEventListener("keydown", event => { if (event.key === "Enter") load1688Products(); });
  });
  document.getElementById("products-1688-status-filter")?.addEventListener("change", () => load1688Products());
  document.getElementById("products-1688-detail-dialog")?.addEventListener("click", event => { if (event.target?.id === "products-1688-detail-dialog") products1688CloseDetail(); });
  if (new URLSearchParams(location.search).get("tab") === "1688-products" && typeof switchTab === "function") switchTab("1688-products");
});
