"use strict";

importScripts("zying-page.js");
importScripts("product-batch.js");

const DEFAULT_SETTINGS = {
  consoleUrl: "http://127.0.0.1:5000",
  openConsoleAfterCollect: false
};
const AUTH_KEY = "browserExtensionAuth";
const QUEUE_KEY = "pendingProducts";
const PURCHASE_TRACKING_SESSION_KEY = "purchaseTrackingSession";
const RETRY_ALARM = "zeshun-collector-retry";
const PURCHASE_TRACKING_RESUME_ALARM = "zeshun-purchase-tracking-resume";
const NOTIFICATION_DEDUPE_KEY = "notificationDedupe";
const NOTIFICATION_DEDUPE_MS = 30 * 60 * 1000;
const MAX_QUEUE_SIZE = 100;
const FLOATING_WINDOW_KEY = "zeshunFloatingWindowId";
const ZYING_HOST = "meli.zying.net";
const ZYING_LOGIN_URL = `https://${ZYING_HOST}/#/login`;
let queueFlushPromise = null;
let purchaseTrackingRunPromise = null;

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
  const stored = await storageGet("session", [AUTH_KEY]);
  const auth = stored[AUTH_KEY];
  if (!auth || !auth.token) return null;
  if (auth.expiresAt && Date.now() >= Number(auth.expiresAt)) {
    await clearAuth();
    return null;
  }
  return auth;
}

async function clearAuth() {
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
  await storageSet("session", {[AUTH_KEY]: auth});
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

function sendTabMessage(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, response => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve(response || {});
    });
  });
}

async function extractFromTab(tabId) {
  const response = await sendTabMessage(tabId, {type: "EXTRACT_PRODUCT"});
  if (!response.ok) throw new Error(response.error || "未读取到商品详情");
  return response.product;
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

async function readZyingContext(tabId) {
  let page = {};
  try {
    const results = await Promise.allSettled([
      chrome.scripting.executeScript({
        target: {tabId},
        world: "MAIN",
        func: zeshunReadZyingPageContext
      }),
      chrome.scripting.executeScript({
        target: {tabId},
        world: "MAIN",
        func: zeshunReadZyingProductDevelopers
      })
    ]);
    if (results[0].status === "fulfilled") {
      page = results[0].value?.[0]?.result || {};
    } else {
      try { page = await sendTabMessage(tabId, {type: "READ_ZYING_CONTEXT"}); } catch (_) {}
    }
    if (results[1].status === "fulfilled") {
      page.developers = results[1].value?.[0]?.result || [];
    }
  } catch (_) {
    try { page = await sendTabMessage(tabId, {type: "READ_ZYING_CONTEXT"}); } catch (_) {}
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
  return apiRequest("/api/browser-extension/zying/options", {
    method: "POST",
    body: JSON.stringify({
      credential: context.credential,
      categories: context.categories || [],
      developers: context.developers || []
    })
  });
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
  const data = await apiRequest("/api/browser-extension/ai-weight-price/status", {method: "GET"});
  const reason = data?.circuit?.reason || (data?.run?.outcome === "blocked" ? data?.run?.message : "");
  if (reason) void notifyAttention(reason, {source: "AI核重核价"}).catch(() => {});
  return data;
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
  return apiRequest(`/api/browser-extension/ai-weight-price/${action}`, {
    method: "POST",
    body: JSON.stringify(body)
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

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
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
      case "LOGIN": return login(String(message.username || "").trim(), String(message.password || ""));
      case "LOGOUT": return logout();
      case "SUBMIT_PRODUCT": return submitProduct(message.product);
      case "READ_ZYING_CONTEXT": return {ok: true, ...(await readZyingContext(Number(message.tabId)))};
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
      case "REPORT_ATTENTION": return {
        ok: true,
        ...(await notifyAttention(message.message, {source: message.source || "泽顺插件"}))
      };
      case "OPEN_AI_WEIGHT_PRICE_LOGIN": return {
        ok: true,
        ...(await aiWeightPriceAction("login/open"))
      };
      case "CONFIRM_AI_WEIGHT_PRICE_LOGIN": return {
        ok: true,
        ...(await aiWeightPriceAction("login/confirm"))
      };
      case "OPEN_AI_WEIGHT_PRICE_SUPPLIER": return {
        ok: true,
        ...(await aiWeightPriceAction("login/supplier"))
      };
      case "CONTINUE_AI_WEIGHT_PRICE": return {
        ok: true,
        ...(await aiWeightPriceAction("continue", message.params || {}))
      };
      case "REFRESH_AI_WEIGHT_PRICE_CATEGORIES": return {
        ok: true,
        ...(await aiWeightPriceAction("categories/refresh"))
      };
      case "START_AI_WEIGHT_PRICE": return {
        ok: true,
        ...(await aiWeightPriceAction("start", message.params || {}))
      };
      case "STOP_AI_WEIGHT_PRICE": return {
        ok: true,
        ...(await aiWeightPriceAction("stop"))
      };
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
        await chrome.tabs.create({url: normalizeConsoleUrl(config.consoleUrl), active: true});
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
