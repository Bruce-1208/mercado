const state = {
  runId: null,
  requestedCount: 200,
  products: [],
  selectedProducts: new Set(),
  stores: [],
  zeshunStores: [],
  selectedStoreId: null,
  exchangeRate: null,
  searchPollTimer: null,
  publishPollTimer: null,
};

const $ = (id) => document.getElementById(id);
const keywordInput = $("keyword");
const countInput = $("count");
const pricePercentInput = $("pricePercent");
const searchButton = $("searchButton");
const publishButton = $("publishButton");
const statusPanel = $("statusPanel");
const resultsSection = $("resultsSection");
const packageInputs = {
  length: $("packageLength"),
  width: $("packageWidth"),
  height: $("packageHeight"),
  weight: $("packageWeight"),
};
const initialStockInput = $("initialStock");
const PACKAGE_STORAGE_KEY = "yandex-reseller-package-v1";

function toast(message, isError = false) {
  const element = $("toast");
  element.textContent = message;
  element.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(element._timer);
  element._timer = setTimeout(() => { element.className = "toast"; }, 4800);
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
  } catch (_) {
    throw new Error("无法连接本地服务。请运行 start.cmd，然后刷新页面重试");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function selectedStore() {
  return state.stores.find((store) => store.id === state.selectedStoreId) || null;
}

function renderZeshunStores() {
  $("zeshunStoreCount").textContent = state.zeshunStores.length;
  $("zeshunStoreEmpty").classList.toggle("hidden", state.zeshunStores.length > 0);
  $("zeshunStoreList").innerHTML = state.zeshunStores.map((store) => {
    const actualName = store.store_name || store.business_name || "尚未连接 Yandex 店铺";
    const authLink = store.authorization_url
      ? `<div class="authorization-link-row"><a href="${escapeHtml(store.authorization_url)}" target="_blank" rel="noreferrer">${escapeHtml(store.authorization_url)}</a><button type="button" data-action="copy-authorization-link">复制</button></div>`
      : `<span>未配置授权链接，请编辑店铺补充</span>`;
    return `
      <article class="authorization-card" data-zeshun-store-id="${store.id}">
        <div class="authorization-card-head">
          <div class="authorization-card-title">
            <strong>${escapeHtml(store.alias)}</strong>
            <small>${escapeHtml(actualName)}</small>
          </div>
          <span class="authorization-status${store.authorized ? " ok" : ""}">${store.authorized ? "TOKEN 已更新" : "等待授权"}</span>
        </div>
        <div class="authorization-details">
          <div class="authorization-detail"><span>TG 码</span><code>${escapeHtml(store.tg_code)}</code></div>
          <div class="authorization-detail"><span>授权链接</span>${authLink}</div>
        </div>
        <form class="authorization-result-form" data-authorization-form>
          <div class="field">
            <label>授权后的链接</label>
            <input name="authorizedUrl" type="url" maxlength="8000" autocomplete="off" spellcheck="false" placeholder="粘贴浏览器授权完成后的完整链接">
            <small>支持从 query 或 #fragment 中读取 token</small>
          </div>
          <div class="field">
            <label>token（链接中没有时填写）</label>
            <input name="token" type="password" autocomplete="off" spellcheck="false" placeholder="ACMA:… / access_token">
            <small>重复提交会更新数据库中的 token</small>
          </div>
          <button class="button secondary" type="submit">${store.authorized ? "更新 token" : "保存授权"}</button>
        </form>
        <div class="authorization-card-actions">
          <button type="button" data-action="edit-zeshun-store">编辑名称/授权链接</button>
          <button type="button" data-action="delete-zeshun-store" class="danger-link">删除授权记录</button>
        </div>
      </article>`;
  }).join("");
}

async function loadZeshunStores() {
  const data = await api("/api/zeshun-stores");
  state.zeshunStores = data.stores || [];
  renderZeshunStores();
}

$("zeshunStoreForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const alias = $("zeshunStoreAlias").value.trim();
  const tgCode = $("zeshunTgCode").value.trim();
  const authorizationUrl = $("zeshunAuthorizationUrl").value.trim();
  if (!alias || !tgCode) return toast("请输入自定义店铺名和 TG 码", true);
  const button = $("addZeshunStoreButton");
  button.disabled = true;
  button.textContent = "正在新增…";
  try {
    await api("/api/zeshun-stores", {
      method: "POST",
      body: JSON.stringify({ alias, tg_code: tgCode, authorization_url: authorizationUrl }),
    });
    $("zeshunStoreAlias").value = "";
    $("zeshunTgCode").value = "";
    $("zeshunAuthorizationUrl").value = "";
    await loadZeshunStores();
    toast(`授权店铺已新增：${alias}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "新增店铺";
  }
});

$("zeshunStoreList").addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-authorization-form]");
  if (!form) return;
  event.preventDefault();
  const card = form.closest("[data-zeshun-store-id]");
  const storeId = Number(card.dataset.zeshunStoreId);
  const authorizedUrl = form.elements.authorizedUrl.value.trim();
  const token = form.elements.token.value.trim();
  if (!authorizedUrl && !token) return toast("请粘贴授权后的链接或填写 token", true);
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  button.textContent = "正在验证…";
  try {
    const data = await api(`/api/zeshun-stores/${storeId}/authorize`, {
      method: "POST",
      body: JSON.stringify({ authorized_url: authorizedUrl, token: token || null }),
    });
    await Promise.all([loadZeshunStores(), loadStores(data.store.id)]);
    toast(`token 已更新：${data.store.alias}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("zeshunStoreList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  const card = event.target.closest("[data-zeshun-store-id]");
  if (!button || !card) return;
  const storeId = Number(card.dataset.zeshunStoreId);
  const store = state.zeshunStores.find((item) => item.id === storeId);
  if (!store) return;
  const action = button.dataset.action;
  if (action === "copy-authorization-link") {
    try {
      await navigator.clipboard.writeText(store.authorization_url);
      toast("授权链接已复制");
    } catch (_) {
      window.prompt("复制授权链接", store.authorization_url);
    }
    return;
  }
  if (action === "edit-zeshun-store") {
    const alias = window.prompt("输入新的自定义店铺名", store.alias)?.trim();
    if (!alias) return;
    const authorizationUrl = window.prompt("输入授权链接；留空时使用系统配置", store.authorization_url || "")?.trim();
    if (authorizationUrl === undefined) return;
    try {
      await api(`/api/zeshun-stores/${storeId}`, {
        method: "PATCH",
        body: JSON.stringify({ alias, authorization_url: authorizationUrl }),
      });
      await Promise.all([loadZeshunStores(), loadStores()]);
      toast("授权店铺信息已更新");
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (action === "delete-zeshun-store") {
    if (!window.confirm(`确定删除“${store.alias}”的授权记录吗？已连接的 Yandex 上传店铺不会删除。`)) return;
    try {
      await api(`/api/zeshun-stores/${storeId}`, { method: "DELETE" });
      await loadZeshunStores();
      toast("授权记录已删除");
    } catch (error) { toast(error.message, true); }
  }
});

function validPricePercent() {
  const value = Number(pricePercentInput.value);
  return Number.isFinite(value) && value >= 1 && value <= 1000;
}

function pricePercentValue() {
  if (!validPricePercent()) throw new Error("上架价格比例必须在 1%–1000% 之间");
  return Number(pricePercentInput.value);
}

function packageValue() {
  const values = Object.fromEntries(
    Object.entries(packageInputs).map(([name, input]) => [name, Number(input.value)])
  );
  if (Object.values(values).some((value) => !Number.isFinite(value) || value <= 0 || value > 1000)) {
    throw new Error("请完整填写包装长度、宽度、高度和毛重，数值必须大于 0");
  }
  return values;
}

function validPackage() {
  try { packageValue(); return true; } catch (_) { return false; }
}

function initialStockValue() {
  const value = Number(initialStockInput.value);
  if (!Number.isInteger(value) || value < 1 || value > 2000000000) {
    throw new Error("初始可售库存必须是 1–2000000000 之间的整数");
  }
  return value;
}

function validInitialStock() {
  try { initialStockValue(); return true; } catch (_) { return false; }
}

function updatePackageStatus() {
  const status = $("packageStatus");
  if (validPackage() && validInitialStock()) {
    const values = packageValue();
    const initialStock = initialStockValue();
    status.className = "package-status ready";
    status.textContent = `将提交：${values.length} × ${values.width} × ${values.height} cm / ${values.weight} kg；每个商品初始库存 ${initialStock} 件`;
  } else if (!validPackage()) {
    status.className = "package-status";
    status.textContent = "请完整填写包装长度、宽度、高度和毛重。";
  } else {
    status.className = "package-status";
    status.textContent = "请填写真实的初始可售库存（正整数）。";
  }
  try {
    const raw = Object.fromEntries(
      Object.entries(packageInputs).map(([name, input]) => [name, input.value])
    );
    raw.initialStock = initialStockInput.value;
    window.localStorage.setItem(PACKAGE_STORAGE_KEY, JSON.stringify(raw));
  } catch (_) { /* Browser storage may be disabled; uploading still works. */ }
  updatePublishButton();
}

function restorePackageValues() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(PACKAGE_STORAGE_KEY) || "{}");
    Object.entries(packageInputs).forEach(([name, input]) => {
      if (saved[name] !== undefined) input.value = String(saved[name]);
    });
    if (saved.initialStock !== undefined) initialStockInput.value = String(saved.initialStock);
  } catch (_) { /* Ignore unavailable or malformed browser storage. */ }
  updatePackageStatus();
}

Object.values(packageInputs).forEach((input) => input.addEventListener("input", updatePackageStatus));
initialStockInput.addEventListener("input", updatePackageStatus);

async function loadExchangeRate(forceRefresh = false) {
  const text = $("exchangeRateText");
  const button = $("refreshExchangeRate");
  button.disabled = true;
  text.textContent = "正在读取 RUB/CNY 汇率…";
  try {
    const data = await api(`/api/exchange-rate${forceRefresh ? "?refresh=true" : ""}`);
    state.exchangeRate = data.exchange_rate;
    const rate = Number(state.exchangeRate.rate);
    text.textContent = `1 ₽ = ${rate.toFixed(6)} ¥（央行 ${state.exchangeRate.effective_date}）`;
    if (forceRefresh) toast("RUB/CNY 汇率已更新");
  } catch (error) {
    state.exchangeRate = null;
    text.textContent = "汇率读取失败";
    toast(error.message, true);
  } finally {
    button.disabled = false;
    updatePublishButton();
  }
}

$("refreshExchangeRate").addEventListener("click", () => loadExchangeRate(true));
pricePercentInput.addEventListener("input", updatePublishButton);

function setStoreBadge() {
  const badge = $("connectionBadge");
  const store = selectedStore();
  if (!store) {
    badge.className = "badge neutral";
    badge.innerHTML = "<span></span>未选择店铺";
    return;
  }
  badge.className = "badge connected";
  badge.innerHTML = `<span></span>${escapeHtml(store.alias)} · ${escapeHtml(store.store_name || store.business_name)}`;
}

function renderStores() {
  $("storeCount").textContent = state.stores.length;
  $("storeEmpty").classList.toggle("hidden", state.stores.length > 0);
  $("storeList").innerHTML = state.stores.map((store) => {
    const isSelected = store.id === state.selectedStoreId;
    const actualName = store.store_name || store.business_name || `Campaign ${store.campaign_id}`;
    return `
      <article class="store-card${isSelected ? " selected" : ""}" data-store-id="${store.id}">
        <button class="store-select" type="button" data-action="select" aria-label="选择 ${escapeHtml(store.alias)}">
          <span class="radio-dot"></span>
          <span class="store-main">
            <strong>${escapeHtml(store.alias)}</strong>
            <small>Yandex：${escapeHtml(actualName)}</small>
          </span>
        </button>
        <div class="store-meta">
          <span>${escapeHtml(store.placement_type || "模式未知")}</span>
          <span class="availability ${store.api_availability === "AVAILABLE" ? "ok" : ""}">${escapeHtml(store.api_availability || "API 状态未知")}</span>
        </div>
        <div class="store-actions">
          <button type="button" data-action="refresh">重新连接</button>
          <button type="button" data-action="rename">重命名</button>
          <button type="button" data-action="delete" class="danger-link">删除</button>
        </div>
      </article>`;
  }).join("");
  setStoreBadge();
  updatePublishButton();
}

async function loadStores(preferredId = null) {
  const data = await api("/api/stores");
  state.stores = data.stores || [];
  const candidate = preferredId ?? state.selectedStoreId;
  if (state.stores.some((store) => store.id === candidate)) {
    state.selectedStoreId = candidate;
  } else {
    state.selectedStoreId = state.stores[0]?.id || null;
  }
  renderStores();
}

$("storeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const alias = $("storeAlias").value.trim();
  const token = $("storeToken").value.trim();
  if (!alias || !token) return toast("请输入自定义店铺名和 token", true);
  const button = $("addStoreButton");
  button.disabled = true;
  button.textContent = "正在连接…";
  try {
    const data = await api("/api/stores", {
      method: "POST",
      body: JSON.stringify({ alias, token }),
    });
    $("storeAlias").value = "";
    $("storeToken").value = "";
    await loadStores(data.store.id);
    toast(`${data.created ? "已添加" : "已更新"}店铺：${data.store.alias}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "添加店铺";
  }
});

$("storeList").addEventListener("click", async (event) => {
  const actionButton = event.target.closest("[data-action]");
  const card = event.target.closest("[data-store-id]");
  if (!actionButton || !card) return;
  const storeId = Number(card.dataset.storeId);
  const store = state.stores.find((item) => item.id === storeId);
  if (!store) return;
  const action = actionButton.dataset.action;
  if (action === "select") {
    state.selectedStoreId = storeId;
    renderStores();
    toast(`上传目标已切换为：${store.alias}`);
    return;
  }
  if (action === "rename") {
    const alias = window.prompt("输入新的自定义店铺名", store.alias)?.trim();
    if (!alias || alias === store.alias) return;
    try {
      await api(`/api/stores/${storeId}`, { method: "PATCH", body: JSON.stringify({ alias }) });
      await loadStores(storeId);
      toast("店铺名称已更新");
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (action === "delete") {
    if (!window.confirm(`确定删除店铺“${store.alias}”吗？本地保存的加密 token 也会删除。`)) return;
    try {
      await api(`/api/stores/${storeId}`, { method: "DELETE" });
      if (state.selectedStoreId === storeId) state.selectedStoreId = null;
      await loadStores();
      toast("店铺已删除");
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (action === "refresh") {
    actionButton.disabled = true;
    try {
      await api(`/api/stores/${storeId}/refresh`, { method: "POST" });
      await loadStores(storeId);
      toast(`店铺连接正常：${store.alias}`);
    } catch (error) {
      toast(error.message, true);
    } finally {
      actionButton.disabled = false;
    }
  }
});

$("toggleStoreToken").addEventListener("click", () => {
  const input = $("storeToken");
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  $("toggleStoreToken").textContent = showing ? "显示" : "隐藏";
});

function formatPrice(product) {
  if (!product.price) return "价格待核对";
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(product.price)} ₽`;
}

function updatePublishButton() {
  const store = selectedStore();
  const ratioValid = validPricePercent();
  const packageValid = validPackage();
  const stockValid = validInitialStock();
  publishButton.disabled = state.selectedProducts.size === 0 || !store || !state.exchangeRate || !ratioValid || !packageValid || !stockValid;
  if (!store) {
    publishButton.textContent = "请先选择店铺";
  } else if (!state.exchangeRate) {
    publishButton.textContent = "人民币汇率未就绪";
  } else if (!ratioValid) {
    publishButton.textContent = "价格比例有误";
  } else if (!packageValid) {
    publishButton.textContent = "请填写包装参数";
  } else if (!stockValid) {
    publishButton.textContent = "请填写初始库存";
  } else if (state.selectedProducts.size) {
    publishButton.textContent = `上传 ${state.selectedProducts.size} 个到“${store.alias}”`;
  } else {
    publishButton.textContent = "上传选中商品";
  }
  const readyIds = state.products.filter((p) => p.ready_to_publish).map((p) => p.id);
  $("selectAll").checked = readyIds.length > 0 && readyIds.every((id) => state.selectedProducts.has(id));
}

function renderProducts(products) {
  state.products = products;
  $("resultCount").textContent = products.length;
  resultsSection.classList.toggle("hidden", products.length === 0);
  const grid = $("productGrid");
  grid.innerHTML = products.map((product) => {
    const selected = state.selectedProducts.has(product.id);
    const missing = product.missing_publish_fields || [];
    const image = product.pictures?.[0] || "";
    const pictureCount = product.pictures?.length || 0;
    const specificationCount = Object.keys(product.specifications || {}).length;
    const descriptionLength = Array.from((product.description || "").trim()).length;
    return `
      <article class="product-card${selected ? " selected" : ""}" data-id="${product.id}">
        <input class="product-select" type="checkbox" data-product-id="${product.id}"
          ${selected ? "checked" : ""} ${product.ready_to_publish ? "" : "disabled"}
          aria-label="选择 ${escapeHtml(product.name)}">
        ${image ? `<img class="product-image" src="${escapeHtml(image)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<div class="product-image"></div>`}
        <div class="product-body">
          <span class="product-tag">国外发货</span>
          <p class="product-name">${escapeHtml(product.name)}</p>
          <div class="product-meta">${escapeHtml(product.vendor || "品牌待核对")} · SKU ${escapeHtml(product.market_sku || "待识别")}</div>
          <div class="product-meta quality-metrics">主图 ${pictureCount} 张 · 规格 ${specificationCount} 项 · 描述 ${descriptionLength} 字</div>
          <div class="product-price">${formatPrice(product)}</div>
          <div class="product-foot">
            <a href="${escapeHtml(product.source_url)}" target="_blank" rel="noreferrer">查看来源 ↗</a>
            <span class="readiness${product.ready_to_publish ? "" : " warn"}">${product.ready_to_publish ? "必要字段齐全，可发布" : `待补全 ${escapeHtml(missing.join(", "))}`}</span>
          </div>
        </div>
      </article>`;
  }).join("");
  grid.querySelectorAll(".product-select").forEach((checkbox) => {
    checkbox.addEventListener("change", (event) => {
      const id = Number(event.target.dataset.productId);
      if (event.target.checked) state.selectedProducts.add(id); else state.selectedProducts.delete(id);
      event.target.closest(".product-card").classList.toggle("selected", event.target.checked);
      updatePublishButton();
    });
  });
  updatePublishButton();
}

function updateSearchStatus(run) {
  statusPanel.classList.remove("hidden");
  $("statusTitle").textContent = ({ queued: "等待开始", running: "正在抓取", completed: "抓取完成", failed: "抓取失败" })[run.status] || run.status;
  $("statusCount").textContent = `${run.found_count} / ${run.requested_count}`;
  $("statusMessage").textContent = run.message || "";
  const percent = Math.min(100, Math.round((run.found_count / run.requested_count) * 100));
  const progress = $("progressBar");
  progress.classList.toggle("indeterminate", run.status === "queued" && !run.found_count);
  progress.style.width = run.status === "completed" ? "100%" : `${percent}%`;
}

async function pollSearch() {
  if (!state.runId) return;
  try {
    const data = await api(`/api/search/${state.runId}`);
    updateSearchStatus(data.run);
    renderProducts(data.products);
    if (["completed", "failed"].includes(data.run.status)) {
      clearInterval(state.searchPollTimer);
      state.searchPollTimer = null;
      searchButton.disabled = false;
      toast(data.run.message, data.run.status === "failed");
    }
  } catch (error) {
    clearInterval(state.searchPollTimer);
    state.searchPollTimer = null;
    searchButton.disabled = false;
    toast(error.message, true);
  }
}

searchButton.addEventListener("click", async () => {
  const keyword = keywordInput.value.trim();
  const count = Number(countInput.value || 200);
  if (!keyword) return toast("请输入搜索关键词", true);
  if (!Number.isInteger(count) || count < 1 || count > Number(countInput.max)) {
    return toast(`商品个数必须在 1–${countInput.max} 之间`, true);
  }
  state.requestedCount = count;
  state.products = [];
  state.selectedProducts.clear();
  renderProducts([]);
  searchButton.disabled = true;
  try {
    const data = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({ keyword, count }),
    });
    state.runId = data.run_id;
    updateSearchStatus({ status: "queued", found_count: 0, requested_count: count, message: "任务已创建" });
    clearInterval(state.searchPollTimer);
    state.searchPollTimer = setInterval(pollSearch, 1600);
    await pollSearch();
  } catch (error) {
    searchButton.disabled = false;
    toast(error.message, true);
  }
});

$("selectAll").addEventListener("change", (event) => {
  state.products.filter((p) => p.ready_to_publish).forEach((product) => {
    if (event.target.checked) state.selectedProducts.add(product.id);
    else state.selectedProducts.delete(product.id);
  });
  renderProducts(state.products);
});

function resetPublishProgress(total, store, pricePercent, packageData, initialStock) {
  clearTimeout(state.publishPollTimer);
  $("publishPanel").classList.remove("hidden");
  $("publishTitle").textContent = "正在上传商品";
  $("publishStoreName").textContent = `目标店铺：${store.alias} · RUB × ${state.exchangeRate.rate.toFixed(6)} × ${pricePercent}% → CNY · 包装 ${packageData.length}×${packageData.width}×${packageData.height} cm / ${packageData.weight} kg · 每个商品库存 ${initialStock} 件`;
  $("publishCount").textContent = `0 / ${total}`;
  $("publishProgressBar").style.width = "0%";
  $("publishProcessed").textContent = "0";
  $("publishSucceeded").textContent = "0";
  $("publishFailed").textContent = "0";
  $("publishResults").innerHTML = `<div class="result-pending">Yandex 正在逐个接收商品，请保持页面打开…</div>`;
  $("publishPanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderPublishJob(job) {
  const processed = Number(job.processed ?? ((job.succeeded || 0) + (job.failed || 0)));
  const total = Number(job.total || 0);
  const percent = total ? Math.min(100, Math.round((processed / total) * 100)) : 0;
  $("publishCount").textContent = `${processed} / ${total}`;
  $("publishProgressBar").style.width = `${percent}%`;
  $("publishProcessed").textContent = processed;
  $("publishSucceeded").textContent = job.succeeded || 0;
  $("publishFailed").textContent = job.failed || 0;
  $("publishTitle").textContent = job.status === "running" ? "正在上传商品" : "上传任务已完成";
  const store = selectedStore();
  if (job.exchange_rate && store) {
    const dimensions = job.package || {};
    const packageText = dimensions.length
      ? ` · 包装 ${dimensions.length}×${dimensions.width}×${dimensions.height} cm / ${dimensions.weight} kg`
      : "";
    const stockText = job.initial_stock
      ? ` · 每个商品库存 ${job.initial_stock} 件${job.warehouse_name ? `（${job.warehouse_name}）` : ""}`
      : "";
    $("publishStoreName").textContent = `目标店铺：${store.alias} · RUB × ${Number(job.exchange_rate).toFixed(6)} × ${Number(job.price_percent)}% → ${job.target_currency || "CNY"}${packageText}${stockText}`;
  }
  const results = job.results || [];
  $("publishResults").innerHTML = results.length ? results.map((result) => `
    <div class="publish-result ${result.pending ? "pending" : (result.success ? "success" : "failure")}">
      <span class="result-icon">${result.pending ? "…" : (result.success ? "✓" : "!")}</span>
      <span class="result-product">
        <strong>${escapeHtml(result.product_name || `商品 #${result.product_id}`)}</strong>
        <small>${escapeHtml(result.offer_id || "未生成 offer ID")}</small>
      </span>
      <span class="result-message">${escapeHtml(result.message || (result.pending ? "等待写库存" : (result.success ? "已提交" : "提交失败")))}</span>
    </div>`).join("") : `<div class="result-pending">Yandex 正在逐个接收商品，请保持页面打开…</div>`;
}

async function pollPublish(jobId) {
  try {
    const data = await api(`/api/publish/${jobId}`);
    const job = data.job;
    renderPublishJob(job);
    if (job.status === "running") {
      state.publishPollTimer = setTimeout(() => pollPublish(jobId), 900);
      return;
    }
    const successfulIds = new Set((job.results || []).filter((item) => item.success).map((item) => item.product_id));
    successfulIds.forEach((id) => state.selectedProducts.delete(id));
    renderProducts(state.products);
    toast(`上传完成：成功 ${job.succeeded}，失败 ${job.failed}`, job.failed > 0);
  } catch (error) {
    toast(error.message, true);
  } finally {
    updatePublishButton();
  }
}

publishButton.addEventListener("click", async () => {
  const store = selectedStore();
  if (!store) return toast("请先选择上传店铺", true);
  if (!state.selectedProducts.size) return;
  let pricePercent;
  try { pricePercent = pricePercentValue(); } catch (error) { return toast(error.message, true); }
  let packageData;
  try { packageData = packageValue(); } catch (error) { return toast(error.message, true); }
  let initialStock;
  try { initialStock = initialStockValue(); } catch (error) { return toast(error.message, true); }
  if (!state.exchangeRate) return toast("请先刷新 RUB/CNY 汇率", true);
  const selectedIds = [...state.selectedProducts];
  const sample = state.products.find((item) => state.selectedProducts.has(item.id) && item.price);
  const example = sample
    ? `例如 ${Number(sample.price).toFixed(2)} RUB → ${(Number(sample.price) * Number(state.exchangeRate.rate) * pricePercent / 100).toFixed(2)} CNY。`
    : "";
  const packageMessage = `本批统一包装：${packageData.length}×${packageData.width}×${packageData.height} cm，毛重 ${packageData.weight} kg。`;
  const stockMessage = `程序会恢复商品展示，并向店铺仓库为每个商品写入 ${initialStock} 件可售库存。`;
  if (!window.confirm(`将把 ${selectedIds.length} 个商品提交到“${store.alias}”。价格按俄罗斯央行汇率换算成人民币，再乘以 ${pricePercent}%。${example}${packageMessage}${stockMessage}请确认库存真实可履约，是否继续？`)) return;
  publishButton.disabled = true;
  resetPublishProgress(selectedIds.length, store, pricePercent, packageData, initialStock);
  try {
    const data = await api("/api/publish", {
      method: "POST",
      body: JSON.stringify({
        store_id: store.id,
        product_ids: selectedIds,
        price_percent: pricePercent,
        package: packageData,
        initial_stock: initialStock,
      }),
    });
    renderPublishJob(data.job);
    await pollPublish(data.job_id);
  } catch (error) {
    $("publishTitle").textContent = "上传任务未能启动";
    $("publishResults").innerHTML = `<div class="publish-result failure"><span class="result-icon">!</span><span class="result-message">${escapeHtml(error.message)}</span></div>`;
    toast(error.message, true);
    updatePublishButton();
  }
});

async function checkBackend() {
  try {
    await api("/api/health");
  } catch (error) {
    const badge = $("connectionBadge");
    badge.className = "badge error";
    badge.innerHTML = "<span></span>本地服务未启动";
    toast(error.message, true);
  }
}

restorePackageValues();
Promise.all([checkBackend(), loadZeshunStores(), loadStores(), loadExchangeRate()]).catch((error) => toast(error.message, true));
