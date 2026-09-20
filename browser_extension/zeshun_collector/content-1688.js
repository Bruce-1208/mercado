"use strict";

(function () {
  function text(selector) {
    const node = document.querySelector(selector);
    return String(node?.textContent || node?.getAttribute?.("content") || "").replace(/\s+/g, " ").trim();
  }

  function itemId() {
    const match = location.href.match(/\/offer\/(\d{5,})\.html/i)
      || location.href.match(/[?&](?:offerId|offer_id|itemId)=(\d{5,})/i);
    return match ? match[1] : "";
  }

  function absoluteImage(value) {
    let url = String(value || "").trim().replace(/&amp;/g, "&");
    if (url.startsWith("//")) url = `https:${url}`;
    url = url.replace(/\.webp(?:_[^?]+)?(?=\?|$)/i, ".jpg");
    return /^https?:\/\//i.test(url) ? url : "";
  }

  function collectImages() {
    const result = [];
    const add = value => {
      const url = absoluteImage(value);
      if (!url || /(?:icon|logo|avatar|sprite)/i.test(url) || result.includes(url)) return;
      result.push(url);
    };
    document.querySelectorAll([
      ".detail-gallery img", ".img-list img", ".main-img img", ".od-gallery img",
      "img[src*='alicdn']", "img[data-src*='alicdn']"
    ].join(",")).forEach(image => {
      add(image.currentSrc || image.src || image.dataset.src || image.dataset.lazyloadSrc);
    });
    document.querySelectorAll("script").forEach(script => {
      const source = String(script.textContent || "");
      if (!source.includes("alicdn")) return;
      const matches = source.match(/https?:\\?\/\\?\/[^"'\\\s]+?\.(?:jpg|jpeg|png|webp)(?:[^"'\\\s]*)?/gi) || [];
      matches.slice(0, 80).forEach(value => add(value.replaceAll("\\/", "/")));
    });
    return result.slice(0, 20);
  }

  function collectProperties() {
    const rows = [];
    const seen = new Set();
    const add = (name, value) => {
      name = String(name || "").replace(/\s+/g, " ").trim();
      value = String(value || "").replace(/\s+/g, " ").trim();
      const key = `${name}:${value}`;
      if (!name || !value || seen.has(key)) return;
      seen.add(key);
      rows.push({name: name.slice(0, 120), value: value.slice(0, 500)});
    };
    document.querySelectorAll(".od-pc-attribute-list li, .detail-attributes li, .offer-attr-item, [class*='attribute'] li").forEach(row => {
      const children = row.querySelectorAll("span, div");
      if (children.length >= 2) add(children[0].textContent, children[children.length - 1].textContent);
      else {
        const parts = String(row.textContent || "").split(/[：:]/, 2);
        if (parts.length === 2) add(parts[0], parts[1]);
      }
    });
    document.querySelectorAll("dt").forEach(node => add(node.textContent, node.nextElementSibling?.textContent));
    return rows.slice(0, 100);
  }

  function numericPrice() {
    const raw = text("[class*='price']") || text("meta[property='og:price:amount']");
    const match = raw.replace(/,/g, "").match(/\d+(?:\.\d+)?/);
    return match ? Number(match[0]) : null;
  }

  function metricValue(content, pattern, multiplier) {
    const match = content.match(pattern);
    return match ? Math.round(Number(match[1]) * multiplier * 100) / 100 : null;
  }

  function extractProduct() {
    const sourceItemId = itemId();
    const title = text("h1") || text("meta[property='og:title']") || document.title.replace(/[-_].*1688.*$/i, "").trim();
    if (!sourceItemId || !title) throw new Error("未识别到 1688 商品编号或标题，请打开商品详情页后重试");
    const images = collectImages();
    const properties = collectProperties();
    const bodyText = document.body?.innerText || "";
    const descriptionNode = document.querySelector("#desc-lazyload-container, .detail-desc-module, [class*='description']");
    const description = String(descriptionNode?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 30000);
    return {
      source_platform: "1688",
      source_item_id: sourceItemId,
      source_url: location.href,
      final_url: location.href,
      title,
      price: numericPrice(),
      currency_id: "CNY",
      main_image_url: images[0] || text("meta[property='og:image']"),
      images,
      properties,
      description_text: description,
      weight_g: metricValue(bodyText, /(?:重量|毛重)[：:\s]*([\d.]+)\s*(kg|千克|公斤)/i, 1000)
        || metricValue(bodyText, /(?:重量|毛重)[：:\s]*([\d.]+)\s*(?:g|克)/i, 1),
      collected_at: new Date().toISOString()
    };
  }

  function showToast(message, kind = "success") {
    let toast = document.querySelector(".zeshun-collector-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.className = "zeshun-collector-toast";
      document.documentElement.appendChild(toast);
    }
    toast.textContent = message;
    toast.className = `zeshun-collector-toast is-${kind}`;
    requestAnimationFrame(() => toast.classList.add("is-visible"));
    window.clearTimeout(Number(toast.dataset.timer || 0));
    toast.dataset.timer = String(window.setTimeout(() => toast.classList.remove("is-visible"), 3200));
  }

  async function collectFromPage(button) {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "正在采集…";
    try {
      const response = await chrome.runtime.sendMessage({type: "SUBMIT_PRODUCT", product: extractProduct()});
      if (!response?.ok) throw new Error(response?.error || "采集失败");
      showToast(response.queued ? "控制台暂不可用，商品已加入待传队列" : "已采集到 AI 原创产品", response.queued ? "warning" : "success");
      button.textContent = "采集成功 ✓";
      window.setTimeout(() => { button.textContent = original; }, 1800);
    } catch (error) {
      showToast(error.message || String(error), "error");
      button.textContent = original;
    } finally {
      button.disabled = false;
    }
  }

  function mountCollectorButton() {
    if (!itemId() || document.querySelector(".zeshun-collector-floating")) return;
    const host = document.createElement("div");
    host.className = "zeshun-collector-floating";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "zeshun-collector-primary";
    button.textContent = "采集到泽顺";
    button.addEventListener("click", () => collectFromPage(button));
    host.appendChild(button);
    document.documentElement.appendChild(host);
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "PING_PAGE") {
      sendResponse({ok: true, detail: Boolean(itemId()), platform: "1688"});
      return;
    }
    if (message?.type === "EXTRACT_PRODUCT") {
      try { sendResponse({ok: true, product: extractProduct()}); }
      catch (error) { sendResponse({ok: false, error: error.message || String(error)}); }
    }
  });

  mountCollectorButton();
})();
