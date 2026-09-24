"use strict";

(function () {
  // A tab can keep the previous isolated-world content script after the
  // extension is reloaded.  Expose a version marker so the background worker
  // can reload that tab before starting a new search instead of silently
  // running stale search logic alongside this copy.
  const CONTENT_VERSION = "1.8.19";
  const previousListener = globalThis.__zeshun1688ContentListener;
  if (globalThis.__zeshun1688ContentVersion === CONTENT_VERSION && previousListener &&
      chrome.runtime.onMessage.hasListener?.(previousListener)) return;
  if (previousListener) {
    try { chrome.runtime.onMessage.removeListener(previousListener); } catch (_) {}
  }
  let mutationTimer = null;

  function text(selector) {
    const node = document.querySelector(selector);
    return String(node?.textContent || node?.getAttribute?.("content") || "").replace(/\s+/g, " ").trim();
  }

  function itemId() {
    const match = location.href.match(/\/offer\/(\d{5,})\.html/i)
      || location.href.match(/[?&](?:offerId|offer_id|itemId)=(\d{5,})/i);
    return match ? match[1] : "";
  }

  function itemIdFromValue(value) {
    const source = String(value || "");
    const match = source.match(/\/offer\/(\d{5,})(?:\.html)?/i)
      || source.match(/[?&](?:offerId|offer_id|itemId)=(\d{5,})/i)
      || source.match(/(?:object_id@|offer_)(\d{5,})/i)
      || source.match(/_(\d{5,})$/);
    return match ? match[1] : "";
  }

  function absoluteUrl(value) {
    const source = String(value || "").trim();
    if (!source) return "";
    try { return new URL(source, location.href).href; }
    catch (_) { return ""; }
  }

  function absoluteImage(value) {
    let url = String(value || "").trim().replace(/&amp;/g, "&");
    if (url.startsWith("//")) url = `https:${url}`;
    if (!/^https?:\/\//i.test(url)) return "";
    try {
      const parsed = new URL(url);
      if (/(^|\.)alicdn\.com$/i.test(parsed.hostname)) {
        parsed.pathname = parsed.pathname.replace(/\.(jpe?g|png|webp)(?:_[^/?#]*)+$/i, ".$1");
        return parsed.href;
      }
    } catch (_) {}
    return url;
  }

  function collectImages() {
    const result = [];
    const add = value => {
      const url = absoluteImage(value);
      if (!url || /(?:icon|logo|avatar|sprite|\.svg(?:[?#]|$))/i.test(url) || result.includes(url)) return;
      result.push(url);
    };
    document.querySelectorAll([
      "#gallery img.preview-img", "#gallery .thumbnail img", "#gallery img",
      ".detail-gallery img", ".od-gallery img", ".mod-detail-gallery img"
    ].join(",")).forEach(image => {
      add(image.dataset.src || image.dataset.lazyloadSrc || image.currentSrc || image.src);
    });
    if (!result.length) add(document.querySelector("meta[property='og:image']")?.content);
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
    document.querySelectorAll("#productAttributes li, .od-pc-attribute-list li, .detail-attributes li").forEach(row => {
      const children = row.querySelectorAll("span, div");
      if (children.length >= 2) add(children[0].textContent, children[children.length - 1].textContent);
      else {
        const parts = String(row.textContent || "").split(/[：:]/, 2);
        if (parts.length === 2) add(parts[0], parts[1]);
      }
    });
    document.querySelectorAll("#productAttributes dt, .detail-attributes dt, .od-pc-attribute-list dt").forEach(node => add(node.textContent, node.nextElementSibling?.textContent));
    return rows.slice(0, 100);
  }

  function numericPrice(skus) {
    const prices = skus.map(row => row.price).filter(value => Number.isFinite(value) && value > 0);
    if (prices.length) return Math.min(...prices);
    const raw = text("#productPrice [class*='price']") || text("#productPrice")
      || text("meta[property='og:price:amount']");
    const match = raw.replace(/,/g, "").match(/(?:¥|￥)\s*(\d+(?:\.\d+)?)/)
      || raw.trim().match(/^(\d+(?:\.\d+)?)$/);
    return match ? Number(match[1]) : null;
  }

  // Keep supplier detail extraction in one place.  The AI weight/price flow
  // reads these values from the React SKU and package modules because the new
  // 1688 shell no longer renders every fact as ordinary page text.  Product
  // collection must use the same evidence, otherwise the two entry points
  // persist different weight/dimension results for the same offer.
  function aiWeightPriceNumber(value) {
    const match = String(value || "").replace(/,/g, "").match(/-?\d+(?:\.\d+)?/);
    return match ? Number(match[0]) : null;
  }

  function aiWeightPriceDecode(value) {
    const box = document.createElement("textarea");
    box.innerHTML = String(value || "");
    return String(box.value || "").replace(/>/g, " / ").replace(/\s+/g, " ").trim();
  }

  function aiWeightPriceVariationAttributes(label) {
    return String(label || "").split(/\s*\/\s*/).map((part, index) => {
      const pieces = part.split(/[：:]/, 2).map(value => value.trim());
      const name = pieces.length > 1 ? pieces[0] : `规格${index + 1}`;
      const value_name = pieces.length > 1 ? pieces[1] : pieces[0];
      return name && value_name ? {name, value_name} : null;
    }).filter(Boolean).slice(0, 20);
  }

  function aiWeightPriceConvertWeight(value, unit = "g") {
    const number = aiWeightPriceNumber(value);
    if (!Number.isFinite(number) || number <= 0) return null;
    const normalized = String(unit || "g").toLowerCase();
    if (/^(kg|公斤|千克)$/.test(normalized)) return number * 1000;
    if (/^(斤)$/.test(normalized)) return number * 500;
    return /^(g|克)$/.test(normalized) ? number : null;
  }

  function aiWeightPriceParseWeight(value, defaultUnit = "g") {
    const raw = String(value || "").replace(/,/g, "");
    const match = raw.match(/(\d+(?:\.\d+)?)\s*(kg|公斤|千克|g|克|斤)(?![A-Za-z0-9])/i);
    if (match) return aiWeightPriceConvertWeight(match[1], match[2]);
    return /^\s*\d+(?:\.\d+)?\s*$/.test(raw) ? aiWeightPriceConvertWeight(raw, defaultUnit) : null;
  }

  function aiWeightPriceParseDimensions(value) {
    const raw = String(value || "").replace(/,/g, ".").replace(/\s+/g, " ").trim();
    const labelled = /(?:包装尺寸|外箱尺寸|商品尺寸|尺寸|长\s*[x×*]\s*宽\s*[x×*]\s*高|长宽高)[^\d]{0,24}(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*(mm|cm|m)?\b/i.exec(raw);
    const plain = /(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*(mm|cm|m)\b/i.exec(raw);
    const match = labelled || plain;
    if (!match) return null;
    const unit = String(match[4] || "cm").toLowerCase();
    const convert = number => unit === "mm" ? number / 10 : unit === "m" ? number * 100 : number;
    const values = [convert(Number(match[1])), convert(Number(match[2])), convert(Number(match[3]))];
    if (!values.every(value => Number.isFinite(value) && value > 0)) return null;
    return {
      package_length_cm: values[0], package_width_cm: values[1], package_height_cm: values[2],
      dimensions_display: `${match[1]} × ${match[2]} × ${match[3]} ${unit}`
    };
  }

  function aiWeightPriceStructuredMetrics(item) {
    if (!item || typeof item !== "object") return null;
    const valueFor = keys => {
      for (const key of keys) {
        if (item[key] !== undefined && item[key] !== null && item[key] !== "") {
          return {key, value: item[key]};
        }
      }
      return null;
    };
    const rawWeight = valueFor(["weight_g", "weightG", "weightKg", "grossWeight", "packageWeight", "weight"]);
    const length = valueFor(["package_length_cm", "packageLengthCm", "lengthCm", "lengthMm", "length"]);
    const width = valueFor(["package_width_cm", "packageWidthCm", "widthCm", "widthMm", "width"]);
    const height = valueFor(["package_height_cm", "packageHeightCm", "heightCm", "heightMm", "height"]);
    const result = {};
    if (rawWeight) {
      result.weight_g = aiWeightPriceParseWeight(rawWeight.value, /kg|公斤|千克/i.test(rawWeight.key) ? "kg" : item.weightUnit || "g");
    }
    const dimensions = [length, width, height];
    if (dimensions.every(Boolean)) {
      const unit = dimensions.some(entry => /mm/i.test(entry.key)) ? "mm"
        : String(item.lengthUnit || item.dimensionUnit || item.sizeUnit || "cm").toLowerCase();
      const convert = entry => {
        const match = String(entry.value).replace(/,/g, "").trim().match(/^(\d+(?:\.\d+)?)\s*(mm|cm|m|毫米|厘米|米)?$/i);
        if (!match) return null;
        const valueUnit = String(match[2] || unit).toLowerCase();
        if (!["mm", "cm", "m", "毫米", "厘米", "米"].includes(valueUnit)) return null;
        const number = Number(match[1]);
        if (!Number.isFinite(number) || number <= 0) return null;
        return ["mm", "毫米"].includes(valueUnit) ? number / 10 : ["m", "米"].includes(valueUnit) ? number * 100 : number;
      };
      result.package_length_cm = convert(length);
      result.package_width_cm = convert(width);
      result.package_height_cm = convert(height);
      if ([result.package_length_cm, result.package_width_cm, result.package_height_cm]
        .every(value => Number.isFinite(value) && value > 0)) {
        result.dimensions_display = `${result.package_length_cm} × ${result.package_width_cm} × ${result.package_height_cm} cm`;
      }
    }
    const parsedText = aiWeightPriceParseDimensions(JSON.stringify(item));
    if (parsedText && !result.dimensions_display) Object.assign(result, parsedText);
    return Object.keys(result).length ? result : null;
  }

  function aiWeightPriceStableArray(arrays) {
    const valid = arrays.filter(Boolean).filter(list => list.length);
    if (!valid.length) return [];
    const largest = Math.max(...valid.map(list => list.length));
    const choices = new Map(valid.filter(list => list.length === largest)
      .map(list => [JSON.stringify(list), list]));
    return choices.size === 1 ? [...choices.values()][0] : [];
  }

  function aiWeightPriceDomRows() {
    const rows = [];
    document.querySelectorAll(".od-pc-sku-table, .sku-table").forEach(table => {
      const headers = [...table.querySelectorAll("thead th, tr:first-child th")].map(cell => cell.textContent.trim());
      for (const row of table.querySelectorAll("tr")) {
        const cells = [...row.querySelectorAll("td")].map(cell => cell.textContent.replace(/\s+/g, " ").trim());
        if (cells.length < 2 || rows.length >= 200) continue;
        const priceIndex = headers.findIndex(value => /价格|单价/.test(value));
        const currencyIndex = cells.findIndex(value => /^(?:¥|￥)\s*\d/.test(value));
        const index = priceIndex >= 0 ? priceIndex : currencyIndex;
        if (index < 0) continue;
        const price = aiWeightPriceNumber(cells[index]);
        const attributes = cells.flatMap((value, i) => {
          const header = headers[i] || "";
          if (!value || i === index || /价格|单价|库存|数量|重量|毛重|净重|尺寸信息|包装|操作/.test(header)) return [];
          if (!header && i >= index) return [];
          return [{name: header || `规格${i + 1}`, value_name: value}];
        });
        if (!attributes.length) continue;
        const stockIndex = headers.findIndex(value => /库存|可售数量/.test(value));
        const stock = stockIndex >= 0 && /^\d+$/.test(cells[stockIndex] || "") ? Number(cells[stockIndex]) : null;
        rows.push({id: row.getAttribute("data-sku-id") || "", sku_id: row.getAttribute("data-sku-id") || "",
          label: attributes.map(item => `${item.name}:${item.value_name}`).join(" / "),
          attribute_combinations: attributes, price: price > 0 ? price : null,
          available_quantity: stock, price_text: cells[index], raw_text: cells.join(" "), raw_weight: ""});
      }
    });
    return rows;
  }

  function aiWeightPriceReactSkus(pageData) {
    const selection = document.querySelector("#skuSelection[data-module='od_sku_selection']")
      || document.querySelector("[data-module='od_sku_selection']");
    if (!selection) return [];
    const sets = pageData.sku_sets || [];
    const normalized = sets.map(list => list.map(item => {
      const label = aiWeightPriceDecode(item.specAttrs);
      const price = [item.discountPrice, item.currentPrice, item.priceNum, item.price]
        .find(value => /^\d+(?:\.\d+)?$/.test(String(value ?? "").trim()) && Number(value) > 0);
      const stock = item.canBookCount ?? item.availableQuantity ?? item.stock ?? item.quantity;
      const image = item.imageUrl ?? item.imgUrl ?? item.image ?? item.pictureUrl;
      return {
        id: String(item.skuId || ""),
        seller_sku: String(item.skuCode ?? item.sku ?? item.sellerSku ?? ""),
        label,
        attribute_combinations: aiWeightPriceVariationAttributes(label),
        price: aiWeightPriceNumber(price),
        price_text: price === undefined ? "" : `¥${price}`,
        available_quantity: stock !== undefined && stock !== null && stock !== "" && Number.isFinite(Number(stock)) ? Number(stock) : null,
        image_url: typeof image === "string" ? absoluteImage(image) : "",
        label_weight: String(label).match(/(?:重量|毛重|净重)?[^\d]{0,8}(\d+(?:\.\d+)?)\s*(kg|公斤|千克|g|克|斤)\b/i)?.[0] || ""
      };
    }).filter(item => /^\d+$/.test(item.id) && item.label)
      .sort((a, b) => a.id.localeCompare(b.id))).filter(list => list.length);
    const selected = aiWeightPriceStableArray(normalized);
    return selected.map(item => ({
      id: item.id, sku_id: item.id, seller_sku: item.seller_sku,
      label: item.label, attribute_combinations: item.attribute_combinations,
      price: item.price, price_text: item.price_text,
      available_quantity: item.available_quantity, image_url: item.image_url,
      raw_weight: item.label_weight, raw_text: item.label
    }));
  }

  function aiWeightPricePackMetrics(pageData) {
    const pack = document.querySelector("#productPackInfo[data-module='od_product_pack_info']")
      || document.querySelector("[data-module='od_product_pack_info']");
    if (!pack) return {byId: new Map(), common: null};
    const arrays = pageData.pack_sets || [];
    const normalizedSets = arrays.map(list => list.map(item => ({
      id: String(item.skuId || item.id || ""), metrics: aiWeightPriceStructuredMetrics(item)
    })).filter(item => item.metrics));
    const selected = aiWeightPriceStableArray(normalizedSets);
    const byId = new Map(selected.filter(item => item.id).map(item => [item.id, item.metrics]));
    const common = selected.length === 1 && !selected[0].id ? selected[0].metrics : null;
    // Some builds expose one common package object in a separate array.
    const commonValues = arrays.flatMap(list => list.length === 1 ? list : [])
      .filter(item => item && typeof item === "object" && !item.skuId && !item.id)
      .map(aiWeightPriceStructuredMetrics).filter(Boolean);
    if (!common && commonValues.length) {
      const unique = new Map(commonValues.map(item => [JSON.stringify(item), item]));
      if (unique.size === 1) return {byId, common: [...unique.values()][0]};
    }
    return {byId, common};
  }

  function aiWeightPriceMergeMetrics(row, metrics) {
    const result = {...row};
    const labelDimensions = aiWeightPriceParseDimensions(`${row.label || ""} ${row.raw_text || ""}`);
    const combined = {...(labelDimensions || {}), ...(metrics || {})};
    if (combined.weight_g) result.raw_weight = `${combined.weight_g}g`;
    result.weight_g = combined.weight_g || aiWeightPriceParseWeight(result.raw_weight) || null;
    if (combined.package_length_cm && combined.package_width_cm && combined.package_height_cm) {
      result.package_length_cm = combined.package_length_cm;
      result.package_width_cm = combined.package_width_cm;
      result.package_height_cm = combined.package_height_cm;
      result.dimensions_display = combined.dimensions_display;
    }
    return result;
  }

  function aiWeightPriceDetailFacts(pageData) {
    const rows = aiWeightPriceReactSkus(pageData);
    const sourceRows = rows.length ? rows : aiWeightPriceDomRows();
    const pack = aiWeightPricePackMetrics(pageData);
    const merged = sourceRows.map(row => aiWeightPriceMergeMetrics(row, pack.byId.get(String(row.id)) || pack.common));
    const commonMetric = key => {
      if (!merged.length) return pack.common?.[key] ?? null;
      const values = merged.map(row => row[key]);
      // A 1688 offer can have a different package weight for each SKU.  The
      // product-level field is a single value, so use the arithmetic mean of
      // all valid SKU weights instead of treating the conflicting values as
      // missing.  Dimensions remain conservative and still require an exact
      // common value because averaging a box size would be misleading.
      if (key === "weight_g") {
        const valid = values.map(value => Number(value))
          .filter(value => Number.isFinite(value) && value > 0);
        if (valid.length) {
          return Number((valid.reduce((total, value) => total + value, 0) / valid.length).toFixed(4));
        }
        return pack.common?.[key] ?? null;
      }
      return values.every(value => Number.isFinite(value) && value > 0 && value === values[0]) ? values[0] : null;
    };
    const metrics = Object.fromEntries(["weight_g", "package_length_cm", "package_width_cm", "package_height_cm"]
      .map(key => [key, commonMetric(key)]));
    const complete = [metrics.package_length_cm, metrics.package_width_cm, metrics.package_height_cm].every(value => value > 0);
    metrics.dimensions_display = complete ? `${metrics.package_length_cm} × ${metrics.package_width_cm} × ${metrics.package_height_cm} cm` : "";
    if (complete) metrics.volumetric_weight_kg = Number((metrics.package_length_cm * metrics.package_width_cm * metrics.package_height_cm / 6000).toFixed(4));
    return {skus: merged, raw_weight: metrics.weight_g ? `${metrics.weight_g}g` : "", ...metrics};
  }

  async function readPageData() {
    const response = await chrome.runtime.sendMessage({type: "READ_1688_PAGE_DATA"});
    if (!response?.ok || !response.data || response.data.offer_id !== itemId()) {
      throw new Error(response?.error || "1688商品数据尚未加载或页面已切换，请刷新详情页后重试");
    }
    return response.data;
  }

  function listCardCandidate(card) {
    const link = card.matches("a[href]")
      ? card
      : card.querySelector([
        "a[href*='/offer/']",
        "a[href*='offerId=']",
        "a[data-key-value*='offer_']",
        "a[href]"
      ].join(","));
    const rawUrl = card.getAttribute("href") || link?.getAttribute("href") || "";
    const dataValues = [
      rawUrl,
      card.getAttribute("data-renderkey"),
      card.getAttribute("data-offer-id"),
      link?.getAttribute("data-aplus-report"),
      link?.getAttribute("data-key-value")
    ];
    const sourceItemId = dataValues.map(itemIdFromValue).find(Boolean) || "";
    if (!sourceItemId) return null;

    const sourceUrl = absoluteUrl(rawUrl) || `https://detail.1688.com/offer/${sourceItemId}.html`;
    const titleNode = card.querySelector(".title-text div, .title-text, [class*='title']");
    const imageNode = card.querySelector("img.main-img, .main-img, img[src*='alicdn'], img[data-src*='alicdn'], img");
    const title = String(
      titleNode?.textContent
      || link?.getAttribute("title")
      || link?.getAttribute("aria-label")
      || imageNode?.getAttribute("alt")
      || ""
    ).replace(/\s+/g, " ").trim();
    if (!title) return null;

    const priceText = String(
      card.querySelector(".price-item, [class*='price']")?.textContent || ""
    ).replace(/,/g, "");
    const priceMatch = priceText.match(/\d+(?:\.\d+)?/);
    const image = absoluteImage(
      imageNode?.currentSrc
      || imageNode?.src
      || imageNode?.dataset?.src
      || imageNode?.dataset?.lazyloadSrc
    );
    return {
      source_platform: "1688",
      source_item_id: sourceItemId,
      source_url: sourceUrl,
      final_url: sourceUrl,
      title,
      price: priceMatch ? Number(priceMatch[0]) : null,
      currency_id: "CNY",
      main_image_url: image,
      images: image ? [image] : [],
      properties: [],
      description_text: "",
      scrape_status: "partial",
      error_message: "1688 列表页快速采集：详情、规格和重量尺寸待补充",
      collected_at: new Date().toISOString()
    };
  }

  async function extractProductSnapshot() {
    const sourceItemId = itemId();
    const title = text("#productTitle .title-content h1") || text("#productTitle h1") || text(".mod-detail-title h1") || text("h1.d-title");
    if (!sourceItemId || !title) throw new Error("未识别到 1688 商品编号或标题，请打开商品详情页后重试");
    const images = collectImages();
    const properties = collectProperties();
    const pageData = await readPageData();
    const detailFacts = aiWeightPriceDetailFacts(pageData);
    const variations = detailFacts.skus;
    if (!images.length) throw new Error("1688商品主图尚未加载，已停止采集");
    if (document.querySelector("#skuSelection") && !variations.length) throw new Error("1688商品规格尚未加载，已停止采集");
    if (sourceItemId !== itemId()) throw new Error("1688页面商品已切换，请重新采集");
    const descriptionNode = document.querySelector("#desc-lazyload-container, .detail-desc-module, #description");
    const description = String(descriptionNode?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 30000);
    const dimensionsComplete = [
      detailFacts.package_length_cm, detailFacts.package_width_cm, detailFacts.package_height_cm
    ].every(value => Number.isFinite(Number(value)) && Number(value) > 0);
    const weightComplete = Boolean(detailFacts.weight_g) || (variations.length > 0 && variations.every(row => row.weight_g > 0));
    const allDimensionsComplete = dimensionsComplete || (variations.length > 0 && variations.every(row =>
      [row.package_length_cm, row.package_width_cm, row.package_height_cm].every(value => value > 0)));
    const errors = [];
    if (!weightComplete) errors.push("未读取到完整的1688规格重量");
    if (!allDimensionsComplete) errors.push("未读取到完整的1688规格包装尺寸");
    return {
      source_platform: "1688",
      source_item_id: sourceItemId,
      source_url: location.href,
      final_url: location.href,
      title,
      price: numericPrice(variations),
      currency_id: "CNY",
      main_image_url: images[0] || text("meta[property='og:image']"),
      images,
      properties,
      variations,
      description_text: description,
      weight_g: detailFacts.weight_g,
      package_length_cm: detailFacts.package_length_cm,
      package_width_cm: detailFacts.package_width_cm,
      package_height_cm: detailFacts.package_height_cm,
      volumetric_weight_kg: detailFacts.volumetric_weight_kg,
      dimensions_display: detailFacts.dimensions_display,
      weight_basis: detailFacts.weight_g ? "1688_source" : "",
      scrape_status: weightComplete && allDimensionsComplete ? "ok" : "partial",
      error_message: errors.join("；"),
      collected_at: new Date().toISOString()
    };
  }

  async function extractProduct() {
    const deadline = Date.now() + 10000;
    let lastError;
    while (Date.now() < deadline) {
      try { return await extractProductSnapshot(); }
      catch (error) {
        lastError = error;
        if (!/尚未加载|未识别到/.test(error.message || "")) throw error;
        await new Promise(resolve => setTimeout(resolve, 400));
      }
    }
    throw lastError;
  }

  async function aiWeightPriceDetail() {
    const product = await extractProduct();
    const facts = { ...product, raw_weight: product.weight_g ? `${product.weight_g}g` : "", skus: product.variations };
    const memberNode = document.querySelector("[data-member-id], [data-company-id], a[href*='memberId']");
    return {
      ...product,
      url: location.href,
      merchant_id: memberNode?.getAttribute("data-member-id")
        || memberNode?.getAttribute("data-company-id") || "",
      raw_weight: facts.raw_weight,
      weight_g: facts.weight_g,
      package_length_cm: facts.package_length_cm,
      package_width_cm: facts.package_width_cm,
      package_height_cm: facts.package_height_cm,
      volumetric_weight_kg: facts.volumetric_weight_kg,
      dimensions_display: facts.dimensions_display,
      skus: facts.skus.length ? facts.skus : (product.variations || [])
    };
  }

  function aiWeightPriceSearchCandidates() {
    const result = [];
    const seen = new Set();
    const add = (node, href) => {
      const url = absoluteUrl(href || node?.href || "");
      const id = itemIdFromValue(url);
      if (!id || seen.has(id)) return;
      const card = node?.closest?.("li, .offer-item, .search-offer-wrapper, [class*='offer'], [class*='card']") || node;
      const image = card?.querySelector?.("img") || node?.querySelector?.("img");
      const titleNode = card?.querySelector?.(".title-text, [class*='title'], h3, h4");
      const title = String(titleNode?.textContent || node?.getAttribute?.("title") || image?.alt || "")
        .replace(/\s+/g, " ").trim();
      if (!title && !image) return;
      seen.add(id);
      result.push({
        source_item_id: id, url: url || `https://detail.1688.com/offer/${id}.html`,
        title: title.slice(0, 500),
        main_image_url: absoluteImage(image?.currentSrc || image?.src || image?.dataset?.src || ""),
        image_url: absoluteImage(image?.currentSrc || image?.src || image?.dataset?.src || ""),
        price: aiWeightPriceNumber(card?.querySelector?.("[class*='price'], .price-item")?.textContent || "")
      });
    };
    document.querySelectorAll("a[href*='/offer/'], a[href*='offerId='], a[data-key-value*='offer_']")
      .forEach(node => add(node, node.href));
    document.querySelectorAll("img").forEach(image => {
      const card = image.closest("a[href*='/offer/'], li, .offer-item, .search-offer-wrapper, [class*='offer'], [class*='card']");
      if (card) add(card, card.href || card.querySelector?.("a[href]")?.href);
    });
    return result.slice(0, 30);
  }

  function aiWeightPriceVisible(node) {
    if (!node?.getClientRects?.().length) return false;
    const style = getComputedStyle(node);
    const box = node.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0;
  }

  function aiWeightPriceClickImageSearchOpen() {
    const nodes = [...document.querySelectorAll("button, a, input[type='button'], input[type='submit'], [role='button'], div, span")];
    const node = nodes.find(item => aiWeightPriceVisible(item) &&
      /^(以图搜货|以图搜款|图片搜索|搜索图片|上传图片|拍照搜同款|找同款|相似货源)$/i.test(
        String(item.textContent || item.value || item.getAttribute("aria-label") || item.title || "").replace(/\s+/g, "")
      ) && ![...item.children].some(child => child.textContent.trim() && child.textContent.trim() === item.textContent.trim()));
    if (node) { node.click(); return true; }
    return false;
  }

  function aiWeightPriceSelectorNode(selector) {
    if (!selector) return null;
    try { return document.querySelector(selector); } catch (_) { return null; }
  }

  function aiWeightPriceImageUploads(selector) {
    let nodes = [];
    try { nodes = [...document.querySelectorAll(selector || "input[type='file']")]; } catch (_) {}
    return nodes.filter(input => input.type === "file" &&
      (!input.accept || /image|jpg|jpeg|png|bmp|webp/i.test(input.accept)));
  }

  function aiWeightPriceActiveUpload(inputs) {
    if (inputs.length === 1) return inputs[0];
    const active = inputs.filter(input => {
      let visibleRegion = false;
      for (let parent = input.parentElement; parent; parent = parent.parentElement) {
        const style = getComputedStyle(parent);
        if (style.display === "none" || style.visibility === "hidden") return false;
        const box = parent.getBoundingClientRect();
        if (box.width > 0 && box.height > 0 && box.bottom > 0 && box.right > 0 &&
            box.top < innerHeight && box.left < innerWidth) visibleRegion = true;
      }
      return visibleRegion;
    });
    if (active.length !== 1) {
      throw new Error(`1688图片上传控件不唯一：实际 ${inputs.length} 个，当前可见区域 ${active.length} 个`);
    }
    return active[0];
  }

  function aiWeightPriceImageSubmitButton() {
    const matches = [];
    const clean = value => String(value || "").replace(/\s+/g, "").trim();
    const generic = /^(提交|确定|确认|搜索)$/;
    const selectedImage = aiWeightPriceImageUploads("input[type='file']")
      .some(input => Boolean(input.files?.length));
    const popupCandidates = new Set();
    const uploadedPanelCandidates = new Set();
    const actionSelector = "button,a,input[type='button'],input[type='submit'],[role='button'],div,span";
    const actionNode = node => {
      const control = node.closest("button,a,[role='button'],[data-spm-click]");
      return control && generic.test(clean(control.innerText || control.value || "")) ? control : node;
    };
    // The visible upload receipt is stronger evidence than input.files:
    // widgets can clear/replace the input and use an HTTPS preview after
    // upload. Scope the action to the smallest receipt/preview panel.
    const receipts = [...document.querySelectorAll("div,span,p,header")].filter(node =>
      aiWeightPriceVisible(node) && /已上传[1-9]\d*张(?:图|图片)?/.test(clean(node.innerText)) &&
      ![...node.children].some(child => /已上传[1-9]\d*张(?:图|图片)?/.test(clean(child.innerText)))
    );
    for (const receipt of receipts) {
      for (let root = receipt.parentElement, depth = 0;
           root && root !== document.body && root !== document.documentElement && depth < 8;
           root = root.parentElement, depth += 1) {
        // Do not climb into the homepage's keyword-search section.
        if (root.querySelector("input[type='search'],input[type='text'],input:not([type]),textarea")) break;
        if (!/以图搜款|以图搜货|图片搜索/.test(clean(root.innerText))) continue;
        if (![...root.querySelectorAll("img,canvas,[style*='background-image']")].some(aiWeightPriceVisible)) continue;
        const actions = [...root.querySelectorAll(actionSelector)].filter(node =>
          aiWeightPriceVisible(node) && generic.test(clean(node.innerText || node.value || "")));
        if (!actions.length) continue;
        const leaves = actions.filter(node => ![...node.querySelectorAll(actionSelector)].some(child =>
          generic.test(clean(child.innerText || child.value || ""))));
        for (const action of leaves) uploadedPanelCandidates.add(actionNode(action));
        break;
      }
    }
    if (selectedImage) {
      // Some 1688 builds render the search action as an unlabelled div with
      // no button role or stable class. Find generic actions inside the local
      // selected-image preview panel, where they cannot be confused with the
      // keyword-search button elsewhere on the page.
      const controls = "button, a, input[type='button'], input[type='submit'], [role='button'], " +
        "[data-spm-click], [class*='btn'], [class*='button'], div, span";
      const previews = [...document.querySelectorAll(
        'img[src^="blob:"],img[src^="data:image"],canvas'
      )].filter(aiWeightPriceVisible);
      for (const preview of previews) {
        for (let root = preview.parentElement, depth = 0;
             root && root !== document.body && root !== document.documentElement && depth < 12;
             root = root.parentElement, depth += 1) {
          const actions = [...root.querySelectorAll(controls)].filter(node =>
            aiWeightPriceVisible(node) && generic.test(clean(node.innerText || node.value || ""))
          );
          if (!actions.length) continue;
          // Prefer the innermost text node so its click bubbles to any React or
          // delegated handler on the actual styled control.
          const leaves = actions.filter(node => ![...node.querySelectorAll("div,span")].some(child =>
            generic.test(clean(child.innerText || ""))
          ));
          for (const action of leaves.length ? leaves : actions) popupCandidates.add(actionNode(action));
          break;
        }
      }
    }
    const previewOwner = button => {
      // 1688 renders the homepage upload input and the selected-image dialog
      // through separate React portals. Walk only the button's own ancestors
      // so a homepage search button cannot borrow a preview from another UI.
      for (let node = button.parentElement, depth = 0;
           node && node !== document.body && node !== document.documentElement && depth < 12;
           node = node.parentElement, depth += 1) {
        const previews = [...node.querySelectorAll('img[src^="blob:"],img[src^="data:image"],canvas')];
        if (previews.some(aiWeightPriceVisible)) return node;
      }
      return null;
    };
    // 1688 has used buttons, anchors, and styled divs for this action. The
    // selected file and a preview in the control's own popup distinguish its
    // generic “搜索” label from the homepage keyword-search button.
    const controls = document.querySelectorAll(
      "button, a, input[type='button'], input[type='submit'], [role='button'], [data-spm-click], " +
      "[class*='searchBtn'], [class*='submitBtn'], [class*='actionPrimary']"
    );
    for (const button of new Set([...controls, ...popupCandidates, ...uploadedPanelCandidates].map(actionNode))) {
      if (!aiWeightPriceVisible(button) || button.disabled || button.getAttribute("aria-disabled") === "true") continue;
      if (button.classList.contains("disabled") || button.classList.contains("is-disabled")) continue;
      const label = clean(
        button.innerText || button.value || button.getAttribute("aria-label") ||
        button.title || button.getAttribute("data-title") || button.getAttribute("data-spm-click")
      );
      const className = String(button.className || "");
      const strong = /^(搜索图片|图片搜索|开始搜索|立即搜索|以图搜货|开始搜图|立即搜图|确认上传)$/;
      const dialog = button.closest('[role="dialog"],.ant-modal,.next-dialog,.dialog,.modal');
      const preview = previewOwner(button);
      let owner = button.parentElement, upload = null, distance = 0;
      while (owner && owner !== document.body && owner !== document.documentElement && distance < 16) {
        upload = owner.querySelector(':scope input[type="file"]');
        if (upload) break;
        owner = owner.parentElement;
        distance += 1;
      }
      const ownsImageUpload = Boolean(upload &&
        (!upload.accept || /image|jpg|jpeg|png|bmp|webp/i.test(upload.accept)));
      const hasSelectedFile = Boolean((ownsImageUpload && upload.files?.length) || selectedImage);
      const hasImagePreview = Boolean(preview ||
        owner?.querySelector('img[src^="blob:"],img[src^="data:image"],canvas'));
      const imagePreviewPrimary = /(^|\s)action--[^\s]+/.test(className) &&
        /(^|\s)actionPrimary--[^\s]+/.test(className);
      let priority = 0;
      if (strong.test(label) && hasSelectedFile) {
        // An explicitly named image-search action is sufficient evidence on
        // older 1688 builds, where the upload widget may not render a preview.
        priority = imagePreviewPrimary ? 8 : dialog ? 7 : ownsImageUpload ? 6 : hasImagePreview ? 5 : 4;
      } else if (generic.test(label) && uploadedPanelCandidates.has(button)) {
        priority = 10;
      } else if (generic.test(label) && hasSelectedFile &&
        (popupCandidates.has(button) || dialog || imagePreviewPrimary)) {
        // A plain homepage “搜索” button often shares the same ancestor as
        // the file input. A selected file alone is not enough evidence that
        // this is the image-search action; require a preview in this button's
        // own popup, or an explicitly marked image-search dialog.
        priority = imagePreviewPrimary ? 8 : dialog ? 7 : 6;
      }
      if (priority) matches.push({button, label, priority});
    }
    if (!matches.length) return null;
    const priority = Math.max(...matches.map(item => item.priority));
    const best = matches.filter(item => item.priority === priority);
    if (best.length !== 1) throw new Error("1688图片上传后出现多个提交按钮，无法确认当前主图的提交入口");
    return best[0];
  }

  function aiWeightPriceInvokeClick(node) {
    if (!node) return;
    try { node.scrollIntoView({block: "center", inline: "center"}); } catch (_) {}
    try { node.focus({preventScroll: true}); } catch (_) {}
    const rect = node.getBoundingClientRect();
    const coordinates = {
      bubbles: true, cancelable: true, view: window,
      clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2
    };
    if (typeof PointerEvent === "function") {
      node.dispatchEvent(new PointerEvent("pointerdown", {
        ...coordinates, pointerId: 1, pointerType: "mouse", isPrimary: true, button: 0, buttons: 1
      }));
    }
    node.dispatchEvent(new MouseEvent("mousedown", {...coordinates, button: 0, buttons: 1}));
    if (typeof PointerEvent === "function") {
      node.dispatchEvent(new PointerEvent("pointerup", {
        ...coordinates, pointerId: 1, pointerType: "mouse", isPrimary: true, button: 0, buttons: 0
      }));
    }
    node.dispatchEvent(new MouseEvent("mouseup", {...coordinates, button: 0, buttons: 0}));
    node.click();
  }

  function aiWeightPriceElementPath(element) {
    const parts = [];
    for (let node = element; node?.nodeType === 1; node = node.parentElement) {
      let position = 1;
      for (let previous = node.previousElementSibling; previous; previous = previous.previousElementSibling) {
        if (previous.tagName === node.tagName) position += 1;
      }
      parts.unshift(`${node.tagName.toLowerCase()}:nth-of-type(${position})`);
    }
    return parts.join(" > ");
  }

  function aiWeightPriceRenderedImageCards() {
    const marker = [...document.querySelectorAll("*")].find(node =>
      aiWeightPriceVisible(node) && !node.children.length && node.textContent.trim() === "找到以下货源");
    if (!marker) return [];
    const top = marker.getBoundingClientRect().bottom;
    const result = [], seen = new Set();
    for (const image of document.querySelectorAll("img")) {
      const box = image.getBoundingClientRect();
      if (!aiWeightPriceVisible(image) || box.width < 80 || box.height < 80 || box.top < top) continue;
      const source = absoluteImage(image.currentSrc || image.src);
      if (!source || seen.has(source)) continue;
      let card = null;
      for (let parent = image.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
        const largeImages = [...parent.querySelectorAll("img")].filter(item => {
          const size = item.getBoundingClientRect();
          return aiWeightPriceVisible(item) && size.width >= 80 && size.height >= 80;
        });
        if (largeImages.length !== 1) break;
        if (/[¥￥]\s*\d/.test(parent.innerText || "")) { card = parent; break; }
      }
      if (!card) continue;
      seen.add(source);
      result.push({
        url: location.href,
        search_url: location.href,
        result_image_path: aiWeightPriceElementPath(image),
        main_image_url: source,
        image_url: source,
        title: String(card.innerText || image.alt || "").replace(/\s+/g, " ").trim().slice(0, 600),
        top: box.top,
        left: box.left,
      });
    }
    return result.sort((a, b) => Math.abs(a.top - b.top) > 30 ? a.top - b.top : a.left - b.left)
      .map(({top: _top, left: _left, ...item}) => item).slice(0, 30);
  }

  function aiWeightPriceSearchSnapshot() {
    const imagePage = /\/1688-search\/pc-image-search\//.test(location.pathname);
    const rendered = imagePage ? aiWeightPriceRenderedImageCards() : [];
    if (rendered.length) return {ready: true, candidates: rendered, url: location.href};
    let candidates = aiWeightPriceSearchCandidates();
    if (location.hostname === "www.1688.com") {
      candidates = candidates.filter(candidate => {
        let link = null;
        try { link = document.querySelector(`a[href^="${CSS.escape(candidate.url)}"]`); } catch (_) {}
        for (let node = link, depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
          const key = `${node.id || ""} ${typeof node.className === "string" ? node.className : ""}`.toLowerCase();
          if (/(^|[-_ ])(?:result|results|image-search|search-result|goods-list|offer-list|product-list)([-_ ]|$)/.test(key)) return true;
        }
        return false;
      });
    }
    if (!imagePage && candidates.length) return {ready: true, candidates, url: location.href};
    const empty = String(document.body?.innerText || "").match(
      /(?:没有找到|未找到|暂无)[^\n]{0,20}(?:相关商品|匹配商品|搜索结果)|没有符合条件的商品/
    );
    return {ready: false, empty: empty?.[0] || "", candidates: [], url: location.href};
  }

  async function aiWeightPriceSearch(data = {}) {
    const selectors = data.selectors || {};
    const searchStartUrl = location.href;
    let inputs = aiWeightPriceImageUploads(selectors.image_search_upload);
    if (!inputs.length) {
      let opened = false;
      const deadline = Date.now() + 15000;
      while (Date.now() < deadline && !inputs.length) {
        // Homepage widgets mount after document.complete. Wait for the actual
        // opener, and activate it once when it becomes available.
        if (!opened) {
          const open = aiWeightPriceSelectorNode(selectors.image_search_open);
          if (open && aiWeightPriceVisible(open)) { open.click(); opened = true; }
          else opened = aiWeightPriceClickImageSearchOpen();
        }
        await new Promise(resolve => setTimeout(resolve, 100));
        inputs = aiWeightPriceImageUploads(selectors.image_search_upload);
      }
    }
    if (!inputs.length) throw new Error("1688首页没有找到图片上传控件");
    const input = aiWeightPriceActiveUpload(inputs);
    const [meta, encoded] = String(data.data_url || "").split(",", 2);
    if (!encoded) throw new Error("当前智赢商品主图数据无效");
    const mime = meta.match(/data:([^;]+)/i)?.[1] || "image/jpeg";
    const binary = atob(encoded);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    const transfer = new DataTransfer();
    transfer.items.add(new File([bytes], "zeshun-product-main.jpg", {type: mime}));
    input.files = transfer.files;
    input.dispatchEvent(new Event("input", {bubbles: true}));
    input.dispatchEvent(new Event("change", {bubbles: true}));
    let submitted = false, submitLabel = "";
    const configured = aiWeightPriceSelectorNode(selectors.image_search_submit);
    if (configured) {
      if (!aiWeightPriceVisible(configured)) throw new Error("配置的1688图片搜索按钮不可见");
      aiWeightPriceInvokeClick(configured); submitted = true; submitLabel = "配置的图片搜索按钮";
    }
    const submitDeadline = Date.now() + 15000;
    while (!submitted && Date.now() < submitDeadline) {
      const action = aiWeightPriceImageSubmitButton();
      if (action) {
        aiWeightPriceInvokeClick(action.button); submitted = true; submitLabel = action.label;
        break;
      }
      const snapshot = aiWeightPriceSearchSnapshot();
      // Do not mistake pre-existing homepage recommendations for search
      // results. An automatic search is accepted only after navigation to the
      // dedicated image-search route; otherwise keep looking for the action.
      const navigatedToImageSearch = location.href !== searchStartUrl &&
        /\/1688-search\/pc-image-search\//.test(location.pathname);
      if (navigatedToImageSearch && (snapshot.ready || snapshot.empty)) {
        return {...snapshot, submitted: true, submit_label: "上传后自动搜索"};
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (!submitted) throw new Error("1688主图已上传，但未找到可执行的图片搜索按钮");
    const deadline = Date.now() + Math.max(3000, Number(data.timeout_ms || 15000));
    while (Date.now() < deadline) {
      const snapshot = aiWeightPriceSearchSnapshot();
      if (snapshot.ready || snapshot.empty) return {...snapshot, submitted, submit_label: submitLabel};
      await new Promise(resolve => setTimeout(resolve, 400));
    }
    return {...aiWeightPriceSearchSnapshot(), submitted, submit_label: submitLabel};
  }

  function aiWeightPriceOpenSearchResult(data = {}) {
    const image = document.querySelector(String(data.result_image_path || ""));
    if (!image || !aiWeightPriceVisible(image)) throw new Error("1688以图搜货结果卡片已变化");
    const current = absoluteImage(image.currentSrc || image.src);
    if (data.main_image_url && current !== data.main_image_url) {
      throw new Error("1688以图搜货结果图片已变化");
    }
    image.click();
    return {clicked: true, url: location.href};
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
      const response = await chrome.runtime.sendMessage({type: "SUBMIT_PRODUCT", product: await extractProduct()});
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

  async function collectFromList(button, product) {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "采集中…";
    try {
      const response = await chrome.runtime.sendMessage({type: "SUBMIT_PRODUCT", product});
      if (!response?.ok) throw new Error(response?.error || "采集失败");
      showToast(
        response.queued ? "控制台暂不可用，商品已加入待传队列" : "商品已采集到 AI 原创产品",
        response.queued ? "warning" : "success"
      );
      button.textContent = response.queued ? "已待传" : "已采集 ✓";
    } catch (error) {
      showToast(error.message || String(error), "error");
      button.textContent = "重试";
    } finally {
      window.setTimeout(() => {
        button.disabled = false;
        button.textContent = original;
      }, 1800);
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

  function mountListButtons() {
    if (location.hostname !== "s.1688.com") return;
    document.querySelectorAll(".search-offer-wrapper").forEach(card => {
      if (card.querySelector(".zeshun-card-collect")) return;
      const product = listCardCandidate(card);
      if (!product) return;
      card.classList.add("zeshun-collector-card-host");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "zeshun-card-collect";
      button.textContent = "采集";
      button.title = "快速采集到泽顺 AI 原创产品";
      button.addEventListener("click", event => {
        event.preventDefault();
        event.stopPropagation();
        const current = listCardCandidate(card);
        if (!current) return showToast("商品列表已变化，请刷新后重试", "error");
        collectFromList(button, current);
      });
      card.appendChild(button);
    });
  }

  function refreshUi() {
    mountCollectorButton();
    mountListButtons();
  }

  const contentListener = (message, _sender, sendResponse) => {
    if (message?.type === "PING_PAGE") {
      sendResponse({ok: true, detail: Boolean(itemId()), platform: "1688"});
      return;
    }
    if (message?.type === "EXTRACT_PRODUCT") {
      extractProduct().then(product => sendResponse({ok: true, product}))
        .catch(error => sendResponse({ok: false, error: error.message || String(error)}));
      return true;
    }
    if (message?.type === "AI_WEIGHT_PRICE_READ_DETAIL") {
      aiWeightPriceDetail().then(detail => sendResponse({ok: true, detail}))
        .catch(error => sendResponse({ok: false, error: error.message || String(error)}));
      return true;
    }
    if (message?.type === "AI_WEIGHT_PRICE_SEARCH") {
      aiWeightPriceSearch(message).then(result => sendResponse({ok: true, ...result})).catch(error => {
        sendResponse({ok: false, error: error.message || String(error)});
      });
      return true;
    }
    if (message?.type === "AI_WEIGHT_PRICE_SEARCH_RESULTS") {
      try { sendResponse({ok: true, ...aiWeightPriceSearchSnapshot()}); }
      catch (error) { sendResponse({ok: false, error: error.message || String(error)}); }
      return;
    }
    if (message?.type === "AI_WEIGHT_PRICE_OPEN_SEARCH_RESULT") {
      try { sendResponse({ok: true, ...aiWeightPriceOpenSearchResult(message)}); }
      catch (error) { sendResponse({ok: false, error: error.message || String(error)}); }
      return;
    }
  };
  chrome.runtime.onMessage.addListener(contentListener);

  // Publish readiness only after registration succeeds.
  globalThis.__zeshun1688ContentListener = contentListener;
  globalThis.__zeshun1688ContentVersion = CONTENT_VERSION;

  // Search/detail message handlers must also exist in child frames.  Only the
  // top frame mounts the floating collector UI and its mutation observer.
  if (window.top === window) {
    const observer = new MutationObserver(() => {
      window.clearTimeout(mutationTimer);
      mutationTimer = window.setTimeout(refreshUi, 180);
    });
    observer.observe(document.documentElement, {childList: true, subtree: true});
    refreshUi();
  }
})();
