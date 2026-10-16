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
          if (types.includes("Product") || types.includes("ProductGroup")) products.push(value);
          if (Array.isArray(value["@graph"])) queue.push(...value["@graph"]);
        }
      } catch (_) {
        // Broken third-party JSON-LD should not block collection.
      }
    }
    return products;
  }

  function variationArrayScore(value) {
    if (!Array.isArray(value) || !value.length || value.length > 500) return -1;
    const rows = value.filter(item => item && typeof item === "object" && !Array.isArray(item) && item.type !== "TITLE_STRIPED");
    if (!rows.length) return -1;
    const useful = rows.filter(item => {
      const combinations = item.attribute_combinations || item.attributeCombinations ||
        item.combinations || item.variation_attributes || item.specifications;
      return Array.isArray(combinations) && combinations.length ||
        Array.isArray(item.attributes) && item.attributes.length ||
        item.sku || item.sku_id || item.skuId || item.seller_sku || item.sellerSku || item.skuCode || item.color || item.size ||
        ((item.label || item.name || item.sku_name || item.skuName) &&
          (item.price !== undefined || item.available_quantity !== undefined || item.stock !== undefined));
    });
    if (!useful.length) return -1;
    const richRows = useful.filter(item => {
      const combinations = item.attribute_combinations || item.attributeCombinations ||
        item.combinations || item.variation_attributes || item.specifications || item.attributes;
      return Array.isArray(combinations) && combinations.length;
    }).length;
    return richRows * 1000 + useful.length * 10 + Math.min(rows.length, 9);
  }

  function assignedJson(scriptText) {
    const text = String(scriptText || "");
    const marker = /(?:__PRELOADED_STATE__|__INITIAL_STATE__|__NEXT_DATA__|__APOLLO_STATE__|preloadedState|_n\.ctx\.r)\s*[:=]\s*/i.exec(text);
    if (!marker) return null;
    const start = text.indexOf("{", marker.index + marker[0].length);
    if (start < 0) return null;
    let depth = 0;
    let quote = "";
    let escaped = false;
    for (let index = start; index < text.length; index += 1) {
      const char = text[index];
      if (quote) {
        if (escaped) escaped = false;
        else if (char === "\\") escaped = true;
        else if (char === quote) quote = "";
        continue;
      }
      if (char === '"') quote = char;
      else if (char === "{" || char === "[") depth += 1;
      else if (char === "}" || char === "]") {
        depth -= 1;
        if (depth === 0) {
          try { return JSON.parse(text.slice(start, index + 1)); } catch (_) { return null; }
        }
      }
    }
    return null;
  }

  function productStates(doc) {
    const parsedScripts = [];
    if (doc && doc.querySelectorAll) {
      doc.querySelectorAll("script").forEach(script => {
        const content = String(script.textContent || "").trim();
        if (!content || content.length > 5_000_000) return;
        let parsed = null;
        if (script.type === "application/ld+json" || script.type === "application/json") {
          try { parsed = JSON.parse(content); } catch (_) {}
        }
        if (!parsed && /(?:__PRELOADED_STATE__|__INITIAL_STATE__|__NEXT_DATA__|__APOLLO_STATE__|preloadedState|_n\.ctx\.r|variations|attribute_combinations)/i.test(content)) {
          parsed = assignedJson(content);
        }
        if (parsed) parsedScripts.push(parsed);
      });
    }
    return parsedScripts;
  }

  function extractProductAttributes(doc, product, specs, states) {
    const rows = [];
    const add = raw => {
      if (!raw || typeof raw !== "object") return;
      const name = clean(raw.name || raw.label || raw.key || raw.id);
      const value = raw.value_name ?? raw.value ?? raw.text;
      if (!name || (value == null && raw.value_id == null && !raw.values && !raw.value_struct)) return;
      rows.push({...raw, name, ...(value != null ? {value_name: typeof value === "object" ? clean(value.text || value.name) : String(value)} : {})});
    };
    let visited = 0;
    const walk = (value, depth = 0) => {
      if (!value || typeof value !== "object" || depth > 22 || ++visited > 120000) return;
      if (Array.isArray(value)) { value.slice(0, 1000).forEach(v => walk(v, depth + 1)); return; }
      for (const [key, child] of Object.entries(value)) {
        if (/recommend|carousel|related|tracking|analytics|variation|variant|sku|picker/i.test(key)) continue;
        if (["attributes", "additionalProperty"].includes(key) && Array.isArray(child)) child.forEach(add);
        else if (key === "specs" && Array.isArray(child)) child.filter(v => v && (v.value != null || v.value_name != null)).forEach(add);
        if (child && typeof child === "object") walk(child, depth + 1);
      }
    };
    [product, ...states].forEach(v => walk(v));
    if (product.brand) add({id:"BRAND", name:"Marca", value: typeof product.brand === "object" ? product.brand.name : product.brand});
    specs.forEach(add);
    const merged = [];
    for (const row of rows) {
      const old = merged.find(v => (row.id && v.id === row.id) || v.name.toLowerCase() === row.name.toLowerCase());
      if (!old) merged.push(row);
      else if (!old.value_name && !old.value_id && !old.values && !old.value_struct) Object.assign(old, row);
    }
    return merged.slice(0, 500);
  }

  function hasUncollectedVariantOptions(states, variantCount = 0) {
    let visited = 0;
    const walk = (value, depth = 0) => {
      if (!value || typeof value !== "object" || depth > 22 || ++visited > 120000) return false;
      if (Array.isArray(value)) return value.slice(0, 1000).some(v => walk(v, depth + 1));
      return Object.entries(value).some(([key, child]) => {
        if (/recommend|carousel|related|tracking|analytics/i.test(key)) return false;
        if (["variation_ids", "variationIds"].includes(key) && Array.isArray(child) && child.length > 500) return true;
        if (key === "pickers" && Array.isArray(child) && child.some(p => p &&
          ["products", "options", "values"].some(k => Array.isArray(p[k]) && p[k].length > Math.max(1, variantCount)))) return true;
        if (/^(?:variations?|variants?|hasVariant|skus?|sku_list|skuList|item_skus|itemSkus|sku_map|skuMap|sku_info_map|skuInfoMap)$/i.test(key)
            && child && typeof child === "object" && Object.keys(child).length > 500) return true;
        return walk(child, depth + 1);
      });
    };
    return states.some(value => walk(value));
  }

  function pickerText(value) {
    if (value && typeof value === "object") value = value.text || value.name || value.value;
    return typeof value === "string" ? value.trim() : "";
  }

  function pickerVariants(pickers, pageUrl) {
    const dimensions = pickers.filter(p => p && ["products", "options", "values"].some(k => Array.isArray(p[k]) && p[k].length));
    const rows = new Map(), dimensionIds = [];
    for (const picker of dimensions) {
      const dimension = picker.attribute_id || picker.picker_id || picker.id;
      if (typeof dimension !== "string" || !dimension || dimensionIds.includes(dimension)) return [];
      dimensionIds.push(dimension);
      const options = ["products", "options", "values"].map(k => picker[k]).find(v => Array.isArray(v) && v.length);
      for (const option of options) {
        if (!option || typeof option !== "object" || Array.isArray(option)) return [];
        const label = ["value_name", "picker_label", "label", "name", "value"].map(k => pickerText(option[k])).find(Boolean);
        let identities = option.variation_ids ?? option.variationIds;
        if (identities == null) {
          const identity = option.variation_id ?? option.variationId;
          if (identity != null) identities = [identity];
        }
        if (identities == null && Array.isArray(option.variations)) identities = option.variations;
        if (identities == null && pageUrl) {
          // Bare IDs can be catalog/value IDs; a link must explicitly identify
          // a variation of this same listing before it is used as SKU evidence.
          const link = option.permalink || option.url;
          if (typeof link === "string") try {
            const base = new URL(pageUrl), target = new URL(link, pageUrl);
            if (!/\/(?:p|up)\//i.test(base.pathname) && base.origin === target.origin && base.pathname === target.pathname) {
              const linkedIds = [...new Set(["variation", "variation_id", "searchVariation"].flatMap(k => target.searchParams.getAll(k)))];
              if (linkedIds.length === 1) identities = linkedIds;
            }
          } catch (_) {}
        }
        if (!label || !Array.isArray(identities) || !identities.length) return [];
        for (const identity of identities) {
          if (!["string", "number"].includes(typeof identity) || !/^\d+$/.test(String(identity))) return [];
          const key = String(identity);
          if (!rows.has(key)) {
            if (rows.size >= 500) return [];
            rows.set(key, {id: identity, attribute_combinations: []});
          }
          const row = rows.get(key), previous = row.attribute_combinations.find(a => a.id === dimension);
          if (previous && previous.value_name !== label) return [];
          if (!previous) row.attribute_combinations.push({id: dimension, value_name: label});
          // An option shared by several SKUs cannot supply per-SKU stock/price.
          if (identities.length === 1) {
            const metadata = {
              seller_sku: option.seller_sku ?? option.sellerSku ?? option.sku ?? option.skuCode,
              price: option.price ?? option.price_amount ?? option.offers?.price,
              available_quantity: option.available_quantity ?? option.availableQuantity ?? option.stock ?? option.quantity ?? option.available,
              picture_ids: option.picture_ids ?? option.pictureIds,
              images: option.images, pictures: option.pictures
            };
            for (const [field, value] of Object.entries(metadata)) if (value != null) {
              if (field in row && JSON.stringify(row[field]) !== JSON.stringify(value)) return [];
              row[field] = value;
            }
          }
        }
      }
    }
    if ([...rows.values()].some(row => row.attribute_combinations.length !== dimensionIds.length)) return [];
    return [...rows.values()];
  }

  function extractProductVariations(doc, product, states, pageUrl) {
    const candidates = [];
    const pictureUrls = new Map();
    const add = value => {
      const score = variationArrayScore(value);
      if (score >= 0) candidates.push({value, score});
    };
    [product && product.variations, product && product.variants, product && product.hasVariant]
      .forEach(add);
    const parsedScripts = states;
    let visited = 0;
    const seen = new WeakSet();
    const walk = (value, depth = 0) => {
      if (!value || typeof value !== "object" || depth > 22 || visited > 120000 || seen.has(value)) return;
      seen.add(value);
      visited += 1;
      if (Array.isArray(value)) {
        value.slice(0, 1000).forEach(child => walk(child, depth + 1));
        return;
      }
      for (const [key, child] of Object.entries(value)) {
        if (/recommend|carousel|related|tracking|analytics/i.test(key)) continue;
        if (key === "pickers" && Array.isArray(child)) {
          const dimensions = child.filter(p => p && ["products", "options", "values"].some(k => Array.isArray(p[k]) && p[k].length));
          for (const picker of dimensions) for (const container of ["products", "options", "values"]) {
            const options = picker[container];
            if (!Array.isArray(options)) continue;
            const rows = options.map(option => {
              if (!option || typeof option !== "object" || Array.isArray(option)) return null;
              const row = {...option};
              const combinations = row.attribute_combinations || row.attributeCombinations || row.combinations || row.variation_attributes || row.specifications;
              if (Array.isArray(combinations) && combinations.length) return row;
              if (Array.isArray(row.attributes) && row.attributes.some(a => a && ["COLOR", "SIZE"].includes(a.id))) return row;
              if (row.color || row.size) return row;
              // Independent picker options are not a Cartesian SKU matrix.
              // Only a single dimension with explicit variation IDs is enough.
              const identity = row.variation_id ?? row.variationId;
              const label = pickerText(row.value_name || row.picker_label || row.label || row.name);
              const dimension = picker.attribute_id || picker.picker_id || picker.id;
              if (dimensions.length !== 1 || identity == null || !dimension || typeof label !== "string" || !label.trim()) return null;
              return {...row, id: identity, attribute_combinations: [{id: dimension, value_name: label}]};
            });
            if (rows.every(Boolean)) add(rows);
          }
          add(pickerVariants(child, pageUrl));
        }
        if (key === "pictures" && Array.isArray(child)) child.forEach(picture => {
          const url = picture && (picture.secure_url || picture.url || picture.source);
          if (picture && picture.id != null && /^https?:\/\//.test(url || "")) pictureUrls.set(String(picture.id), url);
        });
        const collectionKey = /^(?:variations?|variants?|hasVariant|skus?|sku_list|skuList|item_skus|itemSkus|sku_map|skuMap|sku_info_map|skuInfoMap)$/i.test(key);
        if (Array.isArray(child) && collectionKey) add(child);
        else if (child && typeof child === "object" && !Array.isArray(child) && collectionKey) {
          add(Object.entries(child).filter(([, v]) => v && typeof v === "object" && !Array.isArray(v))
            .map(([id, v]) => ({...v, id: v.id ?? id})));
        }
        if (child && typeof child === "object" && !["parent", "owner", "_owner", "stateNode"].includes(key)) walk(child, depth + 1);
      }
    };
    parsedScripts.forEach(value => walk(value));
    if (!candidates.length) return [];
    candidates.sort((left, right) => right.score - left.score);
    const sourceRows = candidates[0].value.filter(item => item && typeof item === "object" && !Array.isArray(item) && item.type !== "TITLE_STRIPED");
    return sourceRows.map(raw => {
      const variation = {...raw};
      const combinations = raw.attribute_combinations || raw.attributeCombinations || raw.combinations || raw.variation_attributes;
      if (Array.isArray(combinations) && combinations.length) variation.attribute_combinations = combinations;
      else if (Array.isArray(raw.specifications) && raw.specifications.length) variation.attribute_combinations = raw.specifications;
      else if (Array.isArray(raw.attributes) && raw.attributes.some(a => a && ["COLOR", "SIZE"].includes(a.id))) {
        variation.attribute_combinations = raw.attributes.filter(a => a && ["COLOR", "SIZE"].includes(a.id));
        variation.attributes = raw.attributes.filter(a => !a || !["COLOR", "SIZE"].includes(a.id));
      }
      else {
        const dimensions = ["color", "size"].filter(key => raw[key] != null && raw[key] !== "")
          .map(key => ({id: key.toUpperCase(), value_name: String(raw[key])}));
        if (dimensions.length) variation.attribute_combinations = dimensions;
      }
      if (Array.isArray(variation.attribute_combinations)) variation.attribute_combinations = variation.attribute_combinations.map(rawAttribute => {
        if (!rawAttribute || typeof rawAttribute !== "object") return rawAttribute;
        const attribute = {...rawAttribute};
        if (!attribute.id && attribute.attribute_id) attribute.id = attribute.attribute_id;
        if (attribute.value_name == null && attribute.value != null) {
          const value = attribute.value;
          attribute.value_name = typeof value === "object" ? value.name || value.text : String(value);
          if (typeof value === "object" && attribute.value_id == null && value.id != null) attribute.value_id = value.id;
        }
        return attribute;
      });
      if (variation.id === undefined && (raw.variation_id !== undefined || raw.variationId !== undefined)) {
        variation.id = raw.variation_id ?? raw.variationId;
      }
      if (variation.available_quantity === undefined) {
        variation.available_quantity = raw.availableQuantity ?? raw.stock ?? raw.quantity ?? raw.available;
      }
      if (variation.price === undefined && raw.price_amount !== undefined) variation.price = raw.price_amount;
      if (variation.price == null && raw.offers && !Array.isArray(raw.offers)) variation.price = raw.offers.price;
      if (variation.seller_sku == null) variation.seller_sku = raw.sellerSku ?? raw.sku ?? raw.skuCode;
      if (!variation.picture_ids && Array.isArray(raw.pictureIds)) variation.picture_ids = raw.pictureIds;
      const resolved = (variation.picture_ids || []).map(id => pictureUrls.get(String(id))).filter(Boolean);
      if (resolved.length) {
        variation.images = [...(Array.isArray(variation.images) ? variation.images : []), ...resolved];
        variation.pictures = variation.picture_ids.filter(id => pictureUrls.has(String(id)))
          .map(id => ({id: String(id), source: pictureUrls.get(String(id))}));
      }
      const aliases = {id: ["variation_id", "variationId"], seller_sku: ["sellerSku", "sku", "skuCode"],
        available_quantity: ["availableQuantity", "stock", "quantity", "available"], price: ["price_amount"],
        picture_ids: ["pictureIds"], attribute_combinations: ["attributeCombinations", "combinations", "variation_attributes", "specifications"]};
      for (const [key, names] of Object.entries(aliases)) {
        if (key in variation) names.forEach(name => delete variation[name]);
      }
      return variation;
    });
  }

  function extractItemId(doc, pageUrl, product) {
    const url = new URL(pageUrl);
    // User-product URLs use MLMU/MLBU/etc. These are not listing IDs and
    // must still enter selected-offer extraction even though normalizeItemId
    // deliberately rejects them.
    const catalogMatch = url.pathname.match(/\/(?:p|up)\/((?:ML[A-Z]|MCO|CBT)U?-?\d+)(?:\/|$)/i);
    const catalogId = normalizeItemId(catalogMatch?.[1]);
    const valid = value => {
      const id = normalizeItemId(value);
      return id && id !== catalogId ? id : "";
    };
    for (const key of ["item_id", "itemId", "wid"]) {
      const id = valid(url.searchParams.get(key));
      if (id) return id;
    }
    const filteredId = valid((url.searchParams.get("pdp_filters") || "").match(/(?:^|\|)item_id:([^|]+)/i)?.[1]);
    if (filteredId) return filteredId;
    // The path of a catalog page identifies a product, not a seller listing.
    // Only use selected-offer facts here, never recommendation-card data IDs.
    if (catalogMatch) {
      const candidates = [product?.sku, product?.productID,
        meta(doc, 'meta[property="product:retailer_item_id"]'),
        meta(doc, 'meta[itemprop="sku"]')];
      for (const node of doc.querySelectorAll(
        '.ui-pdp-buybox [data-item-id], .ui-pdp-buybox [data-itemid], ' +
        '[data-testid="buy-box"] [data-item-id], ' +
        '.ui-pdp-buybox input[name="item_id"], .ui-pdp-buybox input[name="itemId"], ' +
        '.ui-pdp-buybox a[href*="wid="], .ui-pdp-buybox a[href*="item_id="]')) {
        candidates.push(node.getAttribute("data-item-id"), node.getAttribute("data-itemid"), node.value);
        if (node.href) {
          const selected = new URL(node.href, pageUrl);
          for (const key of ["item_id", "itemId", "wid"]) candidates.push(selected.searchParams.get(key));
        }
      }
      // Some catalog layouts put the selected offer only in hydration JSON.
      // Read explicit page/buy-box fields; do not search arbitrary nested IDs.
      for (const script of doc.querySelectorAll('script[type="application/json"], script#__PRELOADED_STATE__, script')) {
        const raw = script.textContent || "";
        if (raw.length > 5_000_000) continue;
        let state = null;
        if (script.type === "application/json" || script.id === "__PRELOADED_STATE__") {
          try { state = JSON.parse(raw); } catch (_) {}
        }
        // Nordic emits `_n.ctx.r={...};` with trailing bootstrap code.
        // Parse only the balanced JSON value, never execute page scripts.
        if (!state) state = assignedJson(raw);
        for (const page of [state, state?.initialState, state?.pageState?.initialState,
          state?.appProps?.pageProps?.initialState]) {
          if (!page || typeof page !== "object") continue;
          candidates.push(page.item_id, page.item?.id,
            page.components?.buy_box?.item_id, page.components?.buy_box?.item?.id,
            page.components?.main_actions?.form?.item_id,
            page.components?.track?.melidata_event?.event_data?.item_id,
            page.track?.melidata?.event_data?.item_id);
        }
      }
      for (const value of candidates) { const id = valid(value); if (id) return id; }
      throw new Error("目录页尚未识别到卖家刊登编号，请选择具体卖家的商品后重试");
    }
    const pathId = valid(url.pathname);
    if (pathId) return pathId;
    for (const value of [product?.sku, product?.productID,
      meta(doc, 'meta[property="og:url"]'), doc.querySelector('link[rel="canonical"]')?.href]) {
      const id = valid(value);
      if (id) return id;
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

  function parsePluginProfitability(text) {
    const normalized = clean(text);
    const amount = label => {
      const match = new RegExp(
        `(?:${label})[^0-9-]{0,36}\\$?\\s*(-?\\d+(?:[.,]\\d+)?)`, "i"
      ).exec(normalized);
      return match ? finiteNumber(match[1]) : null;
    };
    return {
      net_proceeds_usd: amount("净\\s*收益|最终\\s*收益|net\\s*(?:profit|proceeds|revenue)|lucro\\s*(?:líquido|liquido)?"),
      commission_amount_usd: amount("佣\\s*金|commission|comisi[oó]n|comiss[aã]o")
    };
  }

  function parsePluginSales(text) {
    const normalized = clean(text);
    const labels = "(?:\u603b\s*\u9500\s*\u91cf|\u9500\s*\u91cf|\u5df2\s*\u552e|\u552e\s*\u51fa|sold(?:\s+quantity)?|sales|vendidos?|vendas?)";
    const number = "([0-9]+(?:[.,][0-9]+)?)\\s*(\u4e07|k|mil)?";
    const patterns = [
      new RegExp(`${labels}[^0-9]{0,18}${number}`, "i"),
      new RegExp(`${number}\\s*(?:\u4ef6|\u5355)?\\s*${labels}`, "i")
    ];
    for (const pattern of patterns) {
      const match = pattern.exec(normalized);
      if (!match) continue;
      let value = finiteNumber(match[1]);
      if (value === null) continue;
      const suffix = String(match[2] || "").toLowerCase();
      if (suffix === "\u4e07") value *= 10000;
      else if (suffix === "k" || suffix === "mil") value *= 1000;
      return Math.max(0, Math.round(value));
    }
    return null;
  }

  function parsePluginProductInfo(text, originCode = "") {
    const normalized = clean(text);
    const origin = clean(originCode).toUpperCase();
    let fulfillmentType = "unknown";
    let fulfillmentLabel = "\u672a\u8bc6\u522b";
    if (/\u6d77\s*\u5916\s*\u4ed3|overseas\s+warehouse/i.test(normalized)) {
      fulfillmentType = "overseas_warehouse";
      fulfillmentLabel = "\u6d77\u5916\u4ed3";
    } else if (/\u672c\s*(?:\u571f|\u5730)\s*\u4ed3|\u5f53\s*\u5730\s*\u4ed3|local\s+warehouse/i.test(normalized)) {
      fulfillmentType = "local_warehouse";
      fulfillmentLabel = "\u672c\u571f\u4ed3";
    } else if (/\u5168\s*\u6258\s*\u7ba1|mercado\s+env[ií]os\s+full|enviado\s+por\s+mercado\s+libre|\bfulfillment\b|\bfull\b/i.test(normalized)) {
      fulfillmentType = "full_managed";
      fulfillmentLabel = "\u5168\u6258\u7ba1";
    } else if (/\u534a\s*\u6258\s*\u7ba1/.test(normalized)) {
      fulfillmentType = "semi_managed";
      fulfillmentLabel = "\u534a\u6258\u7ba1";
    } else if (/\u81ea\s*\u53d1\s*\u8d27|\u81ea\s*\u884c\s*\u53d1\s*\u8d27|seller\s+fulfilled|merchant\s+fulfilled/i.test(normalized) || origin === "CN") {
      fulfillmentType = "self_ship";
      fulfillmentLabel = "\u81ea\u53d1\u8d27";
    } else if (origin) {
      fulfillmentType = "overseas_warehouse";
      fulfillmentLabel = `\u975e\u4e2d\u56fd\u4ed3/${origin}`;
    }
    return {
      sales: parsePluginSales(normalized),
      fulfillment_type: fulfillmentType,
      fulfillment_label: fulfillmentLabel,
      fulfillment_eligible: fulfillmentType === "self_ship" || fulfillmentType === "semi_managed"
    };
  }

  function shadowRoots(doc) {
    const roots = [];
    const visited = new Set();
    const hostedRoot = node => {
      if (!node) return null;
      try {
        if (node.shadowRoot) return node.shadowRoot;
      } catch (_) {}
      // ZYing 5.1.18 deliberately renders its Mercado detail card in a
      // closed shadow root.  Content scripts cannot reach that root through
      // element.shadowRoot, but Chromium exposes a read-only extension API
      // for exactly this case.  Do not patch Element.attachShadow: ZYing
      // detects that mutation and stops rendering the card entirely.
      try {
        if (typeof chrome !== "undefined" && chrome.dom &&
            typeof chrome.dom.openOrClosedShadowRoot === "function") {
          return chrome.dom.openOrClosedShadowRoot(node) || null;
        }
      } catch (_) {}
      return null;
    };
    const visit = root => {
      if (!root || visited.has(root) || !root.querySelectorAll) return;
      visited.add(root);
      roots.push(root);
      root.querySelectorAll("*").forEach(node => {
        const nested = hostedRoot(node);
        if (nested) visit(nested);
      });
    };
    visit(doc);
    return roots;
  }

  function readPluginMetrics(doc) {
    const lines = [];
    const seen = new Set();
    let selfShipOrigin = "";
    let originIconUrl = "";
    const add = value => {
      const text = clean(value);
      if (text && text.length <= 1200 && !seen.has(text)) {
        seen.add(text);
        lines.push(text);
      }
    };
    for (const root of shadowRoots(doc)) {
      const containers = Array.from(root.querySelectorAll(
        ".zying-meli-detail-metric-line, .zying-meli-detail-metric-column, " +
        ".zying-meli-detail-metrics, [class*='zying-meli-detail'], #zyCardWrap"
      ));
      containers.forEach(node => add(node.innerText || node.textContent));
      containers.forEach(container => {
        // The origin icon may be a sibling of the metric row. A shadow root
        // is already isolated to the ZYing widget, so use it as the fallback
        // scope; for light DOM keep the old narrow scope to avoid marketplace
        // page flags being mistaken for the plugin's origin.
        let scope = root && root.nodeType === 11 ? root : container;
        for (let node = container.parentElement; node && node !== root; node = node.parentElement) {
          const marker = `${node.id || ""} ${node.className || ""}`;
          if (/zying/i.test(marker)) scope = node;
        }
        // The origin icon can be a sibling of the weight column. Scan the
        // nearest ZYing wrapper, never the whole marketplace document.
        const nodes = [scope, ...Array.from(scope.querySelectorAll("*"))];
        nodes.forEach(node => {
          const assets = [
            node.getAttribute && node.getAttribute("src"),
            node.getAttribute && node.getAttribute("data-src"),
            node.getAttribute && node.getAttribute("href"),
            node.getAttribute && node.getAttribute("xlink:href"),
            node.getAttribute && node.getAttribute("data"),
            node.style && node.style.backgroundImage
          ];
          assets.forEach(asset => {
            const value = String(asset || "");
            const flag = /(?:^|[\\/])(CN|US|MX|BR|AR|CL|CO|UY|CA|GB|ES|PT|DE|FR|IT|JP|KR|AU)\.svg(?:[?#"')]|$)/i.exec(value);
            if (flag && (!selfShipOrigin || flag[1].toUpperCase() !== "CN")) {
              selfShipOrigin = flag[1].toUpperCase();
              originIconUrl = value;
            }
          });
        });
      });
    }
    const text = lines.join(" ");
    const metrics = parsePluginMetrics(text);
    const productInfo = parsePluginProductInfo(text, selfShipOrigin);
    return {
      lines: lines.slice(0, 50),
      text: text.slice(0, 12000),
      metrics,
      ...productInfo,
      self_ship_origin: selfShipOrigin,
      origin_icon_url: originIconUrl
    };
  }

  function protectedVisualUrl(node) {
    if (!node) return "";
    const values = [];
    try { values.push(node.style && node.style.backgroundImage); } catch (_) {}
    try {
      const view = node.ownerDocument && node.ownerDocument.defaultView;
      if (view && typeof view.getComputedStyle === "function") {
        values.push(view.getComputedStyle(node).backgroundImage);
      }
    } catch (_) {}
    for (const value of values) {
      const match = /url\(\s*["']?(blob:[^"')]+)["']?\s*\)/i.exec(String(value || ""));
      if (match) return match[1];
    }
    return "";
  }

  async function decodeProtectedVisual(url, doc) {
    if (!url || typeof fetch !== "function") return "";
    let timer = null;
    let controller = null;
    try {
      if (typeof AbortController === "function") {
        controller = new AbortController();
        timer = setTimeout(() => controller.abort(), 1800);
      }
      const response = await fetch(url, controller ? {signal: controller.signal} : undefined);
      if (!response.ok) return "";
      const svg = await response.text();
      if (!svg || svg.length > 100000 || !/<(?:svg|text)\b/i.test(svg)) return "";
      const Parser = (doc && doc.defaultView && doc.defaultView.DOMParser) ||
        (typeof DOMParser !== "undefined" ? DOMParser : null);
      if (!Parser) return "";
      const parsed = new Parser().parseFromString(svg, "image/svg+xml");
      if (parsed.querySelector("parsererror")) return "";
      return clean(Array.from(parsed.querySelectorAll("text"))
        .map(node => node.textContent || "").join(" "));
    } catch (_) {
      return "";
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async function decodedProtectedText(scope, doc, cache) {
    let nodes = [];
    try { nodes = [scope, ...Array.from(scope.querySelectorAll("*"))]; } catch (_) { return ""; }
    const urls = unique(nodes.map(protectedVisualUrl));
    const values = await Promise.all(urls.map(async url => {
      if (!cache.has(url)) cache.set(url, decodeProtectedVisual(url, doc));
      return cache.get(url);
    }));
    return clean(values.filter(Boolean).join(" "));
  }

  async function readPluginMetricsAsync(doc) {
    const fallback = readPluginMetrics(doc);
    const decoded = [];
    const seen = new Set();
    const cache = new Map();
    for (const root of shadowRoots(doc)) {
      let scopes = [];
      try {
        scopes = Array.from(root.querySelectorAll(
          "#zyCardWrap, [class*='zying-meli-detail-wrap'], " +
          ".zying-meli-detail-metric-line, .zying-meli-detail-inline-tag"
        ));
      } catch (_) {}
      for (const scope of scopes) {
        const value = await decodedProtectedText(scope, doc, cache);
        if (value && !seen.has(value)) {
          seen.add(value);
          decoded.push(value);
        }
      }
    }
    if (!decoded.length) return fallback;
    const text = decoded.join(" ").slice(0, 12000);
    const metrics = parsePluginMetrics(text);
    const productInfo = parsePluginProductInfo(text, fallback.self_ship_origin);
    return {
      ...fallback,
      lines: decoded.slice(0, 50),
      text,
      metrics,
      ...productInfo,
      read_method: "browser_extension_protected_svg"
    };
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

  function pagePrice(doc, product, priceMode = "original") {
    const offer = Array.isArray(product && product.offers) ? product.offers[0] : (product && product.offers) || {};
    const readAmount = amount => {
      if (!amount) return null;
      const fraction = amount.querySelector(".andes-money-amount__fraction");
      const cents = amount.querySelector(".andes-money-amount__cents");
      let value = clean(fraction && fraction.textContent).replace(/\D/g, "");
      if (value && cents) value += `.${clean(cents.textContent).replace(/\D/g, "")}`;
      const number = finiteNumber(value);
      return number > 0 ? number : null;
    };
    const original = readAmount(firstNode(doc, [
      ".ui-pdp-price__original-value .andes-money-amount",
      ".ui-pdp-price__second-line .andes-money-amount--previous",
      ".ui-pdp-price .andes-money-amount--previous",
      ".ui-pdp-price s.andes-money-amount"
    ]));
    // Exclude crossed-out and installment amounts from the current sale price.
    const current = readAmount(firstNode(doc, [
      ".ui-pdp-price__second-line .andes-money-amount:not(.andes-money-amount--previous):not(s):not(del)"
    ])) || finiteNumber(meta(doc, 'meta[itemprop="price"]') || meta(doc, 'meta[property="product:price:amount"]')) ||
      finiteNumber(offer.price) || readAmount(firstNode(doc, [
        ".ui-pdp-price > .andes-money-amount:not(.andes-money-amount--previous):not(s):not(del)"
      ]));
    return priceMode === "discount" ? (current || original) : (original || current);
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

  function extractPlatformCategory(doc, states) {
    const valid = value => /^(?:ML[A-Z]|MCO|CBT)\d+$/.test(String(value || "").trim().toUpperCase());
    const queue = [...states];
    let visited = 0;
    while (queue.length && visited++ < 20000) {
      const node = queue.shift();
      if (!node || typeof node !== 'object') continue;
      for (const key of ['category_id', 'categoryId', 'categoryID']) {
        if (valid(node[key])) return {category_id: String(node[key]).trim().toUpperCase(),
          category_name: clean(node.category_name || node.categoryName || '')};
      }
      if (node.category && typeof node.category === 'object' && valid(node.category.id)) {
        return {category_id: String(node.category.id).trim().toUpperCase(), category_name: clean(node.category.name)};
      }
      for (const [key, value] of Object.entries(node)) {
        if (!/recommend|carousel|related|suggest|variations|variants/i.test(key) && value && typeof value === 'object') queue.push(value);
      }
    }
    const id = meta(doc, 'meta[itemprop="categoryID"]') || meta(doc, 'meta[name="category_id"]');
    return valid(id) ? {category_id: id.trim().toUpperCase(), category_name: ''} : {category_id: '', category_name: ''};
  }

  function extractProduct(doc, pageUrl, pluginOverride = null, priceMode = "original") {
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
    const states = productStates(doc);
    const category = extractPlatformCategory(doc, states);
    const variations = extractProductVariations(doc, product, states, pageUrl);
    // Mercado collection gets measurements from ZeshunMercadoData.apply.
    // Never discover another extension's DOM implicitly, including async callers.
    const plugin = pluginOverride || {metrics: {}, lines: [], text: "", sales: null,
      read_method: "native_product_page"};
    const metrics = plugin.metrics;
    const actualWeightComplete = Number.isFinite(Number(metrics.weight_g)) && Number(metrics.weight_g) > 0;
    const dimensionsComplete = [
      metrics.package_length_cm, metrics.package_width_cm, metrics.package_height_cm
    ].every(value => Number.isFinite(Number(value)) && Number(value) > 0);
    const weightBasis = actualWeightComplete ? "plugin_actual" : "";
    const profitability = parsePluginProfitability(plugin.text);
    const price = pagePrice(doc, product, priceMode);
    const offer = Array.isArray(product.offers) ? product.offers[0] : (product.offers || {});
    const currencyId = clean(
      meta(doc, 'meta[itemprop="priceCurrency"]') ||
      meta(doc, 'meta[property="product:price:currency"]') || offer.priceCurrency || currencyForUrl(pageUrl)
    ).toUpperCase();
    const canonical = doc.querySelector('link[rel="canonical"]');
    const finalUrl = clean((canonical && canonical.href) || meta(doc, 'meta[property="og:url"]') || pageUrl);
    const errors = [];
    if (!pictures.length) errors.push("未识别到商品主图");
    if (!actualWeightComplete) errors.push("泽顺商品数据尚未提供实际重量");
    if (!dimensionsComplete) errors.push("泽顺商品数据尚未提供完整包装尺寸");
    const complete = Boolean(title && pictures.length && actualWeightComplete && dimensionsComplete);
    const source = {
      id: itemId,
      site_id: itemId.slice(0, 3),
      ...category,
      title,
      price,
      currency_id: currencyId,
      condition: "new",
      sold_quantity: plugin.sales,
      available_quantity: 1,
      permalink: finalUrl,
      pictures: pictures.map(url => ({source: url})),
      attributes: extractProductAttributes(doc, product, specs, states),
      variations,
      ...(hasUncollectedVariantOptions(states, variations.length) || variations.some(v => !v.attribute_combinations?.length)
        ? {variation_collection_status:"incomplete"} : {}),
      sale_terms: []
    };
    return {
      source_item_id: itemId,
      ...category,
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
      net_proceeds_usd: profitability.net_proceeds_usd,
      commission_amount_usd: profitability.commission_amount_usd,
      scrape_status: complete ? "ok" : "partial",
      error_message: errors.join("；"),
      source,
      description: {plain_text: description},
      page_snapshot: {
        ...category,
        page_title: clean(doc.title),
        specs,
        pictures,
        variation_count: variations.length,
        browser: "zeshun_browser_extension"
      },
      plugin_snapshot: {
        source: "浏览器商品详情页",
        read_method: plugin.read_method || "browser_extension_shadow_dom",
        dom_lines: plugin.lines,
        dom_text: plugin.text,
        weight_basis: weightBasis,
        dimensions_display: metrics.dimensions_display,
        weight_display: metrics.weight_display,
        plugin_volumetric_display: metrics.volumetric_display,
        volumetric_formula: "length_cm * width_cm * height_cm / 6000",
        volumetric_weight_kg: metrics.volumetric_weight_kg,
        net_proceeds_usd: profitability.net_proceeds_usd,
        commission_amount_usd: profitability.commission_amount_usd,
        sales: plugin.sales,
        fulfillment_type: plugin.fulfillment_type,
        fulfillment_label: plugin.fulfillment_label,
        fulfillment_eligible: plugin.fulfillment_eligible,
        self_ship_origin: plugin.self_ship_origin || "unknown",
        origin_icon_url: plugin.origin_icon_url
      },
      collected_at: nowSql()
    };
  }

  async function extractProductAsync(doc, pageUrl) {
    return extractProduct(doc, pageUrl);
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
        "a[href*='/MLB-'], a[href*='/MLA-'], a[href*='/MLC-'], a[href*='/MCO-'], " +
        "a[href*='/MLU-'], a[href*='/CBT-']"
      );
      return link && isSupportedUrl(link.href) ? {
        card,
        link,
        url: link.href,
        isUsOrigin: cardHasUsFlag(card)
      } : null;
    }).filter(Boolean);
  }

  function cardHasUsFlag(card) {
    if (!card || !card.querySelectorAll) return false;
    const isUsFlag = value => /(?:^|[\\/])US\.svg(?:[?#"')]|$)/i.test(String(value || ''));
    const nodes = [card, ...Array.from(card.querySelectorAll('img, source, use, [style]'))];
    return nodes.some(node => [
      node.getAttribute && node.getAttribute('src'),
      node.getAttribute && node.getAttribute('data-src'),
      node.getAttribute && node.getAttribute('srcset'),
      node.getAttribute && node.getAttribute('href'),
      node.getAttribute && node.getAttribute('xlink:href'),
      node.getAttribute && node.getAttribute('data'),
      node.getAttribute && node.getAttribute('alt'),
      node.getAttribute && node.getAttribute('title'),
      node.style && node.style.backgroundImage
    ].some(isUsFlag));
  }

  function cardShippingProfile(card, url = "") {
    if (!card) return {origin: "", managed: false, eligible: false};
    const assets = [card, ...Array.from(card.querySelectorAll("img, source, use, [style]"))]
      .flatMap(node => [
        node.getAttribute && node.getAttribute("src"),
        node.getAttribute && node.getAttribute("data-src"),
        node.getAttribute && node.getAttribute("srcset"),
        node.getAttribute && node.getAttribute("href"),
        node.getAttribute && node.getAttribute("xlink:href"),
        node.getAttribute && node.getAttribute("data"),
        node.getAttribute && node.getAttribute("alt"),
        node.getAttribute && node.getAttribute("title"),
        node.style && node.style.backgroundImage
      ]).map(value => String(value || ""));
    const text = clean(card.innerText || card.textContent);
    const us = assets.some(value => /(?:^|[\\/])US\.svg(?:[?#"')]|$)/i.test(value)) ||
      /(?:internacional.{0,24}(?:usa|eua|estados unidos|united states))|(?:(?:usa|eua|estados unidos|united states).{0,24}internacional)/i.test(text);
    const china = !us && (assets.some(value => /(?:^|[\\/])CN\.svg(?:[?#"')]|$)/i.test(value)) ||
      /(?:internacional.{0,30}china)|(?:china.{0,30}internacional)|(?:enviado|env[ií]o|origen|desde).{0,18}china/i.test(text));
    const managed = /^CBT/i.test(normalizeItemId(url)) ||
      /\b(?:full|fulfillment)\b|mercado\s+env[ií]os\s+full|enviado\s+por\s+mercado\s+libre/i.test(text);
    const origin = us ? "US" : (china ? "CN" : "");
    return {origin, managed, eligible: !us && (china || managed)};
  }

  function pluginLoginStatus(doc) {
    const markers = [];
    const runtimeMarkers = [];
    for (const root of shadowRoots(doc)) {
      try {
        markers.push(...root.querySelectorAll(
          ".zying-meli-detail-metric-line, .zying-meli-detail-metric-column, " +
          "[class*='zying-meli'], [id*='zying-meli'], #zyCardWrap"
        ));
      } catch (_) {}
    }
    try {
      // ZYing 5.x may have inserted its page bridge while the detail card is
      // still loading. This proves the plugin is present even when its panel
      // has not rendered any metric rows yet.
      runtimeMarkers.push(...doc.querySelectorAll(
        "script[data-zying-inject-bundle], script#zying, [data-zying-inject-bundle], #zyCardWrap"
      ));
    } catch (_) {}
    const visibleText = clean(markers.map(node => node.innerText || node.textContent).join(" "));
    const metrics = readPluginMetrics(doc);
    const found = markers.length > 0 || runtimeMarkers.length > 0 || metrics.lines.length > 0;
    const asksLogin = /(?:请先|立即|点击|尚未|未)\s*登录|登录\s*(?:智赢|账号)/i.test(visibleText);
    return {
      found,
      logged_in: found && !asksLogin,
      message: !found ? "未检测到智赢插件，请先安装并刷新当前美客多页面"
        : asksLogin ? "智赢插件尚未登录，请先登录后刷新当前美客多页面"
          : "智赢插件已检测并处于登录状态"
    };
  }

  function extractCardProduct(card, pageUrl) {
    if (!card || !isSupportedUrl(pageUrl)) throw new Error("未识别到可采集的商品卡片");
    if (cardHasUsFlag(card)) {
      throw new Error("商品列表检测到 US.svg：美国自发货商品不采集");
    }
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
      ...category,
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
      ...category,
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
    parsePluginProfitability,
    parsePluginSales,
    parsePluginProductInfo,
    isSupportedUrl,
    currencyForUrl,
    extractProduct,
    extractProductAsync,
    cardCandidates,
    extractCardProduct,
    cardHasUsFlag,
    cardShippingProfile,
    pluginLoginStatus
  };
});
