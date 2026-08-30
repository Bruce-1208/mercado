"use strict";

const DEFAULT_SETTINGS = {
  consoleUrl: "http://127.0.0.1:5000",
  openConsoleAfterCollect: false
};
const AUTH_KEY = "browserExtensionAuth";
const QUEUE_KEY = "pendingProducts";
const RETRY_ALARM = "zeshun-collector-retry";
const MAX_QUEUE_SIZE = 100;
let queueFlushPromise = null;

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
  const result = auth && auth.mode === "legacy"
    ? await uploadLegacyProduct(product)
    : await apiRequest("/api/browser-extension/collect", {
        method: "POST",
        body: JSON.stringify({product})
      });
  const config = await settings();
  if (openConsole && config.openConsoleAfterCollect) {
    await chrome.tabs.create({url: normalizeConsoleUrl(config.consoleUrl), active: true});
  }
  return {taskId: Number(result.task_id), scrapeStatus: result.scrape_status || product.scrape_status};
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
    lastQueueError: queue.length ? queue[0].reason : ""
  };
}

chrome.runtime.onInstalled.addListener(async () => {
  const current = await storageGet("sync", ["consoleUrl", "openConsoleAfterCollect"]);
  await storageSet("sync", {
    consoleUrl: current.consoleUrl || DEFAULT_SETTINGS.consoleUrl,
    openConsoleAfterCollect: Boolean(current.openConsoleAfterCollect)
  });
  chrome.alarms.create(RETRY_ALARM, {periodInMinutes: 1});
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "zeshun-collect-page",
      title: "采集当前商品到泽顺控制台",
      contexts: ["page"],
      documentUrlPatterns: [
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
  await setBadge((await queueItems()).length);
  await flushQueue();
});

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === RETRY_ALARM) flushQueue();
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "zeshun-collect-page" || !tab || !tab.id) return;
  try { await submitProduct(await extractFromTab(tab.id)); } catch (_) {}
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const run = async () => {
    switch (message && message.type) {
      case "LOGIN": return login(String(message.username || "").trim(), String(message.password || ""));
      case "LOGOUT": return logout();
      case "SUBMIT_PRODUCT": return submitProduct(message.product);
      case "GET_STATE": return state();
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
