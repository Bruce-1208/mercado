"use strict";

// The extension intentionally reads the procurement platform's current tab
// instead of collecting credentials.  The browser's own cookies are the only
// login state used by this content script.
(function () {
  if (globalThis.__zeshunPurchaseContent) return;
  globalThis.__zeshunPurchaseContent = true;
  const platformForHost = () => {
    const host = String(location.hostname || "").toLowerCase();
    if (host.includes("1688.com")) return "1688";
    if (host.includes("taobao.com") || host.includes("tmall.com")) return "taobao";
    if (host.includes("yangkeduo.com") || host.includes("pinduoduo.com")) return "pdd";
    if (host.includes("goofish.com") || host.includes("xianyu.com")) return "xianyu";
    return "";
  };

  const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

  function visible(element) {
    if (!element) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  }

  function text() {
    return String(document.body?.innerText || "").replace(/\s+/g, " ").trim();
  }

  function isLoginPage() {
    const url = String(location.href || "").toLowerCase();
    if (/(^|[/?._-])(login|signin|sign-in|passport)([/?._-]|$)/.test(url)) return true;
    return [...document.querySelectorAll("input[type='password']")].some(visible) &&
      /登录|登 录|验证码|扫码登录|短信验证|账号/.test(text().slice(0, 4000));
  }

  function setInputValue(input, value) {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value"
    )?.set;
    if (setter) setter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new Event("input", {bubbles: true}));
    input.dispatchEvent(new Event("change", {bubbles: true}));
  }

  function findSearchInput(platform) {
    const selectors = platform === "1688"
      ? ["input[placeholder*='订单']", "input[placeholder*='搜索']", "input[name*='order']", "input[type='search']"]
      : ["input[placeholder*='订单']", "input[name*='order']", "input[type='search']"];
    for (const selector of selectors) {
      const candidate = [...document.querySelectorAll(selector)].find(visible);
      if (candidate) return candidate;
    }
    return null;
  }

  function findAction(label, scope) {
    return [...scope.querySelectorAll("button,a,[role='button'],span,div")]
      .find(element => visible(element) && String(element.textContent || "").trim() === label);
  }

  function extractTracking(value) {
    const normalized = String(value || "").replace(/\s+/g, " ").trim();
    const patterns = [
      /(?:物流单号|快递单号|运单号|物流编号|快递编号)\s*[：:]?\s*([A-Z0-9][A-Z0-9-]{5,31})/i,
      /\b(?:SF|YT|ZTO|STO|YD|JT|JDVA|EMS)[A-Z0-9-]{6,28}\b/i
    ];
    let trackingNumber = "";
    for (const pattern of patterns) {
      const match = normalized.match(pattern);
      if (match) {
        trackingNumber = String(match[1] || match[0] || "").toUpperCase();
        break;
      }
    }
    const companies = [
      ["顺丰", "shunfeng"], ["中通", "zhongtong"], ["圆通", "yuantong"],
      ["申通", "shentong"], ["韵达", "yunda"], ["京东", "jd"],
      ["邮政", "ems"], ["EMS", "ems"], ["极兔", "jtexpress"]
    ];
    const logisticsCompany = (companies.find(([name]) => normalized.toLowerCase().includes(name.toLowerCase())) || ["", ""])[1];
    return {tracking_number: trackingNumber, logistics_company: logisticsCompany};
  }

  function findOrderScope(order) {
    const matchesOrder = value => String(value || "").split(/[^A-Za-z0-9_-]+/).includes(order);
    let matchedOrder = null;
    // Start at the exact order-number text, never the page-wide result text.
    const nodes = [...document.querySelectorAll("body *")].filter(element =>
      visible(element) && !["INPUT", "TEXTAREA", "SCRIPT", "STYLE"].includes(element.tagName) &&
      matchesOrder(element.innerText) &&
      ![...element.children].some(child => matchesOrder(child.innerText))
    );
    for (const node of nodes) {
      for (let scope = node; scope && scope !== document.body; scope = scope.parentElement) {
        const value = String(scope.innerText || "");
        const orderLabels = value.match(/(?:订单号|订单编号|订单号码)\s*[：:]?\s*([A-Za-z0-9_-]+)/g) || [];
        if (orderLabels.some(label => !matchesOrder(label))) break;
        // An order number proves the order exists even before shipping controls
        // appear. Keep its enclosing single-order scope to read shipping status.
        if (orderLabels.some(matchesOrder)) matchedOrder = scope;
        if (/物流单号|快递单号|运单号|物流编号|快递编号/.test(value) ||
            ["查看物流", "物流详情", "查看快递", "订单详情", "查看详情"].some(label => findAction(label, scope))) return scope;
      }
    }
    return matchedOrder;
  }

  function pageProblem() {
    if (isLoginPage() || /请先登录|请登录后|立即登录|扫码登录|短信验证登录/.test(text())) {
      return {status: "not_logged_in", message: "采购平台尚未登录或登录已失效"};
    }
    if (/安全验证|请完成验证|拖动滑块|访问过于频繁|操作过于频繁|网络异常|加载失败/.test(text())) {
      return {status: "failed", fatal: true, message: "采购平台出现验证、限流或加载异常，请在采购平台处理后重试"};
    }
    return null;
  }

  function capturedOrder(order) {
    let result = null;
    const listener = event => {
      try {
        const value = JSON.parse(event.detail);
        if (value.order === order) result = value;
      } catch (_) {}
    };
    document.addEventListener("zeshun-purchase-result", listener);
    document.dispatchEvent(new CustomEvent("zeshun-purchase-read", {detail: order}));
    document.removeEventListener("zeshun-purchase-result", listener);
    return result;
  }

  function unshipped(value) {
    return /等待卖家发货|等待商家发货|待发货|尚未发货|未发货|等待买家付款|待付款/.test(String(value || ""));
  }

  function explicitMissing() {
    return /订单不存在|订单不属于该用户|没有符合条件的订单|未找到相关订单|没有找到相关订单|暂无相关订单/.test(text());
  }

  function navigation(action, order) {
    const href = action.closest("a[href]")?.getAttribute("href");
    if (!href || /^(javascript:|#)/i.test(href)) return null;
    const url = new URL(href, location.href);
    if (url.protocol !== "https:" || url.href === location.href) return null;
    return {status: "navigate", url: url.href, bound_order: order};
  }

  async function fetchTracking(purchaseOrder, options = {}) {
    const order = String(purchaseOrder || "").trim();
    if (!order) return {status: "failed", message: "采购订单号为空"};
    const problem = pageProblem();
    if (problem) return problem;
    const platform = platformForHost();
    const direct = platform === "pdd" && new URL(location.href).searchParams.get("order_sn") === order;
    let scope = findOrderScope(order);
    let queried = direct || options.boundOrder === order || options.querySubmitted === true;
    let changed = false;
    let observer;
    if (!scope && !queried) {
      const search = findSearchInput(platform);
      if (!search) {
        return {status: "failed", fatal: true, message: "未找到订单查询入口，未执行查询；请打开采购订单列表后重试"};
      }
      // Observe results, not our own input value. Old empty results cannot prove
      // that the new query completed.
      observer = new MutationObserver(() => { changed = true; });
      setInputValue(search, order);
      observer.observe(document.body, {childList: true, subtree: true, characterData: true});
      const root = search.form || search.closest("[role='search']") || document.body;
      const submit = ["搜索", "查询", "搜索订单", "查询订单"].map(label => findAction(label, root)).find(Boolean)
        || search.form?.querySelector("button[type='submit'],input[type='submit']");
      if (submit) submit.click();
      else if (search.form) search.form.requestSubmit();
      else {
        search.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", code: "Enter", bubbles: true}));
        search.dispatchEvent(new KeyboardEvent("keyup", {key: "Enter", code: "Enter", bubbles: true}));
      }
      queried = true;
    }
    const dialogsBefore = new Set([...document.querySelectorAll("[role='dialog'],dialog,.ant-modal,.el-dialog")].filter(visible));
    const clicked = new Set();
    let activeDialog = null;
    const deadline = Date.now() + 15000;
    try {
      while (Date.now() < deadline) {
        const problem = pageProblem();
        if (problem) return problem;
        // PDD's order page loads its own authenticated JSON response. Accept
        // only shipping fields bound to this exact order, including packages.
        const record = direct ? capturedOrder(order) : null;
        if (record?.tracking_number) return {status: "synced", tracking_number: record.tracking_number,
          logistics_company: extractTracking(record.logistics_company).logistics_company};
        scope = findOrderScope(order);
        activeDialog = [...document.querySelectorAll("[role='dialog'],dialog,.ant-modal,.el-dialog")]
          .find(element => visible(element) && !dialogsBefore.has(element));
        let value = String((activeDialog || scope)?.innerText || "");
        // A followed logistics link may omit the order label. Its binding was
        // established from the matching order's own read-only link.
        if (!value && options.boundOrder === order) value = text();
        const labels = [...value.matchAll(/(?:订单号|订单编号|订单号码)\s*[：:]?\s*([A-Za-z0-9_-]+)/g)].map(match => match[1]);
        if (labels.some(id => id !== order)) {
          return {status: "failed", fatal: true, message: "物流详情订单号不匹配，已停止以避免串单"};
        }
        if (scope || activeDialog || options.boundOrder === order) {
          const result = extractTracking(value);
          if (result.tracking_number && result.tracking_number !== order) return {status: "synced", ...result};
          if (unshipped(value)) return {status: "pending", message: "未发货"};
          const action = ["查看物流", "查看物流详情", "物流详情", "查看快递", "订单详情", "查看详情"]
            .map(label => findAction(label, activeDialog || scope || document.body))
            .find(element => element && !clicked.has(element));
          if (action) {
            const target = navigation(action, order);
            if (target) return target;
            clicked.add(action);
            action.click();
          }
        }
        if (record && unshipped(record.status_text)) return {status: "pending", message: "未发货"};
        if (!scope && queried && (direct || changed || options.querySubmitted === true) && explicitMissing()) {
          return {status: "not_found", message: "平台明确返回该采购订单不存在或不属于当前账号"};
        }
        await sleep(250);
      }
      return {status: "failed", fatal: !scope, message: scope
        ? "已找到采购订单，但未读到物流号或明确的未发货状态，请检查物流详情"
        : "订单查询超时或查询未生效，未确认订单不存在，请检查采购平台页面"};
    } finally {
      observer?.disconnect();
      const close = activeDialog?.querySelector("[aria-label='Close'],[aria-label='关闭'],.ant-modal-close,.el-dialog__headerbtn");
      if (close) close.click();
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!["CHECK_PURCHASE_LOGIN", "FETCH_PURCHASE_TRACKING"].includes(message?.type)) {
      return false;
    }
    const run = async () => {
      if (message?.type === "CHECK_PURCHASE_LOGIN") {
        return {
          ok: true,
          platform: platformForHost(),
          loggedIn: !pageProblem(),
          url: location.href,
        };
      }
      if (message?.type === "FETCH_PURCHASE_TRACKING") {
        return {ok: true, ...(await fetchTracking(message.purchaseOrder, message))};
      }
    };
    run().then(sendResponse, error => sendResponse({
      ok: false,
      error: error?.message || String(error),
    }));
    return true;
  });
})();
