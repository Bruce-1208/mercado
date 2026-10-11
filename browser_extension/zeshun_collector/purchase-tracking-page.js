"use strict";

// Observe the buyer page's own responses. No private API requests or credentials.
(() => {
  if (window.__zeshunPurchaseCapture) return;
  window.__zeshunPurchaseCapture = true;
  const records = new Map();
  const idOf = value => String(value?.order_sn || value?.orderSn || "");
  const trackingKeys = ["tracking_number", "trackingNumber", "tracking_no", "waybill_no"];
  const shippingKeys = ["shipping", "logistics", "logistics_info", "shipping_info", "packages", "package_list", "express", "express_list"];
  function shipping(value, owner, result, depth = 0) {
    if (!value || typeof value !== "object" || depth > 8) return;
    if (Array.isArray(value)) { value.forEach(item => shipping(item, owner, result, depth + 1)); return; }
    if (idOf(value) && idOf(value) !== owner) return;
    for (const key of trackingKeys) {
      const number = String(value[key] || "").trim().toUpperCase();
      if (/^[A-Z0-9][A-Z0-9-]{5,31}$/.test(number) && number !== owner) result.add(number);
    }
    for (const key of shippingKeys) shipping(value[key], owner, result, depth + 1);
  }
  function capture(value) {
    let budget = 20000;
    function walk(node, depth = 0) {
      if (!node || typeof node !== "object" || depth > 15 || --budget < 0) return;
      const id = idOf(node);
      if (/^\d{6}-\d{15}$/.test(id)) {
        const numbers = new Set();
        shipping(node, id, numbers);
        const statusText = String(node.order_status_desc || node.status_desc || node.orderStatusDesc || "");
        // An order reference alone (e.g. a recommendation/link) proves nothing.
        if (numbers.size || statusText) {
          records.set(id, {order: id, tracking_number: [...numbers].join(";"),
            status_text: statusText, logistics_company: String(node.shipping_name || node.shippingName || "")});
          if (records.size > 500) records.delete(records.keys().next().value);
        }
      }
      Object.values(node).forEach(child => walk(child, depth + 1));
    }
    walk(value);
  }
  const nativeFetch = window.fetch;
  window.fetch = function (...args) {
    const promise = nativeFetch.apply(this, args);
    promise.then(response => {
      if (response.ok && /json/i.test(response.headers.get("content-type") || "")) {
        response.clone().json().then(capture).catch(() => {});
      }
    }).catch(() => {});
    return promise;
  };
  const nativeSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", () => {
      try {
        if (this.status >= 200 && this.status < 300 && /json/i.test(this.getResponseHeader("content-type") || "")) {
          capture(this.responseType === "json" ? this.response : JSON.parse(this.responseText));
        }
      } catch (_) {}
    }, {once: true});
    return nativeSend.apply(this, args);
  };
  document.addEventListener("zeshun-purchase-read", event => {
    const order = String(event.detail || "");
    if (!/^\d{6}-\d{15}$/.test(order)) return;
    if (!records.has(order)) capture(window.rawData);
    document.dispatchEvent(new CustomEvent("zeshun-purchase-result", {
      detail: JSON.stringify(records.get(order) || {order})
    }));
  });
})();
