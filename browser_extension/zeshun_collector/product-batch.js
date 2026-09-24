"use strict";

// Runs in the extension service worker, independently of the popup's lifetime.
const PRODUCT_BATCH_KEY = "productBatch";
const PRODUCT_SEARCH_HOSTS = {
  MLM: "listado.mercadolibre.com.mx", MLB: "lista.mercadolivre.com.br",
  MLA: "listado.mercadolibre.com.ar", MLC: "listado.mercadolibre.cl",
  MCO: "listado.mercadolibre.com.co", MLU: "listado.mercadolibre.com.uy"
};
const INTERNATIONAL_FILTER = "_NoIndex_True_SHIPPING*ORIGIN_10215069";
let productBatchRun = null;
let productBatchStarting = false;

function reportProductBatchAttention(message, context = {}) {
  if (typeof notifyAttention !== "function") return;
  void notifyAttention(message, context).catch(() => {});
}
let productBatchForegroundLock = Promise.resolve();

function productBatchUrl(value) {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.username || url.password ||
      !/(^|\.)(mercadolibre\.com\.(mx|br|ar|co|uy)|mercadolivre\.com\.br|mercadolibre\.cl)$/i.test(url.hostname)) {
    throw new Error("请选择支持的 Mercado Libre 前端页面");
  }
  url.hash = "";
  return url.href;
}

function hasInternationalFilter(value) {
  let decoded = String(value || "");
  try { decoded = decodeURIComponent(decoded); } catch (_) {}
  return /SHIPPING(?:\*|_)?ORIGIN(?:_|=)10215069/i.test(decoded);
}

function forceInternationalUrl(value) {
  const url = new URL(productBatchUrl(value));
  if (!hasInternationalFilter(url.href)) {
    url.pathname = `${url.pathname.replace(/\/$/, "")}${INTERNATIONAL_FILTER}`;
  }
  return url.href;
}

function productSearchUrl(country, keyword) {
  const host = PRODUCT_SEARCH_HOSTS[String(country || "").toUpperCase()];
  const text = String(keyword || "").replace(/\s+/g, " ").trim();
  if (!host) throw new Error("请选择支持的国家");
  if (!text) throw new Error("请输入采集关键词");
  if (text.length > 160) throw new Error("采集关键词不能超过 160 个字符");
  return `https://${host}/${encodeURIComponent(text.replace(/\s+/g, "-"))}${INTERNATIONAL_FILTER}`;
}

function productBatchParams(params = {}) {
  const read = (key, label, max) => {
    const value = Number(params[key]);
    if (!Number.isInteger(value) || value < 1 || value > max) throw new Error(`${label}须为 1–${max} 的整数`);
    return value;
  };
  const optionalSales = (key, label) => {
    if (params[key] === "" || params[key] === null || params[key] === undefined) return null;
    const value = Number(params[key]);
    if (!Number.isInteger(value) || value < 0 || value > 1000000000) throw new Error(`${label}须为 0–1000000000 的整数`);
    return value;
  };
  const minSales = optionalSales("min_sales", "最低销量");
  const maxSales = optionalSales("max_sales", "最高销量");
  if (minSales !== null && maxSales !== null && maxSales < minSales) {
    throw new Error("最高销量不能低于最低销量");
  }
  return {
    max_items: read("max_items", "采集数量", 500),
    concurrency: read("concurrency", "并发数", 10),
    min_sales: minSales,
    max_sales: maxSales
  };
}

function productBatchEligibility(product, params) {
  const snapshot = product && product.plugin_snapshot || {};
  const fulfillment = String(snapshot.fulfillment_type || "unknown");
  if (!snapshot.fulfillment_eligible || !["self_ship", "semi_managed"].includes(fulfillment)) {
    return {eligible: false, reason: `智赢发货方式：${snapshot.fulfillment_label || "未识别"}`};
  }
  const rawSales = snapshot.sales;
  const sales = Number(rawSales);
  const hasSales = rawSales !== null && rawSales !== undefined && rawSales !== "" &&
    Number.isFinite(sales) && sales >= 0;
  if ((params.min_sales !== null || params.max_sales !== null) && !hasSales) {
    return {eligible: false, reason: "智赢销量未读取到"};
  }
  if (params.min_sales !== null && sales < params.min_sales) {
    return {eligible: false, reason: `智赢销量 ${sales} 低于 ${params.min_sales}`};
  }
  if (params.max_sales !== null && sales > params.max_sales) {
    return {eligible: false, reason: `智赢销量 ${sales} 高于 ${params.max_sales}`};
  }
  return {eligible: true, sales: hasSales ? sales : null};
}

async function getProductBatchStatus() {
  if (productBatchRun) return {...productBatchRun.state};
  const stored = await storageGet("local", [PRODUCT_BATCH_KEY]);
  const state = stored[PRODUCT_BATCH_KEY] || {running: false, message: "等待启动"};
  if (state.running) {
    state.running = false;
    state.phase = "interrupted";
    state.message = "浏览器或插件已重启，任务中断；已上传的商品保留，请重新启动。";
    await storageSet("local", {[PRODUCT_BATCH_KEY]: state});
  }
  return state;
}

async function saveProductBatch(run) {
  const snapshot = {...run.state};
  run.saving = (run.saving || Promise.resolve()).then(() =>
    storageSet("local", {[PRODUCT_BATCH_KEY]: snapshot})
  );
  await run.saving;
}

async function closeProductBatchTab(run, id) {
  if (!run.tabs.delete(id)) return;
  try { await chrome.tabs.remove(id); } catch (_) {}
}

function withProductBatchForegroundTab(run, tabId, action) {
  // 每个页面在完整读取周期开始时激活一次。锁覆盖整个读取周期，避免
  // 重试时多个页面每 500ms 轮流抢占前台。
  let release;
  const previous = productBatchForegroundLock;
  productBatchForegroundLock = new Promise(resolve => { release = resolve; });
  return previous.then(async () => {
    try {
      if (run && run.windowId && chrome.windows && chrome.windows.update) {
        try { await chrome.windows.update(run.windowId, {focused: true}); } catch (_) {}
      }
      if (chrome.tabs.update) {
        try { await chrome.tabs.update(tabId, {active: true}); } catch (_) {}
      }
      // 给页面的 active-tab 监听器一个短暂的初始化窗口。
      await new Promise(resolve => setTimeout(resolve, 180));
      return await action();
    } finally {
      release();
    }
  });
}

async function createProductBatchWindow() {
  if (!chrome.windows || !chrome.windows.create) {
    throw new Error("当前 Edge 不支持创建独立采集窗口，请更新浏览器后重试");
  }
  const created = await chrome.windows.create({
    url: "about:blank",
    type: "normal",
    focused: true
  });
  if (!created || !created.id) throw new Error("无法创建独立采集窗口");
  return created;
}

async function closeProductBatchWindow(run) {
  const windowId = run && run.windowId;
  if (!windowId) return;
  run.windowId = null;
  try {
    if (chrome.windows && chrome.windows.remove) await chrome.windows.remove(windowId);
  } catch (_) {}
}

function productBatchTabUrl(tab) {
  const candidates = [tab && tab.pendingUrl, tab && tab.url]
    .map(value => String(value || "").trim())
    .filter(Boolean);
  let invalidUrl = "";
  for (const value of candidates) {
    // During a new tab navigation Chromium may expose about:blank (or an
    // internal error page) in `url` while the real destination is still in
    // `pendingUrl`. Keep polling instead of treating that short transition as
    // a login or verification redirect.
    if (/^(?:about:blank|chrome(?:-error)?:\/\/|edge(?:-error)?:\/\/|view-source:)/i.test(value)) {
      continue;
    }
    try {
      return productBatchUrl(value);
    } catch (_) {
      invalidUrl = value;
    }
  }
  if (invalidUrl) {
    throw new Error("页面跳转到登录或验证地址，请先在前端页面完成验证");
  }
  return "";
}

async function readProductBatchPage(run, url, type, consume) {
  if (run.stop) return null;
  // Mercado 列表和智赢详情浮层都可能只在激活标签页完成渲染。
  // 用前台锁只包住读取，不包住后续上传，所以仍可并发上传。
  const needsForeground = type === "READ_PRODUCT_LIST" || type === "EXTRACT_BATCH_PRODUCT";
  const tabOptions = {url: productBatchUrl(url), active: false};
  if (run.windowId) tabOptions.windowId = run.windowId;
  const tab = await chrome.tabs.create(tabOptions);
  run.tabs.add(tab.id);
  try {
    const poll = async () => {
      const deadline = Date.now() + 45000;
      let lastError = "页面未加载完成";
      while (!run.stop && Date.now() < deadline) {
        const current = await chrome.tabs.get(tab.id);
        const currentUrl = productBatchTabUrl(current);
        if (!currentUrl) {
          lastError = "页面正在打开，等待商品页加载";
          await new Promise(resolve => setTimeout(resolve, 500));
          continue;
        }
        // Mercado 页面可能长期保持 loading（统计、推荐和浮层请求不会结束），
        // 不能把 tabs.status === complete 当作内容脚本已经可用的前置条件。
        // 直接尝试发消息，内容脚本尚未注入时只会进入下一轮重试。
        let response = null;
        try {
          response = await sendTabMessage(tab.id, {
            type
          });
        } catch (error) { lastError = error.message || String(error); }
        if (response?.blocked) throw new Error(response.error || "请先完成登录或人机验证");
        if (response?.ok) return response;
        if (response?.error) lastError = response.error;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      if (!run.stop) throw new Error(`页面读取超时：${lastError}`);
      return null;
    };
    const response = needsForeground
      ? await withProductBatchForegroundTab(run, tab.id, poll)
      : await poll();
    return response ? await consume(response) : null;
  } finally {
    await closeProductBatchTab(run, tab.id);
  }
}

async function runProductBatch(run) {
  const {params} = run.state;
  const visited = new Set();
  const seen = new Set();
  const uploaded = new Set();
  let url = run.state.source_url;
  let pagesRead = 0;
  const keepAlive = setInterval(() => chrome.runtime.getPlatformInfo(() => {}), 20000);
  try {
    while (!run.stop && url && run.state.completed_count < params.max_items) {
      url = forceInternationalUrl(url);
      if (visited.has(url)) throw new Error("翻页链接重复，已停止以避免重复采集");
      visited.add(url);
      const listing = await readProductBatchPage(run, url, "READ_PRODUCT_LIST", value => value);
      if (!listing || run.stop) break;
      pagesRead += 1;
      if (pagesRead > 100) throw new Error("本次最多读取 100 页，请缩小采集数量");
      const page = Number(listing.page);
      if (!Number.isInteger(page) || page < 1) throw new Error("无法识别当前列表页码");
      run.state.current_page = page;
      run.state.message = listing.international_selected
        ? `正在采集第 ${page} 页（Internacional · 智赢终审 · 并发 ${params.concurrency}）`
        : `第 ${page} 页 Internacional 未生效，正逐件读取智赢发货方式和销量`;
      await saveProductBatch(run);

      const candidates = [];
      for (const item of listing.items || []) {
        const itemUrl = productBatchUrl(item.url);
        const key = item.key || itemUrl;
        if (seen.has(key)) continue;
        seen.add(key);
        candidates.push(itemUrl);
      }

      let index = 0;
      await Promise.all(Array.from({length: Math.min(params.concurrency, candidates.length)}, async () => {
        while (!run.stop && index < candidates.length && run.state.completed_count + run.reserved < params.max_items) {
          const itemUrl = candidates[index++];
          run.state.candidate_count += 1;
          try {
            await readProductBatchPage(run, itemUrl, "EXTRACT_BATCH_PRODUCT", async response => {
              if (run.stop) return;
              const product = response.product;
              run.state.current_item_id = product.source_item_id || "";
              if (uploaded.has(product.source_item_id)) { run.state.skipped += 1; return; }
              const decision = productBatchEligibility(product, params);
              if (!decision.eligible) {
                run.state.skipped += 1;
                run.state.last_skip = decision.reason;
                return;
              }
              if (run.state.completed_count + run.reserved >= params.max_items) return;
              run.reserved += 1;
              uploaded.add(product.source_item_id);
              try {
                await uploadProduct(product, {openConsole: false});
                run.state.completed_count += 1;
              } finally {
                run.reserved -= 1;
              }
            });
          } catch (error) {
            if (!run.stop) {
              run.state.failed_count += 1;
              run.state.last_error = error.message || String(error);
              reportProductBatchAttention(run.state.last_error, {
                source: "美客多商品采集",
                itemId: run.state.current_item_id || ""
              });
              if (error.authRequired) { run.error = error; run.stop = true; }
            }
          }
          run.state.processed_count = run.state.completed_count + run.state.failed_count + run.state.skipped;
          await saveProductBatch(run);
        }
      }));
      if (run.state.completed_count >= params.max_items) break;
      url = listing.next_url;
    }
    if (run.error) throw run.error;
    run.state.phase = run.stop ? "stopped" : (run.state.failed_count ? "partial" : "completed");
    run.state.message = run.stop ? "已停止采集" : "采集结束（已达到采集数量或搜索末页）";
  } catch (error) {
    run.state.phase = run.stop && !run.error ? "stopped" : "error";
    run.state.message = run.state.phase === "stopped" ? "已停止采集" : error.message || String(error);
    if (run.state.phase === "error") {
      reportProductBatchAttention(run.state.message, {source: "美客多商品采集"});
    }
  } finally {
    clearInterval(keepAlive);
    await Promise.all([...run.tabs].map(id => closeProductBatchTab(run, id)));
    await closeProductBatchWindow(run);
    run.state.running = false;
    run.state.finished_at = Date.now();
    await saveProductBatch(run);
    productBatchRun = null;
  }
}

async function startProductBatch(message) {
  if (productBatchRun || productBatchStarting) throw new Error("美客多采集任务正在运行，请先停止当前任务");
  productBatchStarting = true;
  let createdWindowId = null;
  try {
    if (!await authSession()) throw new Error("请先在插件设置中登录泽顺控制台账号");
    const params = productBatchParams(message.params);
    const tab = await chrome.tabs.get(Number(message.tab_id));
    const originalUrl = productBatchUrl(tab.url);
    const pageContext = await sendTabMessage(tab.id, {type: "READ_PRODUCT_LIST"});
    if (!pageContext.ok) throw new Error(pageContext.error || "请选择已加载完成的 Mercado Libre 搜索列表页");
    const zying = await sendTabMessage(tab.id, {type: "CHECK_ZYING_PLUGIN"});
    if (!zying?.logged_in) throw new Error(zying?.message || "无法确认智赢插件登录状态");
    const run = {tabs: new Set(), windowId: null, stop: false, reserved: 0, state: {
      running: true, phase: "running", params,
      source_url: forceInternationalUrl(originalUrl),
      execution_window: "new_edge_window",
      zying_logged_in: true, candidate_count: 0, processed_count: 0,
      completed_count: 0, failed_count: 0, skipped: 0, current_page: 0,
      current_item_id: "", started_at: Date.now(), message: "正在打开搜索页，后续将逐件以智赢数据终审"
    }};
    const batchWindow = await createProductBatchWindow();
    run.windowId = batchWindow.id;
    createdWindowId = batchWindow.id;
    run.state.window_id = batchWindow.id;
    await saveProductBatch(run);
    productBatchRun = run;
    void runProductBatch(run);
    return {...run.state};
  } catch (error) {
    if (createdWindowId && chrome.windows && chrome.windows.remove) {
      try { await chrome.windows.remove(createdWindowId); } catch (_) {}
    }
    reportProductBatchAttention(error.message || String(error), {source: "美客多商品采集"});
    throw error;
  } finally { productBatchStarting = false; }
}

async function stopProductBatch() {
  const run = productBatchRun;
  if (run) {
    run.stop = true;
    run.state.message = "正在停止，等待正在上传的商品完成…";
    await saveProductBatch(run);
    await Promise.all([...run.tabs].map(id => closeProductBatchTab(run, id)));
  }
  return getProductBatchStatus();
}
