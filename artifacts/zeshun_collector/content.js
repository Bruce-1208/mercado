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
      const product = await extractEligibleProduct();
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

  const dataCore = globalThis.ZeshunMercadoData;
  const pendingData = new Map();
  const dataCache = new Map();
  let dataTimer;
  let dataRunning = false;

  function queryProduct(sku, price) {
    const key = `${sku}:${price ?? ""}`;
    const cached = dataCache.get(key);
    if (cached && cached.until > Date.now()) return Promise.resolve(cached.value);
    if (pendingData.has(key)) return pendingData.get(key).promise;
    let resolve, reject;
    const promise = new Promise((a, b) => { resolve = a; reject = b; });
    pendingData.set(key, {key, sku, price, promise, resolve, reject, sent:false});
    clearTimeout(dataTimer);
    dataTimer = setTimeout(flushData, 80);
    return promise;
  }
  async function flushData() {
    if (dataRunning) return;
    const batch = [...pendingData.values()].filter(v => !v.sent).slice(0, 50);
    if (!batch.length) return;
    dataRunning = true;
    batch.forEach(v => {v.sent = true;});
    try {
      const response = await sendMessage({type:"GET_MERCADO_PRODUCT_INFO", items:batch.map(v => ({sku:v.sku, price:v.price}))});
      if (!response.ok) throw Object.assign(Error(response.error || "商品查询失败"), {status: response.status});
      for (const item of batch) {
        const record = response.items.find(v => v.sku === item.sku && (v.requested_price ?? null) === (item.price > 0 ? item.price : null));
        if (!record?.product) {item.reject(Error("数据源暂无该商品记录")); continue;}
        record.warnings = response.warnings || [];
        dataCache.set(item.key, {value:record, until:Date.now()+60000});
        item.resolve(record);
      }
      while (dataCache.size > 500) dataCache.delete(dataCache.keys().next().value);
    } catch (error) {batch.forEach(v => v.reject(error));}
    finally {
      batch.forEach(v => pendingData.delete(v.key));
      dataRunning = false;
      if (pendingData.size) dataTimer = setTimeout(flushData, 80);
    }
  }

  function nativeProduct() {
    // Native facts only; measurements come from the same cache as Zeshun cards.
    const product = core.extractProduct(document, location.href);
    const listing = dataCore.listingId(location.href);
    if (listing) {product.source_item_id = listing; product.source.id = listing;}
    return product;
  }
  async function extractServerProduct() {
    const product = nativeProduct();
    let record;
    try {
      record = await queryProduct(product.source_item_id, currentPrice(document) ?? product.price);
      dataCore.apply(product, record);
    } catch (error) {
      error.retryable = false;
      throw error;
    }
    if (product.plugin_snapshot.fulfillment_type === "unknown") throw Object.assign(Error("数据源尚未给出发货方式，为避免误采已停止"), {retryable:false});
    if (product.scrape_status !== "ok") throw Object.assign(Error(product.error_message), {retryable:false});
    return product;
  }

  function attachDataCard(host, sku, price, detail = false) {
    if (!sku) return;
    const key = `${sku}:${price ?? ""}`;
    const existing = host.querySelector(":scope > .zeshun-product-info");
    if (existing?.dataset.productKey === key) return;
    if (existing) existing.remove();
    const panel = document.createElement("section");
    panel.className = `zeshun-product-info${detail ? " zeshun-product-info--detail" : ""}`;
    panel.dataset.productKey = key;
    panel.setAttribute("aria-label", `泽顺 ERP · ${sku} 商品数据`);
    panel.addEventListener("click", event => {event.stopPropagation();});
    panel.addEventListener("keydown", event => event.stopPropagation());
    host.appendChild(panel);
    let loading = false;
    const showState = (message, retry = false) => {
      const state = document.createElement("div");
      state.className = "zs-info-state";
      const text = document.createElement("span");
      text.className = "zs-info-state-message";
      text.setAttribute("role", "status");
      text.textContent = message;
      state.appendChild(text);
      if (retry) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "zs-info-refresh";
        button.textContent = "重试";
        button.addEventListener("click", event => {event.preventDefault(); event.stopPropagation(); load();});
        state.appendChild(button);
      }
      panel.replaceChildren(state);
    };
    const load = async (refresh = false) => {
      if (loading || !panel.isConnected) return;
      loading = true;
      if (refresh) dataCache.delete(key);
      panel.setAttribute("aria-busy", "true");
      showState("泽顺 · 正在读取商品数据…");
      try {
        const record = await queryProduct(sku, price);
        if (!panel.isConnected) return;
        dataCore.render(document, panel, record, {onRefresh: () => load(true)});
      } catch (error) {
        if (panel.isConnected) showState(`泽顺 · ${error.message}`, true);
      } finally {
        loading = false;
        panel.setAttribute("aria-busy", "false");
      }
    };
    if (typeof IntersectionObserver === "function") {
      const observer = new IntersectionObserver(entries => {
        if (entries.some(v => v.isIntersecting)) {observer.disconnect(); load();}
      }, {rootMargin:"300px"});
      observer.observe(panel);
    } else load();
  }
  function listingCards() {
    const seen = new Set();
    const cards = [];
    for (const candidate of core.cardCandidates(document)) {
      // A result LI can wrap the native visual card. Use that card for both
      // collection and data so the yellow footer shares its background/border.
      const native = candidate.link.closest(".poly-card, .ui-search-result, [data-testid='result']");
      const card = native && candidate.card.contains(native) ? native : candidate.card;
      if (seen.has(card)) continue;
      seen.add(card);
      cards.push({...candidate, card});
    }
    return cards;
  }
  function listingDataHost(card) {
    // Horizontal results keep the image beside the product information. Mount
    // beneath that information rather than adding another horizontal column.
    const horizontal = card.matches(".poly-card--list, .poly-card--horizontal, .ui-search-result") ||
      card.closest(".ui-search-layout--list");
    return horizontal && card.querySelector(".poly-card__content, .ui-search-result__content, .ui-search-result__content-wrapper") || card;
  }
  function currentPrice(root) {
    const amounts = [...root.querySelectorAll(".poly-price__current .andes-money-amount, .ui-search-price__second-line .andes-money-amount, .ui-pdp-price__second-line .andes-money-amount")];
    const amount = amounts.find(v => !v.classList.contains("andes-money-amount--previous") && v.tagName !== "S");
    if (!amount) return null;
    const fraction = amount.querySelector(".andes-money-amount__fraction")?.textContent?.replace(/\D/g, "");
    const cents = amount.querySelector(".andes-money-amount__cents")?.textContent?.replace(/\D/g, "") || "00";
    return fraction ? Number(`${fraction}.${cents}`) : null;
  }
  function installDataCards() {
    if (looksLikeDetailPage()) {
      try {
        const product = nativeProduct();
        const host = document.querySelector(".ui-pdp-container__col.col-2") ||
          document.querySelector(".ui-pdp-container") || document.querySelector("main");
        if (host) attachDataCard(host, product.source_item_id, currentPrice(document) ?? product.price, true);
      } catch (_) {}
      return;
    }
    for (const candidate of listingCards()) {
      const sku = dataCore.listingId(candidate.url);
      candidate.card.classList.add("zeshun-collector-product-card");
      attachDataCard(listingDataHost(candidate.card), sku, currentPrice(candidate.card));
    }
  }

  async function extractEligibleProduct() {
    const product = await extractServerProduct();
    if (!product.plugin_snapshot.fulfillment_eligible) {
      throw new Error(`仅采集自发货和半托管；数据源识别为${product.plugin_snapshot.fulfillment_label || "非允许发货方式"}`);
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
    for (const candidate of listingCards()) {
      const existing = candidate.card.querySelector(":scope > .zeshun-card-collect");
      if (existing) {
        existing.dataset.collectionUrl = candidate.url;
        continue;
      }
      candidate.card.classList.add("zeshun-collector-card-host");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "zeshun-card-collect";
      button.dataset.collectionUrl = candidate.url;
      button.textContent = "采集";
      button.title = "在后台读取详情并采集到泽顺控制台";
      button.addEventListener("click", async event => {
        event.preventDefault();
        event.stopPropagation();
        const original = button.textContent;
        button.disabled = true;
        button.textContent = "采集中…";
        try {
          const detailTab = window.open(button.dataset.collectionUrl, "_blank", "noopener");
          if (!detailTab) throw new Error("浏览器拦截了详情页，请允许弹出窗口后重试");
          button.textContent = "已打开";
          showToast("正在打开详情页，采集时将以泽顺后台发货方式为准", "info");
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
    installDataCards();
  }

  const observer = new MutationObserver(() => {
    clearTimeout(mutationTimer);
    mutationTimer = setTimeout(refreshUi, 180);
  });
  observer.observe(document.documentElement, {childList: true, subtree: true});
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      document.querySelectorAll(".zeshun-product-info").forEach(node => node.remove());
      setTimeout(refreshUi, 300);
    }
  }, 700);

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message && ["READ_PRODUCT_LIST", "EXTRACT_BATCH_PRODUCT"].includes(message.type)) {
      try {
        const pageText = `${document.title} ${(document.body?.innerText || "").slice(0, 16000)}`;
        if (/too many requests|rate limit exceeded|demasiadas solicitudes|muitas requisições|访问过于频繁|请求过于频繁|HTTP\s*429/i.test(pageText)) {
          sendResponse({ok: false, blocked: true, status: 429, error: "美客多访问限频"});
        } else if (document.querySelector("form[action*='login'], #captcha, .g-recaptcha, [data-testid='captcha']") ||
            /\/(?:login|account-verification|challenge|captcha)(?:[/?]|$)/i.test(location.href)) {
          sendResponse({ok: false, blocked: true, error: "请先在前端页面完成登录或人机验证"});
        } else if (message.type === "EXTRACT_BATCH_PRODUCT") {
          if (!looksLikeDetailPage()) {
            sendResponse({ok: false, error: "等待商品详情加载"});
            return false;
          }
          Promise.resolve(extractServerProduct()).then(
            product => sendResponse({ok: true, product}),
            error => sendResponse({ok: false, error: error.message || String(error), retryable: error.retryable, status: error.status})
          );
          return true;
        } else {
          sendResponse(readProductList());
        }
      } catch (error) { sendResponse({ok: false, error: error.message || String(error)}); }
      return false;
    }
    if (message && message.type === "PING_PAGE") {
      sendResponse({ok: true, detail: looksLikeDetailPage(), url: location.href, data_source: "zeshun_server"});
      return false;
    }
    if (message && message.type === "EXTRACT_PRODUCT") {
      Promise.resolve().then(() => extractEligibleProduct()).then(
        product => sendResponse({ok: true, product}),
        error => sendResponse({ok: false, error: error.message || String(error), retryable: error.retryable, status: error.status})
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
