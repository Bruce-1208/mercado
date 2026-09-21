(function () {
  "use strict";

  const core = globalThis.ZeshunCollectorCore;
  if (!core) return;

  let lastUrl = location.href;
  let mutationTimer = null;

  function sendMessage(message) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(message, response => {
        const runtimeError = chrome.runtime.lastError;
        if (runtimeError) reject(new Error(runtimeError.message));
        else resolve(response || {});
      });
    });
  }

  function showToast(message, kind) {
    let toast = document.getElementById("zeshun-collector-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "zeshun-collector-toast";
      document.documentElement.appendChild(toast);
    }
    toast.className = `zeshun-collector-toast is-${kind || "info"}`;
    toast.textContent = message;
    toast.classList.add("is-visible");
    clearTimeout(toast._hideTimer);
    toast._hideTimer = setTimeout(() => toast.classList.remove("is-visible"), 4200);
  }

  async function collectCurrent(button) {
    const original = button ? button.textContent : "";
    try {
      if (button) {
        button.disabled = true;
        button.textContent = "正在读取…";
      }
      const product = extractEligibleProduct();
      if (button) button.textContent = "正在上传…";
      const response = await sendMessage({type: "SUBMIT_PRODUCT", product});
      if (!response.ok) throw new Error(response.error || "采集失败");
      const message = response.queued ? "控制台暂不可用，已加入待传队列" : "已采集到泽顺控制台";
      showToast(message, response.queued ? "warning" : "success");
      if (button) button.textContent = response.queued ? "已待传" : "已采集";
    } catch (error) {
      showToast(error.message || String(error), "error");
      if (button) button.textContent = "采集失败";
    } finally {
      if (button) {
        setTimeout(() => {
          button.disabled = false;
          button.textContent = original || "采集到泽顺";
        }, 1800);
      }
    }
  }

  function looksLikeDetailPage() {
    if (document.querySelector("h1.ui-pdp-title, .ui-pdp-container, [data-testid='vip-container']")) {
      return true;
    }
    // Mercado occasionally serves a new detail-page shell without the old
    // ui-pdp classes. The item URL plus a title/canonical marker is enough to
    // distinguish it from a search result page while the page is settling.
    let decodedUrl = location.href;
    try { decodedUrl = decodeURIComponent(decodedUrl); } catch (_) {}
    return /\b(?:ML[A-Z]|CBT)-?\d{5,}\b/i.test(decodedUrl) && Boolean(
      document.querySelector("h1, meta[property='og:title'], link[rel='canonical']")
    );
  }

  function extractZyingProduct() {
    const plugin = core.pluginLoginStatus(document);
    if (!plugin.logged_in) throw new Error(plugin.message);
    const product = core.extractProduct(document, location.href);
    if (!product.plugin_snapshot?.dom_lines?.length) throw new Error("等待智赢插件读取当前商品信息");
    if (product.plugin_snapshot.fulfillment_type === "unknown") throw new Error("智赢插件尚未给出发货方式，为避免误采已停止");
    return product;
  }

  function extractEligibleProduct() {
    const product = extractZyingProduct();
    if (!product.plugin_snapshot.fulfillment_eligible) {
      throw new Error(`仅采集自发货和半托管；智赢识别为${product.plugin_snapshot.fulfillment_label || "非允许发货方式"}`);
    }
    return product;
  }

  function installFloatingButton() {
    const existing = document.getElementById("zeshun-collector-floating");
    if (!looksLikeDetailPage()) {
      if (existing) existing.remove();
      return;
    }
    if (existing) return;
    const wrapper = document.createElement("div");
    wrapper.id = "zeshun-collector-floating";
    wrapper.className = "zeshun-collector-floating";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "zeshun-collector-primary";
    button.textContent = "采集到泽顺";
    button.title = "把当前商品保存到武汉泽顺综合服务台的商品采集列表";
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      collectCurrent(button);
    });
    wrapper.appendChild(button);
    document.documentElement.appendChild(wrapper);
  }

  function installCardButtons() {
    for (const candidate of core.cardCandidates(document)) {
      if (candidate.card.querySelector(":scope > .zeshun-card-collect")) continue;
      candidate.card.classList.add("zeshun-collector-card-host");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "zeshun-card-collect";
      button.textContent = "采集";
      button.title = "在后台读取详情并采集到泽顺控制台";
      button.addEventListener("click", async event => {
        event.preventDefault();
        event.stopPropagation();
        const original = button.textContent;
        button.disabled = true;
        button.textContent = "采集中…";
        try {
          const detailTab = window.open(candidate.url, "_blank", "noopener");
          if (!detailTab) throw new Error("浏览器拦截了详情页，请允许弹出窗口后重试");
          button.textContent = "已打开";
          showToast("正在打开详情页，采集时将以智赢发货方式为准", "info");
        } catch (error) {
          button.textContent = "重试";
          showToast(error.message || String(error), "error");
        } finally {
          setTimeout(() => {
            button.disabled = false;
            button.textContent = original;
          }, 2200);
        }
      });
      candidate.card.appendChild(button);
    }
  }

  function refreshUi() {
    installFloatingButton();
    installCardButtons();
  }

  const observer = new MutationObserver(() => {
    clearTimeout(mutationTimer);
    mutationTimer = setTimeout(refreshUi, 180);
  });
  observer.observe(document.documentElement, {childList: true, subtree: true});
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      setTimeout(refreshUi, 300);
    }
  }, 700);

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message && message.type === "CHECK_ZYING_PLUGIN") {
      sendResponse({ok: true, ...core.pluginLoginStatus(document)});
      return false;
    }
    if (message && ["READ_PRODUCT_LIST", "EXTRACT_BATCH_PRODUCT"].includes(message.type)) {
      try {
        if (document.querySelector("form[action*='login'], #captcha, .g-recaptcha, [data-testid='captcha']") ||
            /\/(?:login|account-verification|challenge|captcha)(?:[/?]|$)/i.test(location.href)) {
          sendResponse({ok: false, blocked: true, error: "请先在前端页面完成登录或人机验证"});
        } else if (message.type === "EXTRACT_BATCH_PRODUCT") {
          sendResponse(looksLikeDetailPage()
            ? {ok: true, product: extractZyingProduct()}
            : {ok: false, error: "等待商品详情加载"});
        } else {
          sendResponse(readProductList());
        }
      } catch (error) { sendResponse({ok: false, error: error.message || String(error)}); }
      return false;
    }
    if (message && message.type === "PING_PAGE") {
      sendResponse({ok: true, detail: looksLikeDetailPage(), url: location.href, zying: core.pluginLoginStatus(document)});
      return false;
    }
    if (message && message.type === "EXTRACT_PRODUCT") {
      Promise.resolve().then(() => extractEligibleProduct()).then(
        product => sendResponse({ok: true, product}),
        error => sendResponse({ok: false, error: error.message || String(error)})
      );
      return true;
    }
    return false;
  });

  function readProductList() {
    if (looksLikeDetailPage()) return {ok: false, error: "当前是商品详情页，请选择搜索列表页"};
    const cards = core.cardCandidates(document);
    const empty = document.querySelector(".ui-search-rescue, .ui-search-rescue__title");
    if (!cards.length && !empty) return {ok: false, error: "未识别到商品列表，请等待加载完成或先完成登录/验证"};
    const current = document.querySelector(".andes-pagination__button--current, [aria-current='page']");
    const pageText = current?.textContent?.trim() || "";
    const offset = location.pathname.match(/_Desde_(\d+)/i);
    const page = Number(pageText.match(/\d+/)?.[0] || (offset || new URL(location.href).searchParams.has("page") ? 0 : 1));
    const next = document.querySelector(".andes-pagination__button--next:not(.andes-pagination__button--disabled) a[href], a[rel='next']");
    const links = [...document.querySelectorAll(".andes-pagination a[href]")];
    const first = links.find(link => link.textContent.trim() === "1");
    const safeLink = link => link && core.isSupportedUrl(link.href) ? link.href : "";
    let decodedUrl = location.href;
    try { decodedUrl = decodeURIComponent(decodedUrl); } catch (_) {}
    const internationalSelected = /SHIPPING(?:\*|_)?ORIGIN(?:_|=)10215069/i.test(decodedUrl) ||
      [...document.querySelectorAll("[aria-current='true'], .andes-list__item--selected, .ui-search-filter-name")]
        .some(node => /internacional/i.test(node.textContent || ""));
    return {
      ok: true, page, next_url: safeLink(next), first_url: safeLink(first),
      international_selected: internationalSelected,
      items: cards.map(item => {
        const profile = core.cardShippingProfile(item.card, item.url);
        return {
          url: item.url,
          key: core.normalizeItemId(item.url) || item.url.split("#")[0],
          origin: profile.origin,
          managed: profile.managed,
          eligible: profile.eligible,
          isUsOrigin: profile.origin === "US",
          isChinaOrigin: profile.origin === "CN",
          isManaged: profile.managed
        };
      })
    };
  }

  refreshUi();
})();
