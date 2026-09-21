"use strict";

const pageStatus = document.getElementById("page-status");
const authStatus = document.getElementById("auth-status");
const collectButton = document.getElementById("collect");
const queueCount = document.getElementById("queue-count");
const resultBox = document.getElementById("result");
const platformBadge = document.getElementById("platform-badge");
const productPanel = document.getElementById("product-panel");
const zyingPanel = document.getElementById("zying-panel");
const weightPricePanel = document.getElementById("weight-price-panel");
const purchasePanel = document.getElementById("purchase-panel");
const productModeButton = document.getElementById("product-mode");
const zyingModeButton = document.getElementById("zying-mode");
const weightPriceModeButton = document.getElementById("weight-price-mode");
const purchaseModeButton = document.getElementById("purchase-mode");
const zyingPageStatus = document.getElementById("zying-page-status");
const zyingRefreshButton = document.getElementById("zying-refresh");
const zyingStartPage = document.getElementById("zying-start-page");
const zyingEndPage = document.getElementById("zying-end-page");
const zyingCategory = document.getElementById("zying-category");
const zyingDeveloper = document.getElementById("zying-developer");
const zyingStartButton = document.getElementById("zying-start");
const zyingStopButton = document.getElementById("zying-stop");
const zyingTaskStatus = document.getElementById("zying-task-status");
const zyingSummary = document.getElementById("zying-summary");
const zyingLog = document.getElementById("zying-log");
const weightPricePageStatus = document.getElementById("weight-price-page-status");
const weightPriceOpenLoginButton = document.getElementById("weight-price-open-login");
const weightPriceConfirmLoginButton = document.getElementById("weight-price-confirm-login");
const weightPriceRefreshCategoriesButton = document.getElementById("weight-price-refresh-categories");
const weightPriceCategory = document.getElementById("weight-price-category");
const weightPriceStartPage = document.getElementById("weight-price-start-page");
const weightPriceEndPage = document.getElementById("weight-price-end-page");
const weightPriceStartItem = document.getElementById("weight-price-start-item");
const weightPriceLimit = document.getElementById("weight-price-limit");
const weightPriceStartButton = document.getElementById("weight-price-start");
const weightPriceStopButton = document.getElementById("weight-price-stop");
const weightPriceStartHint = document.getElementById("weight-price-start-hint");
const weightPriceResume = document.getElementById("weight-price-resume");
const weightPriceAcknowledged = document.getElementById("weight-price-acknowledged");
const weightPriceOpenSupplierButton = document.getElementById("weight-price-open-supplier");
const weightPriceTaskStatus = document.getElementById("weight-price-task-status");
const weightPriceSummary = document.getElementById("weight-price-summary");
const weightPriceLog = document.getElementById("weight-price-log");
const purchasePageStatus = document.getElementById("purchase-page-status");
const purchaseTaskMeta = document.getElementById("purchase-task-meta");
const purchaseOpenLoginButton = document.getElementById("purchase-open-login");
const purchaseConfirmLoginButton = document.getElementById("purchase-confirm-login");
const purchaseTaskStatus = document.getElementById("purchase-task-status");
const purchaseSummary = document.getElementById("purchase-summary");
const purchaseResults = document.getElementById("purchase-results");
const zyingOpenLoginButton = document.getElementById("zying-open-login");
let activeTab = null;
let detailPage = false;
let productDetailZyingLoggedIn = false;
let authenticated = false;
let pagePlatform = "mercado";
let zyingContext = null;
let zyingRunning = false;
let zyingPollTimer = null;
let weightPriceState = null;
let weightPriceRunning = false;
let weightPricePollTimer = null;
let weightPriceSelectionRestored = false;
let weightPriceBusy = false;
let weightPriceStatusSequence = 0;
let weightPriceStatusError = "";
let purchaseTrackingState = null;
let purchaseTrackingPollTimer = null;
const productBatchFields = Object.fromEntries([
  "country", "keyword", "tab", "max-items", "concurrency", "min-sales", "max-sales"
].map(name => [name, document.getElementById(`product-${name}`)]));
const productBatchStart = document.getElementById("product-batch-start");
const productBatchStop = document.getElementById("product-batch-stop");
let productBatchState = {};
let productBatchBusy = false;
let productBatchPollTimer = null;
let productZyingLoggedIn = false;

function productBatchSelection() {
  const params = {};
  for (const [field, key, label, max] of [
    ["max-items", "max_items", "采集数量", 500], ["concurrency", "concurrency", "并发数", 10]
  ]) {
    const value = Number(productBatchFields[field].value);
    if (!Number.isInteger(value) || value < 1 || value > max) throw new Error(`${label}须为 1–${max} 的整数`);
    params[key] = value;
  }
  for (const [field, key, label] of [
    ["min-sales", "min_sales", "最低销量"], ["max-sales", "max_sales", "最高销量"]
  ]) {
    const raw = productBatchFields[field].value.trim();
    if (!raw) { params[key] = null; continue; }
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 0 || value > 1000000000) throw new Error(`${label}须为 0–1000000000 的整数`);
    params[key] = value;
  }
  if (params.min_sales !== null && params.max_sales !== null && params.max_sales < params.min_sales) {
    throw new Error("最高销量不能低于最低销量");
  }
  if (!productBatchFields.tab.value) throw new Error("请选择已打开的 Mercado Libre 搜索列表页");
  if (!productZyingLoggedIn) throw new Error("请先确认所选美客多页面中的智赢插件已登录");
  return {tab_id: Number(productBatchFields.tab.value), params};
}

function syncProductBatchControls() {
  const locked = productBatchBusy || Boolean(productBatchState.running);
  productBatchStart.disabled = locked || !authenticated || !productZyingLoggedIn;
  productBatchStop.disabled = productBatchBusy || !productBatchState.running;
  for (const field of Object.values(productBatchFields)) field.disabled = locked;
  document.getElementById("product-search").disabled = locked;
  document.getElementById("product-refresh-tabs").disabled = locked;
}

function renderProductBatch(state = {}) {
  productBatchState = state;
  document.getElementById("product-batch-status").textContent = state.message || "等待启动";
  const requested = Number(state.params?.max_items || 0);
  const candidates = Number(state.candidate_count || 0);
  const processed = Number(state.processed_count || 0);
  const success = Number(state.completed_count || 0);
  const failed = Number(state.failed_count || 0);
  document.getElementById("product-batch-summary").textContent =
    `页数 ${state.current_page || 0} · 候选 ${candidates} · 已处理 ${processed} · 成功 ${success} · 失败 ${failed} · 跳过 ${state.skipped || 0}` +
    (state.last_error ? `\n最近失败：${state.last_error}` : "");
  const elapsed = Math.max(0, Math.floor(((state.finished_at || Date.now()) - Number(state.started_at || Date.now())) / 1000));
  const clock = [Math.floor(elapsed / 3600), Math.floor((elapsed % 3600) / 60), elapsed % 60]
    .map(value => String(value).padStart(2, "0")).join(":");
  document.getElementById("product-batch-current").textContent =
    `当前商品 ${state.current_item_id || "-"} · 并发 ${state.params?.concurrency || 0} · 耗时 ${clock}`;
  document.getElementById("product-batch-progress").style.width =
    `${Math.min(100, Math.round(processed * 100 / Math.max(requested, candidates, 1)))}%`;
  syncProductBatchControls();
}

async function checkProductZyingStatus() {
  const status = document.getElementById("product-zying-status");
  const tabId = Number(productBatchFields.tab.value || 0);
  productZyingLoggedIn = false;
  status.className = "plugin-login-status checking";
  if (!tabId) {
    status.textContent = "智赢插件：请先选择美客多列表页面";
    syncProductBatchControls();
    return;
  }
  status.textContent = "智赢插件：正在检查登录状态…";
  try {
    const response = await runtimeMessage({type: "CHECK_PRODUCT_ZYING", tab_id: tabId});
    if (!response.ok) throw new Error(response.error || "检查失败");
    productZyingLoggedIn = Boolean(response.logged_in);
    status.textContent = `智赢插件：${response.message || (productZyingLoggedIn ? "已登录" : "未登录")}`;
    status.className = `plugin-login-status ${productZyingLoggedIn ? "ok" : "error"}`;
  } catch (error) {
    status.textContent = `智赢插件：${error.message || error}`;
    status.className = "plugin-login-status error";
  }
  syncProductBatchControls();
}

async function loadProductBatchStatus() {
  clearTimeout(productBatchPollTimer);
  try {
    const response = await runtimeMessage({type: "GET_PRODUCT_BATCH_STATUS"});
    if (!response.ok) throw new Error(response.error || "读取采集进度失败");
    renderProductBatch(response.state);
  } catch (error) {
    document.getElementById("product-batch-status").textContent = error.message || String(error);
  }
  if (productBatchState.running) productBatchPollTimer = setTimeout(loadProductBatchStatus, 1200);
}

async function refreshProductTabs(preferredId) {
  const previous = String(preferredId || productBatchFields.tab.value || activeTab?.id || "");
  const tabs = (await chrome.tabs.query({})).filter(tab => {
    try { return new URL(tab.url).protocol === "https:" && /(^|\.)(mercadolibre\.com\.(mx|br|ar|co|uy)|mercadolivre\.com\.br|mercadolibre\.cl)$/.test(new URL(tab.url).hostname); }
    catch (_) { return false; }
  });
  productBatchFields.tab.replaceChildren(new Option("请选择已打开的 Mercado Libre 列表页", ""));
  for (const tab of tabs) productBatchFields.tab.add(new Option(`${tab.title || "Mercado Libre"} — ${new URL(tab.url).hostname}`, String(tab.id)));
  const selected = tabs.find(tab => String(tab.id) === previous) || tabs.find(tab => tab.active) || tabs[0];
  if (selected) productBatchFields.tab.value = String(selected.id);
  await checkProductZyingStatus();
}

async function saveProductBatchOptions() {
  if (!chrome.storage?.local) return;
  await chrome.storage.local.set({productBatchOptions: Object.fromEntries(
    Object.entries(productBatchFields).map(([key, field]) => [key, field.value])
  )});
}

async function initializeProductBatch() {
  const saved = chrome.storage?.local ? (await chrome.storage.local.get("productBatchOptions")).productBatchOptions : null;
  if (saved) {
    for (const [key, value] of Object.entries(saved)) {
      if (key !== "tab" && productBatchFields[key]) productBatchFields[key].value = value;
    }
  }
  await refreshProductTabs(saved?.tab);
  await loadProductBatchStatus();
}

function runtimeMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, response => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve(response || {});
    });
  });
}

function tabMessage(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, response => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve(response || {});
    });
  });
}

function showResult(message, kind) {
  resultBox.hidden = false;
  resultBox.className = `result ${kind || ""}`;
  resultBox.textContent = message;
}

function showMode(mode) {
  const zying = mode === "zying";
  const weightPrice = mode === "weight-price";
  const purchase = mode === "purchase";
  productPanel.hidden = zying || weightPrice || purchase;
  zyingPanel.hidden = !zying;
  weightPricePanel.hidden = !weightPrice;
  purchasePanel.hidden = !purchase;
  productModeButton.classList.toggle("active", !zying && !weightPrice && !purchase);
  zyingModeButton.classList.toggle("active", zying);
  weightPriceModeButton.classList.toggle("active", weightPrice);
  purchaseModeButton.classList.toggle("active", purchase);
  if (zying) loadZyingStatus();
  if (weightPrice) {
    refreshState().catch(error => showResult(error.message || String(error), "error"));
    loadWeightPriceStatus();
  }
  if (purchase) loadPurchaseTrackingStatus();
}

function isZyingPage(tab) {
  try { return new URL(tab?.url || "").hostname === "meli.zying.net"; } catch (_) { return false; }
}

async function refreshState() {
  const response = await runtimeMessage({type: "GET_STATE"});
  queueCount.textContent = String(response.queueLength || 0);
  authenticated = Boolean(response.authenticated);
  if (authenticated && response.user) {
    authStatus.textContent = `泽顺账号：${response.user.display_name || response.user.username}` +
      (response.compatibilityMode ? "（兼容模式）" : "");
    authStatus.className = "auth-line logged-in";
  } else {
    authStatus.textContent = "泽顺账号：未登录，请先打开设置登录";
    authStatus.className = "auth-line";
  }
  collectButton.disabled = !detailPage || !authenticated || !productDetailZyingLoggedIn;
  syncProductBatchControls();
  zyingStartButton.disabled = !authenticated || !zyingContext || zyingRunning;
  syncWeightPriceControls();
  if (response.purchaseTracking) renderPurchaseTrackingStatus(response.purchaseTracking);
}

async function initialize() {
  [activeTab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (!activeTab || !activeTab.id) {
    pageStatus.textContent = "未找到当前标签页。";
    await refreshState();
    return;
  }
  if (isZyingPage(activeTab)) {
    showMode("zying");
    await refreshState();
    zyingPageStatus.textContent = "已打开智赢网页版；请完成登录后点击“读取当前网页分类与开发”。";
    return;
  }
  try {
    const response = await tabMessage(activeTab.id, {type: "PING_PAGE"});
    if (response.ok && response.detail) {
      detailPage = true;
      productDetailZyingLoggedIn = Boolean(response.zying?.logged_in);
      pagePlatform = response.platform === "1688" ? "1688" : "mercado";
      platformBadge.hidden = false;
      platformBadge.textContent = pagePlatform === "1688" ? "1688" : "Mercado";
      platformBadge.className = `platform-badge ${pagePlatform === "1688" ? "source-1688" : "mercado"}`;
      pageStatus.textContent = pagePlatform === "1688"
        ? "已识别 1688 商品详情页，将采集到“AI原创产品”。"
        : `已识别 Mercado Libre 商品详情页。${response.zying?.message || "正在检查智赢插件"}`;
      if (pagePlatform === "1688") productDetailZyingLoggedIn = true;
      collectButton.textContent = pagePlatform === "1688" ? "采集到 AI原创产品" : "采集当前商品";
    } else {
      platformBadge.hidden = true;
      pageStatus.textContent = "请打开 Mercado Libre 或 1688 商品详情页；Mercado 列表页也可使用卡片上的采集按钮。";
    }
  } catch (_) {
    pageStatus.textContent = "当前页面不支持采集，请打开 Mercado Libre 商品详情页。";
  }
  await refreshState();
}

function zyingCategoryDisplayName(row) {
  const id = String(row.category_id || row.value || "").trim();
  for (const value of [row.category_name, row.category_leaf_name, row.label, row.name]) {
    let name = String(value || "").trim();
    for (const suffix of [` [${id}]`, `[${id}]`, `（${id}）`, `(${id})`]) {
      if (id && name.endsWith(suffix)) name = name.slice(0, -suffix.length).trim();
    }
    if (name && name !== id && !/^\d+$/.test(name) && name !== "[object Object]") return name;
  }
  return "";
}

function renderZyingOptions(data) {
  const categories = Array.isArray(data.categories) ? data.categories : [];
  const developers = Array.isArray(data.developers) ? data.developers : [];
  const previousCategory = zyingCategory.value;
  const previousDeveloper = zyingDeveloper.value;
  zyingCategory.innerHTML = '<option value="">全部分类</option>' + categories.map(row => {
    const value = String(row.category_id || row.category_name || "");
    const label = zyingCategoryDisplayName(row);
    return `<option value="${escapeHtml(value)}"${label ? "" : " disabled"}>${escapeHtml(label || "分类名称待同步，请刷新智赢产品页")}</option>`;
  }).join("");
  zyingDeveloper.innerHTML = '<option value="">全部产品开发</option>' + developers.map(row => (
    `<option value="${escapeHtml(String(row.id || ""))}">${escapeHtml(String(row.name || row.id || ""))}</option>`
  )).join("");
  if ([...zyingCategory.options].some(option => option.value === previousCategory && !option.disabled)) zyingCategory.value = previousCategory;
  if ([...zyingDeveloper.options].some(option => option.value === previousDeveloper)) zyingDeveloper.value = previousDeveloper;
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

const purchasePlatformLabels = {
  "1688": "1688",
  taobao: "淘宝 / 天猫",
  pdd: "拼多多",
  xianyu: "闲鱼"
};

function schedulePurchaseTrackingPoll() {
  clearTimeout(purchaseTrackingPollTimer);
  const phase = String(purchaseTrackingState?.phase || "");
  if (purchaseTrackingState?.running || phase === "waiting_plugin" || phase === "waiting_login") {
    purchaseTrackingPollTimer = setTimeout(loadPurchaseTrackingStatus, 1400);
  }
}

function renderPurchaseTrackingStatus(state = {}) {
  purchaseTrackingState = state;
  const hasTask = Boolean(state.request_id);
  const running = Boolean(state.running);
  const phase = String(state.phase || "idle");
  const total = Number(state.total || 0);
  const synced = Number(state.synced || 0);
  const pending = Number(state.pending || 0);
  const failed = Number(state.failed || 0);
  purchasePageStatus.textContent = hasTask
    ? `${purchasePlatformLabels[state.platform] || state.platform || "采购平台"} · 已选择 ${total} 个订单`
    : "请先在泽顺控制台订单页创建物流号同步任务。";
  purchaseTaskMeta.hidden = !hasTask;
  purchaseTaskMeta.textContent = hasTask
    ? `任务 ${state.request_id.slice(0, 8)}… · ${purchasePlatformLabels[state.platform] || state.platform} · ${total} 单`
    : "";
  purchaseTaskStatus.textContent = state.message || (running ? "同步进行中…" : "等待同步任务");
  purchaseSummary.textContent = `成功 ${synced} · 待发货 ${pending} · 未同步 ${failed}`;
  const statusLabels = {synced: "已同步", pending: "待发货", not_found: "未找到", failed: "失败"};
  purchaseResults.innerHTML = (state.results || []).map(item => `
    <div class="purchase-result">
      <strong>${escapeHtml(item.purchase_order || item.order_id || "-")}</strong>
      <span>${escapeHtml(item.tracking_number || item.message || statusLabels[item.status] || item.status || "-")}</span>
    </div>
  `).join("");
  purchaseOpenLoginButton.disabled = !authenticated || !hasTask || phase === "completed" || phase === "error";
  purchaseConfirmLoginButton.disabled = !authenticated || !hasTask || running || phase === "completed" || phase === "error";
  purchaseOpenLoginButton.textContent = phase === "waiting_login" ? "重新打开采购平台" : "打开采购平台登录";
  purchaseConfirmLoginButton.textContent = running ? "同步进行中…" : "确定登录并开始同步";
  schedulePurchaseTrackingPoll();
}

async function loadPurchaseTrackingStatus() {
  try {
    const response = await runtimeMessage({type: "GET_PURCHASE_TRACKING_REQUEST"});
    if (!response.ok) throw new Error(response.error || "读取订单物流同步任务失败");
    renderPurchaseTrackingStatus(response);
  } catch (error) {
    purchasePageStatus.textContent = error.message || String(error);
    purchaseOpenLoginButton.disabled = true;
    purchaseConfirmLoginButton.disabled = true;
  }
}

purchaseOpenLoginButton.addEventListener("click", async () => {
  purchaseOpenLoginButton.disabled = true;
  purchasePageStatus.textContent = "正在打开采购平台…";
  try {
    const response = await runtimeMessage({
      type: "OPEN_PURCHASE_TRACKING_LOGIN",
      request: purchaseTrackingState
    });
    if (!response.ok) throw new Error(response.error || "打开采购平台失败");
    purchaseTaskStatus.textContent = response.message || "请手动登录采购平台后点击确定登录";
    await loadPurchaseTrackingStatus();
  } catch (error) {
    purchasePageStatus.textContent = error.message || String(error);
    purchaseOpenLoginButton.disabled = false;
  }
});

purchaseConfirmLoginButton.addEventListener("click", async () => {
  purchaseConfirmLoginButton.disabled = true;
  purchaseTaskStatus.textContent = "正在检查采购平台登录状态…";
  try {
    const response = await runtimeMessage({type: "CONFIRM_PURCHASE_TRACKING_LOGIN"});
    if (!response.ok) throw new Error(response.error || "尚未确认登录");
    purchaseTaskStatus.textContent = response.message || "已确认登录，正在自动同步物流号";
    await loadPurchaseTrackingStatus();
  } catch (error) {
    purchaseTaskStatus.textContent = error.message || String(error);
    purchaseConfirmLoginButton.disabled = false;
  }
});

async function refreshZyingOptions() {
  if (!activeTab?.id || !isZyingPage(activeTab)) {
    zyingContext = null;
    zyingPageStatus.textContent = "正在打开智赢网页版登录页面；登录后重新打开插件并点击读取。";
    try { await runtimeMessage({type: "OPEN_ZYING_LOGIN"}); }
    catch (error) { zyingPageStatus.textContent = error.message || String(error); }
    await refreshState();
    return;
  }
  zyingRefreshButton.disabled = true;
  zyingRefreshButton.textContent = "正在读取本地智赢网页…";
  try {
    const contextResponse = await runtimeMessage({type: "READ_ZYING_CONTEXT", tabId: activeTab.id});
    if (!contextResponse.ok) throw new Error(contextResponse.error || "未读取到智赢登录状态");
    zyingContext = {
      credential: contextResponse.credential,
      categories: contextResponse.categories || []
    };
    const options = await runtimeMessage({type: "GET_ZYING_OPTIONS", context: zyingContext});
    if (!options.ok) throw new Error(options.error || "读取智赢筛选项失败");
    renderZyingOptions(options);
    zyingPageStatus.textContent = `已连接当前智赢网页：${Number(options.categories?.length || 0)} 个分类，${Number(options.developers?.length || 0)} 位产品开发。`;
    if ((options.categories || []).some(row => !zyingCategoryDisplayName(row))) {
      zyingPageStatus.textContent += " 部分分类名称尚未读取，请等待智赢产品页加载完成后重新读取。";
    }
  } catch (error) {
    zyingContext = null;
    zyingPageStatus.textContent = error.message || String(error);
  } finally {
    zyingRefreshButton.disabled = zyingRunning;
    zyingRefreshButton.textContent = "读取当前网页分类与开发";
    await refreshState();
  }
}

function scheduleZyingPoll() {
  clearTimeout(zyingPollTimer);
  zyingPollTimer = setTimeout(loadZyingStatus, zyingRunning ? 1200 : 5000);
}

async function loadZyingStatus() {
  try {
    const response = await runtimeMessage({type: "GET_ZYING_STATUS"});
    if (!response.ok) throw new Error(response.error || "读取智赢采集状态失败");
    zyingRunning = Boolean(response.running);
    const summary = response.summary || {};
    zyingTaskStatus.textContent = response.message || (zyingRunning ? "采集中" : "等待启动");
    zyingSummary.textContent = `入库 ${Number(summary.inserted_count || 0)} · 已有跳过 ${Number(summary.skipped_existing_count || 0)} · 重复 ${Number(summary.duplicate_count || 0)}`;
    const logs = Array.isArray(response.logs) ? response.logs : [];
    zyingLog.textContent = logs.length ? logs.slice(-80).join("\n") : "等待任务启动…";
    zyingLog.scrollTop = zyingLog.scrollHeight;
    zyingStartButton.disabled = !authenticated || !zyingContext || zyingRunning;
    zyingStopButton.disabled = !zyingRunning || response.status === "stopping";
    zyingStartPage.disabled = zyingRunning;
    zyingEndPage.disabled = zyingRunning;
    zyingCategory.disabled = zyingRunning;
    zyingDeveloper.disabled = zyingRunning;
    zyingRefreshButton.disabled = zyingRunning;
  } catch (error) {
    zyingTaskStatus.textContent = error.message || String(error);
  } finally {
    scheduleZyingPoll();
  }
}

function renderWeightPriceCategories(rows) {
  const previous = weightPriceCategory.value;
  const options = Array.isArray(rows) ? rows : [];
  weightPriceCategory.innerHTML = '<option value="">全部分类</option>' + options.map(row => (
    `<option value="${escapeHtml(String(row.value || ""))}">${escapeHtml(String(row.label || row.name || row.value || ""))}</option>`
  )).join("");
  if ([...weightPriceCategory.options].some(option => option.value === previous)) {
    weightPriceCategory.value = previous;
  }
}

function weightPriceParams() {
  const startPage = Number(weightPriceStartPage.value);
  const endPage = Number(weightPriceEndPage.value);
  const startItem = Number(weightPriceStartItem.value);
  const maxItems = Number(weightPriceLimit.value);
  if (!Number.isInteger(startPage) || startPage < 1 || startPage > 10000 ||
      !Number.isInteger(endPage) || endPage < startPage || endPage > 10000) {
    throw new Error("结束页必须大于或等于起始页，页码范围为 1–10000。");
  }
  if (endPage - startPage + 1 > Number(weightPriceState?.max_pages || 100)) {
    throw new Error(`本次最多可选择 ${Number(weightPriceState?.max_pages || 100)} 页。`);
  }
  if (!Number.isInteger(startItem) || startItem < 1 || startItem > 10000) {
    throw new Error("首件序号必须是 1–10000 的整数。");
  }
  if (!Number.isInteger(maxItems) || maxItems < 1 || maxItems > 10000) {
    throw new Error("最多商品数必须是 1–10000 的整数。");
  }
  return {
    selection: {
      category: weightPriceCategory.value,
      start_page: startPage,
      end_page: endPage,
      start_item: startItem
    },
    max_items: maxItems
  };
}

function weightPriceStartReason() {
  if (weightPriceBusy) return "正在处理操作，请稍候…";
  if (!authenticated) return "请先打开插件设置，登录泽顺账号。";
  if (!weightPriceState) return weightPriceStatusError || "正在连接本机控制台，请稍候…";
  if (weightPriceState.can_execute === false) return "当前账号没有核重核价执行权限，请联系管理员开通。";
  if (weightPriceRunning) return "任务正在运行，可查看进度或停止任务。";
  if (!weightPriceState.login?.confirmed) return "请打开智赢登录页面，完成登录后点击“确认已登录”。";
  if (weightPriceState.circuit) {
    return weightPriceAcknowledged.checked ? "" : "请先处理上方暂停原因，再勾选“已完成登录或人机验证，继续原任务”。";
  }
  try { weightPriceParams(); } catch (error) { return error.message; }
  return "";
}

function syncWeightPriceControls() {
  const state = weightPriceState || {};
  const canExecute = Boolean(weightPriceState) && state.can_execute !== false;
  const confirmed = Boolean(state.login?.confirmed);
  const blocked = Boolean(state.circuit);
  const idleDisabled = !authenticated || !canExecute || weightPriceRunning || weightPriceBusy;
  weightPriceOpenLoginButton.disabled = idleDisabled;
  weightPriceConfirmLoginButton.disabled = idleDisabled;
  weightPriceOpenSupplierButton.disabled = idleDisabled;
  weightPriceRefreshCategoriesButton.disabled = idleDisabled || !confirmed;
  const reason = weightPriceStartReason();
  weightPriceStartButton.disabled = Boolean(reason);
  weightPriceStartButton.title = reason;
  weightPriceStartHint.textContent = reason || (blocked ? "将保留原任务范围和进度，从暂停商品继续。" : "已就绪，可以启动核重核价。");
  weightPriceStartButton.textContent = weightPriceBusy ? "正在处理…" : blocked ? "继续核重核价" : "启动核重核价";
  weightPriceStopButton.disabled = !authenticated || !canExecute || !weightPriceRunning || weightPriceBusy;
  weightPriceResume.hidden = !blocked;
  weightPriceAcknowledged.disabled = idleDisabled;
  for (const element of [weightPriceCategory, weightPriceStartPage, weightPriceEndPage, weightPriceStartItem, weightPriceLimit]) {
    element.disabled = weightPriceRunning || weightPriceBusy || blocked;
  }
}

function renderWeightPriceStatus(state = {}) {
  if (JSON.stringify(weightPriceState?.circuit) !== JSON.stringify(state.circuit)) {
    weightPriceAcknowledged.checked = false;
  }
  weightPriceStatusError = "";
  weightPriceState = state;
  weightPriceRunning = Boolean(state.running);
  renderWeightPriceCategories(state.categories);
  if (!weightPriceSelectionRestored && state.selection) {
    weightPriceSelectionRestored = true;
    weightPriceStartPage.value = Number(state.selection.start_page || 1);
    weightPriceEndPage.value = Number(state.selection.end_page || state.selection.start_page || 1);
    weightPriceStartItem.value = Number(state.selection.start_item || 1);
    if ([...weightPriceCategory.options].some(option => option.value === state.selection.category)) {
      weightPriceCategory.value = state.selection.category;
    }
  }
  const confirmed = Boolean(state.login?.confirmed);
  const run = state.run || {};
  const counts = state.current_counts || state.counts || {};
  const statusLabel = state.running ? "运行中" : state.circuit ? "已暂停" : ({
    completed: "已完成",
    stopped: "已停止",
    blocked: "已暂停",
    failed: "失败"
  }[run.outcome] || "等待启动");
  weightPricePageStatus.textContent = state.circuit?.reason
    ? `等待人工处理：${state.circuit.reason}`
    : confirmed
      ? `已连接 ${state.computer || "本机"}，智赢登录已确认；1688登录将在执行时检查。`
      : "请先打开智赢登录页面，完成登录后点击确认。";
  weightPriceTaskStatus.textContent = `${statusLabel} · ${run.message || state.run_error || "可设置范围后启动"}`;
  weightPriceSummary.textContent = `屏蔽 ${Number(counts.blocked || 0)} · 风险 ${Number(counts.risk || 0)} · 成功 ${Number(counts.success || 0)} · 异常 ${Number(counts.exception || 0)}`;
  weightPriceLog.textContent = state.circuit?.reason || state.run_error ||
    (run.current_task_id ? `当前商品 ${run.current_task_id}\n${run.message || "正在逐件核验"}` :
      "任务启动后可在泽顺控制台查看完整逐件日志与结果。");
  syncWeightPriceControls();
}

function scheduleWeightPricePoll() {
  clearTimeout(weightPricePollTimer);
  weightPricePollTimer = setTimeout(loadWeightPriceStatus, weightPriceRunning ? 1200 : 5000);
}

async function loadWeightPriceStatus() {
  if (weightPriceBusy) return;
  const sequence = ++weightPriceStatusSequence;
  try {
    const response = await runtimeMessage({type: "GET_AI_WEIGHT_PRICE_STATUS"});
    if (!response.ok) throw new Error(response.error || "读取AI核重核价状态失败");
    if (sequence === weightPriceStatusSequence) renderWeightPriceStatus(response);
  } catch (error) {
    if (sequence !== weightPriceStatusSequence) return;
    weightPriceStatusError = error.message || String(error);
    weightPricePageStatus.textContent = error.message || String(error);
    weightPriceState = null;
    weightPriceRunning = false;
    syncWeightPriceControls();
  } finally {
    scheduleWeightPricePoll();
  }
}

async function runWeightPriceAction(type, pendingText) {
  weightPriceTaskStatus.textContent = pendingText;
  syncWeightPriceControls();
  const response = await runtimeMessage({type});
  if (!response.ok) throw new Error(response.error || pendingText.replace("正在", "") + "失败");
  renderWeightPriceStatus(response);
  return response;
}

function beginWeightPriceAction() {
  weightPriceBusy = true;
  weightPriceStatusSequence += 1;
  clearTimeout(weightPricePollTimer);
  syncWeightPriceControls();
}

async function finishWeightPriceAction() {
  weightPriceBusy = false;
  await loadWeightPriceStatus();
}

collectButton.addEventListener("click", async () => {
  collectButton.disabled = true;
  collectButton.textContent = "正在读取商品…";
  try {
    const extracted = await tabMessage(activeTab.id, {type: "EXTRACT_PRODUCT"});
    if (!extracted.ok) throw new Error(extracted.error || "无法读取商品详情");
    collectButton.textContent = "正在上传…";
    const response = await runtimeMessage({type: "SUBMIT_PRODUCT", product: extracted.product});
    if (!response.ok) throw new Error(response.error || "采集失败");
    if (response.queued) showResult("控制台暂不可用，商品已加入待传队列。", "warning");
    else if (response.sourcePlatform === "1688") showResult(`1688 商品采集成功，产品编号：${response.productId}`, "");
    else showResult(`采集成功，任务编号：${response.taskId}`, "");
  } catch (error) {
    runtimeMessage({
      type: "REPORT_ATTENTION",
      source: pagePlatform === "1688" ? "1688采购平台" : "美客多商品采集",
      message: error.message || String(error)
    }).catch(() => {});
    showResult(error.message || String(error), "error");
  } finally {
    collectButton.textContent = pagePlatform === "1688" ? "采集到 AI原创产品" : "采集当前商品";
    await refreshState();
  }
});

document.getElementById("retry").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "重试中…";
  try {
    const response = await runtimeMessage({type: "RETRY_QUEUE"});
    if (!response.ok) throw new Error(response.error || "重试失败");
    showResult(`本次上传 ${response.uploaded || 0} 件，剩余 ${response.remaining || 0} 件。`, response.remaining ? "warning" : "");
  } catch (error) {
    showResult(error.message || String(error), "error");
  } finally {
    button.disabled = false;
    button.textContent = "立即重试";
    await refreshState();
  }
});

productModeButton.addEventListener("click", () => showMode("product"));
zyingModeButton.addEventListener("click", async () => {
  showMode("zying");
  if (!activeTab?.id || !isZyingPage(activeTab)) {
    zyingPageStatus.textContent = "正在打开智赢网页版登录页面；登录后重新打开插件并点击读取。";
    try { await runtimeMessage({type: "OPEN_ZYING_LOGIN"}); }
    catch (error) { showResult(error.message || String(error), "error"); }
  }
});
weightPriceModeButton.addEventListener("click", () => showMode("weight-price"));
purchaseModeButton.addEventListener("click", () => showMode("purchase"));
zyingRefreshButton.addEventListener("click", refreshZyingOptions);
zyingOpenLoginButton.addEventListener("click", async () => {
  zyingOpenLoginButton.disabled = true;
  try {
    await runtimeMessage({type: "OPEN_ZYING_LOGIN"});
    zyingPageStatus.textContent = "已打开智赢网页版；完成登录后重新打开插件，再点击读取。";
  } catch (error) {
    showResult(error.message || String(error), "error");
  } finally {
    zyingOpenLoginButton.disabled = false;
  }
});

for (const input of [weightPriceCategory, weightPriceStartPage, weightPriceEndPage, weightPriceStartItem, weightPriceLimit]) {
  input.addEventListener("input", syncWeightPriceControls);
  input.addEventListener("change", syncWeightPriceControls);
}
weightPriceAcknowledged.addEventListener("change", syncWeightPriceControls);

weightPriceOpenLoginButton.addEventListener("click", async () => {
  beginWeightPriceAction();
  try {
    await runWeightPriceAction("OPEN_AI_WEIGHT_PRICE_LOGIN", "正在打开智赢登录页面…");
    showResult("已打开智赢登录页面，请完成登录后回到插件确认。", "");
  } catch (error) {
    showResult(error.message || String(error), "error");
  } finally {
    await finishWeightPriceAction();
  }
});

weightPriceOpenSupplierButton.addEventListener("click", async () => {
  beginWeightPriceAction();
  try {
    await runWeightPriceAction("OPEN_AI_WEIGHT_PRICE_SUPPLIER", "正在打开1688登录页面…");
    showResult("请在任务使用的窗口完成1688登录或人机验证，再勾选继续原任务。", "");
  } catch (error) {
    showResult(error.message || String(error), "error");
  } finally {
    await finishWeightPriceAction();
  }
});

weightPriceConfirmLoginButton.addEventListener("click", async () => {
  beginWeightPriceAction();
  try {
    await runWeightPriceAction("CONFIRM_AI_WEIGHT_PRICE_LOGIN", "正在检查智赢登录状态…");
    showResult("登录已确认，可以刷新分类并启动核重核价。", "");
  } catch (error) {
    showResult(error.message || String(error), "error");
  } finally {
    await finishWeightPriceAction();
  }
});

weightPriceRefreshCategoriesButton.addEventListener("click", async () => {
  beginWeightPriceAction();
  weightPriceRefreshCategoriesButton.textContent = "正在刷新分类…";
  try {
    await runWeightPriceAction("REFRESH_AI_WEIGHT_PRICE_CATEGORIES", "正在从智赢读取分类…");
    showResult("智赢商品分类已刷新。", "");
  } catch (error) {
    showResult(error.message || String(error), "error");
  } finally {
    weightPriceRefreshCategoriesButton.textContent = "刷新智赢分类";
    await finishWeightPriceAction();
  }
});

weightPriceStartButton.addEventListener("click", async () => {
  const reason = weightPriceStartReason();
  if (reason) { showResult(reason, "warning"); return; }
  const resuming = Boolean(weightPriceState?.circuit);
  let params;
  try {
    params = resuming ? {acknowledged: weightPriceAcknowledged.checked} : weightPriceParams();
  } catch (error) {
    showResult(error.message || String(error), "error");
    return;
  }
  beginWeightPriceAction();
  try {
    const response = await runtimeMessage({type: resuming ? "CONTINUE_AI_WEIGHT_PRICE" : "START_AI_WEIGHT_PRICE", params});
    if (!response.ok) throw new Error(response.error || "启动AI核重核价失败");
    renderWeightPriceStatus(response);
    showResult(resuming ? "已从暂停商品继续核重核价，原任务进度保留。" : "AI核重核价已从泽顺插件启动。", "");
  } catch (error) {
    showResult(error.message || String(error), "error");
  } finally {
    await finishWeightPriceAction();
  }
});

weightPriceStopButton.addEventListener("click", async () => {
  beginWeightPriceAction();
  weightPriceStopButton.textContent = "停止中…";
  try {
    const response = await runtimeMessage({type: "STOP_AI_WEIGHT_PRICE"});
    if (!response.ok) throw new Error(response.error || "停止AI核重核价失败");
    renderWeightPriceStatus(response);
    showResult("已发送停止指令，当前操作结束后保留进度。", "warning");
  } catch (error) {
    showResult(error.message || String(error), "error");
  } finally {
    weightPriceStopButton.textContent = "停止";
    await finishWeightPriceAction();
  }
});

zyingStartButton.addEventListener("click", async () => {
  const startRaw = zyingStartPage.value.trim();
  const endRaw = zyingEndPage.value.trim();
  const startPage = Number(startRaw);
  const endPage = Number(endRaw);
  if (!startRaw || !endRaw || !Number.isInteger(startPage) || startPage < 1 ||
      !Number.isInteger(endPage) || endPage < startPage || endPage > 10000) {
    showResult("必须指定有效的起始页和结束页，结束页须大于或等于起始页。", "error");
    return;
  }
  if (!zyingContext) {
    await refreshZyingOptions();
    if (!zyingContext) return;
  }
  zyingStartButton.disabled = true;
  zyingStartButton.textContent = "正在启动…";
  try {
    const response = await runtimeMessage({
      type: "START_ZYING_COLLECTION",
      context: zyingContext,
      params: {
        start_page: startPage,
        end_page: endPage,
        category: zyingCategory.value,
        category_name: zyingCategory.selectedOptions[0]?.textContent || "",
        product_developer_id: zyingDeveloper.value,
        product_developer_name: zyingDeveloper.selectedOptions[0]?.textContent || ""
      }
    });
    if (!response.ok) throw new Error(response.error || "启动智赢产品采集失败");
    zyingRunning = true;
    await loadZyingStatus();
  } catch (error) {
    showResult(error.message || String(error), "error");
  } finally {
    zyingStartButton.textContent = "启动产品采集";
  }
});

zyingStopButton.addEventListener("click", async () => {
  zyingStopButton.disabled = true;
  zyingStopButton.textContent = "结束中…";
  try {
    const response = await runtimeMessage({type: "STOP_ZYING_COLLECTION"});
    if (!response.ok) throw new Error(response.error || "结束智赢产品采集失败");
    await loadZyingStatus();
  } catch (error) {
    showResult(error.message || String(error), "error");
  } finally {
    zyingStopButton.textContent = "结束";
  }
});

for (const field of Object.values(productBatchFields)) {
  field.addEventListener("change", () => {
    saveProductBatchOptions().catch(error => showResult(error.message, "error"));
    if (field === productBatchFields.tab) checkProductZyingStatus();
  });
}
document.getElementById("product-refresh-tabs").addEventListener("click", () => {
  refreshProductTabs().catch(error => showResult(error.message, "error"));
});
document.getElementById("product-search").addEventListener("click", async () => {
  const button = document.getElementById("product-search");
  button.disabled = true;
  try {
    if (!productBatchFields.keyword.value.trim()) throw new Error("请输入搜索关键词");
    await saveProductBatchOptions();
    const response = await runtimeMessage({type: "OPEN_PRODUCT_SEARCH", country: productBatchFields.country.value, keyword: productBatchFields.keyword.value.trim()});
    if (!response.ok) throw new Error(response.error || "打开搜索页面失败");
    await refreshProductTabs(response.tab_id);
    await saveProductBatchOptions();
    showResult("已打开 Internacional 搜索页。页面加载完成后重新打开插件，确认智赢插件已登录再启动。", "");
  } catch (error) { showResult(error.message || String(error), "error"); }
  finally { syncProductBatchControls(); }
});
productBatchFields.keyword.addEventListener("keydown", event => {
  if (event.key === "Enter") document.getElementById("product-search").click();
});
productBatchStart.addEventListener("click", async () => {
  try {
    const selection = productBatchSelection();
    productBatchBusy = true;
    syncProductBatchControls();
    await saveProductBatchOptions();
    const response = await runtimeMessage({type: "START_PRODUCT_BATCH", ...selection});
    if (!response.ok) throw new Error(response.error || "启动采集失败");
    renderProductBatch(response.state);
    await loadProductBatchStatus();
    showResult("批量采集已启动，关闭弹窗后会继续运行。", "");
  } catch (error) { showResult(error.message || String(error), "error"); }
  finally { productBatchBusy = false; syncProductBatchControls(); }
});
productBatchStop.addEventListener("click", async () => {
  productBatchBusy = true;
  syncProductBatchControls();
  try {
    const response = await runtimeMessage({type: "STOP_PRODUCT_BATCH"});
    if (!response.ok) throw new Error(response.error || "停止采集失败");
    renderProductBatch(response.state);
    await loadProductBatchStatus();
  } catch (error) { showResult(error.message || String(error), "error"); }
  finally { productBatchBusy = false; syncProductBatchControls(); }
});

document.getElementById("open-console").addEventListener("click", () => runtimeMessage({type: "OPEN_CONSOLE"}));
document.getElementById("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
initialize();
initializeProductBatch().catch(error => showResult(error.message || String(error), "error"));
