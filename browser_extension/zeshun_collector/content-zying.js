"use strict";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "PING_ZYING_PAGE") sendResponse({ok: true, zying: true});
  if (message?.type === "READ_ZYING_CONTEXT") {
    sendResponse({ok: true, zying: true, ...zeshunReadZyingPageContext()});
  }
});

function aiWeightPriceText(node) {
  return String(node?.textContent || node?.value || node?.getAttribute?.("title") || "")
    .replace(/\s+/g, " ").trim();
}

function aiWeightPriceValue(node) {
  const raw = String(node?.value || aiWeightPriceText(node) || "").replace(/,/g, "");
  const match = raw.match(/-?\d+(?:\.\d+)?/);
  return match ? Number(match[0]) : null;
}

function aiWeightPriceAbsolute(value) {
  const source = String(value || "").trim();
  if (!source) return "";
  try { return new URL(source, location.href).href; } catch (_) { return ""; }
}

function aiWeightPriceQuery(root, selector) {
  if (!selector) return null;
  try {
    const local = root.querySelector(selector);
    if (local) return local;
    if (root !== document) return document.querySelector(selector);
  } catch (_) {}
  return null;
}

function aiWeightPriceId(value) {
  const source = String(value || "");
  const match = source.match(/(?:goodsId|productId|itemId|id)[=:]([\w-]+)/i)
    || source.match(/(?:^|\D)(\d{4,})(?:\D|$)/);
  return match ? String(match[1]) : "";
}

function aiWeightPriceProductId(row, idSelector) {
  const values = [
    idSelector && aiWeightPriceQuery(row, idSelector)?.value,
    idSelector && aiWeightPriceQuery(row, idSelector)?.textContent,
    row.getAttribute("data-id"), row.getAttribute("data-product-id"),
    row.getAttribute("data-goods-id"), row.getAttribute("data-item-id"),
    row.querySelector("[data-id]")?.getAttribute("data-id"),
    row.querySelector("[data-product-id]")?.getAttribute("data-product-id"),
    row.querySelector("a[href]")?.href
  ];
  return values.map(aiWeightPriceId).find(Boolean) || String(values.find(Boolean) || "").trim();
}

function aiWeightPriceExtractProducts(data = {}) {
  const selectors = data.selectors || {};
  const rowSelector = selectors.erp_rows || ".product-item, .goods-item, [data-product-id], [data-goods-id]";
  const rows = [...document.querySelectorAll(rowSelector)].filter(row => {
    const rect = row.getBoundingClientRect?.();
    return !rect || (rect.width > 0 && rect.height > 0);
  });
  const maxItems = Math.max(1, Number(data.max_items || 100));
  const products = [];
  rows.slice(0, maxItems).forEach((row, index) => {
    const titleNode = aiWeightPriceQuery(row, selectors.erp_title)
      || row.querySelector("[data-title], .product-title, .goods-title, .title, [class*='title']");
    const imageNode = aiWeightPriceQuery(row, selectors.erp_image)
      || row.querySelector("img[data-src], img[src], [style*='background-image']");
    const title = aiWeightPriceText(titleNode) || aiWeightPriceText(row);
    let image = imageNode?.currentSrc || imageNode?.src || imageNode?.dataset?.src || "";
    if (!image) image = String(imageNode?.style?.backgroundImage || "").match(/url\(["']?([^"')]+)["']?\)/i)?.[1] || "";
    const id = aiWeightPriceProductId(row, selectors.erp_id);
    if (!title || !id) return;
    const editNode = aiWeightPriceQuery(row, selectors.erp_edit_url) || row.querySelector("a[href]");
    const skuNode = aiWeightPriceQuery(row, selectors.erp_sku) || row.querySelector("[data-sku], .sku, [class*='sku']");
    products.push({
      erp_goods_id: id,
      title: title.slice(0, 500),
      main_image_url: aiWeightPriceAbsolute(image),
      description: aiWeightPriceText(row).slice(0, 2000),
      reference_weight_g: aiWeightPriceValue(aiWeightPriceQuery(row, selectors.erp_weight) || row.querySelector("[data-weight], .weight, [class*='weight']")),
      erp_sku: aiWeightPriceText(skuNode).slice(0, 200),
      erp_edit_url: aiWeightPriceAbsolute(editNode?.href || ""),
      category_path: aiWeightPriceText(aiWeightPriceQuery(row, selectors.erp_category) || row.querySelector("[data-category], .category")),
      source_index: index,
      raw_erp: {text: aiWeightPriceText(row).slice(0, 5000)}
    });
  });
  return products;
}

function aiWeightPriceSelectPage(data = {}) {
  const wanted = String(data.page || "").trim();
  if (!wanted) return {ok: false, error: "页码无效"};
  const selectors = data.selectors || {};
  const nodes = [];
  try { nodes.push(...document.querySelectorAll(selectors.erp_page_item || "li.ant-pagination-item, [data-page], [data-current-page]")); } catch (_) {}
  const node = nodes.find(item => String(item.getAttribute("title") || item.getAttribute("data-page") || aiWeightPriceText(item)).trim() === wanted);
  if (!node) return {ok: false, error: `当前页面找不到第 ${wanted} 页`};
  const changed = !node.classList.contains("ant-pagination-item-active");
  if (changed) node.click();
  return {ok: true, page: wanted, changed};
}

function aiWeightPriceDetailSnapshot(data = {}) {
  const selectors = data.selectors || {};
  const root = document.querySelector(selectors.erp_detail || ".curd-detail-wrap") || document;
  const id = aiWeightPriceProductId(root, selectors.erp_id)
    || String(data.erp_goods_id || "").trim();
  const checked = root.querySelector("input[name='stat']:checked, input[type='radio']:checked");
  const weightNode = aiWeightPriceQuery(root, selectors.erp_weight)
    || root.querySelector("input[name*='weight' i], input[placeholder*='重量'], [data-weight]");
  const incomeNode = aiWeightPriceQuery(root, selectors.erp_net_income)
    || root.querySelector("input[name*='income' i], input[name*='profit' i], input[placeholder*='净收入'], input[placeholder*='利润']");
  return {
    erp_goods_id: id,
    review_status: aiWeightPriceText(checked?.closest?.("label")) || String(checked?.value || "").trim(),
    weight_g: aiWeightPriceValue(weightNode),
    net_income_usd: aiWeightPriceValue(incomeNode),
    url: location.href,
    title: aiWeightPriceText(root.querySelector("h1, .product-title, [data-title]")),
    raw_text: aiWeightPriceText(root).slice(0, 10000)
  };
}

async function aiWeightPriceWriteback(data = {}) {
  const selectors = data.selectors || {};
  const changes = data.changes || {};
  const root = document.querySelector(selectors.erp_detail || ".curd-detail-wrap") || document;
  const before = aiWeightPriceDetailSnapshot({selectors, erp_goods_id: data.erp_goods_id});
  const statusNode = root.querySelector("input[name='stat']:checked, input[type='radio']:checked");
  const currentStatus = aiWeightPriceText(statusNode?.closest?.("label")) || String(statusNode?.value || "");
  if (currentStatus && changes.review_status && currentStatus !== changes.review_status) {
    throw new Error(`商品 ${data.erp_goods_id || ""} 当前状态为“${currentStatus}”，不是待审核`);
  }
  const setValue = (selector, value) => {
    if (value === undefined || value === null || !selector) return false;
    const node = aiWeightPriceQuery(root, selector);
    if (!node) return false;
    const setter = Object.getOwnPropertyDescriptor(node.constructor.prototype, "value")?.set;
    if (setter) setter.call(node, String(value)); else node.value = String(value);
    node.dispatchEvent(new Event("input", {bubbles: true}));
    node.dispatchEvent(new Event("change", {bubbles: true}));
    return true;
  };
  setValue(selectors.erp_weight || "input[name*='weight' i], input[placeholder*='重量']", changes.weight_g);
  setValue(selectors.erp_net_income || "input[name*='income' i], input[name*='profit' i], input[placeholder*='净收入'], input[placeholder*='利润']", changes.net_income_usd);
  if (changes.review_status) {
    [...root.querySelectorAll("input[name='stat'], input[type='radio']")].find(node => {
      const label = aiWeightPriceText(node.closest("label"));
      return String(node.value || "") === String(changes.review_status) || label === String(changes.review_status);
    })?.click();
  }
  let saveNodes = [];
  try { saveNodes = [...root.querySelectorAll(selectors.erp_save || "button[type='submit'], button")]; } catch (_) {}
  const save = saveNodes
    .find(node => /保存|提交|更新|确定/i.test(aiWeightPriceText(node)));
  if (save) save.click();
  await new Promise(resolve => setTimeout(resolve, 1200));
  return {before, after: aiWeightPriceDetailSnapshot({selectors, erp_goods_id: data.erp_goods_id}), saved: Boolean(save)};
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "AI_WEIGHT_PRICE_EXTRACT_PRODUCTS") {
    try { sendResponse({ok: true, products: aiWeightPriceExtractProducts(message)}); }
    catch (error) { sendResponse({ok: false, error: error.message || String(error)}); }
    return;
  }
  if (message?.type === "AI_WEIGHT_PRICE_READ_DETAIL") {
    try { sendResponse({ok: true, detail: aiWeightPriceDetailSnapshot(message)}); }
    catch (error) { sendResponse({ok: false, error: error.message || String(error)}); }
    return;
  }
  if (message?.type === "AI_WEIGHT_PRICE_SELECT_PAGE") {
    try { sendResponse(aiWeightPriceSelectPage(message)); }
    catch (error) { sendResponse({ok: false, error: error.message || String(error)}); }
    return;
  }
  if (message?.type === "AI_WEIGHT_PRICE_WRITEBACK") {
    aiWeightPriceWriteback(message).then(result => sendResponse({ok: true, ...result})).catch(error => {
      sendResponse({ok: false, error: error.message || String(error)});
    });
    return true;
  }
});
