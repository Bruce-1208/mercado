"use strict";

const pageStatus = document.getElementById("page-status");
const authStatus = document.getElementById("auth-status");
const collectButton = document.getElementById("collect");
const queueCount = document.getElementById("queue-count");
const resultBox = document.getElementById("result");
const platformBadge = document.getElementById("platform-badge");
const productPanel = document.getElementById("product-panel");
const zyingPanel = document.getElementById("zying-panel");
const purchasePanel = document.getElementById("purchase-panel");
const productModeButton = document.getElementById("product-mode");
const zyingModeButton = document.getElementById("zying-mode");
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
const purchasePageStatus = document.getElementById("purchase-page-status");
const purchaseTaskMeta = document.getElementById("purchase-task-meta");
const purchaseOpenLoginButton = document.getElementById("purchase-open-login");
const purchaseConfirmLoginButton = document.getElementById("purchase-confirm-login");
const purchaseTaskStatus = document.getElementById("purchase-task-status");
const purchaseSummary = document.getElementById("purchase-summary");
const purchaseResults = document.getElementById("purchase-results");
let activeTab = null;
let detailPage = false;
let authenticated = false;
let pagePlatform = "mercado";
let zyingContext = null;
let zyingRunning = false;
let zyingPollTimer = null;
let purchaseTrackingState = null;
let purchaseTrackingPollTimer = null;

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
  const purchase = mode === "purchase";
  productPanel.hidden = zying || purchase;
  zyingPanel.hidden = !zying;
  purchasePanel.hidden = !purchase;
  productModeButton.classList.toggle("active", !zying && !purchase);
  zyingModeButton.classList.toggle("active", zying);
  purchaseModeButton.classList.toggle("active", purchase);
  if (zying) loadZyingStatus();
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
  collectButton.disabled = !detailPage || !authenticated;
  zyingStartButton.disabled = !authenticated || !zyingContext || zyingRunning;
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
    await refreshZyingOptions();
    return;
  }
  try {
    const response = await tabMessage(activeTab.id, {type: "PING_PAGE"});
    if (response.ok && response.detail) {
      detailPage = true;
      pagePlatform = response.platform === "1688" ? "1688" : "mercado";
      platformBadge.hidden = false;
      platformBadge.textContent = pagePlatform === "1688" ? "1688" : "Mercado";
      platformBadge.className = `platform-badge ${pagePlatform === "1688" ? "source-1688" : "mercado"}`;
      pageStatus.textContent = pagePlatform === "1688"
        ? "已识别 1688 商品详情页，将采集到“AI原创产品”。"
        : "已识别 Mercado Libre 商品详情页，可以直接采集。";
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

function renderZyingOptions(data) {
  const categories = Array.isArray(data.categories) ? data.categories : [];
  const developers = Array.isArray(data.developers) ? data.developers : [];
  const previousCategory = zyingCategory.value;
  const previousDeveloper = zyingDeveloper.value;
  zyingCategory.innerHTML = '<option value="">全部分类</option>' + categories.map(row => {
    const value = String(row.category_id || row.category_name || "");
    const label = String(row.category_name || row.category_leaf_name || value);
    return `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`;
  }).join("");
  zyingDeveloper.innerHTML = '<option value="">全部产品开发</option>' + developers.map(row => (
    `<option value="${escapeHtml(String(row.id || ""))}">${escapeHtml(String(row.name || row.id || ""))}</option>`
  )).join("");
  if ([...zyingCategory.options].some(option => option.value === previousCategory)) zyingCategory.value = previousCategory;
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
    zyingPageStatus.textContent = "请先打开 meli.zying.net 产品列表页并完成登录。";
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
zyingModeButton.addEventListener("click", () => showMode("zying"));
purchaseModeButton.addEventListener("click", () => showMode("purchase"));
zyingRefreshButton.addEventListener("click", refreshZyingOptions);

zyingStartButton.addEventListener("click", async () => {
  const startPage = Number(zyingStartPage.value || 1);
  const endPage = Number(zyingEndPage.value || 1);
  if (!Number.isInteger(startPage) || startPage < 1 || !Number.isInteger(endPage) || endPage < startPage) {
    showResult("页码无效：结束页必须大于或等于起始页。", "error");
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

document.getElementById("open-console").addEventListener("click", () => runtimeMessage({type: "OPEN_CONSOLE"}));
document.getElementById("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
initialize();
