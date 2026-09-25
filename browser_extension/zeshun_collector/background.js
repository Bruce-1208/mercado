"use strict";

importScripts("zying-page.js");
importScripts("1688-page.js");
importScripts("product-batch.js");

const DEFAULT_SETTINGS = {
  consoleUrl: "http://127.0.0.1:5000",
  openConsoleAfterCollect: false
};
const AUTH_KEY = "browserExtensionAuth";
const QUEUE_KEY = "pendingProducts";
const YANDEX_SEARCH_RUN_KEY = "yandexSearchRunId";
const PURCHASE_TRACKING_SESSION_KEY = "purchaseTrackingSession";
const RETRY_ALARM = "zeshun-collector-retry";
const PURCHASE_TRACKING_RESUME_ALARM = "zeshun-purchase-tracking-resume";
const NOTIFICATION_DEDUPE_KEY = "notificationDedupe";
const NOTIFICATION_DEDUPE_MS = 30 * 60 * 1000;
const AI_WEIGHT_PRICE_PAUSE_KEY = "aiWeightPricePauseNotification";
const AI_WEIGHT_PRICE_PAUSE_NOTICE_MS = 10 * 60 * 1000;
const MAX_QUEUE_SIZE = 100;
const FLOATING_WINDOW_KEY = "zeshunFloatingWindowId";
const ZYING_HOST = "meli.zying.net";
const ZYING_LOGIN_URL = `https://${ZYING_HOST}/#/login`;
let queueFlushPromise = null;
let purchaseTrackingRunPromise = null;
let aiWeightPriceRunPromise = null;

async function openFloatingWindow() {
  const stored = await storageGet("local", [FLOATING_WINDOW_KEY]);
  const savedWindowId = Number(stored[FLOATING_WINDOW_KEY] || 0);
  if (savedWindowId) {
    try {
      const existing = await chrome.windows.get(savedWindowId, {populate: true});
      const isOurWindow = existing.type === "popup" && existing.tabs?.some(
        tab => String(tab.url || "").startsWith(chrome.runtime.getURL("popup.html"))
      );
      if (!isOurWindow) throw new Error("stale floating window id");
      await chrome.windows.update(existing.id, {focused: true});
      return existing;
    } catch (_) {
      await storageRemove("local", [FLOATING_WINDOW_KEY]);
    }
  }
  const created = await chrome.windows.create({
    url: chrome.runtime.getURL("popup.html"),
    type: "popup",
    width: 440,
    height: 720,
    focused: true
  });
  if (created?.id) {
    await storageSet("local", {[FLOATING_WINDOW_KEY]: created.id});
  }
  return created;
}

const PURCHASE_TRACKING_PLATFORMS = {
  "1688": {
    label: "1688",
    loginUrl: "https://login.1688.com/member/signin.htm",
    ordersUrl: "https://trade.1688.com/order/buyer_order_list.htm"
  },
  taobao: {
    label: "淘宝 / 天猫",
    loginUrl: "https://login.taobao.com/member/login.jhtml",
    ordersUrl: "https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm"
  },
  pdd: {
    label: "拼多多",
    loginUrl: "https://mobile.yangkeduo.com/login.html",
    ordersUrl: "https://mobile.yangkeduo.com/orders.html"
  },
  xianyu: {
    label: "闲鱼",
    loginUrl: "https://login.taobao.com/member/login.jhtml?redirectURL=https%3A%2F%2Fwww.goofish.com%2F",
    ordersUrl: "https://www.goofish.com/personal"
  }
};

class ApiError extends Error {
  constructor(message, {authRequired = false, status = 0} = {}) {
    super(message);
    this.name = "ApiError";
    this.authRequired = authRequired;
    this.status = status;
  }
}

function storageGet(area, keys) {
  return new Promise(resolve => chrome.storage[area].get(keys, resolve));
}

function storageSet(area, values) {
  return new Promise(resolve => chrome.storage[area].set(values, resolve));
}

function storageRemove(area, keys) {
  return new Promise(resolve => chrome.storage[area].remove(keys, resolve));
}

function normalizeConsoleUrl(value) {
  const raw = String(value || DEFAULT_SETTINGS.consoleUrl).trim().replace(/\/+$/, "");
  const parsed = new URL(raw);
  if (!/^https?:$/.test(parsed.protocol)) throw new Error("控制台地址必须以 http:// 或 https:// 开头");
  return raw;
}

async function settings() {
  const synced = await storageGet("sync", ["consoleUrl", "openConsoleAfterCollect"]);
  return {...DEFAULT_SETTINGS, ...synced};
}

async function authSession() {
  // Keep the short-lived token across an extension/service-worker reload. The
  // password is never stored; using local storage here only prevents a code
  // update from silently logging the operator out in the middle of a task.
  const stored = await storageGet("local", [AUTH_KEY]);
  let auth = stored[AUTH_KEY];
  if (!auth) {
    const legacy = await storageGet("session", [AUTH_KEY]);
    auth = legacy[AUTH_KEY];
    if (auth) await storageSet("local", {[AUTH_KEY]: auth});
  }
  if (!auth || !auth.token) return null;
  if (auth.expiresAt && Date.now() >= Number(auth.expiresAt)) {
    await clearAuth();
    return null;
  }
  return auth;
}

async function clearAuth() {
  await storageRemove("local", [AUTH_KEY]);
  await storageRemove("session", [AUTH_KEY]);
}

async function purchaseTrackingSession() {
  const stored = await storageGet("local", [PURCHASE_TRACKING_SESSION_KEY]);
  return stored[PURCHASE_TRACKING_SESSION_KEY] || null;
}

async function clearPurchaseTrackingSession() {
  await storageRemove("local", [PURCHASE_TRACKING_SESSION_KEY]);
}

async function purchaseTrackingRequest() {
  if (!await authSession()) return null;
  return apiRequest("/api/browser-extension/purchase-tracking/request", {method: "GET"});
}

async function openPurchaseTrackingLogin(request) {
  const platform = PURCHASE_TRACKING_PLATFORMS[String(request?.platform || "")];
  if (!platform) throw new Error("同步任务的采购平台无效");
  const prepared = await apiRequest("/api/browser-extension/purchase-tracking/prepare", {
    method: "POST",
    body: JSON.stringify({request_id: request.request_id})
  });
  const current = await purchaseTrackingSession();
  let tab = null;
  if (current && String(current.requestId) === String(request.request_id) && current.tabId) {
    try { tab = await chrome.tabs.get(Number(current.tabId)); } catch (_) {}
  }
  if (!tab) {
    tab = await chrome.tabs.create({url: platform.ordersUrl, active: true});
  } else {
    tab = await chrome.tabs.update(tab.id, {url: platform.ordersUrl, active: true});
  }
  await storageSet("local", {
    [PURCHASE_TRACKING_SESSION_KEY]: {
      requestId: String(request.request_id),
      tabId: Number(tab.id),
      platform: String(request.platform),
    }
  });
  return {
    ok: true,
    state: prepared,
    tabId: tab.id,
    platform: platform.label,
    message: "已打开采购平台，请手动登录后回到泽顺插件点击确定登录"
  };
}

async function checkPurchaseTrackingLogin(tabId, platform) {
  let response;
  try {
    response = await sendTabMessage(Number(tabId), {type: "CHECK_PURCHASE_LOGIN"});
  } catch (_) {
    throw new Error("采购平台页面尚未准备好，请等待页面加载完成后重试");
  }
  if (!response.ok || response.platform !== platform || !response.loggedIn) {
    throw new Error("尚未检测到采购平台登录状态，请先在打开的页面完成手动登录");
  }
  return response;
}

async function runPurchaseTracking(request, tabId) {
  const requestId = String(request.request_id || "");
  const completedOrderIds = new Set(
    (Array.isArray(request.results) ? request.results : [])
      .map(item => String(item.order_id || ""))
      .filter(Boolean)
  );
  try {
    for (const row of Array.isArray(request.orders) ? request.orders : []) {
      if (completedOrderIds.has(String(row.order_id || ""))) continue;
      const result = await sendTabMessage(Number(tabId), {
        type: "FETCH_PURCHASE_TRACKING",
        purchaseOrder: row.purchase_order
      });
      if (!result?.ok) throw new Error(result?.error || "采购平台未返回物流信息");
      if (result.status === "not_logged_in") {
        throw new Error("采购平台登录已失效，请重新手动登录后再同步");
      }
      await apiRequest("/api/browser-extension/purchase-tracking/result", {
        method: "POST",
        body: JSON.stringify({
          request_id: requestId,
          order_id: row.order_id,
          status: result.status,
          tracking_number: result.tracking_number || "",
          logistics_company: result.logistics_company || "",
          message: result.message || ""
        })
      });
    }
  } catch (error) {
    void notifyAttention(error.message || String(error), {source: "采购平台物流同步"}).catch(() => {});
    try {
      await apiRequest("/api/browser-extension/purchase-tracking/fail", {
        method: "POST",
        body: JSON.stringify({request_id: requestId, message: error.message || String(error)})
      });
    } catch (_) {}
  } finally {
    await clearPurchaseTrackingSession();
  }
}

function startPurchaseTrackingRun(request, tabId) {
  purchaseTrackingRunPromise = runPurchaseTracking(request, tabId).finally(() => {
    purchaseTrackingRunPromise = null;
  });
  return purchaseTrackingRunPromise;
}

async function resumePurchaseTracking() {
  if (purchaseTrackingRunPromise) return;
  const request = await purchaseTrackingRequest();
  if (!request || !request.request_id || !request.running || request.phase !== "syncing") return;
  const session = await purchaseTrackingSession();
  if (!session || String(session.requestId) !== String(request.request_id) || !session.tabId) return;
  try {
    await chrome.tabs.get(Number(session.tabId));
    await checkPurchaseTrackingLogin(session.tabId, request.platform);
    startPurchaseTrackingRun(request, session.tabId);
  } catch (error) {
    void notifyAttention(error.message || String(error), {source: "采购平台物流同步"}).catch(() => {});
    try {
      await apiRequest("/api/browser-extension/purchase-tracking/fail", {
        method: "POST",
        body: JSON.stringify({request_id: request.request_id, message: error.message || String(error)})
      });
    } catch (_) {}
    await clearPurchaseTrackingSession();
  }
}

async function confirmPurchaseTrackingLogin() {
  if (purchaseTrackingRunPromise) {
    return {ok: false, error: "物流号同步正在进行中"};
  }
  const request = await purchaseTrackingRequest();
  if (!request || !request.request_id) throw new Error("当前没有待处理的订单物流同步任务");
  const session = await purchaseTrackingSession();
  if (!session || String(session.requestId) !== String(request.request_id) || !session.tabId) {
    throw new Error("请先点击打开采购平台并完成手动登录");
  }
  await checkPurchaseTrackingLogin(session.tabId, request.platform);
  const claimed = await apiRequest("/api/browser-extension/purchase-tracking/claim", {
    method: "POST",
    body: JSON.stringify({request_id: request.request_id})
  });
  startPurchaseTrackingRun(claimed, session.tabId);
  return {ok: true, state: claimed, message: "已确认登录，正在自动同步物流号"};
}

async function queueItems() {
  const stored = await storageGet("local", [QUEUE_KEY]);
  return Array.isArray(stored[QUEUE_KEY]) ? stored[QUEUE_KEY] : [];
}

async function setBadge(count) {
  await chrome.action.setBadgeBackgroundColor({color: "#b3261e"});
  await chrome.action.setBadgeText({text: count ? String(Math.min(count, 99)) : ""});
}

async function saveQueue(queue) {
  await storageSet("local", {[QUEUE_KEY]: queue.slice(-MAX_QUEUE_SIZE)});
  await setBadge(queue.length);
}

function productKey(product) {
  return String(product && (product.source_item_id || product.final_url || product.source_url) || "");
}

async function enqueue(product, reason) {
  const queue = await queueItems();
  const key = productKey(product);
  const entry = {
    product,
    reason: String(reason || "控制台暂不可用").slice(0, 500),
    queuedAt: new Date().toISOString(),
    attempts: 0
  };
  const existing = queue.findIndex(item => productKey(item.product) === key);
  if (existing >= 0) queue[existing] = {...queue[existing], ...entry};
  else queue.push(entry);
  await saveQueue(queue);
  return queue.length;
}

async function apiRequest(path, options = {}, {requireAuth = true} = {}) {
  const config = await settings();
  const base = normalizeConsoleUrl(config.consoleUrl);
  const headers = {"Accept": "application/json", ...(options.headers || {})};
  if (options.body) headers["Content-Type"] = "application/json";
  if (requireAuth) {
    const auth = await authSession();
    if (!auth) throw new ApiError("请先登录泽顺控制台账号", {authRequired: true, status: 401});
    if (auth.mode !== "legacy") headers.Authorization = `Bearer ${auth.token}`;
  }
  let response;
  try {
    response = await fetch(`${base}${path}`, {
      ...options,
      headers,
      credentials: "include",
      cache: "no-store"
    });
  } catch (error) {
    throw new ApiError(`无法连接泽顺控制台 ${base}：${error.message || error}`);
  }
  let payload = {};
  try { payload = await response.json(); } catch (_) {}
  if (!response.ok || payload.status === "error") {
    const authRequired = response.status === 401 && requireAuth;
    if (authRequired) await clearAuth();
    throw new ApiError(payload.message || `泽顺控制台返回 HTTP ${response.status}`, {
      authRequired,
      status: response.status
    });
  }
  return payload.data === undefined ? payload : payload.data;
}

async function startYandexSearch(keyword, count) {
  const normalizedKeyword = String(keyword || "").trim();
  const requestedCount = Number(count);
  if (!normalizedKeyword || normalizedKeyword.length > 200) throw new Error("关键词须为 1–200 个字符");
  if (!Number.isInteger(requestedCount) || requestedCount < 1 || requestedCount > 500) {
    throw new Error("采集数量须为 1–500 的整数");
  }
  const result = await apiRequest("/api/browser-extension/yandex/search", {
    method: "POST", body: JSON.stringify({keyword: normalizedKeyword, count: requestedCount})
  });
  const runId = Number(result.run_id);
  if (!Number.isSafeInteger(runId) || runId <= 0) throw new Error("Yandex 控制台没有返回采集任务编号");
  await storageSet("local", {[YANDEX_SEARCH_RUN_KEY]: runId});
  return {ok: true, run_id: runId, status: result.status || "queued"};
}

async function getYandexSearchStatus() {
  const stored = await storageGet("local", [YANDEX_SEARCH_RUN_KEY]);
  const runId = Number(stored[YANDEX_SEARCH_RUN_KEY] || 0);
  if (!Number.isSafeInteger(runId) || runId <= 0) return {ok: true, run: null};
  try {
    const result = await apiRequest(`/api/browser-extension/yandex/search/${runId}`, {method: "GET"});
    return {ok: true, run: result.run || null};
  } catch (error) {
    if (error.status !== 404) throw error;
    await storageRemove("local", [YANDEX_SEARCH_RUN_KEY]);
    return {ok: true, run: null, message: "历史采集任务已不存在，可以重新启动"};
  }
}

async function openYandexSearchResults() {
  const stored = await storageGet("local", [YANDEX_SEARCH_RUN_KEY]);
  const runId = Number(stored[YANDEX_SEARCH_RUN_KEY] || 0);
  if (!Number.isSafeInteger(runId) || runId <= 0) throw new Error("还没有可查看的 Yandex 采集任务");
  const config = await settings();
  const url = new URL(`${normalizeConsoleUrl(config.consoleUrl)}/yandex-console/`);
  url.searchParams.set("run_id", String(runId));
  await chrome.tabs.create({url: url.href, active: true});
  return {ok: true, run_id: runId};
}

function attentionEventType(message, source = "") {
  const text = `${source} ${String(message || "")}`;
  if (/限频|访问频繁|操作频繁|请求频繁|rate.?limit|too many requests|HTTP\s*429/i.test(text)) {
    return "rate_limit";
  }
  if (/滑块|验证码|人机验证|captcha|challenge|安全验证|验证地址/i.test(text)) {
    return "slider_verification";
  }
  if (/采购平台|1688|淘宝|天猫|拼多多|闲鱼/i.test(source) && /登录|login|not.?logged.?in/i.test(text)) {
    return "purchase_login";
  }
  if (/买家|Mercado|美客多|商品采集/i.test(source) && !/智赢插件/i.test(text) && /登录|login|account.?verification|登录失效/i.test(text)) {
    return "buyer_login";
  }
  if (/AI核重核价/i.test(source) && /登录|login/i.test(text)) return "task_blocked";
  if (/等待人工|已暂停|blocked|登录或人机|完成验证/i.test(text)) {
    return "task_blocked";
  }
  return "";
}

async function notifyAttention(message, context = {}) {
  const source = String(context.source || "泽顺插件").slice(0, 120);
  const eventType = context.eventType || attentionEventType(message, source);
  if (!eventType || !await authSession()) return {sent: false, ignored: true};
  const summary = String(message || "需要人工处理").replace(/\s+/g, " ").trim().slice(0, 1000);
  const key = `${eventType}|${source}|${summary}`;
  const stored = await storageGet("local", [NOTIFICATION_DEDUPE_KEY]);
  const dedupe = stored[NOTIFICATION_DEDUPE_KEY] || {};
  const now = Date.now();
  for (const [savedKey, sentAt] of Object.entries(dedupe)) {
    if (now - Number(sentAt || 0) >= NOTIFICATION_DEDUPE_MS) delete dedupe[savedKey];
  }
  if (dedupe[key] && now - Number(dedupe[key]) < NOTIFICATION_DEDUPE_MS) {
    return {sent: false, deduplicated: true};
  }
  const result = await apiRequest("/api/browser-extension/notifications/send", {
    method: "POST",
    body: JSON.stringify({event_type: eventType, source, message: summary})
  });
  if (result?.sent) {
    dedupe[key] = now;
    await storageSet("local", {[NOTIFICATION_DEDUPE_KEY]: dedupe});
  }
  return result || {sent: false};
}

async function login(username, password) {
  let auth;
  try {
    const data = await apiRequest("/api/browser-extension/login", {
      method: "POST",
      body: JSON.stringify({username, password})
    }, {requireAuth: false});
    if (!data.token || !data.user) throw new Error("控制台登录成功，但未返回插件会话");
    auth = {
      mode: "token",
      token: data.token,
      user: data.user,
      expiresAt: Date.now() + Number(data.expires_in || 0) * 1000
    };
  } catch (error) {
    if (error.status !== 404) throw error;
    // Compatibility for a workbench process that has not yet been restarted
    // after installing the extension backend routes.  Authentication still
    // goes through the workbench's existing account login endpoint.
    const user = await apiRequest("/api/login", {
      method: "POST",
      body: JSON.stringify({username, password, remember: true})
    }, {requireAuth: false});
    if (!user || !user.username) throw new Error("泽顺控制台登录成功，但未返回账号信息");
    auth = {
      mode: "legacy",
      token: "legacy-workbench-session",
      user,
      expiresAt: Date.now() + 6 * 60 * 60 * 1000
    };
  }
  await storageSet("local", {[AUTH_KEY]: auth});
  flushQueue();
  return {ok: true, user: auth.user, expiresAt: auth.expiresAt};
}

async function logout() {
  await clearAuth();
  return {ok: true};
}

async function finishLegacyTask(taskId, product, errorMessage) {
  const ok = !errorMessage;
  const complete = product.scrape_status === "ok";
  return apiRequest(`/api/db/mercado-collection/tasks/${taskId}`, {
    method: "PATCH",
    body: JSON.stringify({
      status: ok ? (complete ? "completed" : "partial") : "error",
      message: errorMessage || (complete ? "浏览器插件采集完成" : "浏览器插件快速采集完成，详情待补充"),
      collected_count: ok ? 1 : 0,
      completed_count: ok && complete ? 1 : 0,
      failed_count: ok && complete ? 0 : 1,
      current_page: 1,
      started: true,
      finished: true
    })
  });
}

async function uploadLegacyProduct(product) {
  const task = await apiRequest("/api/db/mercado-collection/tasks", {
    method: "POST",
    body: JSON.stringify({
      source_url: product.source_url || product.final_url,
      requested_count: 1,
      created_by: "泽顺商品采集助手（兼容模式）"
    })
  });
  const taskId = Number(task.task_id);
  if (!taskId) throw new Error("旧版控制台未返回采集任务编号");
  try {
    await apiRequest("/api/db/mercado-collection/items", {
      method: "POST",
      body: JSON.stringify({task_id: taskId, rows: [product]})
    });
  } catch (error) {
    try { await finishLegacyTask(taskId, product, error.message || String(error)); } catch (_) {}
    throw error;
  }
  try { await finishLegacyTask(taskId, product, ""); } catch (_) {}
  return {task_id: taskId, scrape_status: product.scrape_status};
}

async function uploadProduct(product, {openConsole = true} = {}) {
  if (!product || !product.source_item_id || !product.title) {
    throw new Error("商品数据不完整，缺少商品编号或标题");
  }
  const auth = await authSession();
  const is1688 = product.source_platform === "1688" || String(product.source_url || "").includes("1688.com");
  const result = auth && auth.mode === "legacy" && !is1688
    ? await uploadLegacyProduct(product)
    : await apiRequest("/api/browser-extension/collect", {
        method: "POST",
        body: JSON.stringify({product})
      });
  const config = await settings();
  if (openConsole && config.openConsoleAfterCollect) {
    const destination = is1688 ? "/?tab=ai-original-products" : "";
    await chrome.tabs.create({url: `${normalizeConsoleUrl(config.consoleUrl)}${destination}`, active: true});
  }
  return {
    taskId: Number(result.task_id || 0),
    productId: Number(result.product_id || 0),
    sourcePlatform: result.source_platform || (is1688 ? "1688" : "mercado"),
    scrapeStatus: result.scrape_status || product.scrape_status
  };
}

async function submitProduct(product) {
  if (!await authSession()) {
    return {ok: false, authRequired: true, error: "请先在插件设置中登录泽顺控制台账号"};
  }
  try {
    const result = await uploadProduct(product);
    return {ok: true, queued: false, ...result};
  } catch (error) {
    if (error.authRequired) {
      return {ok: false, authRequired: true, error: error.message || "插件登录已失效"};
    }
    const queueLength = await enqueue(product, error.message || String(error));
    return {ok: true, queued: true, queueLength, error: error.message || String(error)};
  }
}

async function flushQueueOnce() {
  const queue = await queueItems();
  if (!queue.length) return {ok: true, uploaded: 0, remaining: 0};
  if (!await authSession()) {
    return {ok: false, authRequired: true, uploaded: 0, remaining: queue.length};
  }
  const failed = new Map();
  const succeeded = new Set();
  let uploaded = 0;
  let lastError = "";
  for (const entry of queue) {
    const key = productKey(entry.product);
    try {
      await uploadProduct(entry.product, {openConsole: false});
      uploaded += 1;
      succeeded.add(key);
    } catch (error) {
      lastError = error.message || String(error);
      if (error.authRequired) break;
      failed.set(key, {...entry, reason: lastError, attempts: Number(entry.attempts || 0) + 1});
    }
  }
  const latest = await queueItems();
  const remaining = latest.flatMap(entry => {
    const key = productKey(entry.product);
    if (succeeded.has(key)) return [];
    return [failed.get(key) || entry];
  });
  await saveQueue(remaining);
  return {
    ok: !lastError,
    authRequired: !await authSession(),
    uploaded,
    remaining: remaining.length,
    lastError
  };
}

async function flushQueue() {
  if (queueFlushPromise) return queueFlushPromise;
  queueFlushPromise = flushQueueOnce().finally(() => {
    queueFlushPromise = null;
  });
  return queueFlushPromise;
}

function sendTabMessage(tabId, message, frameId) {
  return new Promise((resolve, reject) => {
    const done = response => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve(response || {});
    };
    if (Number.isInteger(frameId)) chrome.tabs.sendMessage(tabId, message, {frameId}, done);
    else chrome.tabs.sendMessage(tabId, message, done);
  });
}

async function extractFromTab(tabId) {
  const response = await sendTabMessage(tabId, {type: "EXTRACT_PRODUCT"});
  if (!response.ok) throw new Error(response.error || "未读取到商品详情");
  return response.product;
}

async function collect1688ListProduct(candidate) {
  const sourceUrl = String(candidate?.source_url || candidate?.final_url || "").trim();
  if (!sourceUrl) throw new Error("1688 列表商品缺少详情链接");
  const tab = await chrome.tabs.create({url: sourceUrl, active: false});
  let extracted = false;
  try {
    await aiWeightPriceWaitTab(tab.id);
    const product = await extractFromTab(tab.id);
    if (String(product.source_item_id) !== String(candidate.source_item_id)) {
      throw new Error("1688详情跳转到了其他商品，已停止采集");
    }
    extracted = true;
    return await submitProduct(product);
  } catch (error) {
    // Keep failed detail tabs available for login, slider or captcha handling.
    try { await chrome.tabs.update(tab.id, {active: true}); } catch (_) {}
    throw error;
  } finally {
    if (extracted) {
      try { await chrome.tabs.remove(tab.id); } catch (_) {}
    }
  }
}

function normalizeZyingToken(value) {
  if (!value) return "";
  let token = value;
  try { token = JSON.parse(value); } catch (_) {}
  if (token && typeof token === "object") {
    token = token.token || token.access_token || token.accessToken || "";
  }
  return String(token || "").trim();
}

async function readZyingContext(tabId, {includeDevelopers = true, developerTimeoutMs = 5000} = {}) {
  let page = {};
  try {
    const contextResult = await chrome.scripting.executeScript({
      target: {tabId},
      world: "MAIN",
      func: zeshunReadZyingPageContext
    });
    page = contextResult?.[0]?.result || {};
  } catch (_) {
    try { page = await sendTabMessage(tabId, {type: "READ_ZYING_CONTEXT"}); } catch (_) {}
  }
  if (includeDevelopers) {
    let timeoutId;
    try {
      const developerScript = chrome.scripting.executeScript({
        target: {tabId},
        world: "MAIN",
        func: zeshunReadZyingProductDevelopers
      });
      const timeout = new Promise((_, reject) => {
        timeoutId = setTimeout(
          () => reject(new Error("读取智赢产品开发人员超时")), developerTimeoutMs
        );
      });
      const developerResult = await Promise.race([developerScript, timeout]);
      page.developers = developerResult?.[0]?.result || [];
    } catch (_) {
    } finally {
      clearTimeout(timeoutId);
    }
  }
  let credential = String(page.credential || "").trim();
  if (!credential) {
    const cookie = await chrome.cookies.get({url: "https://meli.zying.net/", name: "token"});
    const token = normalizeZyingToken(cookie && cookie.value);
    if (token) credential = `cookie://${token}`;
  }
  if (!credential) {
    throw new Error("当前智赢页面尚未登录，请登录后再刷新采集选项");
  }
  return {
    credential,
    categories: Array.isArray(page.categories) ? page.categories : [],
    developers: Array.isArray(page.developers) ? page.developers : [],
    url: page.url || "https://meli.zying.net/#/product"
  };
}

async function openZyingLoginPage() {
  // Query all tabs instead of relying on a URL match pattern: tabs.query URL
  // filters are not reliable for SPA hash routes in every Chromium build.
  const tabs = await chrome.tabs.query({});
  const existing = tabs.find(tab => {
    try { return new URL(tab.url || "").hostname === ZYING_HOST && tab.id; }
    catch (_) { return false; }
  });
  if (existing) {
    // An existing tab may be parked at `/` or an old protected route. Merely
    // activating it makes the login button appear to do nothing, so always
    // put the tab on the explicit login route first.
    await chrome.tabs.update(existing.id, {url: ZYING_LOGIN_URL, active: true});
    if (existing.windowId) await chrome.windows?.update?.(existing.windowId, {focused: true});
    return {tab_id: existing.id, existing: true};
  }
  const tab = await chrome.tabs.create({url: ZYING_LOGIN_URL, active: true});
  return {tab_id: tab.id, existing: false};
}

async function loadZyingOptions(context) {
  const options = await apiRequest("/api/browser-extension/zying/options", {
    method: "POST",
    body: JSON.stringify({
      credential: context.credential,
      categories: context.categories || [],
      developers: context.developers || [],
      refresh: true
    })
  });
  // Older servers may still return cached people after a live page refresh.
  return {...options, developers: context.developers?.length ? context.developers : options.developers || []};
}

async function startZyingCollection(context, params) {
  return apiRequest("/api/browser-extension/zying/start", {
    method: "POST",
    body: JSON.stringify({
      ...params,
      credential: context.credential,
      categories: context.categories || []
    })
  });
}

async function zyingCollectionStatus() {
  const data = await apiRequest("/api/browser-extension/zying/status", {method: "GET"});
  if (["error", "blocked"].includes(String(data?.status || "").toLowerCase())) {
    void notifyAttention(data.message || data.last_error, {source: "智赢产品采集"}).catch(() => {});
  }
  return data;
}

async function stopZyingCollection() {
  return apiRequest("/api/browser-extension/zying/stop", {
    method: "POST",
    body: "{}"
  });
}

async function aiWeightPriceStatus() {
  const data = await apiRequest("/api/browser-extension/ai-weight-price/client/status", {method: "GET"});
  void handleAiWeightPricePauseNotification(data).catch(() => {});
  return data;
}

async function weightDimensionsRecordsStatus() {
  return apiRequest("/api/browser-extension/weight-dimensions-records/status", {method: "GET"});
}

async function startWeightDimensionsRecordsUpdate() {
  return apiRequest("/api/browser-extension/weight-dimensions-records/execute", {
    method: "POST", body: "{}"
  });
}

async function handleAiWeightPricePauseNotification(data = {}) {
  const circuit = data?.circuit;
  const reason = circuit?.reason || (data?.run?.outcome === "blocked" ? data?.run?.message : "");
  if (!circuit || !reason) {
    await storageRemove("local", [AI_WEIGHT_PRICE_PAUSE_KEY]);
    return {sent: false, active: false};
  }
  const pausedAt = Number(circuit.at || 0) * 1000 || Date.now();
  const signature = [data?.run?.run_id || "", circuit.kind || "", circuit.at || "", reason].join("|");
  const stored = await storageGet("local", [AI_WEIGHT_PRICE_PAUSE_KEY]);
  let pause = stored[AI_WEIGHT_PRICE_PAUSE_KEY] || {};
  if (pause.signature !== signature) {
    pause = {signature, pausedAt, attempted: false, sent: false};
    await storageSet("local", {[AI_WEIGHT_PRICE_PAUSE_KEY]: pause});
  }
  if (pause.attempted || Date.now() - Number(pause.pausedAt || pausedAt) < AI_WEIGHT_PRICE_PAUSE_NOTICE_MS) {
    return {sent: false, pending: !pause.attempted};
  }
  // Persist the one-shot guard before sending. Even if SMTP succeeds but the
  // local response is lost, the same overnight pause will never send twice.
  pause = {...pause, attempted: true, attemptedAt: Date.now()};
  await storageSet("local", {[AI_WEIGHT_PRICE_PAUSE_KEY]: pause});
  const result = await notifyAttention(reason, {source: "AI核重核价"});
  pause = {
    ...pause,
    sent: Boolean(result?.channels?.email?.sent || result?.sent),
  };
  await storageSet("local", {[AI_WEIGHT_PRICE_PAUSE_KEY]: pause});
  return result;
}

async function startZyingInfringement(context, params) {
  return apiRequest("/api/browser-extension/zying-infringement/start", {
    method: "POST",
    body: JSON.stringify({
      ...params,
      credential: context.credential
    })
  });
}

async function zyingInfringementStatus() {
  const data = await apiRequest("/api/browser-extension/zying-infringement/status", {method: "GET"});
  if (["error", "blocked"].includes(String(data?.status || "").toLowerCase())) {
    void notifyAttention(data.message || data.last_error, {source: "智赢产品查侵权"}).catch(() => {});
  }
  return data;
}

async function stopZyingInfringement() {
  return apiRequest("/api/browser-extension/zying-infringement/stop", {
    method: "POST",
    body: "{}"
  });
}

async function monitorAttentionEvents() {
  if (!await authSession()) return;
  const batch = await getProductBatchStatus();
  if (batch?.phase === "error" && batch.message) {
    void notifyAttention(batch.message, {source: "美客多商品采集"}).catch(() => {});
  }
  try { await aiWeightPriceStatus(); } catch (_) {}
}

async function aiWeightPriceAction(action, body = {}) {
  return apiRequest(`/api/browser-extension/ai-weight-price/client/${action}`, {
    method: "POST",
    body: JSON.stringify(body)
  });
}

async function aiWeightPriceTab(host, url, {active = false} = {}) {
  const tabs = await chrome.tabs.query({});
  const existing = tabs.find(tab => {
    try { return tab.id && new URL(tab.url || "").hostname.endsWith(host); }
    catch (_) { return false; }
  });
  if (existing) {
    if (url && existing.url !== url) await chrome.tabs.update(existing.id, {url, active});
    else if (active) await chrome.tabs.update(existing.id, {active: true});
    return existing.id;
  }
  const created = await chrome.tabs.create({url, active});
  return created.id;
}

async function aiWeightPriceWaitTab(tabId, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    let done = false;
    let pollTimer;
    const finish = async (error, tab) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      clearTimeout(pollTimer);
      chrome.tabs.onUpdated.removeListener(listener);
      if (error) reject(error);
      else {
        await new Promise(ready => setTimeout(ready, 250));
        resolve(tab);
      }
    };
    const listener = (updatedId, info, tab) => {
      if (updatedId === tabId && info.status === "complete") finish(null, tab);
    };
    const poll = async () => {
      if (done) return;
      try {
        const current = await chrome.tabs.get(tabId);
        if (current?.status === "complete") return finish(null, current);
      } catch (error) {
        return finish(new Error(`浏览器标签页不可用：${error.message || error}`));
      }
      pollTimer = setTimeout(poll, 150);
    };
    const timer = setTimeout(() => finish(new Error("浏览器页面加载超时")), timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
    void poll();
  });
}

async function aiWeightPriceSendTabMessage(tabId, message, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      return await sendTabMessage(tabId, message);
    } catch (error) {
      lastError = error;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
  }
  throw lastError || new Error("浏览器页面脚本加载超时");
}

function aiWeightPriceMissingReceiver(error) {
  return /Receiving end does not exist|Could not establish connection/i.test(
    String(error?.message || error || "")
  );
}

async function aiWeightPriceSendContentMessage(tabId, message, files, timeoutMs = 10000) {
  try {
    return await sendTabMessage(tabId, message);
  } catch (error) {
    if (!aiWeightPriceMissingReceiver(error)) throw error;
    await chrome.scripting.executeScript({target: {tabId}, files});
    return aiWeightPriceSendTabMessage(tabId, message, timeoutMs);
  }
}

const AI_WEIGHT_PRICE_CONTENT_VERSION = "1.8.22";

async function aiWeightPriceFrameIds(tabId) {
  const ids = [0];
  try {
    const frames = await chrome.webNavigation?.getAllFrames?.({tabId});
    for (const frame of Array.isArray(frames) ? frames : []) {
      const id = Number(frame?.frameId);
      if (Number.isInteger(id) && id >= 0 && !ids.includes(id)) ids.push(id);
    }
  } catch (_) {
    // Older Chromium builds or restricted tabs may not expose webNavigation;
    // the top frame remains a valid fallback.
  }
  return ids;
}

async function aiWeightPriceEnsureCurrentContent(tabId, {reloadIfStale = true} = {}) {
  let marker = null;
  try {
    const result = await chrome.scripting.executeScript({
      target: {tabId},
      func: () => globalThis.__zeshun1688ContentVersion || ""
    });
    marker = result?.[0]?.result || "";
  } catch (_) {}
  if (marker === AI_WEIGHT_PRICE_CONTENT_VERSION) return true;
  // No listener in a new document is an injection case, not a reason to
  // restart the page (and its lazy-loaded upload widget) once more.
  if (!marker) return false;
  // A newly opened 1688 image-result tab may be in the middle of a SPA
  // navigation. Reloading it during the result poll loses the search state
  // and prevents the page from ever exposing its candidates. The caller can
  // instead inject the current listener into this tab and keep polling.
  if (!reloadIfStale) return false;
  // Reloading is deliberate: injecting the new file beside an old listener
  // would make both isolated-world listeners upload/click the same image.
  if (typeof chrome.tabs.reload !== "function") return false;
  await chrome.tabs.reload(tabId, {bypassCache: true});
  await aiWeightPriceWaitTab(tabId, 30000);
  return false;
}

async function aiWeightPriceSendFrameMessage(tabId, frameId, message, files, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      return await sendTabMessage(tabId, message, frameId);
    } catch (error) {
      // Only an absent receiver proves the action never started. A channel
      // closed by navigation can mean search succeeded; never upload twice.
      if (!aiWeightPriceMissingReceiver(error)) throw error;
      lastError = error;
    }
    try {
      await chrome.scripting.executeScript({target: {tabId, frameIds: [frameId]}, files});
    } catch (error) {
      if (!/frame.*(?:removed|not found)|No frame|Cannot access contents|document.*(?:unloaded|loading)/i.test(String(error?.message || error))) throw error;
      lastError = error;
      // A denied child frame must not prevent using the permitted top frame.
      if (frameId !== 0) throw error;
    }
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error(`1688页面连接未就绪（等待${Math.round(timeoutMs / 1000)}秒）：${lastError?.message || "内容脚本未响应"}`);
}

async function aiWeightPriceSendImageMessage(tabId, message, files, timeoutMs = 10000) {
  const isResultPoll = message?.type === "AI_WEIGHT_PRICE_SEARCH_RESULTS";
  await aiWeightPriceEnsureCurrentContent(tabId, {
    reloadIfStale: !isResultPoll
  });
  let lastError = null;
  const frameIds = await aiWeightPriceFrameIds(tabId);
  let fallback = null;
  for (const frameId of frameIds) {
    try {
      const response = await aiWeightPriceSendFrameMessage(tabId, frameId, message, files, timeoutMs);
      if (response?.ok === false || (isResultPoll && !response?.ready && !response?.empty)) {
        fallback = fallback || response;
        continue;
      }
      return response;
    } catch (error) {
      if (aiWeightPriceMessageLostDuringNavigation(error)) throw error;
      lastError = error;
    }
  }
  if (fallback) return fallback;
  throw lastError || new Error("1688页面脚本尚未准备好");
}

function aiWeightPriceMessageLostDuringNavigation(error) {
  return /message (?:port|channel) closed|frame was removed|context invalidated|tab was closed|asynchronous response.*channel closed/i.test(
    String(error?.message || error || "")
  );
}

function aiWeightPriceIs1688Tab(tab) {
  try {
    const host = new URL(tab?.url || "").hostname;
    return host === "1688.com" || host.endsWith(".1688.com");
  } catch (_) {
    return false;
  }
}

async function aiWeightPriceSearchSupplier(tabId, payload, timeoutMs = 35000) {
  const beforeTabs = new Set((await chrome.tabs.query({})).map(tab => tab.id));
  let started = null;
  try {
    started = await aiWeightPriceSendImageMessage(
      tabId, {type: "AI_WEIGHT_PRICE_SEARCH", ...payload}, ["content-1688.js"], 10000
    );
  } catch (error) {
    if (!aiWeightPriceMessageLostDuringNavigation(error)) throw error;
  }
  if (started && !started.ok) throw new Error(started.error || "1688以图搜货启动失败");
  if (started?.ready && started.candidates?.length) {
    return started.candidates.map(candidate => ({...candidate, result_tab_id: tabId}));
  }
  if (started?.empty) throw new Error(`1688以图搜货返回空结果：${started.empty}`);
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    const tabs = await chrome.tabs.query({});
    const targets = tabs.filter(tab => tab.id === tabId ||
      (!beforeTabs.has(tab.id) && aiWeightPriceIs1688Tab(tab)));
    for (const tab of targets) {
      if (!tab?.id || tab.status === "loading") continue;
      try {
        const snapshot = await aiWeightPriceSendImageMessage(
          tab.id, {type: "AI_WEIGHT_PRICE_SEARCH_RESULTS"}, ["content-1688.js"], 5000
        );
        if (snapshot?.ready && snapshot.candidates?.length) {
          return snapshot.candidates.map(candidate => ({...candidate, result_tab_id: tab.id}));
        }
        if (snapshot?.empty) throw new Error(`1688以图搜货返回空结果：${snapshot.empty}`);
      } catch (error) {
        if (/1688以图搜货返回空结果/.test(error.message || "")) throw error;
        lastError = error;
      }
    }
    await new Promise(resolve => setTimeout(resolve, 400));
  }
  throw new Error(
    `搜索超时：主图上传后${Math.round(timeoutMs / 1000)}秒内1688未返回可读取的新结果` +
    (lastError ? `；${lastError.message || lastError}` : "")
  );
}

async function aiWeightPriceOpenSearchCandidate(candidate, timeoutMs = 20000) {
  if (!candidate?.result_image_path) {
    const url = candidate?.url || candidate?.source_url;
    if (!url) throw new Error("服务器没有返回1688候选商品链接");
    const tabId = await aiWeightPriceTab("1688.com", url);
    await aiWeightPriceWaitTab(tabId);
    return tabId;
  }
  const sourceTabId = Number(candidate.result_tab_id || 0);
  if (!sourceTabId) throw new Error("1688图片结果缺少来源标签页");
  const beforeTabs = new Set((await chrome.tabs.query({})).map(tab => tab.id));
  try {
    const clicked = await aiWeightPriceSendImageMessage(sourceTabId, {
      type: "AI_WEIGHT_PRICE_OPEN_SEARCH_RESULT",
      result_image_path: candidate.result_image_path,
      main_image_url: candidate.main_image_url,
    }, ["content-1688.js"], 5000);
    if (clicked && !clicked.ok) throw new Error(clicked.error || "1688候选商品点击失败");
  } catch (error) {
    if (!aiWeightPriceMessageLostDuringNavigation(error)) throw error;
  }
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tabs = await chrome.tabs.query({});
    const detail = tabs.find(tab => {
      if (tab.id !== sourceTabId && beforeTabs.has(tab.id)) return false;
      try {
        const parsed = new URL(tab.url || "");
        return parsed.hostname === "detail.1688.com" && /\/offer\/\d+\.html/.test(parsed.pathname);
      } catch (_) { return false; }
    });
    if (detail?.id && detail.status !== "loading") return detail.id;
    await new Promise(resolve => setTimeout(resolve, 300));
  }
  throw new Error("点击1688以图搜货候选后，没有进入对应商品详情页");
}

async function aiWeightPriceReadZyingTab({
  requireCategories = false, includeDevelopers = false, timeoutMs = 15000, tabId: requestedTabId
} = {}) {
  const tabId = requestedTabId || await aiWeightPriceTab(ZYING_HOST, "https://meli.zying.net/#/product");
  if (requestedTabId) {
    const tab = await chrome.tabs.get(tabId);
    if (new URL(tab.url || "").hostname !== ZYING_HOST) throw new Error("请选择智赢商品页面");
    if (!String(tab.url).includes("#/product")) {
      await chrome.tabs.update(tabId, {url: "https://meli.zying.net/#/product", active: false});
    }
  }
  await aiWeightPriceWaitTab(tabId);
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const context = await readZyingContext(tabId, {includeDevelopers: false});
      if (!requireCategories || context.categories.length) {
        return {tabId, context: includeDevelopers ? await readZyingContext(tabId, {includeDevelopers: true}) : context};
      }
      lastError = new Error("智赢商品分类尚未加载完成");
    } catch (error) {
      lastError = error;
    }
    await new Promise(resolve => setTimeout(resolve, 400));
  }
  throw lastError || new Error("智赢商品页面尚未准备完成，请稍后重试");
}

function aiWeightPriceUiResult(result = {}) {
  const snapshot = result?.data && typeof result.data === "object" ? result.data : {};
  const extra = {...result};
  delete extra.data;
  return {...snapshot, ...extra};
}

async function aiWeightPriceImageDataUrl(url) {
  const source = String(url || "").trim();
  if (!source) throw new Error("智赢商品缺少主图地址");
  try {
    const response = await fetch(source, {credentials: "include", cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const mime = response.headers.get("content-type") || "image/jpeg";
    const buffer = await response.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let index = 0; index < bytes.length; index += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
    }
    return `data:${mime};base64,${btoa(binary)}`;
  } catch (directError) {
    const proxied = await aiWeightPriceAction("image", {url: source});
    if (String(proxied?.data_url || "").startsWith("data:image/")) return proxied.data_url;
    throw new Error(`读取智赢商品主图失败：${directError.message || directError}`);
  }
}

async function aiWeightPriceReadNextProduct(clientConfig, selection, cursor = {}) {
  const tabId = await aiWeightPriceTab(ZYING_HOST, "https://meli.zying.net/#/product");
  await aiWeightPriceWaitTab(tabId);
  if ((selection?.category || selection?.product_developer_id) && !cursor.filters_applied) {
    const deadline = Date.now() + 15000;
    let applied = {};
    while (Date.now() < deadline) {
      const filterResult = await chrome.scripting.executeScript({
        target: {tabId},
        world: "MAIN",
        func: zeshunApplyZyingProductFilters,
        args: [selection || {}]
      });
      applied = filterResult?.[0]?.result || {};
      if (applied.ok && (!selection.category || applied.category?.selected === true) &&
          (!selection.product_developer_id || applied.developer?.selected === true)) break;
      await new Promise(resolve => setTimeout(resolve, 400));
    }
    if (!applied.ok || (selection.category && applied.category?.selected !== true) ||
        (selection.product_developer_id && applied.developer?.selected !== true)) {
      throw new Error(applied.error || "智赢筛选条件未成功应用，已停止以避免读取错误商品");
    }
    // The SPA replaces cards asynchronously after the search click. Give the
    // new list a short settling window before reading its first card.
    await new Promise(resolve => setTimeout(resolve, 700));
  }
  const startPage = Math.max(1, Number(selection?.start_page || 1));
  const endPage = Math.max(startPage, Number(selection?.end_page || startPage));
  const seenIds = new Set((cursor.seen_ids || []).map(value => String(value || "")));
  // A successful writeback can remove the previous card from the pending list.
  // Rescan from the selected range and skip known ids so the shifted next card
  // is never skipped, while dry runs that keep cards visible also make progress.
  let page = seenIds.size ? startPage : Math.max(startPage, Number(cursor.page || startPage));
  let index = seenIds.size ? 0 : Math.max(0, Number(cursor.index || 0));
  const wanted = String(selection?.start_product_id || "").trim();
  let seeking = cursor.seeking === undefined ? Boolean(wanted) : Boolean(cursor.seeking);
  while (page <= endPage) {
    const selected = await aiWeightPriceSendContentMessage(tabId, {
      type: "AI_WEIGHT_PRICE_SELECT_PAGE", page,
      selectors: clientConfig?.selectors || {}
    }, ["zying-page.js", "content-zying.js"]);
    if (!selected?.ok && page !== 1) throw new Error(selected?.error || `无法切换到智赢第 ${page} 页`);
    if (selected?.changed) await new Promise(resolve => setTimeout(resolve, 1000));
    const response = await aiWeightPriceSendContentMessage(tabId, {
      type: "AI_WEIGHT_PRICE_EXTRACT_PRODUCTS",
      selectors: clientConfig?.selectors || {},
      selection: selection || {},
      start_index: index,
      max_items: 1
    }, ["zying-page.js", "content-zying.js"]);
    if (!response?.ok) throw new Error(response?.error || "插件没有读取到智赢商品列表");
    const product = response.products?.[0];
    if (!product) {
      page += 1;
      index = 0;
      continue;
    }
    index += 1;
    const normalized = {
      ...product,
      source_page: page,
      source_index: Number(product.source_index || index)
    };
    if (seenIds.has(String(normalized.erp_goods_id || ""))) continue;
    if (seeking && String(normalized.erp_goods_id || "") !== wanted) continue;
    seeking = false;
    return {tabId, product: normalized, cursor: {page, index, seeking, filters_applied: true}};
  }
  if (seeking) throw new Error(`当前智赢列表未找到起始产品编号 ${wanted}`);
  return {tabId, product: null, cursor: {page, index, seeking: false, filters_applied: true}};
}

async function runAIWeightPriceClient(step) {
  if (!step || step.action === "done") return step;
  const task = step.task || {};
  const clientConfig = step.data?.client_config || {};
  let current = step;
  try {
    while (current && current.action !== "done") {
      const currentTask = current.task || task;
      const taskId = current.task_id || currentTask.erp_goods_id;
      if (current.action === "collect") {
        const next = await aiWeightPriceReadNextProduct(
          clientConfig,
          current.collection?.selection || {},
          current.collection?.cursor || {}
        );
        current = await aiWeightPriceAction("collect", next.product
          ? {product: next.product, cursor: next.cursor}
          : {exhausted: true, cursor: next.cursor});
        continue;
      }
      if (!taskId) throw new Error("服务器没有返回当前核重核价商品");
      if (current.action === "search") {
        const supplierTab = await aiWeightPriceTab("1688.com", "https://www.1688.com/");
        await aiWeightPriceWaitTab(supplierTab);
        const dataUrl = await aiWeightPriceImageDataUrl(currentTask.main_image_url);
        const candidates = await aiWeightPriceSearchSupplier(supplierTab, {
          data_url: dataUrl,
          selectors: clientConfig.selectors || {},
          timeout_ms: 15000
        });
        current = await aiWeightPriceAction("search", {
          task_id: taskId, candidates
        });
        continue;
      }
      if (current.action === "detail") {
        const supplierTab = await aiWeightPriceOpenSearchCandidate(current.candidate || {});
        const response = await aiWeightPriceSendContentMessage(
          supplierTab, {type: "AI_WEIGHT_PRICE_READ_DETAIL"}, ["content-1688.js"], 20000
        );
        if (!response?.ok) throw new Error(response?.error || "读取1688商品详情失败");
        current = await aiWeightPriceAction("detail", {
          task_id: taskId, detail: response.detail || {},
          runtime_api_key: ""
        });
        continue;
      }
      if (current.action === "writeback") {
        const erp = await aiWeightPriceTab(ZYING_HOST, current.task?.erp_edit_url || "https://meli.zying.net/#/product");
        await aiWeightPriceWaitTab(erp);
        const response = await aiWeightPriceSendContentMessage(erp, {
          type: "AI_WEIGHT_PRICE_WRITEBACK",
          selectors: clientConfig.selectors || {},
          erp_goods_id: taskId,
          task: current.task || currentTask,
          changes: current.changes || {}
        }, ["zying-page.js", "content-zying.js"], 20000);
        if (!response?.ok) throw new Error(response?.error || "智赢商品回写失败");
        if (response.submitted !== true || !response.before) {
          throw new Error("智赢保存请求未确认提交");
        }
        await chrome.tabs.reload(erp, {bypassCache: true});
        await aiWeightPriceWaitTab(erp);
        const verified = await aiWeightPriceSendContentMessage(erp, {
          type: "AI_WEIGHT_PRICE_VERIFY_WRITEBACK",
          selectors: clientConfig.selectors || {},
          erp_goods_id: taskId,
          task: current.task || currentTask,
          changes: current.changes || {}
        }, ["zying-page.js", "content-zying.js"], 20000);
        if (!verified?.ok || verified.persisted !== true || !verified.after) {
          throw new Error(verified?.error || "智赢保存后持久化回读失败");
        }
        current = await aiWeightPriceAction("writeback", {
          task_id: taskId,
          changes: current.changes || {},
          before: response.before,
          actual: verified.after,
          submitted: true,
          persisted: true
        });
        continue;
      }
      throw new Error(`未知核重核价浏览器动作：${current.action}`);
    }
    await storageRemove("local", ["aiWeightPriceClientRun"]);
    return current;
  } catch (error) {
    const failure = error.message || String(error);
    await storageSet("local", {aiWeightPriceClientRun: {
      action: current?.action || "error", task_id: current?.task_id || task?.erp_goods_id || "",
      error: failure, at: Date.now()
    }});
    try {
      await aiWeightPriceAction("fail", {
        action: current?.action || "error",
        task_id: current?.task_id || task?.erp_goods_id || "",
        error: failure
      });
    } catch (_) {}
    void notifyAttention(failure, {source: "AI核重核价"}).catch(() => {});
    throw error;
  }
}

function launchAIWeightPriceClient(step) {
  if (!step || step.action === "done" || aiWeightPriceRunPromise) return;
  aiWeightPriceRunPromise = runAIWeightPriceClient(step).catch(() => null).finally(() => {
    aiWeightPriceRunPromise = null;
  });
}

async function state() {
  const config = await settings();
  const auth = await authSession();
  const queue = await queueItems();
  return {
    ok: true,
    settings: config,
    authenticated: Boolean(auth),
    user: auth && auth.user || null,
    compatibilityMode: Boolean(auth && auth.mode === "legacy"),
    expiresAt: auth && auth.expiresAt || null,
    queueLength: queue.length,
    lastQueueError: queue.length ? queue[0].reason : "",
  };
}

chrome.runtime.onInstalled.addListener(async () => {
  const current = await storageGet("sync", ["consoleUrl", "openConsoleAfterCollect"]);
  await storageSet("sync", {
    consoleUrl: current.consoleUrl || DEFAULT_SETTINGS.consoleUrl,
    openConsoleAfterCollect: Boolean(current.openConsoleAfterCollect)
  });
  chrome.alarms.create(RETRY_ALARM, {periodInMinutes: 1});
  chrome.alarms.create(PURCHASE_TRACKING_RESUME_ALARM, {periodInMinutes: 0.5});
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "zeshun-collect-page",
      title: "采集当前商品到泽顺控制台 / AI原创产品",
      contexts: ["page"],
      documentUrlPatterns: [
        "https://*.1688.com/*",
        "https://*.mercadolibre.com.mx/*", "https://*.mercadolibre.com.br/*",
        "https://*.mercadolivre.com.br/*", "https://*.mercadolibre.com.ar/*",
        "https://*.mercadolibre.cl/*", "https://*.mercadolibre.com.co/*",
        "https://*.mercadolibre.com.uy/*"
      ]
    });
  });
  await setBadge((await queueItems()).length);
});

chrome.runtime.onStartup.addListener(async () => {
  chrome.alarms.create(RETRY_ALARM, {periodInMinutes: 1});
  chrome.alarms.create(PURCHASE_TRACKING_RESUME_ALARM, {periodInMinutes: 0.5});
  await setBadge((await queueItems()).length);
  await flushQueue();
  try { await resumePurchaseTracking(); } catch (_) {}
  try { await monitorAttentionEvents(); } catch (_) {}
});

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === RETRY_ALARM) {
    flushQueue();
    monitorAttentionEvents().catch(() => {});
  }
  if (alarm.name === PURCHASE_TRACKING_RESUME_ALARM) resumePurchaseTracking().catch(() => {});
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "zeshun-collect-page" || !tab || !tab.id) return;
  try { await submitProduct(await extractFromTab(tab.id)); } catch (_) {}
});

chrome.action?.onClicked?.addListener(() => {
  openFloatingWindow().catch(() => {});
});

chrome.commands?.onCommand?.addListener(command => {
  if (command === "open-zeshun") openFloatingWindow().catch(() => {});
});

chrome.windows.onRemoved?.addListener(async windowId => {
  const stored = await storageGet("local", [FLOATING_WINDOW_KEY]);
  if (Number(stored[FLOATING_WINDOW_KEY] || 0) === Number(windowId)) {
    await storageRemove("local", [FLOATING_WINDOW_KEY]);
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const run = async () => {
    switch (message && message.type) {
      case "OPEN_FLOATING_WINDOW":
        await openFloatingWindow();
        return {ok: true};
      case "OPEN_PRODUCT_SEARCH": {
        const tab = await chrome.tabs.create({url: productSearchUrl(message.country, message.keyword), active: true});
        const saved = await storageGet("local", ["productBatchOptions"]);
        await storageSet("local", {productBatchOptions: {
          ...saved.productBatchOptions, country: message.country, keyword: String(message.keyword).trim(), tab: String(tab.id)
        }});
        return {ok: true, tab_id: tab.id};
      }
      case "CHECK_PRODUCT_ZYING": {
        const status = await sendTabMessage(Number(message.tab_id), {type: "CHECK_ZYING_PLUGIN"});
        return {ok: true, ...status};
      }
      case "GET_PRODUCT_BATCH_STATUS": return {ok: true, state: await getProductBatchStatus()};
      case "START_PRODUCT_BATCH": return {ok: true, state: await startProductBatch(message)};
      case "STOP_PRODUCT_BATCH": return {ok: true, state: await stopProductBatch()};
      case "START_YANDEX_SEARCH": return startYandexSearch(message.keyword, message.count);
      case "GET_YANDEX_SEARCH_STATUS": return getYandexSearchStatus();
      case "OPEN_YANDEX_SEARCH_RESULTS": return openYandexSearchResults();
      case "LOGIN": return login(String(message.username || "").trim(), String(message.password || ""));
      case "LOGOUT": return logout();
      case "READ_1688_PAGE_DATA": {
        if (!sender?.tab?.id || new URL(sender.tab.url || "").hostname !== "detail.1688.com") {
          throw new Error("只能从1688详情页读取商品数据");
        }
        const result = await chrome.scripting.executeScript({
          target: {tabId: sender.tab.id}, world: "MAIN", func: zeshunRead1688PageData
        });
        return {ok: true, data: result?.[0]?.result};
      }
      case "SUBMIT_PRODUCT": {
        const senderHost = String(sender?.tab?.url || "").match(/^https?:\/\/([^/]+)/i)?.[1]?.toLowerCase() || "";
        if (senderHost === "s.1688.com" && message.product?.source_platform === "1688") {
          return collect1688ListProduct(message.product);
        }
        return submitProduct(message.product);
      }
      case "READ_ZYING_CONTEXT": {
        const page = await aiWeightPriceReadZyingTab({tabId: Number(message.tabId), requireCategories: true, includeDevelopers: true});
        return {ok: true, ...page.context};
      }
      case "OPEN_ZYING_LOGIN": return {ok: true, ...(await openZyingLoginPage())};
      case "GET_ZYING_OPTIONS": return {ok: true, ...(await loadZyingOptions(message.context || {}))};
      case "START_ZYING_COLLECTION": return {
        ok: true,
        ...(await startZyingCollection(message.context || {}, message.params || {}))
      };
      case "GET_ZYING_STATUS": return {ok: true, ...(await zyingCollectionStatus())};
      case "STOP_ZYING_COLLECTION": return {ok: true, ...(await stopZyingCollection())};
      case "START_ZYING_INFRINGEMENT": return {
        ok: true,
        ...(await startZyingInfringement(message.context || {}, message.params || {}))
      };
      case "GET_ZYING_INFRINGEMENT_STATUS": return {ok: true, ...(await zyingInfringementStatus())};
      case "STOP_ZYING_INFRINGEMENT": return {ok: true, ...(await stopZyingInfringement())};
      case "GET_AI_WEIGHT_PRICE_STATUS": return {ok: true, ...(await aiWeightPriceStatus())};
      case "GET_WEIGHT_DIMENSIONS_STATUS": return {ok: true, ...(await weightDimensionsRecordsStatus())};
      case "START_WEIGHT_DIMENSIONS_UPDATE": return {
        ok: true, ...(await startWeightDimensionsRecordsUpdate())
      };
      case "REPORT_ATTENTION": return {
        ok: true,
        ...(await notifyAttention(message.message, {source: message.source || "泽顺插件"}))
      };
      case "OPEN_AI_WEIGHT_PRICE_LOGIN": {
        const page = await openZyingLoginPage();
        // Opening the ERP login page is a browser action and should return as
        // soon as the tab is ready. The workbench audit/status update can take
        // longer (for example while MySQL is busy), so send it in the
        // background instead of making the login button wait for the API.
        void aiWeightPriceAction("login/open").catch(() => {});
        return {ok: true, ...page};
      }
      case "CONFIRM_AI_WEIGHT_PRICE_LOGIN": {
        const page = await aiWeightPriceReadZyingTab();
        const result = await aiWeightPriceAction("login/confirm", {context: page.context});
        return {ok: true, ...aiWeightPriceUiResult(result)};
      }
      case "OPEN_AI_WEIGHT_PRICE_SUPPLIER": {
        const tabId = await aiWeightPriceTab("1688.com", "https://www.1688.com/", {active: true});
        return {ok: true, tab_id: tabId};
      }
      case "CONTINUE_AI_WEIGHT_PRICE": {
        const result = await aiWeightPriceAction("continue", message.params || {});
        launchAIWeightPriceClient(result);
        return {ok: true, ...aiWeightPriceUiResult(result)};
      }
      case "REFRESH_AI_WEIGHT_PRICE_CATEGORIES": {
        const page = await aiWeightPriceReadZyingTab({requireCategories: true, includeDevelopers: true});
        if (!page.context.developers.length) {
          const options = await loadZyingOptions(page.context);
          page.context.developers = options.developers || [];
        }
        const result = await aiWeightPriceAction("categories/refresh", {context: page.context});
        return {ok: true, ...aiWeightPriceUiResult(result)};
      }
      case "START_AI_WEIGHT_PRICE": {
        const status = await aiWeightPriceStatus();
        const maxItems = Number(message.params?.max_items || 10);
        const page = await aiWeightPriceReadNextProduct(
          status.client_config || {}, message.params?.selection || {}, {}
        );
        if (!page.product) throw new Error("智赢所选范围没有可处理商品");
        const result = await aiWeightPriceAction("start", {
          ...(message.params || {}), products: [page.product], max_items: maxItems,
          lazy_collection: true, collection_cursor: page.cursor
        });
        launchAIWeightPriceClient(result);
        return {ok: true, ...aiWeightPriceUiResult(result)};
      }
      case "STOP_AI_WEIGHT_PRICE": {
        const result = await aiWeightPriceAction("stop");
        return {ok: true, ...aiWeightPriceUiResult(result)};
      }
      case "GET_STATE": return state();
      case "GET_PURCHASE_TRACKING_REQUEST": return {
        ok: true,
        ...(await purchaseTrackingRequest() || {phase: "idle", message: "暂无订单物流同步任务"})
      };
      case "OPEN_PURCHASE_TRACKING_LOGIN":
        return openPurchaseTrackingLogin(message.request || await purchaseTrackingRequest());
      case "CONFIRM_PURCHASE_TRACKING_LOGIN": {
        try {
          return await confirmPurchaseTrackingLogin();
        } catch (error) {
          void notifyAttention(error.message || String(error), {source: "采购平台物流同步"}).catch(() => {});
          throw error;
        }
      }
      case "RETRY_QUEUE": return flushQueue();
      case "TEST_CONNECTION": {
        const auth = await authSession();
        if (!auth) throw new ApiError("请先登录泽顺控制台账号", {authRequired: true, status: 401});
        if (auth.mode === "legacy") {
          await apiRequest("/api/db/mercado-collection/items?limit=1", {method: "GET"});
          return {ok: true, user: auth.user, compatibilityMode: true};
        }
        const data = await apiRequest("/api/browser-extension/session", {method: "GET"});
        return {ok: true, user: data.user};
      }
      case "OPEN_CONSOLE": {
        const config = await settings();
        const url = new URL(normalizeConsoleUrl(config.consoleUrl));
        if (/^[a-z0-9-]{1,80}$/i.test(String(message.tab || ""))) url.searchParams.set("tab", message.tab);
        await chrome.tabs.create({url: url.href, active: true});
        return {ok: true};
      }
      default: return {ok: false, error: "未知操作"};
    }
  };
  run().then(sendResponse, error => sendResponse({
    ok: false,
    authRequired: Boolean(error.authRequired),
    error: error.message || String(error)
  }));
  return true;
});
