(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.ZeshunCollectorCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const ITEM_PATTERN = /\b(ML[A-Z]|CBT)-?(\d{5,})\b/i;
  const SUPPORTED_HOST = /(^|\.)(mercadolibre\.com\.(mx|br|ar|co|uy)|mercadolibre\.cl|mercadolivre\.com\.br)$/i;
  const CURRENCY_BY_HOST = {
    "mercadolibre.com.mx": "MXN",
    "mercadolibre.com.br": "BRL",
    "mercadolivre.com.br": "BRL",
    "mercadolibre.com.ar": "ARS",
    "mercadolibre.cl": "CLP",
    "mercadolibre.com.co": "COP",
    "mercadolibre.com.uy": "UYU"
  };

  function clean(value) {
    return String(value == null ? "" : value).replace(/\s+/g, " ").trim();
  }

  function unique(values) {
    return Array.from(new Set((values || []).filter(Boolean)));
  }

  function finiteNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    let raw = String(value).replace(/[^0-9,.-]/g, "").trim();
    if (!raw) return null;
    const comma = raw.lastIndexOf(",");
    const dot = raw.lastIndexOf(".");
    if (comma >= 0 && dot >= 0) {
      const decimal = comma > dot ? "," : ".";
      const thousands = decimal === "," ? /\./g : /,/g;
      raw = raw.replace(thousands, "").replace(decimal, ".");
    } else if (comma >= 0) {
      const tail = raw.length - comma - 1;
      raw = tail > 0 && tail <= 2 ? raw.replace(",", ".") : raw.replace(/,/g, "");
    } else if (dot >= 0) {
      const tail = raw.length - dot - 1;
      if (tail === 3 && raw.indexOf(".") === dot) raw = raw.replace(".", "");
    }
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function normalizeItemId(value) {
    const decoded = (() => {
      try { return decodeURIComponent(String(value || "")); } catch (_) { return String(value || ""); }
    })();
    const match = ITEM_PATTERN.exec(decoded);
    return match ? `${match[1].toUpperCase()}${match[2]}` : "";
  }

  function meta(doc, selector) {
    const node = doc && doc.querySelector ? doc.querySelector(selector) : null;
    return clean(node && (node.content || node.getAttribute("content")));
  }

  function firstNode(doc, selectors) {
    if (!doc || !doc.querySelector) return null;
    for (const selector of selectors) {
      const node = doc.querySelector(selector);
      if (node) return node;
    }
    return null;
  }

  function structuredProducts(doc) {
    const products = [];
    if (!doc || !doc.querySelectorAll) return products;
    for (const script of doc.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const parsed = JSON.parse(script.textContent || "null");
        const queue = Array.isArray(parsed) ? parsed.slice() : [parsed];
        while (queue.length) {
          const value = queue.shift();
          if (!value || typeof value !== "object") continue;
          if (Array.isArray(value)) {
            queue.push(...value);
            continue;
          }
          const types = Array.isArray(value["@type"]) ? value["@type"] : [value["@type"]];
          if (types.includes("Product")) products.push(value);
          if (Array.isArray(value["@graph"])) queue.push(...value["@graph"]);
        }
      } catch (_) {
        // Broken third-party JSON-LD should not block collection.
      }
    }
    return products;
  }

  function extractItemId(doc, pageUrl, product) {
    const candidates = [];
    try {
      const url = new URL(pageUrl);
      for (const key of ["item_id", "itemId", "wid"]) candidates.push(url.searchParams.get(key));
    } catch (_) {}
    if (doc && doc.querySelectorAll) {
      for (const node of doc.querySelectorAll("[data-item-id], [data-itemid], [data-id]")) {
        candidates.push(node.getAttribute("data-item-id"));
        candidates.push(node.getAttribute("data-itemid"));
        const dataId = node.getAttribute("data-id");
        if (ITEM_PATTERN.test(dataId || "")) candidates.push(dataId);
      }
    }
    candidates.push(product && product.sku, product && product.productID);
    candidates.push(meta(doc, 'meta[property="og:url"]'));
    const canonical = doc && doc.querySelector ? doc.querySelector('link[rel="canonical"]') : null;
    candidates.push(canonical && canonical.href, pageUrl);
    for (const value of candidates) {
      const itemId = normalizeItemId(value);
      if (itemId) return itemId;
    }
    if (doc && doc.documentElement) {
      const html = String(doc.documentElement.innerHTML || "");
      const explicit = /(?:item_id|itemId|wid)["'\s:=\\/]+((?:ML[A-Z]|CBT)-?\d{5,})/i.exec(html);
      if (explicit) return normalizeItemId(explicit[1]);
    }
    return "";
  }

  function imageUrl(image) {
    if (!image) return "";
    let value = clean(
      image.currentSrc || image.getAttribute("data-src") || image.getAttribute("data-zoom") ||
      image.getAttribute("src")
    );
    if (!value || value.startsWith("data:") || value.startsWith("blob:")) return "";
    if (value.startsWith("//")) value = `https:${value}`;
    return value;
  }

  function extractImages(doc, product) {
    const values = [];
    const add = value => {
      let url = clean(value);
      if (!url || url.startsWith("data:") || url.startsWith("blob:")) return;
      if (url.startsWith("//")) url = `https:${url}`;
      values.push(url);
    };
    const structured = Array.isArray(product && product.image) ? product.image : [product && product.image];
    structured.forEach(value => add(typeof value === "object" ? value.url : value));
    add(meta(doc, 'meta[property="og:image"]'));
    if (doc && doc.querySelectorAll) {
      doc.querySelectorAll(".ui-pdp-gallery img, figure img, img.ui-pdp-image").forEach(img => add(imageUrl(img)));
    }
    return unique(values).slice(0, 24);
  }

  function extractSpecs(doc) {
    const rows = [];
    if (!doc || !doc.querySelectorAll) return rows;
    doc.querySelectorAll(
      ".andes-table__row, .ui-pdp-specs__table tr, .ui-vpp-striped-specs__row"
    ).forEach(row => {
      const cells = Array.from(row.querySelectorAll(
        "th, td, .andes-table__header, .andes-table__column, .ui-vpp-striped-specs__row__column"
      )).map(node => clean(node.textContent)).filter(Boolean);
      if (cells.length >= 2) rows.push({name: cells[0], value: cells.slice(1).join(" ")});
    });
    const seen = new Set();
    return rows.filter(row => {
      const key = `${row.name}\n${row.value}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).slice(0, 200);
  }

  function convertWeight(value, unit) {
    const number = finiteNumber(value);
    if (number === null) return null;
    return /^(kg|公斤|千克)$/i.test(clean(unit)) ? number * 1000 : number;
  }

  function convertLength(value, unit) {
    const number = finiteNumber(value);
    if (number === null) return null;
    const normalized = clean(unit).toLowerCase();
    if (normalized === "mm") return number / 10;
    if (normalized === "m") return number * 100;
    return number;
  }

  function parsePluginMetrics(text) {
    const normalized = clean(text);
    const result = {
      weight_g: null,
      volumetric_weight_kg: null,
      package_length_cm: null,
      package_width_cm: null,
      package_height_cm: null,
      dimensions_display: "",
      weight_display: "",
      volumetric_display: ""
    };
    const dimensions = /(?:尺寸|长\s*[x×*]\s*宽\s*[x×*]\s*高|dimensiones?|dimensões?)?[^\d]{0,24}(\d+(?:[.,]\d+)?)\s*[x×*]\s*(\d+(?:[.,]\d+)?)\s*[x×*]\s*(\d+(?:[.,]\d+)?)\s*(mm|cm|m)?\b/i.exec(normalized);
    if (dimensions) {
      const unit = dimensions[4] || "cm";
      result.package_length_cm = convertLength(dimensions[1], unit);
      result.package_width_cm = convertLength(dimensions[2], unit);
      result.package_height_cm = convertLength(dimensions[3], unit);
      result.dimensions_display = `${dimensions[1]} × ${dimensions[2]} × ${dimensions[3]} ${unit}`;
      if ([result.package_length_cm, result.package_width_cm, result.package_height_cm].every(value => value !== null)) {
        result.volumetric_weight_kg = Number((
          result.package_length_cm * result.package_width_cm * result.package_height_cm / 6000
        ).toFixed(4));
      }
    }
    const volumetric = /(?:计\s*抛|抛\s*重|体积\s*重(?:量)?|peso\s+volum[eé]trico)[^\d]{0,24}(\d+(?:[.,]\d+)?)\s*(kg|公斤|千克|g|克)\b/i.exec(normalized);
    if (volumetric) {
      const grams = convertWeight(volumetric[1], volumetric[2]);
      if (grams !== null) result.volumetric_weight_kg = grams / 1000;
      result.volumetric_display = `${volumetric[1]} ${volumetric[2]}`;
    }
    const withoutVolume = normalized.replace(/(?:计\s*抛|抛\s*重|体积\s*重(?:量)?|peso\s+volum[eé]trico)[^\d]{0,24}\d+(?:[.,]\d+)?\s*(?:kg|公斤|千克|g|克)/ig, " ");
    const weight = /(?:商品\s*)?(?:重量|毛重|净重|peso(?:\s+bruto|\s+neto)?)[^\d]{0,24}(\d+(?:[.,]\d+)?)\s*(kg|公斤|千克|g|克)\b/i.exec(withoutVolume);
    if (weight) {
      result.weight_g = convertWeight(weight[1], weight[2]);
      result.weight_display = `${weight[1]} ${weight[2]}`;
    }
    return result;
  }

  function shadowRoots(doc) {
    const roots = [];
    const visited = new Set();
    const visit = root => {
      if (!root || visited.has(root) || !root.querySelectorAll) return;
      visited.add(root);
      roots.push(root);
      root.querySelectorAll("*").forEach(node => {
        if (node.shadowRoot) visit(node.shadowRoot);
      });
    };
    visit(doc);
    return roots;
  }

  function readPluginMetrics(doc) {
    const lines = [];
    const seen = new Set();
    const add = value => {
      const text = clean(value);
      if (text && text.length <= 1200 && !seen.has(text)) {
        seen.add(text);
        lines.push(text);
      }
    };
    for (const root of shadowRoots(doc)) {
      root.querySelectorAll(
        ".zying-meli-detail-metric-line, .zying-meli-detail-metric-column, [class*='zying-meli-detail']"
      ).forEach(node => add(node.innerText || node.textContent));
    }
    const text = lines.join(" ");
    const metrics = parsePluginMetrics(text);
    return {lines: lines.slice(0, 50), text: text.slice(0, 12000), metrics};
  }

  function currencyForUrl(pageUrl) {
    try {
      const host = new URL(pageUrl).hostname.toLowerCase().replace(/^www\./, "");
      return CURRENCY_BY_HOST[host] || Object.entries(CURRENCY_BY_HOST)
        .find(([domain]) => host.endsWith(`.${domain}`))?.[1] || "";
    } catch (_) {
      return "";
    }
  }

  function pagePrice(doc, product) {
    const offer = Array.isArray(product && product.offers) ? product.offers[0] : (product && product.offers) || {};
    const metaPrice = meta(doc, 'meta[itemprop="price"]') || meta(doc, 'meta[property="product:price:amount"]');
    const previous = firstNode(doc, [
      ".ui-pdp-price__original-value .andes-money-amount",
      ".ui-pdp-price__second-line .andes-money-amount--previous",
      ".andes-money-amount--previous",
      "s.andes-money-amount"
    ]);
    const amount = previous || firstNode(doc, [
      ".ui-pdp-price__second-line .andes-money-amount",
      ".ui-pdp-price .andes-money-amount"
    ]);
    let visible = "";
    if (amount) {
      const fraction = amount.querySelector(".andes-money-amount__fraction");
      const cents = amount.querySelector(".andes-money-amount__cents");
      visible = clean(fraction && fraction.textContent).replace(/\D/g, "");
      if (visible && cents) visible += `.${clean(cents.textContent).replace(/\D/g, "")}`;
    }
    return finiteNumber(previous ? visible : (metaPrice || offer.price || visible));
  }

  function nowSql() {
    const now = new Date();
    const pad = value => String(value).padStart(2, "0");
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
      `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  }

  function isSupportedUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === "https:" && SUPPORTED_HOST.test(url.hostname);
    } catch (_) {
      return false;
    }
  }

  function extractProduct(doc, pageUrl) {
    if (!isSupportedUrl(pageUrl)) throw new Error("当前页面不是支持的 Mercado Libre 页面");
    const product = structuredProducts(doc)[0] || {};
    const itemId = extractItemId(doc, pageUrl, product);
    if (!itemId) throw new Error("当前页面未识别到 Mercado Libre 商品编号，请打开商品详情页后重试");
    const titleNode = firstNode(doc, ["h1.ui-pdp-title", "h1"]);
    const title = clean((titleNode && titleNode.textContent) || product.name || meta(doc, 'meta[property="og:title"]'));
    if (!title) throw new Error("当前页面未识别到商品标题，请等待详情页加载完成后重试");
    const descriptionNode = firstNode(doc, [
      ".ui-pdp-description__content", "[data-testid='description-content']", ".ui-pdp-description"
    ]);
    const description = clean((descriptionNode && descriptionNode.textContent) || product.description);
    const pictures = extractImages(doc, product);
    const specs = extractSpecs(doc);
    const plugin = readPluginMetrics(doc);
    const metrics = plugin.metrics;
    let weightBasis = "plugin_actual";
    if (metrics.weight_g === null && metrics.volumetric_weight_kg !== null && [
      metrics.package_length_cm, metrics.package_width_cm, metrics.package_height_cm
    ].every(value => value !== null)) {
      metrics.weight_g = metrics.volumetric_weight_kg * 1000;
      weightBasis = "plugin_volumetric_fallback";
    }
    const price = pagePrice(doc, product);
    const offer = Array.isArray(product.offers) ? product.offers[0] : (product.offers || {});
    const currencyId = clean(
      meta(doc, 'meta[itemprop="priceCurrency"]') ||
      meta(doc, 'meta[property="product:price:currency"]') || offer.priceCurrency || currencyForUrl(pageUrl)
    ).toUpperCase();
    const canonical = doc.querySelector('link[rel="canonical"]');
    const finalUrl = clean((canonical && canonical.href) || meta(doc, 'meta[property="og:url"]') || pageUrl);
    const completeMeasurements = [
      metrics.weight_g, metrics.package_length_cm, metrics.package_width_cm, metrics.package_height_cm
    ].every(value => value !== null && value > 0);
    const errors = [];
    if (!pictures.length) errors.push("未识别到商品主图");
    if (!plugin.lines.length) errors.push("未读取到智赢重量尺寸，可在泽顺控制台后续补充");
    else if (!completeMeasurements) errors.push("已检测到智赢浮层，但重量尺寸不完整");
    const complete = Boolean(title && pictures.length && completeMeasurements);
    const source = {
      id: itemId,
      site_id: itemId.slice(0, 3),
      title,
      price,
      currency_id: currencyId,
      condition: "new",
      available_quantity: 1,
      permalink: finalUrl,
      pictures: pictures.map(url => ({source: url})),
      attributes: specs.map(row => ({name: row.name, value_name: row.value})),
      variations: [],
      sale_terms: []
    };
    return {
      source_item_id: itemId,
      source_url: pageUrl,
      final_url: finalUrl,
      main_image_url: pictures[0] || "",
      title,
      price,
      currency_id: currencyId,
      weight_g: metrics.weight_g,
      volumetric_weight_kg: metrics.volumetric_weight_kg,
      package_length_cm: metrics.package_length_cm,
      package_width_cm: metrics.package_width_cm,
      package_height_cm: metrics.package_height_cm,
      weight_basis: weightBasis,
      scrape_status: complete ? "ok" : "partial",
      error_message: errors.join("；"),
      source,
      description: {plain_text: description},
      page_snapshot: {
        page_title: clean(doc.title),
        specs,
        pictures,
        browser: "zeshun_browser_extension"
      },
      plugin_snapshot: {
        source: "浏览器商品详情页与智赢插件浮层",
        read_method: "browser_extension_shadow_dom",
        dom_lines: plugin.lines,
        dom_text: plugin.text,
        weight_basis: weightBasis,
        dimensions_display: metrics.dimensions_display,
        weight_display: metrics.weight_display,
        plugin_volumetric_display: metrics.volumetric_display,
        volumetric_formula: "length_cm * width_cm * height_cm / 6000",
        volumetric_weight_kg: metrics.volumetric_weight_kg
      },
      collected_at: nowSql()
    };
  }

  function cardCandidates(doc) {
    if (!doc || !doc.querySelectorAll) return [];
    const selectors = [
      "li.ui-search-layout__item", ".ui-search-result", ".poly-card", "[data-testid='result']"
    ];
    const cards = unique(selectors.flatMap(selector => Array.from(doc.querySelectorAll(selector))));
    return cards.map(card => {
      const link = card.querySelector(
        "a.poly-component__title, a.ui-search-link, a[href*='item_id='], a[href*='itemId='], " +
        "a[href*='wid='], a[href*='/p/ML'], a[href*='/MLM-'], " +
        "a[href*='/MLB-'], a[href*='/MLA-'], a[href*='/MLC-'], a[href*='/MCO-'], a[href*='/MLU-']"
      );
      return link && isSupportedUrl(link.href) ? {card, link, url: link.href} : null;
    }).filter(Boolean);
  }

  function extractCardProduct(card, pageUrl) {
    if (!card || !isSupportedUrl(pageUrl)) throw new Error("未识别到可采集的商品卡片");
    let decodedUrl = String(pageUrl || "");
    try { decodedUrl = decodeURIComponent(decodedUrl); } catch (_) {}
    const explicit = /(?:item_id|itemId|wid)\s*[:=]\s*((?:ML[A-Z]|CBT)-?\d{5,})/i.exec(decodedUrl);
    const itemId = normalizeItemId(explicit ? explicit[1] : decodedUrl);
    if (!itemId) throw new Error("商品卡片中没有可识别的商品编号");
    const link = card.querySelector(
      "a.poly-component__title, a.ui-search-link, a[href*='item_id='], a[href*='wid='], a[href]"
    );
    const titleNode = card.querySelector(
      ".poly-component__title, .ui-search-item__title, h2, h3"
    );
    const title = clean((titleNode && titleNode.textContent) || (link && link.textContent));
    if (!title) throw new Error("商品卡片中没有可识别的标题");
    const image = card.querySelector("img");
    const mainImage = imageUrl(image);
    const originalPrice = card.querySelector(
      ".andes-money-amount--previous, .ui-search-price__original-value .andes-money-amount, s.andes-money-amount"
    );
    const currentPrice = card.querySelector(
      ".poly-price__current .andes-money-amount, .ui-search-price__second-line .andes-money-amount, .andes-money-amount"
    );
    const amount = originalPrice || currentPrice;
    const fraction = amount && amount.querySelector(".andes-money-amount__fraction");
    const cents = amount && amount.querySelector(".andes-money-amount__cents");
    let priceText = clean(fraction && fraction.textContent).replace(/\D/g, "");
    if (priceText && cents) priceText += `.${clean(cents.textContent).replace(/\D/g, "")}`;
    const price = finiteNumber(priceText);
    const currencyId = currencyForUrl(pageUrl);
    const pictures = mainImage ? [mainImage] : [];
    return {
      source_item_id: itemId,
      source_url: pageUrl,
      final_url: pageUrl,
      main_image_url: mainImage,
      title,
      price,
      currency_id: currencyId,
      weight_g: null,
      volumetric_weight_kg: null,
      package_length_cm: null,
      package_width_cm: null,
      package_height_cm: null,
      weight_basis: "card_quick_collect",
      scrape_status: "partial",
      error_message: "列表页快速采集：未打开详情页，描述、规格及重量尺寸待补充",
      source: {
        id: itemId,
        site_id: itemId.slice(0, 3),
        title,
        price,
        currency_id: currencyId,
        condition: "new",
        available_quantity: 1,
        permalink: pageUrl,
        pictures: pictures.map(url => ({source: url})),
        attributes: [],
        variations: [],
        sale_terms: []
      },
      description: {plain_text: ""},
      page_snapshot: {
        page_title: clean(card.ownerDocument && card.ownerDocument.title),
        specs: [],
        pictures,
        browser: "zeshun_browser_extension_card"
      },
      plugin_snapshot: {
        source: "Mercado Libre 商品列表卡片",
        read_method: "browser_extension_card_no_navigation"
      },
      collected_at: nowSql()
    };
  }

  return {
    clean,
    finiteNumber,
    normalizeItemId,
    parsePluginMetrics,
    isSupportedUrl,
    extractProduct,
    cardCandidates,
    extractCardProduct
  };
});
