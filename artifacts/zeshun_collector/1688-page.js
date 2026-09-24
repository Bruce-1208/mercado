"use strict";

// This function runs in MAIN world. DOM content scripts cannot see React's
// expando properties. Return only product fields, never application state.
function zeshunRead1688PageData() {
  const offerId = location.pathname.match(/^\/offer\/(\d+)\.html$/)?.[1] || "";
  if (location.hostname !== "detail.1688.com" || !offerId) throw new Error("不是1688商品详情页");
  const arrays = (selector, predicate, fields) => {
    const element = document.querySelector(selector);
    if (!element) return [];
    const key = Object.keys(element).find(name => /^__react(Fiber|InternalInstance)\$/.test(name));
    let fiber = key ? element[key] : null;
    const seen = new WeakSet();
    for (let level = 0; fiber && level < 12; level++, fiber = fiber.return) {
      const found = [];
      const visit = (value, depth) => {
        if (!value || typeof value !== "object" || depth > 8 || seen.has(value)) return;
        seen.add(value);
        if (Array.isArray(value) && value.length && value.length <= 500 && value.every(predicate)) {
          found.push(value.map(item => Object.fromEntries(fields.filter(field => item[field] !== undefined)
            .map(field => [field, JSON.parse(JSON.stringify(item[field]))]))));
          return;
        }
        for (const [name, child] of Object.entries(value)) {
          if (["_owner", "stateNode", "return", "child", "sibling", "children"].includes(name)) continue;
          if (child && typeof child === "object" && !child.$$typeof) visit(child, depth + 1);
        }
      };
      visit(fiber.memoizedProps, 0);
      if (found.length) return found;
    }
    return [];
  };
  return {
    offer_id: offerId,
    sku_sets: arrays("#skuSelection[data-module='od_sku_selection'], [data-module='od_sku_selection']",
      item => item && typeof item === "object" && item.skuId && item.specAttrs,
      ["skuId", "specAttrs", "discountPrice", "currentPrice", "priceNum", "price", "skuCode", "sku", "sellerSku",
        "canBookCount", "availableQuantity", "stock", "quantity", "imageUrl", "imgUrl", "image", "pictureUrl"]),
    pack_sets: arrays("#productPackInfo[data-module='od_product_pack_info'], [data-module='od_product_pack_info']",
      item => item && typeof item === "object" && ["weight", "weight_g", "weightG", "weightKg", "grossWeight", "packageWeight", "length", "package_length_cm"].some(key => key in item),
      ["skuId", "id", "weight", "weight_g", "weightG", "weightKg", "grossWeight", "packageWeight", "weightUnit",
        "length", "width", "height", "lengthCm", "widthCm", "heightCm", "lengthMm", "widthMm", "heightMm",
        "package_length_cm", "package_width_cm", "package_height_cm", "packageLengthCm", "packageWidthCm", "packageHeightCm",
        "lengthUnit", "sizeUnit", "dimensionUnit"])
  };
}
