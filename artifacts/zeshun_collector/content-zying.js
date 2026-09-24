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
    idSelector && row.querySelector(idSelector)?.value,
    idSelector && row.querySelector(idSelector)?.textContent,
    row.getAttribute("data-id"), row.getAttribute("data-product-id"),
    row.getAttribute("data-goods-id"), row.getAttribute("data-item-id"),
    row.querySelector("[data-id]")?.getAttribute("data-id"),
    row.querySelector("[data-product-id]")?.getAttribute("data-product-id"),
    row.querySelector("a[href*='goodsId='], a[href*='productId=']")?.href
  ];
  return values.map(aiWeightPriceId).find(Boolean) || String(values.find(Boolean) || "").trim();
}

async function aiWeightPriceExtractProducts(data = {}) {
  await aiWeightPriceWaitFor(() => aiWeightPriceVisibleRows(data.selectors || {}).length, "智赢商品列表尚未加载，请确认已登录并打开商品列表");
  const selectors = data.selectors || {};
  const rowSelector = selectors.erp_rows || ".product-item, .goods-item, [data-product-id], [data-goods-id]";
  const visibleRows = [...document.querySelectorAll(rowSelector)].filter(row => {
    const rect = row.getBoundingClientRect?.();
    return !rect || (rect.width > 0 && rect.height > 0);
  });
  const selection = data.selection || {};
  const developerId = String(selection.product_developer_id || "").trim();
  const developerName = String(selection.product_developer_name || "").trim();
  const rows = developerId || developerName ? visibleRows.filter(row => {
    const text = aiWeightPriceText(row);
    return (developerName && text.includes(developerName)) ||
      (developerId && [
        row.getAttribute("data-developer-id"), row.getAttribute("data-login-id"),
        row.querySelector("[data-developer-id]")?.getAttribute("data-developer-id"),
        row.querySelector("[data-login-id]")?.getAttribute("data-login-id")
      ].some(value => String(value || "").trim() === developerId));
  }) : visibleRows;
  if ((developerId || developerName) && !rows.length) {
    throw new Error(`当前智赢列表未找到产品开发“${developerName || developerId}”的商品`);
  }
  const maxItems = Math.max(1, Number(data.max_items || 100));
  const startIndex = Math.max(0, Number(data.start_index || 0));
  const products = [];
  for (const row of rows.slice(startIndex, startIndex + maxItems)) {
    const titleNode = aiWeightPriceQuery(row, selectors.erp_title)
      || row.querySelector("[data-title], .product-title, .goods-title, .title, [class*='title']");
    const imageNode = aiWeightPriceQuery(row, selectors.erp_image)
      || row.querySelector("img[data-src], img[src], [style*='background-image']");
    const title = aiWeightPriceText(titleNode) || aiWeightPriceText(row);
    let image = imageNode?.currentSrc || imageNode?.src || imageNode?.dataset?.src || "";
    if (!image) image = String(imageNode?.style?.backgroundImage || "").match(/url\(["']?([^"')]+)["']?\)/i)?.[1] || "";
    let id = aiWeightPriceProductId(row, selectors.erp_id);
    if (!title) continue;
    if (!id) id = await aiWeightPriceReadCardId(row, titleNode, title, aiWeightPriceAbsolute(image), selectors);
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
      source_index: visibleRows.indexOf(row) + 1,
      product_developer_id: developerId,
      product_developer_name: developerName,
      raw_erp: {text: aiWeightPriceText(row).slice(0, 5000)}
    });
  }
  return products;
}

async function aiWeightPriceReadCardId(row, titleNode, title, image, selectors) {
  const detailSelector = selectors.erp_detail || ".curd-detail-wrap";
  const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== "hidden";
  const roots = () => [...document.querySelectorAll(detailSelector)].filter(visible);
  if (roots().length) {
    const close = roots().flatMap(root => [...root.querySelectorAll(".crud-detail-close")]).filter(visible);
    if (close.length !== 1) throw new Error("无法关闭旧智赢详情，已停止以避免商品编号错配");
    close[0].click();
    await aiWeightPriceWaitFor(() => !roots().length, "旧智赢详情未关闭");
  }
  if (!row.isConnected || !titleNode) throw new Error("智赢商品列表发生变化，请重试");
  titleNode.click();
  let previous = "";
  return aiWeightPriceWaitFor(() => {
    const panels = roots();
    if (panels.length !== 1) return false;
    const root = panels[0];
    const headers = root.querySelectorAll(selectors.erp_edit_id || ".crud-detail-header .h1");
    const titles = [...root.querySelectorAll("textarea[placeholder='请输入内容'], .product-title")];
    const images = [...root.querySelectorAll("img.ant-image-img")];
    const matched = titles.some(node => String(node.value || node.textContent || "").replace(/\s+/g, " ").trim() === title)
      || (image && images.some(node => aiWeightPriceAbsolute(node.currentSrc || node.src) === image));
    const id = headers.length === 1 ? aiWeightPriceText(headers[0]).replace(/^(?:产品编号|商品编号|产品ID|商品ID|ID)\s*[:：]?\s*/i, "") : "";
    const valid = matched && /^[1-9]\d*$/.test(id);
    const stable = valid && previous === id;
    previous = valid ? id : "";
    return stable ? id : false;
  }, `无法确认商品“${title}”的详情编号，已停止以避免错配`);
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
  const root = document.querySelector(selectors.erp_detail || ".curd-detail-wrap");
  if (!root) throw new Error("智赢商品详情尚未打开");
  const idNode = aiWeightPriceQuery(root, selectors.erp_edit_id || ".crud-detail-header .h1");
  const id = aiWeightPriceId(aiWeightPriceText(idNode));
  if (!id) throw new Error("无法读取智赢详情商品编号");
  const checkedNodes = [...root.querySelectorAll("input[name='stat']:checked")];
  if (checkedNodes.length !== 1) throw new Error("无法确认智赢当前商品审核状态");
  const checked = checkedNodes[0];
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

function aiWeightPriceDelay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function aiWeightPriceSame(field, actual, expected) {
  if (field === "review_status") {
    return String(actual || "").replace(/\s+/g, "") === String(expected || "").replace(/\s+/g, "");
  }
  const left = Number(String(actual ?? "").replace(/,/g, ""));
  const right = Number(String(expected ?? "").replace(/,/g, ""));
  return Number.isFinite(left) && Number.isFinite(right) && left === right;
}

async function aiWeightPriceWaitFor(check, message, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  let value;
  while (Date.now() < deadline) {
    value = check();
    if (value) return value;
    await aiWeightPriceDelay(200);
  }
  throw new Error(message);
}

function aiWeightPriceVisibleRows(selectors = {}) {
  const rowSelector = selectors.erp_rows || ".product-item, .goods-item, [data-product-id], [data-goods-id]";
  return [...document.querySelectorAll(rowSelector)].filter(row => {
    const style = getComputedStyle(row);
    const rect = row.getBoundingClientRect?.();
    return style.display !== "none" && style.visibility !== "hidden" &&
      (!rect || rect.width > 0 || rect.height > 0);
  });
}

async function aiWeightPriceOpenTarget(data = {}) {
  const selectors = data.selectors || {};
  const task = data.task || {};
  const wantedId = String(data.erp_goods_id || task.erp_goods_id || "").trim();
  if (!wantedId) throw new Error("核重核价任务缺少智赢商品编号");
  try {
    const current = aiWeightPriceDetailSnapshot({selectors});
    if (current.erp_goods_id === wantedId) return current;
  } catch (_) {}

  const sourcePage = Math.max(1, Number(task.source_page || 1));
  const selected = aiWeightPriceSelectPage({selectors, page: sourcePage});
  if (!selected.ok && sourcePage !== 1) throw new Error(selected.error || `无法返回智赢第 ${sourcePage} 页`);
  if (selected.changed) await aiWeightPriceDelay(900);
  const rows = await aiWeightPriceWaitFor(
    () => aiWeightPriceVisibleRows(selectors).length ? aiWeightPriceVisibleRows(selectors) : null,
    "智赢商品列表加载超时"
  );
  const title = String(task.title || "").replace(/\s+/g, " ").trim();
  let matches = rows.filter(row => {
    const titleNode = aiWeightPriceQuery(row, selectors.erp_title)
      || row.querySelector("[data-title], .product-title, .goods-title, .title, [class*='title']");
    return aiWeightPriceText(titleNode) === title;
  });
  if (matches.length > 1) {
    const idMatches = matches.filter(row => aiWeightPriceProductId(row, selectors.erp_id) === wantedId);
    if (idMatches.length) matches = idMatches;
  }
  if (matches.length > 1) {
    const sourceIndex = Number(task.source_index || 0);
    const indexed = sourceIndex >= 1 ? rows[sourceIndex - 1] : null;
    if (indexed && matches.includes(indexed)) matches = [indexed];
  }
  if (matches.length > 1 && task.main_image_url) {
    const expected = aiWeightPriceAbsolute(task.main_image_url);
    const imageMatches = matches.filter(row => {
      const image = aiWeightPriceQuery(row, selectors.erp_image)
        || row.querySelector("img[data-src], img[src]");
      return aiWeightPriceAbsolute(image?.currentSrc || image?.src || image?.dataset?.src) === expected;
    });
    if (imageMatches.length) matches = imageMatches;
  }
  if (matches.length !== 1) {
    throw new Error(`无法唯一定位待回填商品 ${wantedId}，匹配到 ${matches.length} 个`);
  }
  const rowId = aiWeightPriceProductId(matches[0], selectors.erp_id);
  if (rowId && rowId !== wantedId) throw new Error("待回填商品ID与采集记录不一致");
  const target = aiWeightPriceQuery(matches[0], selectors.erp_title)
    || matches[0].querySelector("[data-title], .product-title, .goods-title, .title, [class*='title']");
  if (!target) throw new Error("无法打开待回填智赢商品详情");
  target.click();
  return aiWeightPriceWaitFor(() => {
    try {
      const snapshot = aiWeightPriceDetailSnapshot({selectors});
      return snapshot.erp_goods_id === wantedId ? snapshot : null;
    } catch (_) { return null; }
  }, `打开的智赢详情不是目标商品 ${wantedId}`);
}

function aiWeightPriceSetRequiredValue(root, selector, value, field) {
  if (value === undefined || value === null) return;
  const node = aiWeightPriceQuery(root, selector);
  if (!node) throw new Error(`无法定位智赢${field}输入框`);
  const prototype = Object.getPrototypeOf(node);
  const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  if (setter) setter.call(node, String(value)); else node.value = String(value);
  node.dispatchEvent(new Event("input", {bubbles: true}));
  node.dispatchEvent(new Event("change", {bubbles: true}));
  if (!aiWeightPriceSame(field === "审核状态" ? "review_status" : field === "重量" ? "weight_g" : "net_income_usd", node.value, value)) {
    throw new Error(`智赢${field}输入失败`);
  }
}

function aiWeightPriceSaveButton(root, selector) {
  const query = selector || "button, [role='button'], input[type='submit'], input[type='button']";
  const scopes = selector ? [document] : [root, document];
  for (const scope of scopes) {
    let nodes = [];
    try { nodes = [...scope.querySelectorAll(query)]; } catch (_) {}
    const usable = nodes.filter(node => {
      const style = getComputedStyle(node);
      const label = aiWeightPriceText(node).replace(/\s+/g, "");
      const labelled = /^(保存(?:并关闭|商品|修改)?|提交(?:保存|修改)?|更新|确认修改|确定)$/.test(label);
      const submit = ["button", "input"].includes(node.tagName.toLowerCase()) && node.type === "submit";
      return !node.disabled && node.getAttribute("aria-disabled") !== "true" &&
        style.display !== "none" && style.visibility !== "hidden" && (labelled || submit);
    });
    if (usable.length === 1) return usable[0];
    if (usable.length > 1) throw new Error(`智赢详情保存按钮必须唯一且可用，实际 ${usable.length} 个`);
  }
  throw new Error("智赢详情保存按钮必须唯一且可用，实际 0 个");
}

async function aiWeightPriceWriteback(data = {}) {
  const selectors = data.selectors || {};
  const changes = data.changes || {};
  if (!changes || Object.keys(changes).some(field => !["weight_g", "net_income_usd", "review_status"].includes(field))) {
    throw new Error("智赢回填字段无效");
  }
  await aiWeightPriceOpenTarget(data);
  const root = document.querySelector(selectors.erp_detail || ".curd-detail-wrap");
  const before = aiWeightPriceDetailSnapshot({selectors});
  if (before.erp_goods_id !== String(data.erp_goods_id || "")) throw new Error("智赢详情商品编号与任务不一致");
  const statusNodes = [...root.querySelectorAll("input[name='stat']:checked")];
  if (statusNodes.length !== 1) throw new Error("无法确认智赢当前商品审核状态");
  const statusNode = statusNodes[0];
  const currentStatus = (aiWeightPriceText(statusNode?.closest?.("label")) || String(statusNode?.value || "")).replace(/\s+/g, "");
  if (currentStatus !== "待审核") throw new Error(`商品 ${data.erp_goods_id || ""} 当前状态为“${currentStatus || "未读取"}”，仅待审核允许核重核价`);
  aiWeightPriceSetRequiredValue(
    root, selectors.erp_weight || "input[name*='weight' i], input[placeholder*='重量']",
    changes.weight_g, "重量"
  );
  aiWeightPriceSetRequiredValue(
    root, selectors.erp_net_income || "input[name*='income' i], input[name*='profit' i], input[placeholder*='净收入'], input[placeholder*='利润']",
    changes.net_income_usd, "净收益"
  );
  if (changes.review_status) {
    const matches = [...root.querySelectorAll("input[name='stat']")].filter(node => {
      const label = aiWeightPriceText(node.closest("label"));
      return String(node.value || "") === String(changes.review_status) || label === String(changes.review_status);
    });
    if (matches.length !== 1) throw new Error(`无法唯一定位智赢审核状态：${changes.review_status}`);
    matches[0].click();
    await aiWeightPriceDelay(50);
    const selected = root.querySelector("input[name='stat']:checked");
    const selectedStatus = aiWeightPriceText(selected?.closest?.("label")) || String(selected?.value || "");
    if (!aiWeightPriceSame("review_status", selectedStatus, changes.review_status)) {
      throw new Error(`智赢审核状态未切换为${changes.review_status}`);
    }
  }
  const filled = aiWeightPriceDetailSnapshot({selectors});
  for (const [field, expected] of Object.entries(changes)) {
    if (!aiWeightPriceSame(field, filled[field], expected)) throw new Error(`智赢${field}填写后校验不一致`);
  }
  const save = aiWeightPriceSaveButton(root, selectors.erp_save);
  save.click();
  await aiWeightPriceDelay(250);
  return {before, submitted: true};
}

async function aiWeightPriceVerifyWriteback(data = {}) {
  await aiWeightPriceOpenTarget(data);
  const after = aiWeightPriceDetailSnapshot({selectors: data.selectors || {}});
  if (after.erp_goods_id !== String(data.erp_goods_id || "")) throw new Error("保存后打开的不是目标智赢商品");
  return {after, persisted: true};
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "AI_WEIGHT_PRICE_EXTRACT_PRODUCTS") {
    aiWeightPriceExtractProducts(message).then(products => sendResponse({ok: true, products}))
      .catch(error => sendResponse({ok: false, error: error.message || String(error)}));
    return true;
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
  if (message?.type === "AI_WEIGHT_PRICE_VERIFY_WRITEBACK") {
    aiWeightPriceVerifyWriteback(message).then(result => sendResponse({ok: true, ...result})).catch(error => {
      sendResponse({ok: false, error: error.message || String(error)});
    });
    return true;
  }
});
