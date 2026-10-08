"use strict";

let products1688Rows = [];
let products1688Selected = new Set();
let products1688Page = 1;
const PRODUCTS_1688_PAGE_SIZES = [50, 100, 200];
let products1688PageSize = 50;
let products1688Categories = [];
let products1688CategorySaving = false;
let products1688EditState = null;
let products1688EditSaving = false;
let products1688EditRevision = 0;
let products1688LoadSequence = 0;
let products1688SelectionScope = "";
let products1688ReviewSaving = false;
let products1688Loading = false;
let products1688OptionsPromise = null;
let products1688OptionsLoadedAt = 0;
const PRODUCTS_1688_OPTIONS_TTL_MS = 60000;
const PRODUCTS_1688_EDIT_FIELDS = [
  ["weight_g", "重量（g）"],
  ["package_length_cm", "包装长度（cm）"],
  ["package_width_cm", "包装宽度（cm）"],
  ["package_height_cm", "包装高度（cm）"],
  ["net_proceeds_usd", "净收益价（USD）"],
  ["price", "采购价（CNY）"]
];

function products1688ReviewOptions() {
  return [
    ["unreviewed", "未审核"],
    ["approved", "已审核"],
    ["suspected", "疑似风险"],
    ["infringing", "侵权"],
    ["risk", "待复核"]
  ];
}

function products1688Escape(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function products1688DisplayImageUrl(value) {
  const source = String(value || "").trim();
  if (!source) return "";
  try {
    const parsed = new URL(source, window.location.origin);
    const hostname = parsed.hostname.toLowerCase();
    if (/^cbu\d+\.alicdn\.com$/.test(hostname) && parsed.pathname.startsWith("/img/ibank/")) {
      parsed.pathname = parsed.pathname.replace(/\.(jpe?g|png|webp)(?:_[^/?#]*)+$/i, ".$1");
      return `/api/ai-original-products/source-image?url=${encodeURIComponent(parsed.href)}`;
    }
  } catch (_error) {
    return "";
  }
  return source;
}

function products1688ImageError(image) {
  let candidates = [];
  try { candidates = JSON.parse(image.dataset.imageCandidates || "[]"); } catch (_error) {}
  const next = candidates.shift();
  if (next) {
    image.dataset.imageCandidates = JSON.stringify(candidates);
    image.src = products1688DisplayImageUrl(next);
    return;
  }
  image.onerror = null;
  image.removeAttribute("src");
  image.classList.add("is-missing");
  image.alt = "主图加载失败";
}

function products1688Original(row) {
  return row?.original_1688 && typeof row.original_1688 === "object" ? row.original_1688 : {};
}

function products1688SourceUrl(row) {
  const original = products1688Original(row);
  const source = String(original.source_url || row?.source_url || "").trim();
  if (!source) return "";
  try {
    const url = new URL(source, window.location.origin);
    return /^https?:$/i.test(url.protocol) ? url.href : "";
  } catch (_error) {
    return "";
  }
}

function products1688Images(row) {
  const original = products1688Original(row);
  const values = [original.main_image_url, ...(Array.isArray(original.images) ? original.images : [])];
  return [...new Set(values.map(value => String(value || "").trim()).filter(value => /^https?:\/\//i.test(value)))].slice(0, 20);
}

function products1688ImageFallbacks(images) {
  const fallbacks = images.slice(1);
  images.forEach(value => {
    try {
      const url = new URL(value);
      if (/^cbu\d+\.alicdn\.com$/i.test(url.hostname) && /\.jpg$/i.test(url.pathname)) {
        url.pathname = url.pathname.replace(/\.jpg$/i, ".webp");
        fallbacks.push(url.href);
      }
    } catch (_error) {}
  });
  return [...new Set(fallbacks)];
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
  return row?.management_category_name || "未分类";
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

function products1688SourceValue(row, key) {
  const overrides = row?.source_1688_overrides;
  if (overrides && Object.prototype.hasOwnProperty.call(overrides, key)) return overrides[key];
  const sourceKey = key === "price" ? "source_purchase_price_cny" : `source_${key}`;
  if (Object.prototype.hasOwnProperty.call(row || {}, sourceKey)) return row[sourceKey];
  const original = products1688Original(row);
  const originalValue = original[key];
  if (key === "price") {
    if (Object.prototype.hasOwnProperty.call(original, key) && originalValue == null) return null;
    const originalPrice = Number(originalValue);
    if (Number.isFinite(originalPrice) && originalPrice > 0) return originalValue;
    return row.source_purchase_price_cny ?? row.price ?? originalValue;
  }
  if (originalValue !== null && originalValue !== undefined && originalValue !== "") return originalValue;
  return row[key] ?? originalValue;
}

function products1688Weight(row) {
  const weight = Number(products1688SourceValue(row, "weight_g"));
  return Number.isFinite(weight) && weight > 0 ? `${weight.toLocaleString("zh-CN")} g` : "—";
}

function products1688Dimensions(row) {
  const values = ["package_length_cm", "package_width_cm", "package_height_cm"].map(key => products1688SourceValue(row, key));
  return values.map((value, index) => `${["长", "宽", "高"][index]}：${Number.isFinite(Number(value)) && Number(value) > 0 ? `${Number(value).toLocaleString("zh-CN")} cm` : "未填写"}`).join(" · ");
}

function products1688SetStatus(message, kind = "") {
  const node = document.getElementById("products-1688-status");
  if (node) {
    node.textContent = message;
    node.className = `p1688-toolbar-note ${kind}`.trim();
    globalThis.WorkbenchUI?.feedback(node, kind || "info");
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
  if (summary) summary.textContent = `本页已选择 ${count} 件`;
  const selectAll = document.getElementById("products-1688-select-all");
  if (selectAll) {
    const visible = products1688Rows.length;
    selectAll.checked = visible > 0 && products1688Rows.every(row => products1688Selected.has(Number(row.id)));
    selectAll.indeterminate = count > 0 && !selectAll.checked;
  }
  const deleteButton = document.getElementById("products-1688-delete");
  if (deleteButton) deleteButton.disabled = products1688Loading || !products1688Selected.size || aiOriginalDeleteRunning;
  const reviewButton = document.getElementById("products-1688-review-submit");
  if (reviewButton) reviewButton.disabled = products1688ReviewSaving || products1688Loading || count === 0 || !document.getElementById("products-1688-review-status")?.value;
  const categorySelect = document.getElementById("products-1688-batch-category");
  if (categorySelect) categorySelect.disabled = count === 0 || products1688Loading || products1688CategorySaving;
  const categoryButton = document.getElementById("products-1688-category-submit");
  if (categoryButton) categoryButton.disabled = count === 0 || products1688Loading || products1688CategorySaving || !categorySelect?.value;
}

function products1688RenderRows() {
  const body = document.getElementById("products-1688-body");
  if (!body) return;
  const rows = products1688Rows;
  body.innerHTML = rows.length ? rows.map(row => {
    const original = products1688Original(row);
    const images = products1688Images(row);
    const image = products1688DisplayImageUrl(images[0] || "");
    const title = original.title || row.title || "未命名商品";
    const sourceUrl = products1688SourceUrl(row);
    const imageMarkup = image
      ? `<img class="p1688-thumb" src="${products1688Escape(image)}" data-image-candidates="${products1688Escape(JSON.stringify(products1688ImageFallbacks(images)))}" alt="1688 商品主图" loading="lazy" referrerpolicy="no-referrer" onerror="products1688ImageError(this)">`
      : `<span class="p1688-thumb"></span>`;
    const linkedImage = sourceUrl
      ? `<a class="p1688-product-image-link" href="${products1688Escape(sourceUrl)}" target="_blank" rel="noopener noreferrer" title="打开 1688 商品页">${imageMarkup}</a>`
      : imageMarkup;
    const linkedTitle = sourceUrl
      ? `<a class="p1688-product-title-link" href="${products1688Escape(sourceUrl)}" target="_blank" rel="noopener noreferrer" title="打开 1688 商品页"><strong title="${products1688Escape(title)}">${products1688Escape(title)}</strong></a>`
      : `<strong title="${products1688Escape(title)}">${products1688Escape(title)}</strong>`;
    const status = String(row.ai_status || "pending").toLowerCase();
    return `<tr class="p1688-editable-row" onclick="products1688RowClick(event, ${Number(row.id)})" title="点击空白处修改资料">
      <td data-label="选择" class="p1688-check-cell"><input type="checkbox" aria-label="选择 ${products1688Escape(title)}" ${products1688Selected.has(Number(row.id)) ? "checked" : ""} onchange="products1688Toggle(${Number(row.id)}, this.checked)"></td>
      <td data-label="商品"><div class="p1688-product-cell">${linkedImage}<div class="p1688-product-copy">${linkedTitle}</div></div></td>
      <td data-label="自建产品分类" title="${products1688Escape(products1688Category(row))}">${products1688Escape(products1688Category(row))}</td>
      <td data-label="采购价"><span class="p1688-price">${products1688Money(products1688SourceValue(row, "price"))}</span><small class="p1688-muted"> CNY</small></td>
      <td data-label="净收益价 · USD">${row.net_proceeds_usd === null || row.net_proceeds_usd === undefined || row.net_proceeds_usd === "" ? '<span class="p1688-muted">—</span>' : `<span class="p1688-price">$${products1688Escape(Number(row.net_proceeds_usd).toFixed(2))}</span>`}</td>
      <td data-label="包装毛重">${products1688Escape(products1688Weight(row))}</td>
      <td data-label="包装尺寸">${products1688Escape(products1688Dimensions(row))}</td>
      <td data-label="审核状态"><select class="p1688-review-select" aria-label="修改 ${products1688Escape(title)} 的审核状态" onchange="products1688UpdateReviewStatus([${Number(row.id)}], this.value, this)">${products1688ReviewOptions().map(([value, label]) => `<option value="${value}" ${String(row.review_status || "unreviewed") === value ? "selected" : ""}>${label}</option>`).join("")}</select></td>
      <td data-label="AI 状态">${products1688Status(row) ? `<span class="p1688-status ${products1688Escape(status)}">${products1688Escape(products1688Status(row))}</span>` : "—"}</td>
      <td data-label="采集业务员">${products1688Escape(row.collector_salesperson || "未记录")}</td>
      <td data-label="采集时间">${products1688Escape(products1688Date(original.collected_at || row.added_at))}</td>
      <td data-label="操作"><div class="p1688-row-actions">${window.catalogVisibilityControl ? window.catalogVisibilityControl(row, "products") : ""}<button class="secondary" type="button" onclick="products1688OpenDetail(${Number(row.id)})">查看详情</button><button class="secondary" type="button" onclick="products1688OpenEdit(${Number(row.id)})">修改资料</button><button class="secondary" type="button" onclick="products1688OpenAiEditor(${Number(row.id)})">编辑刊登内容</button>${sourceUrl ? `<a href="${products1688Escape(sourceUrl)}" target="_blank" rel="noopener noreferrer">打开1688</a>` : ""}</div></td>
    </tr>`;
  }).join("") : '<tr><td class="p1688-empty" colspan="12">暂无 1688 商品；请先在 1688 商品页使用泽顺插件采集。</td></tr>';
  const total = Number(document.getElementById("products-1688-total-value")?.dataset.total || products1688Rows.length);
  const totalPages = Math.max(1, Math.ceil(total / products1688PageSize));
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
  const totalPages = Math.max(1, Math.ceil(total / products1688PageSize));
  products1688Page = Math.min(totalPages, Math.max(1, products1688Page + Number(delta || 0)));
  load1688Products(false);
}

function products1688Query() {
  const params = new URLSearchParams({limit: String(products1688PageSize), offset: String((products1688Page - 1) * products1688PageSize)});
  const fields = {
    search: "products-1688-search",
    collector_salesperson: "products-1688-salesperson",
    management_category_id: "products-1688-category-filter",
    ai_status: "products-1688-status-filter",
    review_status: "products-1688-review-filter",
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

function products1688FillSelect(id, options) {
  const select = document.getElementById(id);
  if (!select) return;
  const selected = select.value;
  select.innerHTML = options.map(([value, label]) => `<option value="${products1688Escape(value)}">${products1688Escape(label)}</option>`).join("");
  select.value = options.some(([value]) => String(value) === selected) ? selected : "";
}

function products1688LoadOptions() {
  if (products1688OptionsPromise) return products1688OptionsPromise;
  if (products1688OptionsLoadedAt && Date.now() - products1688OptionsLoadedAt < PRODUCTS_1688_OPTIONS_TTL_MS) return Promise.resolve([]);
  products1688OptionsPromise = products1688FetchOptions().then(warnings => {
    if (!warnings.length) products1688OptionsLoadedAt = Date.now();
    return warnings;
  }).finally(() => { products1688OptionsPromise = null; });
  return products1688OptionsPromise;
}

async function products1688FetchOptions() {
  const results = await Promise.allSettled([
    fetch("/api/1688-products/options", {cache: "no-store"}).then(async response => {
      const payload = await response.json();
      if (!response.ok || payload.status !== "success") throw Object.assign(new Error(payload.message || `HTTP ${response.status}`), {status: response.status});
      const names = [...new Set((Array.isArray(payload.data?.collector_salespeople) ? payload.data.collector_salespeople : []).map(value => String(value || "").trim()).filter(Boolean))];
      products1688FillSelect("products-1688-salesperson", [["", "全部采集业务员"], ...names.map(name => [name, name])]);
    }),
    fetch("/api/mercado-management-categories", {cache: "no-store"}).then(async response => {
      const payload = await response.json();
      if (!response.ok || payload.status !== "success") throw Object.assign(new Error(payload.message || `HTTP ${response.status}`), {status: response.status});
      products1688Categories = (Array.isArray(payload.data?.rows) ? payload.data.rows : []).filter(row => Number(row.id) > 0);
      const categories = products1688Categories.map(row => [String(row.id), row.name || "未命名分类"]);
      products1688FillSelect("products-1688-category-filter", [["", "全部分类"], ["uncategorized", "未分类"], ...categories]);
      products1688FillSelect("products-1688-batch-category", [["", "选择分类"], ["uncategorized", "移出分类"], ...categories]);
      products1688UpdateSelection();
    })
  ]);
  return results.flatMap((result, index) => result.status === "rejected" ? [`${index === 0 ? "业务员" : "分类"}选项读取失败：${result.reason?.message || result.reason}`] : []);
}

function products1688ResetFilters() {
  ["search", "salesperson", "category-filter", "status-filter", "review-filter", "price-min", "price-max", "date-from", "date-to"].forEach(name => {
    const control = document.getElementById(`products-1688-${name}`);
    if (control) control.value = "";
  });
  load1688Products();
}

async function products1688OpenCategoryManager() {
  if (typeof openMercadoCategoryManager === "function") await openMercadoCategoryManager();
}

async function products1688ApplyCategory() {
  const ids = [...products1688Selected].map(Number).filter(id => id > 0);
  const value = document.getElementById("products-1688-batch-category")?.value || "";
  if (!ids.length || !value || products1688CategorySaving || products1688Loading) return;
  const categoryId = value === "uncategorized" ? null : Number(value);
  if (categoryId !== null && !products1688Categories.some(row => Number(row.id) === categoryId)) return;
  products1688CategorySaving = true;
  products1688UpdateSelection();
  products1688SetStatus("正在更新所选产品的分类…");
  try {
    const response = await fetch("/api/mercado-management-categories/assign", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({item_type: "products", item_ids: ids, category_id: categoryId})
    });
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw Object.assign(new Error(payload.message || `HTTP ${response.status}`), {status: response.status});
    const categoryName = categoryId === null ? null : products1688Categories.find(row => Number(row.id) === categoryId)?.name;
    products1688Rows = products1688Rows.map(row => ids.includes(Number(row.id)) ? {...row, management_category_id: categoryId, management_category_name: categoryName} : row);
    products1688RenderRows();
    const refreshed = await load1688Products(false, true);
    const refreshError = document.getElementById("products-1688-status")?.textContent || "列表刷新失败";
    const savedMessage = `已更新 ${Number(payload.data?.changed ?? ids.length)} 件产品的分类`;
    products1688SetStatus(refreshed ? savedMessage : `${savedMessage}，列表刷新失败：${refreshError}；请重新刷新数据`, refreshed ? "success" : "error");
  } catch (error) {
    products1688SetStatus(`分类更新失败：${error.message || error}`, "error");
  } finally {
    products1688CategorySaving = false;
    products1688UpdateSelection();
  }
}

function products1688EditValue(row, key) {
  const value = key === "net_proceeds_usd" ? row[key] : products1688SourceValue(row, key);
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) && Number(value) > 0 ? String(value) : "";
}

function products1688EditPatch(initial, current) {
  const patch = {};
  PRODUCTS_1688_EDIT_FIELDS.forEach(([key, label]) => {
    const before = String(initial[key] ?? "").trim();
    const after = String(current[key] ?? "").trim();
    if (before === after || before !== "" && after !== "" && Number(before) === Number(after)) return;
    if (after === "") { patch[key] = null; return; }
    const value = Number(after);
    if (!Number.isFinite(value) || value <= 0) throw new Error(`${label}必须填写大于 0 的有效数字，或留空清除`);
    patch[key] = value;
  });
  return patch;
}

function products1688EditMessage(message, kind = "") {
  const node = document.getElementById("products-1688-edit-message");
  if (node) { node.textContent = message; node.className = `p1688-edit-message ${kind}`.trim(); }
}

function products1688SetEditBusy(busy) {
  products1688EditSaving = busy;
  ["products-1688-edit-save", "products-1688-edit-cancel", "products-1688-edit-close", ...PRODUCTS_1688_EDIT_FIELDS.map(([key]) => `products-1688-edit-${key}`)].forEach(id => {
    const control = document.getElementById(id);
    if (control) control.disabled = busy;
  });
  const save = document.getElementById("products-1688-edit-save");
  if (save) save.textContent = busy ? "正在保存…" : "保存修改";
}

function products1688OpenEdit(id) {
  if (products1688EditSaving) return;
  const row = products1688Rows.find(item => Number(item.id) === Number(id));
  const dialog = document.getElementById("products-1688-edit-dialog");
  if (!row || !dialog) return;
  const initial = {};
  PRODUCTS_1688_EDIT_FIELDS.forEach(([key]) => {
    initial[key] = products1688EditValue(row, key);
    const input = document.getElementById(`products-1688-edit-${key}`);
    if (input) input.value = initial[key];
  });
  products1688EditState = {id: Number(id), initial};
  document.getElementById("products-1688-edit-title").textContent = products1688Original(row).title || row.title || "未命名商品";
  products1688EditMessage("");
  products1688SetEditBusy(false);
  if (!dialog.open) dialog.showModal();
}

function products1688CloseEdit() {
  if (products1688EditSaving) return;
  document.getElementById("products-1688-edit-dialog")?.close();
  products1688EditState = null;
}

async function products1688SaveEdit(event) {
  event?.preventDefault();
  if (!products1688EditState || products1688EditSaving) return;
  const current = {};
  for (const [key, label] of PRODUCTS_1688_EDIT_FIELDS) {
    const input = document.getElementById(`products-1688-edit-${key}`);
    if (input?.validity?.badInput) {
      products1688EditMessage(`${label}必须填写大于 0 的有效数字，或留空清除`, "error");
      input.focus();
      return;
    }
    current[key] = input?.value || "";
  }
  let patch;
  try { patch = products1688EditPatch(products1688EditState.initial, current); }
  catch (error) { products1688EditMessage(error.message || String(error), "error"); return; }
  if (!Object.keys(patch).length) { products1688EditMessage("没有需要保存的修改"); return; }
  const id = products1688EditState.id;
  products1688SetEditBusy(true);
  products1688EditMessage("正在保存商品资料…");
  try {
    const response = await fetch(`/api/1688-products/${id}`, {
      method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(patch)
    });
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw Object.assign(new Error(payload.message || `HTTP ${response.status}`), {status: response.status});
    const hasSavedRow = payload.data && typeof payload.data === "object" && Number(payload.data.id) === id;
    products1688EditRevision++;
    if (hasSavedRow) {
      products1688Rows = products1688Rows.map(row => Number(row.id) === id ? payload.data : row);
      products1688RenderRows();
    }
    const detail = document.getElementById("products-1688-detail-dialog");
    if (hasSavedRow && detail?.open) products1688OpenDetail(id);
    products1688SetEditBusy(false);
    products1688CloseEdit();
    products1688SetStatus("商品资料已保存", "success");
    // The PATCH response is authoritative. Refreshing up to 500 AI products
    // and the source page must not keep a committed edit in the saving state.
    Promise.resolve().then(() => {
      if (hasSavedRow && typeof aiOriginalRows !== "undefined" && typeof renderAiOriginalProducts === "function") {
        aiOriginalRows = aiOriginalRows.map(row => Number(row.id) === id ? {...row, ...payload.data} : row);
        renderAiOriginalProducts({preserveInputsExcept: [id]});
      }
      if (typeof loadAiOriginalProducts === "function") {
        return loadAiOriginalProducts({preserveTaskStatus: true});
      }
    }).catch(error => console.warn("1688 资料已保存，AI 原创列表同步失败", error));
    if (!hasSavedRow) {
      // Compatibility with a server that does not yet return the saved row.
      load1688Products(false, true).then(refreshed => {
        if (refreshed === false) {
          const refreshError = document.getElementById("products-1688-status")?.textContent || "列表刷新失败";
          products1688SetStatus(`商品资料已保存，列表刷新失败：${refreshError}；请重新刷新数据`, "error");
        }
      }).catch(error => console.warn("1688 资料已保存，列表刷新失败", error));
    }
  } catch (error) {
    products1688EditMessage(`保存失败：${error.message || error}`, "error");
  } finally {
    products1688SetEditBusy(false);
  }
}

async function products1688UpdateReviewStatus(itemIds, reviewStatus, control = null) {
  if (products1688ReviewSaving || products1688Loading) return;
  const ids = [...new Set((itemIds || []).map(Number).filter(value => value > 0))];
  const status = String(reviewStatus || "").trim().toLowerCase();
  if (!ids.length || !products1688ReviewOptions().some(([value]) => value === status)) return;
  products1688ReviewSaving = true;
  products1688UpdateSelection();
  if (control) control.disabled = true;
  products1688SetStatus("正在更新审核状态…", "loading");
  try {
    const response = await fetch("/api/mercado-products/review-status", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({product_item_ids: ids, review_status: status})
    });
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw Object.assign(new Error(payload.message || `HTTP ${response.status}`), {status: response.status});
    const changed = Number(payload.data?.changed || 0);
    products1688SetStatus(`已更新 ${changed} 件产品的审核状态`, "success");
    await load1688Products(false);
  } catch (error) {
    products1688SetStatus(`审核状态更新失败：${error.message || error}`, "error");
    if (control) await load1688Products(false);
  } finally {
    products1688ReviewSaving = false;
    if (control) control.disabled = false;
    products1688UpdateSelection();
  }
}

async function products1688DeleteSelected() {
  if (products1688Loading) return;
  await deleteAiOriginalProductSelection([...products1688Selected], products1688SetStatus);
}

async function products1688ApplyReviewStatus() {
  const status = document.getElementById("products-1688-review-status")?.value || "";
  await products1688UpdateReviewStatus([...products1688Selected], status);
}

async function load1688Products(resetPage = true, preserveRowsOnError = false) {
  const loadSequence = ++products1688LoadSequence;
  const editRevision = products1688EditRevision;
  if (resetPage) products1688Page = 1;
  const scope = products1688Query().toString();
  if (scope !== products1688SelectionScope) {
    products1688Selected.clear();
    products1688SelectionScope = scope;
  }
  products1688Loading = true;
  products1688UpdateSelection();
  products1688SetStatus("正在读取泽顺插件采集的 1688 商品…", "loading");
  const body = document.getElementById("products-1688-body");
  if (body && !products1688Rows.length) body.innerHTML = '<tr><td class="p1688-empty" colspan="12">正在加载…</td></tr>';
  try {
    // Filter metadata must never hold up the product list. Capture the query
    // before background option updates can touch the select controls.
    const optionsPromise = products1688LoadOptions();
    const response = await fetch(`/api/1688-products?${scope}`, {cache: "no-store"});
    const payload = await response.json();
    if (loadSequence !== products1688LoadSequence) return;
    // A list requested before a completed edit may contain the old values.
    if (editRevision !== products1688EditRevision) return await load1688Products(false, true);
    if (!response.ok || payload.status !== "success") throw Object.assign(new Error(payload.message || `HTTP ${response.status}`), {status: response.status});
    const data = payload.data || {};
    const lastPage = Math.max(1, Math.ceil(Number(data.total || 0) / products1688PageSize));
    if (products1688Page > lastPage) {
      products1688Page = lastPage;
      return await load1688Products(false, preserveRowsOnError);
    }
    products1688Rows = Array.isArray(data.rows) ? data.rows : [];
    products1688Selected = new Set([...products1688Selected].filter(id => products1688Rows.some(row => Number(row.id) === id)));
    const totalValue = document.getElementById("products-1688-total-value");
    if (totalValue) { totalValue.dataset.total = String(Number(data.total || 0)); totalValue.textContent = Number(data.total || 0).toLocaleString("zh-CN"); }
    products1688UpdateSummary(Number(data.total || 0));
    products1688RenderRows();
    const loadedMessage = `已加载 ${products1688Rows.length} 件，数据来源：泽顺插件`;
    products1688SetStatus(loadedMessage, "success");
    optionsPromise.then(warnings => {
      if (loadSequence === products1688LoadSequence && warnings.length) {
        products1688SetStatus(`${loadedMessage}；${warnings.join("；")}`, "partial");
      }
    });
    return true;
  } catch (error) {
    if (loadSequence !== products1688LoadSequence) return;
    if (editRevision !== products1688EditRevision) return await load1688Products(false, true);
    if (!preserveRowsOnError) products1688Rows = [];
    products1688RenderRows();
    if (!preserveRowsOnError) products1688Selected.clear();
    const kind = globalThis.WorkbenchUI?.errorKind(error) || "error";
    products1688SetStatus(kind === "denied" ? "权限不足，请联系管理员开通商品查看权限。" : `读取失败：${error.message || error}；请刷新数据重试`, kind);
    return false;
  } finally {
    if (loadSequence === products1688LoadSequence) { products1688Loading = false; products1688UpdateSelection(); }
  }
}

function products1688OpenAiWorkflow() {
  if (typeof switchTab === "function") switchTab("ai-original-products");
}

async function products1688OpenAiEditor(id) {
  const source = products1688Rows.find(item => Number(item.id) === Number(id));
  const reason = products1688WorkflowIssue(source);
  if (reason) {
    products1688SetStatus(`产品 ${id} 无法转入 AI 原创产品：${reason}`, "error");
    return;
  }
  products1688OpenAiWorkflow();
  if (typeof loadAiOriginalProducts === "function") await loadAiOriginalProducts();
  const match = typeof aiOriginalRows !== "undefined" && aiOriginalRows.find(item => Number(item.id) === Number(id))
    || (typeof aiOriginalRows !== "undefined" && aiOriginalRows.find(item => String(item.source_item_id || "") === String(source?.source_item_id || "")));
  if (match && typeof openAiOriginalEditor === "function") openAiOriginalEditor(match.id);
}

function products1688PropertyRows(properties) {
  const rows = Array.isArray(properties) ? properties.filter(item => item && (item.name || item.key) && (item.value !== undefined || item.value_name !== undefined)).slice(0, 100) : [];
  return rows.map(item => `<div class="p1688-property"><b>${products1688Escape(item.name || item.key || "属性")}</b><span>${products1688Escape(item.value ?? item.value_name ?? "—")}</span></div>`).join("");
}

function products1688VariationLabel(variation) {
  const combinations = variation?.attribute_combinations || variation?.attributes || variation?.properties || [];
  const labels = Array.isArray(combinations) ? combinations.map((item, index) => {
    const name = item?.name || item?.id || `规格${index + 1}`;
    const value = item?.value_name ?? item?.value ?? item?.text ?? item?.name_value ?? "";
    return value ? `${name}: ${value}` : "";
  }).filter(Boolean) : [];
  return labels.join(" / ") || variation?.label || variation?.name || variation?.sku_name || "—";
}

function products1688VariationRows(variations) {
  const rows = Array.isArray(variations) ? variations.filter(item => item && typeof item === "object").slice(0, 200) : [];
  if (!rows.length) return '<div class="p1688-variant-empty">采集快照没有 SKU 变体。</div>';
  return `<div class="p1688-variant-wrap"><table><thead><tr><th>变体</th><th>SKU（选填）</th><th>库存</th><th>采购价</th><th>重量</th><th>包装尺寸</th><th>加价</th><th>加重</th><th>图片</th></tr></thead><tbody>${rows.map(variation => {
    const imageValues = [variation.image_url, variation.image, ...(Array.isArray(variation.images) ? variation.images : [])]
      .map(value => typeof value === "object" ? (value.url || value.source || value.src || "") : value)
      .filter(value => /^https?:\/\//i.test(String(value || ""))).slice(0, 4);
    const sku = variation.seller_sku || variation.sku || variation.sku_id || variation.skuId || variation.id || "";
    const price = variation.price ?? variation.price_text ?? "—";
    const stock = variation.available_quantity ?? variation.stock ?? variation.stock_text ?? "—";
    const extraPrice = variation.price_addition ?? variation.additional_price ?? variation.markup ?? "—";
    const extraWeight = variation.weight_addition_g ?? variation.additional_weight_g ?? variation.added_weight_g ?? "—";
    const images = imageValues.length ? imageValues.map(url => `<a href="${products1688Escape(url)}" target="_blank" rel="noopener noreferrer"><img src="${products1688Escape(products1688DisplayImageUrl(url))}" alt="变体图片" loading="lazy" referrerpolicy="no-referrer" onerror="products1688ImageError(this)"></a>`).join("") : "—";
    return `<tr><td>${products1688Escape(products1688VariationLabel(variation))}</td><td>${products1688Escape(sku || "—")}</td><td>${products1688Escape(stock)}</td><td>${products1688Escape(price)}</td><td>${products1688Escape(products1688Weight(variation))}</td><td>${products1688Escape(products1688Dimensions(variation))}</td><td>${products1688Escape(extraPrice)}</td><td>${products1688Escape(extraWeight)}</td><td><div class="p1688-variant-images">${images}</div></td></tr>`;
  }).join("")}</tbody></table></div>`;
}

function products1688OpenDetail(id) {
  const row = products1688Rows.find(item => Number(item.id) === Number(id));
  if (!row) return;
  const original = products1688Original(row);
  const images = products1688Images(row);
  const dialog = document.getElementById("products-1688-detail-dialog");
  if (!dialog) return;
  const title = original.title || row.title || "未命名商品";
  const sourceUrl = products1688SourceUrl(row);
  const mainImage = products1688DisplayImageUrl(images[0] || "");
  document.getElementById("products-1688-detail-title").textContent = title;
  document.getElementById("products-1688-detail-subtitle").textContent = `采集于 ${products1688Date(original.collected_at || row.added_at)}`;
  document.getElementById("products-1688-detail-content").innerHTML = `<div class="p1688-detail-top">
    <div class="p1688-gallery">${mainImage ? `${sourceUrl ? `<a class="p1688-detail-main-image-link" href="${products1688Escape(sourceUrl)}" target="_blank" rel="noopener noreferrer" title="打开 1688 商品页">` : ""}<img class="p1688-detail-main-image" src="${products1688Escape(mainImage)}" alt="1688 商品主图" referrerpolicy="no-referrer" onerror="products1688ImageError(this)">${sourceUrl ? "</a>" : ""}` : '<div class="p1688-empty">暂无商品图片</div>'}${images.slice(1).map(image => `<img src="${products1688Escape(products1688DisplayImageUrl(image))}" alt="1688 商品图片" loading="lazy" referrerpolicy="no-referrer" onerror="products1688ImageError(this)">`).join("")}</div>
    <div class="p1688-detail-facts">
      <div class="p1688-detail-fact"><label>商品标题</label><span>${sourceUrl ? `<a href="${products1688Escape(sourceUrl)}" target="_blank" rel="noopener noreferrer">${products1688Escape(title)} ↗</a>` : products1688Escape(title)}</span></div>
      <div class="p1688-detail-fact"><label>自建产品分类</label><span>${products1688Escape(products1688Category(row))}</span></div>
      <div class="p1688-detail-fact"><label>采购价</label><span class="p1688-price">${products1688Money(products1688SourceValue(row, "price"))} CNY</span></div>
      <div class="p1688-detail-fact"><label>净收益</label><span>${row.net_proceeds_usd === null || row.net_proceeds_usd === undefined || row.net_proceeds_usd === "" ? "—" : `$${products1688Escape(Number(row.net_proceeds_usd).toFixed(2))} USD`}</span></div>
      <div class="p1688-detail-fact"><label>实重</label><span>${products1688Escape(products1688Weight(row))}</span></div>
      <div class="p1688-detail-fact"><label>包装尺寸</label><span>${products1688Escape(products1688Dimensions(row))}</span></div>
      <div class="p1688-detail-fact"><label>审核状态</label><span><span class="p1688-status ${products1688Escape(String(row.review_status || "unreviewed"))}">${products1688Escape(products1688ReviewStatus(row))}</span></span></div>
      <div class="p1688-detail-fact"><label>AI 状态</label><span><span class="p1688-status ${products1688Escape(String(row.ai_status || "pending"))}">${products1688Escape(products1688Status(row))}</span></span></div>
      <div class="p1688-detail-fact"><label>来源链接</label><span>${sourceUrl ? `<a href="${products1688Escape(sourceUrl)}" target="_blank" rel="noopener noreferrer">打开 1688 商品页 ↗</a>` : "—"}</span></div>
    </div>
  </div>
  <section class="p1688-detail-section"><h4>商品属性（${Array.isArray(original.properties) ? original.properties.length : 0}）</h4><div class="p1688-property-grid">${products1688PropertyRows(original.properties) || '<span class="p1688-muted">页面未采集到规格属性</span>'}</div></section>
  <section class="p1688-detail-section"><h4>商品变体 / SKU（${Array.isArray(original.variations) ? original.variations.length : 0}）</h4>${products1688VariationRows(original.variations)}</section>
  <section class="p1688-detail-section"><h4>1688 商品详情</h4><p class="p1688-description">${products1688Escape(original.description_text || "页面未采集到详情描述")}</p></section>`;
  if (!dialog.open) dialog.showModal();
}

function products1688CloseDetail() {
  document.getElementById("products-1688-detail-dialog")?.close();
}

function products1688WorkflowIssue(row) {
  if (!row) return "产品已不在当前列表，请刷新后重试";
  const missing = [];
  const weight = Number(products1688SourceValue(row, "weight_g"));
  const net = Number(products1688SourceValue(row, "net_proceeds_usd"));
  if (!Number.isFinite(weight) || weight <= 0) missing.push("重量");
  if (!Number.isFinite(net) || net <= 0) missing.push("净收益");
  return missing.length ? `请先填写有效${missing.join("、")}（须大于 0）` : "";
}

function products1688SendSelectedToWorkflow() {
  const selected = [...products1688Selected].map(Number).filter(value => value > 0);
  if (!selected.length) {
    products1688SetStatus("请先勾选要执行 AI 任务的产品", "error");
    return;
  }
  const ids = [], failures = [];
  selected.forEach(id => {
    const reason = products1688WorkflowIssue(products1688Rows.find(row => Number(row.id) === id));
    if (reason) failures.push(`产品 ${id}：${reason}`);
    else ids.push(id);
  });
  const message = `已转入 ${ids.length} 件产品${failures.length ? `，未通过 ${failures.length} 件：${failures.slice(0, 3).join("；")}` : ""}`;
  const kind = failures.length ? (ids.length ? "partial" : "error") : "success";
  products1688SetStatus(message, kind);
  if (!ids.length) return;
  try { localStorage.setItem("mercado.aiOriginalSelected", JSON.stringify(ids)); } catch (_error) {}
  if (typeof aiOriginalSelected !== "undefined") ids.forEach(id => aiOriginalSelected.add(id));
  ids.forEach(id => products1688Selected.delete(id));
  products1688UpdateSelection();
  products1688OpenAiWorkflow();
  if (typeof renderAiOriginalProducts === "function") renderAiOriginalProducts();
  if (typeof aiOriginalStatus === "function") aiOriginalStatus(message, kind);
}

document.addEventListener("DOMContentLoaded", () => {
  try {
    const saved = Number(localStorage.getItem("mercado.1688PageSize") || 50);
    if (PRODUCTS_1688_PAGE_SIZES.includes(saved)) products1688PageSize = saved;
  } catch (_error) {}
  const pageSize = document.getElementById("products-1688-page-size");
  if (pageSize) {
    pageSize.value = String(products1688PageSize);
    pageSize.addEventListener("change", () => {
      const next = Number(pageSize.value);
      if (!PRODUCTS_1688_PAGE_SIZES.includes(next)) return;
      products1688PageSize = next;
      products1688Page = 1;
      try { localStorage.setItem("mercado.1688PageSize", String(next)); } catch (_error) {}
      load1688Products(false);
    });
  }
  ["products-1688-search", "products-1688-salesperson", "products-1688-price-min", "products-1688-price-max", "products-1688-date-from", "products-1688-date-to"].forEach(id => {
    document.getElementById(id)?.addEventListener("keydown", event => { if (event.key === "Enter") load1688Products(); });
  });
  document.getElementById("products-1688-status-filter")?.addEventListener("change", () => load1688Products());
  document.getElementById("products-1688-salesperson")?.addEventListener("change", () => load1688Products());
  document.getElementById("products-1688-category-filter")?.addEventListener("change", () => load1688Products());
  document.getElementById("products-1688-review-filter")?.addEventListener("change", () => load1688Products());
  document.getElementById("products-1688-review-status")?.addEventListener("change", products1688UpdateSelection);
  document.getElementById("products-1688-batch-category")?.addEventListener("change", products1688UpdateSelection);
  document.getElementById("products-1688-edit-dialog")?.addEventListener("cancel", event => {
    if (products1688EditSaving) event.preventDefault();
    else products1688EditState = null;
  });
  document.getElementById("mercado-category-dialog")?.addEventListener("close", () => {
    if (document.getElementById("tab-1688-products")?.classList.contains("active")) load1688Products(false);
  });
  document.getElementById("products-1688-detail-dialog")?.addEventListener("click", event => { if (event.target?.id === "products-1688-detail-dialog") products1688CloseDetail(); });
  if (new URLSearchParams(location.search).get("tab") === "1688-products" && typeof switchTab === "function") switchTab("1688-products");
});

function products1688RowClick(event, id) {
  if (event.target.closest("a, button, input, select, textarea, label, [role=button], .p1688-check-cell")) return;
  if (window.getSelection?.().toString()) return;
  products1688OpenEdit(id);
}
