"use strict";

// The extension intentionally reads the procurement platform's current tab
// instead of collecting credentials.  The browser's own cookies are the only
// login state used by this content script.
(function () {
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
    return Boolean(document.querySelector("input[type='password']")) &&
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
      : ["input[placeholder*='订单']", "input[placeholder*='商品']", "input[placeholder*='搜索']", "input[name*='order']", "input[type='search']"];
    for (const selector of selectors) {
      const candidate = [...document.querySelectorAll(selector)].find(visible);
      if (candidate) return candidate;
    }
    return null;
  }

  function findAction(label) {
    return [...document.querySelectorAll("button,a,[role='button'],span")]
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

  async function fetchTracking(purchaseOrder) {
    const order = String(purchaseOrder || "").trim();
    if (!order) return {status: "failed", message: "采购订单号为空"};
    if (isLoginPage()) return {status: "not_logged_in", message: "采购平台尚未登录"};
    const platform = platformForHost();
    const search = findSearchInput(platform);
    if (search) {
      setInputValue(search, order);
      search.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", code: "Enter", bubbles: true}));
      search.dispatchEvent(new KeyboardEvent("keyup", {key: "Enter", code: "Enter", bubbles: true}));
      try { if (search.form) search.form.requestSubmit(); } catch (_) {}
      await sleep(1800);
    }
    const orderText = text();
    if (!orderText.includes(order)) {
      return {status: "not_found", message: "平台未找到该采购订单"};
    }
    for (const label of ["查看物流", "物流详情", "查看快递", "订单详情", "查看详情"]) {
      const action = findAction(label);
      if (!action) continue;
      try {
        action.click();
        await sleep(1200);
        break;
      } catch (_) {}
    }
    const result = extractTracking(text());
    if (!result.tracking_number) {
      return {status: "pending", message: "订单尚未发货或平台未显示物流号"};
    }
    return {status: "synced", ...result};
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    const run = async () => {
      if (message?.type === "CHECK_PURCHASE_LOGIN") {
        return {
          ok: true,
          platform: platformForHost(),
          loggedIn: !isLoginPage(),
          url: location.href,
        };
      }
      if (message?.type === "FETCH_PURCHASE_TRACKING") {
        return {ok: true, ...(await fetchTracking(message.purchaseOrder))};
      }
      return {ok: false, error: "未知采购物流操作"};
    };
    run().then(sendResponse, error => sendResponse({
      ok: false,
      error: error?.message || String(error),
    }));
    return true;
  });
})();
