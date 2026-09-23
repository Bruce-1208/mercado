"use strict";

let aiOriginalRows = [];
let aiOriginalSelected = new Set();
let aiOriginalTaskTimer = null;
let aiOriginalPublishTimer = null;
let aiOriginalEditorState = null;

function aiOriginalEscape(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function aiOriginalDisplayImageUrl(value) {
  const source = String(value || "").trim();
  if (!source) return "";
  try {
    const parsed = new URL(source, window.location.origin);
    const hostname = parsed.hostname.toLowerCase();
    if (["127.0.0.1", "localhost"].includes(hostname) && parsed.pathname.startsWith("/api/ai-original-products/images/")) {
      return `${parsed.pathname}${parsed.search}`;
    }
    if (/^cbu\d+\.alicdn\.com$/.test(hostname) && parsed.pathname.startsWith("/img/ibank/")) {
      return `/api/ai-original-products/source-image?url=${encodeURIComponent(parsed.href)}`;
    }
  } catch (_error) {
    return "";
  }
  return source;
}

function aiOriginalImageError(image) {
  let candidates = [];
  try { candidates = JSON.parse(image.dataset.imageCandidates || "[]"); } catch (_error) {}
  const next = candidates.shift();
  if (next) {
    image.dataset.imageCandidates = JSON.stringify(candidates);
    image.src = aiOriginalDisplayImageUrl(next);
    return;
  }
  image.onerror = null;
  image.removeAttribute("src");
  image.classList.add("is-missing");
  image.alt = "主图加载失败";
}

function aiOriginalSourceImageFallbacks(original) {
  const sources = [original.main_image_url, ...(original.images || [])].filter(Boolean);
  return [...new Set(sources.slice(1).concat(sources.flatMap(value => {
    try {
      const url = new URL(value);
      if (/^cbu\d+\.alicdn\.com$/i.test(url.hostname) && /\.jpg$/i.test(url.pathname)) {
        url.pathname = url.pathname.replace(/\.jpg$/i, ".webp");
        return [url.href];
      }
    } catch (_error) {}
    return [];
  })))];
}

function aiOriginalStatus(message, kind = "") {
  const node = document.getElementById("ai-original-task-status");
  if (node) { node.textContent = message; node.className = `ai-original-status ${kind}`.trim(); }
}

function aiOriginalStateLabel(status) {
  return {pending: "待处理", processing: "处理中", completed: "已完成", failed: "失败"}[status] || status || "待处理";
}

function aiOriginalAttributes(attributes) {
  return (Array.isArray(attributes) ? attributes : []).filter(attribute => {
    return attribute && (attribute.value_name || attribute.value_id || attribute.values);
  }).slice(0, 30);
}

function aiOriginalEditorRow(id) {
  return aiOriginalRows.find(row => Number(row.id) === Number(id)) || null;
}

function aiOriginalSource(row) {
  return row?.original_1688 && typeof row.original_1688 === "object" ? row.original_1688 : {};
}

function aiOriginalEditorTokenId() {
  const selected = document.getElementById("ai-original-stores")?.selectedOptions?.[0];
  return selected?.value || "";
}

function aiOriginalCurrentAttributes() {
  return Array.isArray(aiOriginalEditorState?.attributes) ? aiOriginalEditorState.attributes : [];
}

function aiOriginalAttributeValue(attribute) {
  return String(attribute?.value_name ?? attribute?.value ?? "").trim();
}

function aiOriginalAttributeById(id) {
  return aiOriginalCurrentAttributes().find(item => String(item?.id || "").toUpperCase() === String(id || "").toUpperCase()) || {};
}

function aiOriginalAttributeNameKey(value) {
  const compact = String(value || "").toLocaleLowerCase().normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "").replace(/[^\p{L}\p{N}]+/gu, "");
  const aliases = {
    "颜色": "color", "色": "color", color: "color", colour: "color", cor: "color",
    "材质": "material", "材料": "material", material: "material", materiais: "material", materia: "material",
    "品牌": "brand", "牌子": "brand", brand: "brand", marca: "brand",
    "尺寸": "size", "尺码": "size", "大小": "size", size: "size", tamano: "size", talla: "size", tamanho: "size",
    "长度": "length", length: "length", longitud: "length", comprimento: "length",
    "宽度": "width", width: "width", ancho: "width", largura: "width",
    "高度": "height", height: "height", altura: "height",
    "重量": "weight", weight: "weight", peso: "weight",
    "图案": "pattern", pattern: "pattern", diseno: "pattern", estampa: "pattern",
    "风格": "style", "款式": "style", style: "style", estilo: "style",
    "性别": "gender", gender: "gender", genero: "gender", sexo: "gender",
    "适用年龄": "age", "年龄": "age", age: "age", edad: "age"
  };
  return aliases[compact] || compact;
}

function aiOriginalSourceAttributeRows() {
  const original = aiOriginalEditorState?.row?.original_1688 || {};
  const properties = original.properties;
  const rows = Array.isArray(properties)
    ? properties
    : (properties && typeof properties === "object" ? Object.entries(properties).map(([name, value]) => ({name, value})) : []);
  return rows.map((raw, index) => {
    if (!raw || typeof raw !== "object") return null;
    const name = raw.name ?? raw.key ?? raw.label ?? raw.attribute ?? raw.property_name ?? raw.name_cn ?? "";
    const value = raw.value_name ?? raw.value ?? raw.text ?? raw.content ?? raw.display_value ?? raw.valueName ?? raw.val ?? raw.values ?? "";
    const id = raw.id ?? raw.attribute_id ?? raw.attributeId ?? raw.nameid ?? raw.name_id ?? raw.key_id ?? raw.keyId ?? "";
    const valueId = raw.value_id ?? raw.valueId ?? raw.valueid ?? raw.option_id ?? raw.optionId ?? "";
    if (!String(name).trim() || !String(value).trim()) return null;
    return {id: String(id || `SOURCE_ATTRIBUTE_${index + 1}`), name: String(name).trim(), value_name: String(value).trim(), value_id: String(valueId || "").trim()};
  }).filter(Boolean);
}

function aiOriginalAttributeForDefinition(definition) {
  const id = String(definition?.id || "").toUpperCase();
  const nameKey = aiOriginalAttributeNameKey(definition?.name || id);
  const current = aiOriginalCurrentAttributes();
  const byId = current.find(item => String(item?.id || "").toUpperCase() === id);
  if (byId) return byId;
  const byName = current.find(item => [item?.name, item?.name_es, item?.name_pt].some(value => aiOriginalAttributeNameKey(value) === nameKey));
  if (byName) return byName;
  const source = aiOriginalSourceAttributeRows();
  return source.find(item => {
    const sourceId = String(item.id || "").toUpperCase();
    return sourceId === id || aiOriginalAttributeNameKey(item.name) === nameKey;
  }) || {};
}

function aiOriginalCategoryFieldId(id) {
  return `ai-original-editor-attr-${String(id || "field").replace(/[^a-zA-Z0-9_-]/g, "_")}`;
}

function renderAiOriginalCategoryFields() {
  const requiredNode = document.getElementById("ai-original-editor-required-fields");
  const optionalNode = document.getElementById("ai-original-editor-optional-fields");
  if (!requiredNode || !optionalNode) return;
  const schema = Array.isArray(aiOriginalEditorState?.schema) ? aiOriginalEditorState.schema : [];
  const render = definitions => definitions.length ? definitions.map(definition => {
    const id = String(definition.id || "");
    // Match stable IDs first, then localized names, then the raw 1688
    // properties. This keeps category fields populated even when the model
    // returned a translated label without Mercado's attribute ID.
    const current = aiOriginalAttributeForDefinition(definition);
    const value = aiOriginalAttributeValue(current);
    const options = Array.isArray(definition.values) ? definition.values : [];
    const control = options.length
      ? `<select id="${aiOriginalCategoryFieldId(id)}" data-attribute-id="${aiOriginalEscape(id)}" data-attribute-name="${aiOriginalEscape(definition.name || id)}"><option value="">请选择</option>${options.map(option => `<option value="${aiOriginalEscape(option.name || option.id)}" data-value-id="${aiOriginalEscape(option.id || "")}" ${String(option.name || option.id) === value ? "selected" : ""}>${aiOriginalEscape(option.name || option.id)}</option>`).join("")}${value && !options.some(option => String(option.name || option.id) === value) ? `<option value="${aiOriginalEscape(value)}" selected>${aiOriginalEscape(value)}（当前值）</option>` : ""}</select>`
      : `<input id="${aiOriginalCategoryFieldId(id)}" data-attribute-id="${aiOriginalEscape(id)}" data-attribute-name="${aiOriginalEscape(definition.name || id)}" value="${aiOriginalEscape(value)}" placeholder="填写${aiOriginalEscape(definition.name || id)}">`;
    return `<label class="ai-original-editor-attribute"><span>${aiOriginalEscape(definition.name || id)}${definition.required ? " *" : ""}<small>${aiOriginalEscape(id)}</small></span>${control}</label>`;
  }).join("") : '<p class="ai-original-editor-empty">请先选择 Mercado 类目以读取字段。</p>';
  requiredNode.innerHTML = render(schema.filter(item => item.required));
  optionalNode.innerHTML = render(schema.filter(item => !item.required));
}

function aiOriginalVariationLabel(variation) {
  if (!variation || typeof variation !== "object") return "";
  if (variation.label || variation.name || variation.sku_name) return String(variation.label || variation.name || variation.sku_name);
  const combinations = variation.attribute_combinations || variation.attributes || variation.properties || [];
  return combinations.map(item => `${item.name || item.id || "规格"}: ${item.value_name || item.value || item.text || ""}`).filter(Boolean).join(" / ");
}

function renderAiOriginalEditorVariations() {
  const node = document.getElementById("ai-original-editor-variations");
  if (!node) return;
  const rows = Array.isArray(aiOriginalEditorState?.variations) ? aiOriginalEditorState.variations : [];
  node.innerHTML = rows.length ? rows.map((variation, index) => `<tr>
    <td><input data-variant-index="${index}" data-variant-field="label" value="${aiOriginalEscape(aiOriginalVariationLabel(variation))}" placeholder="颜色 / 尺码"></td>
    <td><input data-variant-index="${index}" data-variant-field="price" value="${aiOriginalEscape(variation.price ?? variation.price_text ?? "")}" placeholder="1688价格"></td>
    <td><input data-variant-index="${index}" data-variant-field="stock" value="${aiOriginalEscape(variation.available_quantity ?? variation.stock ?? variation.stock_text ?? "")}" placeholder="库存"></td>
    <td><button type="button" class="text-button" onclick="removeAiOriginalEditorVariation(${index})">删除</button></td>
  </tr>`).join("") : '<tr><td colspan="4" class="ai-original-editor-empty">采集快照没有变体；可以手动新增。</td></tr>';
}

function collectAiOriginalEditorVariations() {
  const rows = Array.isArray(aiOriginalEditorState?.variations) ? aiOriginalEditorState.variations : [];
  return rows.map((variation, index) => {
    const next = {...variation};
    const value = field => document.querySelector(`[data-variant-index="${index}"][data-variant-field="${field}"]`)?.value?.trim() || "";
    const label = value("label");
    if (label) next.label = label; else delete next.label;
    const price = value("price");
    if (price) { next.price_text = price; if (Object.prototype.hasOwnProperty.call(next, "price")) next.price = Number(price) || price; }
    else if (next.price_text) delete next.price_text;
    const stock = value("stock");
    if (stock) { next.stock_text = stock; if (Object.prototype.hasOwnProperty.call(next, "available_quantity")) next.available_quantity = Number(stock) || stock; }
    else if (next.stock_text) delete next.stock_text;
    return next;
  });
}

function collectAiOriginalEditorAttributes() {
  if (!Array.isArray(aiOriginalEditorState?.schema) || !aiOriginalEditorState.schema.length) {
    return [...aiOriginalCurrentAttributes()];
  }
  const attributes = [];
  document.querySelectorAll("#ai-original-editor-required-fields [data-attribute-id], #ai-original-editor-optional-fields [data-attribute-id]").forEach(control => {
    const value = String(control.value || "").trim();
    const id = control.dataset.attributeId || "";
    if (!id || !value) return;
    const current = {...aiOriginalAttributeForDefinition({id, name: control.dataset.attributeName || id})};
    current.id = id;
    current.name = control.dataset.attributeName || current.name || id;
    if (aiOriginalAttributeValue(current) !== value) {
      delete current.value_name_es;
      delete current.value_name_pt;
    }
    current.value_name = value;
    const selected = control.tagName === "SELECT" ? control.selectedOptions?.[0] : null;
    if (selected?.dataset.valueId) current.value_id = selected.dataset.valueId;
    attributes.push(current);
  });
  return attributes;
}

function renderAiOriginalAttributes(attributes) {
  const rows = aiOriginalAttributes(attributes);
  if (!rows.length) return '<span class="ai-original-muted">等待 AI 生成属性</span>';
  return `<div class="ai-original-attribute-list">${rows.map(attribute => {
    const name = attribute.name_es || attribute.name || attribute.id || "属性";
    const localizedValues = [attribute.value_name_es, attribute.value_name_pt].filter(Boolean);
    const value = localizedValues.length ? [...new Set(localizedValues)].join(" / ") : (attribute.value_name || attribute.value_id || (Array.isArray(attribute.values) ? attribute.values.map(item => item.name || item.id).join(" / ") : ""));
    return `<span><b>${aiOriginalEscape(name)}</b>${aiOriginalEscape(value)}</span>`;
  }).join("")}</div>`;
}

function renderAiOriginalProducts() {
  const grid = document.getElementById("ai-original-grid");
  if (!grid) return;
  const filter = document.getElementById("ai-original-status-filter")?.value || "";
  const rows = filter ? aiOriginalRows.filter(row => row.ai_status === filter) : aiOriginalRows;
  document.getElementById("ai-original-total").textContent = `共 ${rows.length} 件`;
  grid.innerHTML = rows.length ? rows.map(row => {
    const original = row.original_1688 || {};
    const status = String(row.ai_status || "pending");
    const sourceImage = aiOriginalDisplayImageUrl(original.main_image_url || (original.images || [])[0] || "");
    const aiImage = aiOriginalDisplayImageUrl(row.main_image_url || "");
    const titleEs = row.title_es || "等待 AI 生成西班牙语标题";
    const titlePt = row.title_pt || "等待 AI 生成葡萄牙语标题";
    const aiAttributes = aiOriginalAttributes(row.ai_original?.attributes);
    const ready = status === "completed" && aiAttributes.some(attribute => !["BRAND", "ITEM_CONDITION"].includes(String(attribute.id || "").toUpperCase()));
    const aiImageReady = ["ai_image_edit", "local_background_removal"].includes(row.ai_original?.image_generation_method) && aiImage.includes("-ai-white.jpg");
    return `<article class="ai-original-card">
      <input type="checkbox" value="${Number(row.id)}" ${aiOriginalSelected.has(Number(row.id)) ? "checked" : ""} onchange="toggleAiOriginalProduct(${Number(row.id)}, this.checked)">
      <div class="ai-original-image-pair">
        <figure>${sourceImage ? `<img src="${aiOriginalEscape(sourceImage)}" data-image-candidates="${aiOriginalEscape(JSON.stringify(aiOriginalSourceImageFallbacks(original)))}" alt="1688 原始主图" loading="lazy" referrerpolicy="no-referrer" onerror="aiOriginalImageError(this)">` : '<span class="ai-original-image-placeholder">暂无原图</span>'}<figcaption>1688 原图</figcaption></figure>
        <figure>${aiImage ? `<img src="${aiOriginalEscape(aiImage)}" alt="AI 美客多白底主图" loading="lazy" referrerpolicy="no-referrer" onerror="aiOriginalImageError(this)">` : '<span class="ai-original-image-placeholder">待生成</span>'}<figcaption>AI 白底主图</figcaption></figure>
      </div>
      <div class="ai-original-source"><span class="ai-original-section-label source">1688 原始资料</span><h4>${aiOriginalEscape(original.title || row.title)}</h4><p>1688 编号：${aiOriginalEscape(original.source_1688_item_id || row.source_item_id)}</p><p>采购价：${aiOriginalEscape(original.price ?? row.price ?? "-")} CNY</p><a href="${aiOriginalEscape(original.source_url || row.source_url || "#")}" target="_blank" rel="noopener">打开 1688 详情页 ↗</a>${row.ai_error ? `<p class="bad">${aiOriginalEscape(row.ai_error)}</p>` : ""}</div>
      <div class="ai-original-copy"><span class="ai-original-section-label generated">AI 美客多刊登稿</span><strong>西语标题 ${titleEs.length}/60</strong><p class="${titleEs.length > 60 ? "bad" : ""}">${aiOriginalEscape(titleEs)}</p><strong>葡语标题 ${titlePt.length}/60</strong><p class="${titlePt.length > 60 ? "bad" : ""}">${aiOriginalEscape(titlePt)}</p><strong>AI 商品属性（${aiAttributes.length}）</strong>${renderAiOriginalAttributes(row.ai_original?.attributes)}<details><summary>查看 AI 双语详情</summary><p>${aiOriginalEscape(row.description_es || "待生成西语详情")}</p><p>${aiOriginalEscape(row.description_pt || "待生成葡萄牙语详情")}</p></details><div class="ai-original-readiness"><span class="${ready ? "ok" : "pending"}">${ready ? "✓" : "!"} AI 属性</span><span class="${aiImageReady ? "ok" : "pending"}">${aiImageReady ? "✓" : "!"} AI 白底主图</span><span class="${row.category_id ? "ok" : "pending"}">${row.category_id ? "✓" : "!"} CBT 类目</span></div><button class="secondary ai-original-edit-button" type="button" onclick="openAiOriginalEditor(${Number(row.id)})">编辑分类、字段、详情与变体</button></div>
      <div class="ai-original-fields">
        <label>实重(g)<input id="ai-original-weight-${Number(row.id)}" type="number" min="1" step="1" value="${aiOriginalEscape(row.weight_g || "")}"></label>
        <label>净收益USD<input id="ai-original-net-${Number(row.id)}" type="number" min="0.01" step="0.01" value="${aiOriginalEscape(row.net_proceeds_usd || "")}"></label>
        <label class="wide">CBT 类目（可留空自动预测）<input id="ai-original-category-${Number(row.id)}" type="text" value="${aiOriginalEscape(row.category_id || "")}" placeholder="例如 CBT1234"></label>
        <button class="secondary" type="button" onclick="saveAiOriginalProduct(${Number(row.id)})">保存参数并审核通过</button>
      </div>
    </article>`;
  }).join("") : '<div class="empty-state">暂无符合条件的 AI 原创产品；请先用泽顺插件采集 1688 商品。</div>';
  updateAiOriginalSelection();
}

function openAiOriginalEditor(id) {
  const row = aiOriginalEditorRow(id);
  const dialog = document.getElementById("ai-original-editor-dialog");
  if (!row || !dialog) return;
  const original = aiOriginalSource(row);
  const prepared = row.ai_original && typeof row.ai_original === "object" ? row.ai_original : {};
  aiOriginalEditorState = {
    id: Number(id),
    row,
    schema: [],
    attributes: Array.isArray(prepared.attributes) ? JSON.parse(JSON.stringify(prepared.attributes)) : [],
    variations: Array.isArray(prepared.variations) ? JSON.parse(JSON.stringify(prepared.variations)) : (Array.isArray(original.variations) ? JSON.parse(JSON.stringify(original.variations)) : [])
  };
  document.getElementById("ai-original-editor-title").textContent = original.title || row.title || "编辑 1688 商品";
  document.getElementById("ai-original-editor-source-title").value = original.title || "";
  document.getElementById("ai-original-editor-source-description").value = original.description_text || "";
  document.getElementById("ai-original-editor-category-id").value = row.category_id || original.category_id || "";
  document.getElementById("ai-original-editor-category-name").value = row.category_name || original.category_name || "";
  document.getElementById("ai-original-editor-title-es").value = prepared.title_es || "";
  document.getElementById("ai-original-editor-title-pt").value = prepared.title_pt || "";
  document.getElementById("ai-original-editor-description-es").value = prepared.description_es || "";
  document.getElementById("ai-original-editor-description-pt").value = prepared.description_pt || "";
  document.getElementById("ai-original-editor-category-results").innerHTML = "";
  document.getElementById("ai-original-editor-category-hint").textContent = row.category_id ? `当前类目：${row.category_id}${row.category_name ? ` · ${row.category_name}` : ""}` : "输入商品关键词搜索 Mercado CBT 类目";
  renderAiOriginalCategoryFields();
  renderAiOriginalEditorVariations();
  dialog.showModal();
  if (row.category_id) loadAiOriginalCategoryAttributes(row.category_id, false);
}

function closeAiOriginalEditor() {
  document.getElementById("ai-original-editor-dialog")?.close();
  aiOriginalEditorState = null;
}

async function searchAiOriginalCategories() {
  const query = document.getElementById("ai-original-editor-category-search")?.value?.trim() || "";
  const node = document.getElementById("ai-original-editor-category-results");
  if (query.length < 2) { if (node) node.innerHTML = '<span class="ai-original-editor-empty">请输入至少 2 个字符。</span>'; return; }
  if (node) node.innerHTML = "正在搜索类目…";
  try {
    const tokenId = aiOriginalEditorTokenId();
    const response = await fetch(`/api/ai-original-products/categories/search?q=${encodeURIComponent(query)}${tokenId ? `&token_id=${encodeURIComponent(tokenId)}` : ""}`, {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    const rows = payload.data?.rows || [];
    node.innerHTML = rows.length ? rows.map((row, index) => `<button type="button" class="ai-original-category-option" data-category-index="${index}"><strong>${aiOriginalEscape(row.category_name)}</strong><small>${aiOriginalEscape(row.category_id)}</small></button>`).join("") : '<span class="ai-original-editor-empty">没有找到 CBT 类目，请换一个关键词。</span>';
    node.querySelectorAll("[data-category-index]").forEach(button => button.addEventListener("click", () => {
      const row = rows[Number(button.dataset.categoryIndex)];
      selectAiOriginalCategory(row?.category_id, row?.category_name);
    }));
  } catch (error) { if (node) node.innerHTML = `<span class="ai-original-editor-error">类目搜索失败：${aiOriginalEscape(error.message || error)}</span>`; }
}

async function selectAiOriginalCategory(categoryId, categoryName) {
  document.getElementById("ai-original-editor-category-id").value = categoryId || "";
  document.getElementById("ai-original-editor-category-name").value = categoryName || "";
  document.getElementById("ai-original-editor-category-hint").textContent = `已选择：${categoryId}${categoryName ? ` · ${categoryName}` : ""}`;
  await loadAiOriginalCategoryAttributes(categoryId, true);
}

async function loadAiOriginalCategoryAttributes(categoryId, showStatus = true) {
  const normalized = String(categoryId || "").trim().toUpperCase();
  if (!normalized) { aiOriginalEditorState.schema = []; renderAiOriginalCategoryFields(); return; }
  if (showStatus) document.getElementById("ai-original-editor-category-hint").textContent = `正在读取 ${normalized} 的必填/选填字段…`;
  try {
    const tokenId = aiOriginalEditorTokenId();
    const response = await fetch(`/api/ai-original-products/categories/${encodeURIComponent(normalized)}/attributes${tokenId ? `?token_id=${encodeURIComponent(tokenId)}` : ""}`, {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    aiOriginalEditorState.schema = [...(payload.data?.required || []), ...(payload.data?.optional || [])];
    renderAiOriginalCategoryFields();
    document.getElementById("ai-original-editor-category-hint").textContent = `已读取 ${aiOriginalEditorState.schema.filter(item => item.required).length} 个必填、${aiOriginalEditorState.schema.filter(item => !item.required).length} 个选填字段`;
  } catch (error) {
    aiOriginalEditorState.schema = [];
    renderAiOriginalCategoryFields();
    document.getElementById("ai-original-editor-category-hint").textContent = `字段读取失败：${error.message || error}`;
  }
}

function addAiOriginalEditorVariation() {
  if (!aiOriginalEditorState) return;
  aiOriginalEditorState.variations = [...(aiOriginalEditorState.variations || []), {label: "", attribute_combinations: []}];
  renderAiOriginalEditorVariations();
}

function removeAiOriginalEditorVariation(index) {
  if (!aiOriginalEditorState) return;
  aiOriginalEditorState.variations = (aiOriginalEditorState.variations || []).filter((_, position) => position !== Number(index));
  renderAiOriginalEditorVariations();
}

function collectAiOriginalEditorPayload() {
  const data = {
    source_title: document.getElementById("ai-original-editor-source-title")?.value?.trim() || "",
    source_description: document.getElementById("ai-original-editor-source-description")?.value?.trim() || "",
    category_id: document.getElementById("ai-original-editor-category-id")?.value?.trim() || "",
    category_name: document.getElementById("ai-original-editor-category-name")?.value?.trim() || "",
    title_es: document.getElementById("ai-original-editor-title-es")?.value?.trim() || "",
    title_pt: document.getElementById("ai-original-editor-title-pt")?.value?.trim() || "",
    description_es: document.getElementById("ai-original-editor-description-es")?.value?.trim() || "",
    description_pt: document.getElementById("ai-original-editor-description-pt")?.value?.trim() || "",
    attributes: collectAiOriginalEditorAttributes(),
    variations: collectAiOriginalEditorVariations()
  };
  aiOriginalEditorState.attributes = data.attributes;
  aiOriginalEditorState.variations = data.variations;
  return data;
}

async function saveAiOriginalEditor(close = true) {
  if (!aiOriginalEditorState?.id) return false;
  const id = aiOriginalEditorState.id;
  const payload = collectAiOriginalEditorPayload();
  try {
    const response = await fetch(`/api/ai-original-products/${id}/listing`, {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok || data.status !== "success") throw new Error(data.message || `HTTP ${response.status}`);
    aiOriginalStatus(`产品 ${id} 的分类、字段、详情和变体已保存`, "success");
    if (close) closeAiOriginalEditor();
    await loadAiOriginalProducts();
    return true;
  } catch (error) { document.getElementById("ai-original-editor-status").textContent = `保存失败：${error.message || error}`; return false; }
}

async function translateAiOriginalEditor(language) {
  if (!aiOriginalEditorState?.id) return;
  const saved = await saveAiOriginalEditor(false);
  if (!saved) return;
  const label = language === "es" ? "西班牙语" : "葡萄牙语";
  const status = document.getElementById("ai-original-editor-status");
  status.textContent = `正在翻译标题、描述、属性和变体为${label}…`;
  try {
    const response = await fetch(`/api/ai-original-products/${aiOriginalEditorState.id}/translate`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({language})});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    const row = payload.data || {};
    const prepared = row.ai_original || {};
    document.getElementById(`ai-original-editor-title-${language}`).value = prepared[`title_${language}`] || "";
    document.getElementById(`ai-original-editor-description-${language}`).value = prepared[`description_${language}`] || "";
    aiOriginalEditorState.attributes = Array.isArray(prepared.attributes) ? prepared.attributes : aiOriginalEditorState.attributes;
    aiOriginalEditorState.variations = Array.isArray(prepared.variations) ? prepared.variations : aiOriginalEditorState.variations;
    renderAiOriginalCategoryFields();
    renderAiOriginalEditorVariations();
    status.textContent = `${label}翻译完成，请检查内容后点击保存`;
    await loadAiOriginalProducts();
  } catch (error) { status.textContent = `翻译失败：${error.message || error}`; }
}

async function loadAiOriginalProducts() {
  const search = document.getElementById("ai-original-search")?.value?.trim() || "";
  aiOriginalStatus("正在读取 1688 产品...");
  try {
    const response = await fetch(`/api/ai-original-products?limit=500&search=${encodeURIComponent(search)}`, {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    aiOriginalRows = payload.data?.rows || [];
    let transferredIds = [];
    try {
      const stored = JSON.parse(localStorage.getItem("mercado.aiOriginalSelected") || "[]");
      transferredIds = Array.isArray(stored) ? stored.map(Number).filter(id => id > 0) : [];
    } catch (_error) {}
    transferredIds.forEach(id => aiOriginalSelected.add(id));
    const validIds = new Set(aiOriginalRows.map(row => Number(row.id)));
    aiOriginalSelected = new Set([...aiOriginalSelected].filter(id => validIds.has(id)));
    if (transferredIds.length) {
      try { localStorage.removeItem("mercado.aiOriginalSelected"); } catch (_error) {}
    }
    renderAiOriginalProducts();
    aiOriginalStatus(`已加载 ${aiOriginalRows.length} 件 1688 产品`, "success");
  } catch (error) { aiOriginalStatus(`读取失败：${error.message || error}`, "error"); }
}

function toggleAiOriginalProduct(id, checked) {
  if (checked) aiOriginalSelected.add(Number(id)); else aiOriginalSelected.delete(Number(id));
  updateAiOriginalSelection();
}

function toggleAllAiOriginalProducts(checked) {
  const filter = document.getElementById("ai-original-status-filter")?.value || "";
  aiOriginalRows.filter(row => !filter || row.ai_status === filter).forEach(row => {
    if (checked) aiOriginalSelected.add(Number(row.id)); else aiOriginalSelected.delete(Number(row.id));
  });
  renderAiOriginalProducts();
}

function updateAiOriginalSelection() {
  const count = aiOriginalSelected.size;
  const summary = document.getElementById("ai-original-selection");
  if (summary) summary.textContent = `已选择 ${count} 件`;
  const process = document.getElementById("ai-original-process");
  const publish = document.getElementById("ai-original-publish");
  if (process) process.disabled = !count;
  if (publish) publish.disabled = !count;
}

async function processSelectedAiOriginalProducts() {
  const ids = [...aiOriginalSelected];
  if (!ids.length) return;
  const button = document.getElementById("ai-original-process");
  button.disabled = true;
  aiOriginalStatus(`正在创建 ${ids.length} 件 AI 原创任务...`);
  try {
    const response = await fetch("/api/ai-original-products/process", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      product_item_ids: ids,
      api_key: document.getElementById("ai-original-api-key")?.value || "",
      base_url: document.getElementById("ai-original-base-url")?.value || "",
      model: document.getElementById("ai-original-model")?.value || "",
      image_api_key: document.getElementById("ai-original-image-api-key")?.value || "",
      image_base_url: document.getElementById("ai-original-image-base-url")?.value || "",
      image_model: document.getElementById("ai-original-image-model")?.value || "",
      workers: Number(document.getElementById("ai-original-workers")?.value || 3)
    })});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    renderAiOriginalTaskStatus(payload.data || {});
    loadAiOriginalTaskStatus();
  } catch (error) { aiOriginalStatus(`启动失败：${error.message || error}`, "error"); button.disabled = false; }
}

function renderAiOriginalTaskStatus(data) {
  const text = `${data.message || "AI 原创任务"} · ${Number(data.processed_count || 0)}/${Number(data.requested_count || 0)} · 成功 ${Number(data.completed_count || 0)} · 失败 ${Number(data.failed_count || 0)}`;
  aiOriginalStatus(text, data.status === "error" ? "error" : (!data.running && data.status !== "idle" ? "success" : ""));
  const button = document.getElementById("ai-original-process");
  if (button) { button.textContent = data.running ? "AI 任务执行中..." : "执行所选 AI 任务"; button.disabled = Boolean(data.running) || !aiOriginalSelected.size; }
}

async function loadAiOriginalTaskStatus() {
  if (aiOriginalTaskTimer) clearTimeout(aiOriginalTaskTimer);
  try {
    const response = await fetch("/api/ai-original-products/process/status", {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    renderAiOriginalTaskStatus(payload.data || {});
    if (payload.data?.running) aiOriginalTaskTimer = setTimeout(loadAiOriginalTaskStatus, 1500);
    else if (payload.data?.status !== "idle") await loadAiOriginalProducts();
  } catch (error) { aiOriginalStatus(`任务状态读取失败：${error.message || error}`, "error"); }
}

async function saveAiOriginalProduct(id) {
  const weight = Number(document.getElementById(`ai-original-weight-${id}`)?.value || 0);
  const netProceeds = Number(document.getElementById(`ai-original-net-${id}`)?.value || 0);
  const row = aiOriginalRows.find(item => Number(item.id) === Number(id));
  if (row?.ai_status !== "completed") {
    aiOriginalStatus("请先完成该产品的 AI 原创任务，再审核通过", "error");
    return;
  }
  if (!(weight > 0) || !(netProceeds > 0)) {
    aiOriginalStatus("审核通过前必须填写大于 0 的实重和净收益 USD", "error");
    return;
  }
  try {
    const response = await fetch(`/api/ai-original-products/${id}`, {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      weight_g: weight,
      net_proceeds_usd: netProceeds,
      category_id: document.getElementById(`ai-original-category-${id}`)?.value?.trim() || "",
      review_status: "approved"
    })});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    aiOriginalStatus(`产品 ${id} 已保存并审核通过`, "success");
    await loadAiOriginalProducts();
  } catch (error) { aiOriginalStatus(`保存失败：${error.message || error}`, "error"); }
}

async function loadAiOriginalStores() {
  const select = document.getElementById("ai-original-stores");
  if (!select || select.dataset.loaded === "1") return;
  try {
    const response = await fetch("/api/mercado-tokens", {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    const rows = (payload.data?.rows || []).filter(row => Number(row.id) > 0 && row.enabled !== false && String(row.site_id || "").toUpperCase() === "CBT" && (row.status !== "expired" || row.has_refresh_token));
    select.innerHTML = rows.map(row => `<option value="${Number(row.id)}">${aiOriginalEscape(row.display_name || row.nickname || `账号 ${row.id}`)} · ${aiOriginalEscape(row.site_id || "CBT")}</option>`).join("") || '<option value="">暂无可用店铺</option>';
    select.dataset.loaded = "1";
  } catch (error) { select.innerHTML = '<option value="">店铺加载失败</option>'; }
}

async function publishSelectedAiOriginalProducts() {
  const ids = [...aiOriginalSelected];
  const tokenIds = [...(document.getElementById("ai-original-stores")?.selectedOptions || [])].map(option => Number(option.value)).filter(Boolean);
  const siteIds = [...document.querySelectorAll('input[name="ai-original-site"]:checked')].map(input => input.value);
  const status = document.getElementById("ai-original-publish-status");
  if (!ids.length || !tokenIds.length || !siteIds.length) { status.textContent = "请选择产品、对应店铺和至少一个目标站点"; status.className = "ai-original-status error"; return; }
  if (!window.confirm(`确定把 ${ids.length} 件产品上架到 ${tokenIds.length} 个店铺、${siteIds.length} 个站点吗？`)) return;
  try {
    const response = await fetch("/api/mercado-products/publish", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      product_item_ids: ids, selection_mode: "accounts", token_ids: tokenIds, site_ids: siteIds,
      quantity: Number(document.getElementById("ai-original-quantity")?.value || 500),
      worker_count: Number(document.getElementById("ai-original-publish-workers")?.value || 8)
    })});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    status.textContent = payload.data?.message || "已启动批量上架"; status.className = "ai-original-status success";
    loadAiOriginalPublishStatus();
  } catch (error) { status.textContent = `上架失败：${error.message || error}`; status.className = "ai-original-status error"; }
}

async function loadAiOriginalPublishStatus() {
  if (aiOriginalPublishTimer) clearTimeout(aiOriginalPublishTimer);
  const status = document.getElementById("ai-original-publish-status");
  try {
    const response = await fetch("/api/mercado-products/publish/status", {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") return;
    const data = payload.data || {};
    if (data.status && data.status !== "idle") status.textContent = `${data.message || "批量上架"} · ${Number(data.processed_count || 0)}/${Number(data.requested_count || 0)} · 成功 ${Number(data.published_count || 0)} · 失败 ${Number(data.failed_count || 0)}`;
    if (data.running) aiOriginalPublishTimer = setTimeout(loadAiOriginalPublishStatus, 1600);
    else if (data.status && data.status !== "idle") await loadAiOriginalProducts();
  } catch (_) {}
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("ai-original-search")?.addEventListener("keydown", event => { if (event.key === "Enter") loadAiOriginalProducts(); });
  document.getElementById("ai-original-status-filter")?.addEventListener("change", renderAiOriginalProducts);
  if (new URLSearchParams(location.search).get("tab") === "ai-original-products" && typeof switchTab === "function") switchTab("ai-original-products");
});
