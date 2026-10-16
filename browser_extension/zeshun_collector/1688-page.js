"use strict";

// This function runs in MAIN world. DOM content scripts cannot see React's
// expando properties. Return only product fields, never application state.
function zeshunRead1688PageData() {
  const offerId = location.pathname.match(/^\/offer\/(\d+)\.html$/)?.[1] || "";
  if (location.hostname !== "detail.1688.com" || !offerId) throw new Error("不是1688商品详情页");
  const metricFields = ["weight", "weight_g", "weightG", "weightKg", "grossWeight", "packageWeight", "weightUnit",
    "length", "width", "height", "lengthCm", "widthCm", "heightCm", "lengthMm", "widthMm", "heightMm",
    "package_length_cm", "package_width_cm", "package_height_cm", "packageLengthCm", "packageWidthCm", "packageHeightCm",
    "lengthUnit", "widthUnit", "heightUnit", "sizeUnit", "dimensionUnit", "raw_dimensions", "dimensions_display",
    "dimensions", "packageDimensions", "packageSize", "packingSize", "size"];
  const imageFields = ['imageUrl', 'imgUrl', 'image', 'pictureUrl', 'image_url', 'skuImageUrl',
    'skuImage', 'largeImageUrl', 'bigImageUrl', 'picture', 'images'];
  const optionFields = ['skuId', 'specAttrs', 'name', 'value', 'valueName', 'specValue', 'label',
    'valueId', 'propValueId', 'propId', 'fid', 'vid', 'pvs', 'specId', 'skuAttr'];
  const arrays = (selector, predicate, fields, includeObject = false, collectAll = false) => {
    const element = document.querySelector(selector);
    if (!element) return [];
    const key = Object.keys(element).find(name => /^__react(Fiber|InternalInstance)\$/.test(name));
    let fiber = key ? element[key] : null;
    const seen = new WeakSet();
    const collected = [];
    for (let level = 0; fiber && level < 12; level++, fiber = fiber.return) {
      const found = [];
      const visit = (value, depth) => {
        if (!value || typeof value !== "object" || depth > 8 || seen.has(value)) return;
        seen.add(value);
        if (Array.isArray(value) && value.length && value.length <= 500 && value.every(predicate)
            || includeObject && !Array.isArray(value) && predicate(value)) {
          found.push((Array.isArray(value) ? value : [value]).map(item => Object.fromEntries(fields.filter(field => item[field] !== undefined)
            .map(field => [field, JSON.parse(JSON.stringify(item[field]))]))));
          if (Array.isArray(value)) return;
        }
        for (const [name, child] of Object.entries(value)) {
          if (["_owner", "stateNode", "return", "child", "sibling", "children"].includes(name)) continue;
          if (/recommend|related|tracking|analytics/i.test(name)) continue;
          if (child && typeof child === "object" && !child.$$typeof) visit(child, depth + 1);
        }
      };
      visit(fiber.memoizedProps, 0);
      if (found.length && !collectAll) return found;
      collected.push(...found);
    }
    return collected;
  };
  return {
    offer_id: offerId,
    sku_image_sets: arrays("#skuSelection[data-module='od_sku_selection'], [data-module='od_sku_selection']",
      item => item && typeof item === "object"
        && imageFields.some(key => item[key]) && optionFields.some(key => item[key]),
      [...optionFields, ...imageFields], true, true),
    sku_sets: arrays("#skuSelection[data-module='od_sku_selection'], [data-module='od_sku_selection']",
      item => item && typeof item === "object" && item.skuId && item.specAttrs,
      ["skuId", "specAttrs", "discountPrice", "currentPrice", "priceNum", "price", "salePrice", "priceAmount", "priceValue", "skuCode", "sku", "sellerSku",
        "canBookCount", "availableQuantity", "stock", "quantity", ...optionFields, ...imageFields, ...metricFields]),
    price_sets: arrays("#productPrice, [data-module='od_product_price']",
      item => item && typeof item === "object" && ["price", "priceAmount", "priceValue"].some(key => key in item),
      ["price", "priceAmount", "priceValue"], true, true),
    pack_sets: arrays("#productPackInfo[data-module='od_product_pack_info'], [data-module='od_product_pack_info']",
      item => item && typeof item === "object" && metricFields.some(key => key in item && !/Unit$/.test(key)),
      ["skuId", "id", "specAttrs", ...metricFields], true)
  };
}
