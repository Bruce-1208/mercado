"use strict";

(function () {
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

  function collectVariations() {
    const rows = [];
    const selectors = [
      ".od-pc-sku-table tbody tr", ".od-pc-sku-table tr",
      ".sku-table tbody tr", ".sku-table tr", "[class*='sku'] table tbody tr",
      "[class*='sku'] table tr"
    ];
    document.querySelectorAll(selectors.join(",")).forEach(row => {
      const cells = [...row.querySelectorAll("th,td")]
        .map(cell => String(cell.textContent || "").replace(/\s+/g, " ").trim())
        .filter(Boolean);
      if (cells.length < 2 || rows.length >= 200) return;
      const text = cells.join(" ");
      if (!/(?:¥|￥|价格|库存|数量|现货|起批)/i.test(text)) return;
      const combinations = [];
      cells.slice(0, -2).forEach((value, index) => {
        if (value) combinations.push({name: `规格${index + 1}`, value_name: value});
      });
      rows.push({
        label: cells.slice(0, -2).join(" / ") || cells[0],
        price_text: cells.find(value => /(?:¥|￥|价格)/i.test(value)) || "",
        stock_text: cells.find(value => /(?:库存|数量|现货)/i.test(value)) || "",
        attribute_combinations: combinations
      });
    });
    return rows;
  }

  function numericPrice() {
    const raw = text("[class*='price']") || text("meta[property='og:price:amount']");
    const match = raw.replace(/,/g, "").match(/\d+(?:\.\d+)?/);
    return match ? Number(match[0]) : null;
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

  function aiWeightPriceReactArrays(element, predicate) {
    if (!element) return [];
    const key = Object.keys(element).find(name => name.startsWith("__reactFiber$"));
    let fiber = key ? element[key] : null;
    const found = [];
    const seen = new WeakSet();
    const scan = (value, depth) => {
      if (!value || typeof value !== "object" || depth > 9 || seen.has(value)) return;
      seen.add(value);
      if (Array.isArray(value) && predicate(value)) { found.push(value); return; }
      for (const [name, child] of Object.entries(value)) {
        if (["_owner", "stateNode", "return", "child", "sibling"].includes(name)) continue;
        if (child && typeof child === "object" && !child.$$typeof) scan(child, depth + 1);
      }
    };
    for (let depth = 0; fiber && depth < 18; depth += 1, fiber = fiber.return) {
      try { scan(fiber.memoizedProps, 0); } catch (_) {}
    }
    return found;
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
    return number;
  }

  function aiWeightPriceParseWeight(value, defaultUnit = "g") {
    const raw = String(value || "").replace(/,/g, "");
    const match = raw.match(/(\d+(?:\.\d+)?)\s*(kg|公斤|千克|g|克|斤)(?![A-Za-z0-9])/i);
    if (match) return aiWeightPriceConvertWeight(match[1], match[2]);
    const number = aiWeightPriceNumber(raw);
    return number === null ? null : aiWeightPriceConvertWeight(number, defaultUnit);
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
    const rawWeight = valueFor(["weight_g", "weightG", "grossWeight", "packageWeight", "weight"]);
    const length = valueFor(["package_length_cm", "packageLengthCm", "lengthCm", "length"]);
    const width = valueFor(["package_width_cm", "packageWidthCm", "widthCm", "width"]);
    const height = valueFor(["package_height_cm", "packageHeightCm", "heightCm", "height"]);
    const result = {};
    if (rawWeight) {
      result.weight_g = aiWeightPriceParseWeight(rawWeight.value, /kg|公斤|千克/i.test(rawWeight.key) ? "kg" : "g");
    }
    const dimensions = [length, width, height];
    if (dimensions.every(Boolean)) {
      const unit = dimensions.some(entry => /mm/i.test(entry.key)) ? "mm"
        : dimensions.some(entry => /(?:^|[_-])m$|M$/.test(entry.key)) ? "m" : "cm";
      const convert = entry => {
        const number = aiWeightPriceNumber(entry.value);
        if (!Number.isFinite(number) || number <= 0) return null;
        return unit === "mm" ? number / 10 : unit === "m" ? number * 100 : number;
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
    document.querySelectorAll([
      ".od-pc-sku-table tbody tr", ".od-pc-sku-table tr", ".sku-table tbody tr",
      ".sku-table tr", "[class*='sku'] table tbody tr", "[class*='sku'] table tr"
    ].join(",")).forEach((row, index) => {
      const cells = [...row.querySelectorAll("th,td")]
        .map(cell => String(cell.textContent || "").replace(/\s+/g, " ").trim()).filter(Boolean);
      if (cells.length < 2 || rows.length >= 200) return;
      const raw = cells.join(" ");
      const priceText = cells.find(value => /(?:¥|￥|价格)/i.test(value)) || "";
      const price = aiWeightPriceNumber(priceText);
      if (price === null) return;
      rows.push({
        id: row.getAttribute("data-sku-id") || row.getAttribute("data-id") || String(index),
        label: cells.slice(0, -2).join(" / ") || cells[0], price, price_text: priceText,
        raw_weight: raw.match(/(?:包装)?(?:重量|毛重|净重)[：:\s]*[\d.,]+\s*(?:kg|千克|公斤|g|克|斤)/i)?.[0] || "",
        raw_text: raw.slice(0, 1000)
      });
    });
    return rows;
  }

  function aiWeightPriceReactSkus() {
    const selection = document.querySelector("#skuSelection[data-module='od_sku_selection']")
      || document.querySelector("[data-module='od_sku_selection']");
    if (!selection) return [];
    const sets = aiWeightPriceReactArrays(selection, list => list.length > 0 && list.length <= 500 &&
      list.every(item => item && typeof item === "object" && item.skuId && item.specAttrs));
    const normalized = sets.map(list => list.map(item => {
      const label = aiWeightPriceDecode(item.specAttrs);
      const price = item.discountPrice ?? item.currentPrice ?? item.priceNum ?? item.price;
      const stock = item.canBookCount ?? item.availableQuantity ?? item.stock ?? item.quantity;
      const image = item.imageUrl ?? item.imgUrl ?? item.image ?? item.pictureUrl;
      return {
        id: String(item.skuId || ""),
        seller_sku: String(item.skuCode ?? item.sku ?? item.sellerSku ?? ""),
        label,
        attribute_combinations: aiWeightPriceVariationAttributes(label),
        price: aiWeightPriceNumber(price),
        price_text: price === undefined ? "" : `¥${price}`,
        available_quantity: Number.isFinite(Number(stock)) ? Number(stock) : null,
        image_url: typeof image === "string" ? absoluteImage(image) : "",
        label_weight: String(label).match(/(?:重量|毛重|净重)?[^\d]{0,8}(\d+(?:\.\d+)?)\s*(kg|公斤|千克|g|克|斤)\b/i)?.[0] || ""
      };
    }).filter(item => /^\d+$/.test(item.id) && item.label && Number.isFinite(item.price) && item.price > 0)
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

  function aiWeightPricePackMetrics(skus) {
    const pack = document.querySelector("#productPackInfo[data-module='od_product_pack_info']")
      || document.querySelector("[data-module='od_product_pack_info']");
    if (!pack) return {byId: new Map(), common: null};
    const arrays = aiWeightPriceReactArrays(pack, list => list.length > 0 && list.length <= 500 &&
      list.every(item => item && typeof item === "object" && aiWeightPriceStructuredMetrics(item)));
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
    if (!result.raw_weight && combined.weight_g) result.raw_weight = `${combined.weight_g}g`;
    if (combined.package_length_cm && combined.package_width_cm && combined.package_height_cm) {
      result.package_length_cm = combined.package_length_cm;
      result.package_width_cm = combined.package_width_cm;
      result.package_height_cm = combined.package_height_cm;
      result.dimensions_display = combined.dimensions_display;
    }
    return result;
  }

  function aiWeightPriceDetailFacts() {
    const rows = aiWeightPriceReactSkus();
    const sourceRows = rows.length ? rows : aiWeightPriceDomRows();
    const pack = aiWeightPricePackMetrics(sourceRows);
    const merged = sourceRows.map(row => aiWeightPriceMergeMetrics(row, pack.byId.get(String(row.id)) || pack.common));
    const priced = merged.filter(row => Number.isFinite(Number(row.price))).sort((a, b) => Number(a.price) - Number(b.price));
    const selected = priced[priced.length - 1] || merged[merged.length - 1] || {};
    const bodyText = document.body?.innerText || "";
    const pageWeight = aiWeightPriceParseWeight(bodyText, "g");
    const pageDimensions = aiWeightPriceParseDimensions(bodyText);
    const selectedWeight = aiWeightPriceParseWeight(selected.raw_weight, "g");
    const selectedDimensions = selected.package_length_cm ? selected : null;
    const metrics = selectedWeight ? {weight_g: selectedWeight}
      : pack.common?.weight_g ? {weight_g: pack.common.weight_g} : {weight_g: pageWeight};
    const dimensions = selectedDimensions || pack.common || pageDimensions || {};
    Object.assign(metrics, {
      package_length_cm: dimensions.package_length_cm ?? null,
      package_width_cm: dimensions.package_width_cm ?? null,
      package_height_cm: dimensions.package_height_cm ?? null,
      dimensions_display: dimensions.dimensions_display || ""
    });
    if (metrics.package_length_cm && metrics.package_width_cm && metrics.package_height_cm) {
      metrics.volumetric_weight_kg = Number((metrics.package_length_cm * metrics.package_width_cm * metrics.package_height_cm / 6000).toFixed(4));
    }
    return {
      skus: merged,
      selected,
      raw_weight: selected.raw_weight || (metrics.weight_g ? `${metrics.weight_g}g` : ""),
      ...metrics
    };
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

  function extractProduct() {
    const sourceItemId = itemId();
    const title = text("h1") || text("meta[property='og:title']") || document.title.replace(/[-_].*1688.*$/i, "").trim();
    if (!sourceItemId || !title) throw new Error("未识别到 1688 商品编号或标题，请打开商品详情页后重试");
    const images = collectImages();
    const properties = collectProperties();
    const detailFacts = aiWeightPriceDetailFacts();
    const variations = detailFacts.skus.length ? detailFacts.skus : collectVariations();
    const descriptionNode = document.querySelector("#desc-lazyload-container, .detail-desc-module, [class*='description']");
    const description = String(descriptionNode?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 30000);
    const dimensionsComplete = [
      detailFacts.package_length_cm, detailFacts.package_width_cm, detailFacts.package_height_cm
    ].every(value => Number.isFinite(Number(value)) && Number(value) > 0);
    const errors = [];
    if (!detailFacts.weight_g) errors.push("未读取到 1688 商品重量");
    if (!dimensionsComplete) errors.push("未读取到 1688 包装尺寸");
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
      variations,
      description_text: description,
      weight_g: detailFacts.weight_g,
      package_length_cm: detailFacts.package_length_cm,
      package_width_cm: detailFacts.package_width_cm,
      package_height_cm: detailFacts.package_height_cm,
      volumetric_weight_kg: detailFacts.volumetric_weight_kg,
      dimensions_display: detailFacts.dimensions_display,
      weight_basis: detailFacts.weight_g ? "1688_source" : "",
      scrape_status: detailFacts.weight_g && dimensionsComplete ? "ok" : "partial",
      error_message: errors.join("；"),
      collected_at: new Date().toISOString()
    };
  }

  function aiWeightPriceDetail() {
    const product = extractProduct();
    const facts = aiWeightPriceDetailFacts();
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

  function aiWeightPriceClickSearchButton() {
    const nodes = [...document.querySelectorAll("button, a, input[type='button'], input[type='submit'], [role='button']")];
    const node = nodes.find(item => /以图搜货|图片搜索|上传图片|找同款|相似货源|搜索货源/i.test(
      String(item.textContent || item.value || item.getAttribute("aria-label") || "")
    ));
    if (node) { node.click(); return true; }
    return false;
  }

  function aiWeightPriceSelectorNode(selector) {
    if (!selector) return null;
    try { return document.querySelector(selector); } catch (_) { return null; }
  }

  async function aiWeightPriceSearch(data = {}) {
    const selectors = data.selectors || {};
    let input = aiWeightPriceSelectorNode(selectors.image_search_upload) || document.querySelector("input[type='file']");
    if (!input) {
      const open = aiWeightPriceSelectorNode(selectors.image_search_open);
      if (open) open.click(); else aiWeightPriceClickSearchButton();
      await new Promise(resolve => setTimeout(resolve, 500));
      input = aiWeightPriceSelectorNode(selectors.image_search_upload) || document.querySelector("input[type='file']");
    }
    if (data.data_url) {
      if (input) {
        const [meta, encoded] = String(data.data_url).split(",", 2);
        const mime = meta.match(/data:([^;]+)/i)?.[1] || "image/jpeg";
        const binary = atob(encoded || "");
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
        const transfer = new DataTransfer();
        transfer.items.add(new File([bytes], "zeshun-search.jpg", {type: mime}));
        input.files = transfer.files;
        input.dispatchEvent(new Event("change", {bubbles: true}));
      }
    }
    const submit = aiWeightPriceSelectorNode(selectors.image_search_submit);
    if (submit) submit.click(); else aiWeightPriceClickSearchButton();
    const deadline = Date.now() + Math.max(3000, Number(data.timeout_ms || 15000));
    let candidates = [];
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 700));
      candidates = aiWeightPriceSearchCandidates();
      if (candidates.length) break;
    }
    return candidates;
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
        collectFromList(button, product);
      });
      card.appendChild(button);
    });
  }

  function refreshUi() {
    mountCollectorButton();
    mountListButtons();
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "PING_PAGE") {
      sendResponse({ok: true, detail: Boolean(itemId()), platform: "1688"});
      return;
    }
    if (message?.type === "EXTRACT_PRODUCT") {
      try { sendResponse({ok: true, product: extractProduct()}); }
      catch (error) { sendResponse({ok: false, error: error.message || String(error)}); }
      return;
    }
    if (message?.type === "AI_WEIGHT_PRICE_READ_DETAIL") {
      try { sendResponse({ok: true, detail: aiWeightPriceDetail()}); }
      catch (error) { sendResponse({ok: false, error: error.message || String(error)}); }
      return;
    }
    if (message?.type === "AI_WEIGHT_PRICE_SEARCH") {
      aiWeightPriceSearch(message).then(candidates => sendResponse({ok: true, candidates})).catch(error => {
        sendResponse({ok: false, error: error.message || String(error)});
      });
      return true;
    }
  });

  const observer = new MutationObserver(() => {
    window.clearTimeout(mutationTimer);
    mutationTimer = window.setTimeout(refreshUi, 180);
  });
  observer.observe(document.documentElement, {childList: true, subtree: true});
  refreshUi();
})();
