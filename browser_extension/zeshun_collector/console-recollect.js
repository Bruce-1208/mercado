"use strict";

// The background verifies the configured console origin and product ownership.
window.addEventListener("message", event => {
  if (event.source !== window || event.origin !== location.origin || event.data?.type !== "ZESHUN_RECOLLECT") return;
  const {requestId, productId} = event.data;
  if (typeof requestId !== "string" || requestId.length > 80 || !Number.isSafeInteger(productId) || productId <= 0) return;
  const reply = data => window.postMessage({type: "ZESHUN_RECOLLECT_RESULT", requestId, ...data}, location.origin);
  reply({accepted: true});
  try {
    chrome.runtime.sendMessage({type: "RECOLLECT_ORIGINAL_PRODUCT", productId}, result => {
      const error = chrome.runtime.lastError;
      reply({result: error ? {ok: false, error: "插件连接已失效，请重新加载插件并刷新工作台"} : result});
    });
  } catch (_) { reply({result: {ok: false, error: "插件连接已失效，请重新加载插件并刷新工作台"}}); }
});
