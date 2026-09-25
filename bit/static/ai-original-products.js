"use strict";

let aiOriginalRows = [];
let aiOriginalSelected = new Set();
let aiOriginalDeleteRunning = false;
let aiOriginalTaskTimer = null;
let aiOriginalPublishTimer = null;
let aiOriginalEditorState = null;
let aiOriginalStoreRows = [];
let aiOriginalLastTaskStatus = null;

function aiOriginalEscape(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function aiOriginalDisplayImageUrl(value) {
  let source = String(value || "").trim();
  if (!source) return "";
  try {
    const parsed = new URL(source, window.location.origin);
    const hostname = parsed.hostname.toLowerCase();
    if (["127.0.0.1", "localhost"].includes(hostname) && parsed.pathname.startsWith("/api/ai-original-products/images/")) {
      return `${parsed.pathname}${parsed.search}`;
    }
    if (/^cbu\d+\.alicdn\.com$/.test(hostname) && parsed.pathname.startsWith("/img/ibank/")) {
      parsed.pathname = parsed.pathname.replace(/\.(jpe?g|png|webp)(?:_[^/?#]*)+$/i, ".$1");
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

function renderAiOriginalFailureLog(lines) {
  const log = document.getElementById("ai-original-task-log");
  if (!log) return;
  log.hidden = !lines.length;
  log.textContent = lines.length ? `失败原因（${lines.length}）\n${lines.join("\n")}` : "";
}

async function aiOriginalReadJsonResponse(response, action) {
  const body = await response.text();
  try {
    return body ? JSON.parse(body) : {};
  } catch (_error) {
    if (response.redirected && /\/login(?:[?#]|$)/i.test(response.url || "")) {
      throw new Error("登录状态已失效，请刷新页面并重新登录");
    }
    const html = /^\s*(?:<!doctype\s+html|<html\b)/i.test(body);
    const title = html ? body.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1]?.replace(/\s+/g, " ").trim() : "";
    const detail = title ? `：${title}` : "";
    throw new Error(`${action}接口返回${html ? "了网页" : "了无效数据"}（HTTP ${response.status}${detail}）`);
  }
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

function aiOriginalVariationDimensionsFromRows(rows) {
  const dimensions = new Map();
  (Array.isArray(rows) ? rows : []).forEach(variation => {
    const combinations = variation?.attribute_combinations || variation?.attributes || variation?.properties || [];
    if (!Array.isArray(combinations)) return;
    combinations.forEach((attribute, index) => {
      if (!attribute || typeof attribute !== "object") return;
      const id = String(attribute.id || "");
      if (["SELLER_SKU", "SKU"].includes(id.toUpperCase())) return;
      const name = String(attribute.name || id || `规格${index + 1}`);
      const key = (id || name).toLocaleLowerCase();
      const value = String(attribute.value_name ?? attribute.value ?? attribute.text ?? "").trim();
      if (!value) return;
      if (!dimensions.has(key)) dimensions.set(key, {id, name, values: []});
      if (!dimensions.get(key).values.includes(value)) dimensions.get(key).values.push(value);
    });
  });
  return [...dimensions.values()];
}

function renderAiOriginalEditorDimensions() {
  const node = document.getElementById("ai-original-editor-variation-dimensions");
  if (!node) return;
  const rows = Array.isArray(aiOriginalEditorState?.variationDimensions) ? aiOriginalEditorState.variationDimensions : [];
  node.innerHTML = rows.length ? rows.map((dimension, index) => `<div class="ai-original-variant-dimension-row">
    <label><span>规格名称</span><input data-variation-dimension="${index}" data-dimension-field="name" value="${aiOriginalEscape(dimension.name || "")}" placeholder="例如 Color"></label>
    <label><span>规格选项（逗号分隔）</span><input data-variation-dimension="${index}" data-dimension-field="values" value="${aiOriginalEscape((dimension.values || []).join(", "))}" placeholder="例如 Red, Blue"></label>
    <button type="button" class="text-button" onclick="removeAiOriginalEditorDimension(${index})">删除</button>
  </div>`).join("") : '<span class="ai-original-variant-hint">按需填写规格和选项，再生成 SKU 组合。</span>';
}

function collectAiOriginalEditorDimensions() {
  const node = document.getElementById("ai-original-editor-variation-dimensions");
  const rows = Array.isArray(aiOriginalEditorState?.variationDimensions) ? aiOriginalEditorState.variationDimensions : [];
  if (!node) return rows;
  return rows.map((dimension, index) => {
    const field = name => node.querySelector(`[data-variation-dimension="${index}"][data-dimension-field="${name}"]`);
    const name = field("name")?.value?.trim() || `规格${index + 1}`;
    const values = (field("values")?.value || "").split(/[，,]/).map(value => value.trim()).filter(Boolean);
    return {...dimension, name, values: [...new Set(values)].slice(0, 200)};
  });
}

function addAiOriginalEditorDimension() {
  if (!aiOriginalEditorState) return;
  aiOriginalEditorState.variationDimensions = collectAiOriginalEditorDimensions();
  aiOriginalEditorState.variationDimensions.push({id: "", name: `规格${aiOriginalEditorState.variationDimensions.length + 1}`, values: []});
  renderAiOriginalEditorDimensions();
}

function removeAiOriginalEditorDimension(index) {
  if (!aiOriginalEditorState) return;
  aiOriginalEditorState.variationDimensions = collectAiOriginalEditorDimensions().filter((_, position) => position !== Number(index));
  renderAiOriginalEditorDimensions();
}

function aiOriginalVariationAttributeId(name) {
  const key = String(name || "").toLocaleLowerCase();
  if (/颜色|色|^color$|^colour$|^cor$/.test(key)) return "COLOR";
  if (/尺寸|尺码|^size$|^talla$|^tamanho$/.test(key)) return "SIZE";
  if (/材质|材料|^material$|^materia$/.test(key)) return "MATERIAL";
  if (/风格|款式|^style$|^estilo$/.test(key)) return "STYLE";
  return String(name || "").trim();
}

function generateAiOriginalEditorVariations() {
  if (!aiOriginalEditorState) return;
  const dimensions = collectAiOriginalEditorDimensions().filter(item => item.name && item.values.length);
  const status = document.getElementById("ai-original-editor-status");
  if (!dimensions.length) { if (status) status.textContent = "请先填写至少一个规格及其选项。"; return; }
  aiOriginalEditorState.variations = collectAiOriginalEditorVariations();
  let combinations = [[]];
  for (const dimension of dimensions) {
    combinations = combinations.flatMap(previous => dimension.values.map(value => [...previous, {dimension, value}]));
    if (combinations.length > 200) { if (status) status.textContent = "规格组合超过 200 个，请减少规格选项。"; return; }
  }
  const previousRows = new Map(aiOriginalEditorState.variations.map(variation => [
    aiOriginalVariationLabel(variation).toLocaleLowerCase(), variation
  ]));
  aiOriginalEditorState.variations = combinations.map(combination => {
    const label = combination.map(({dimension, value}) => `${dimension.name}: ${value}`).join(" / ");
    const prior = previousRows.get(label.toLocaleLowerCase()) || {};
    const oldAttributes = prior.attribute_combinations || prior.attributes || prior.properties || [];
    const attributes = combination.map(({dimension, value}, index) => {
      const old = Array.isArray(oldAttributes) ? oldAttributes.find(attribute =>
        String(attribute?.id || "").toLocaleLowerCase() === String(dimension.id || "").toLocaleLowerCase() ||
        String(attribute?.name || "").toLocaleLowerCase() === dimension.name.toLocaleLowerCase()
      ) || oldAttributes[index] || {} : {};
      const attribute = {...old, id: String(dimension.id || old.id || aiOriginalVariationAttributeId(dimension.name)),
        name: dimension.name, value_name: value};
      const nameChanged = String(old.name || old.id || "") !== dimension.name;
      const valueChanged = String(old.value_name || old.value || "") !== value;
      if (valueChanged) delete attribute.value_id;
      if (nameChanged || valueChanged) {
        for (const key of ["name_es", "name_pt", "value_name_es", "value_name_pt", "value_es", "value_pt"]) delete attribute[key];
      }
      return attribute;
    });
    return {...prior, label, attribute_combinations: attributes};
  });
  aiOriginalEditorState.variationDimensions = dimensions;
  renderAiOriginalEditorVariations();
  renderAiOriginalEditorDimensions();
  if (status) status.textContent = `已生成 ${aiOriginalEditorState.variations.length} 个规格组合；已有匹配 SKU 的库存、价格和图片已保留。`;
}

function renderAiOriginalEditorVariations() {
  const node = document.getElementById("ai-original-editor-variations");
  if (!node) return;
  const rows = Array.isArray(aiOriginalEditorState?.variations) ? aiOriginalEditorState.variations : [];
  node.innerHTML = rows.length ? rows.map((variation, index) => `<tr>
    <td><input data-variant-index="${index}" data-variant-field="label" value="${aiOriginalEscape(aiOriginalVariationLabel(variation))}" placeholder="颜色 / 尺码"></td>
    <td><input data-variant-index="${index}" data-variant-field="sku" value="${aiOriginalEscape(variation.seller_sku ?? variation.sku ?? variation.skuCode ?? "")}" placeholder="SKU（选填）"></td>
    <td><input type="number" min="0" data-variant-index="${index}" data-variant-field="stock" value="${aiOriginalEscape(variation.available_quantity ?? variation.stock ?? variation.stock_text ?? "")}" placeholder="库存"></td>
    <td><input type="number" min="0" step="any" data-variant-index="${index}" data-variant-field="price" value="${aiOriginalEscape(variation.price ?? variation.price_text ?? "")}" placeholder="采购价"></td>
    <td><input type="number" step="any" data-variant-index="${index}" data-variant-field="extra-price" value="${aiOriginalEscape(variation.price_addition ?? variation.additional_price ?? variation.markup ?? "")}" placeholder="加价"></td>
    <td><input type="number" step="any" data-variant-index="${index}" data-variant-field="extra-weight" value="${aiOriginalEscape(variation.weight_addition_g ?? variation.additional_weight_g ?? variation.added_weight_g ?? "")}" placeholder="加重(g)"></td>
    <td><div class="ai-original-variant-image-cell">${aiOriginalVariantImages(variation)}<input data-variant-index="${index}" data-variant-field="image" value="${aiOriginalEscape(variation.image_url ?? variation.image ?? "")}" placeholder="图片链接"></div></td>
    <td><button type="button" class="text-button" onclick="removeAiOriginalEditorVariation(${index})">删除</button></td>
  </tr>`).join("") : '<tr><td colspan="8" class="ai-original-editor-empty">采集快照没有变体；可以手动新增。</td></tr>';
}

function aiOriginalVariantImages(variation) {
  const urls = [variation?.image_url, variation?.image,
    ...(Array.isArray(variation?.images) ? variation.images : [])]
    .map(value => typeof value === "object" ? (value.url || value.source || value.src || "") : value)
    .filter(value => /^https?:\/\//i.test(String(value || ""))).slice(0, 4);
  return urls.length ? `<span class="ai-original-variant-thumbnails">${urls.map(url => `<a href="${aiOriginalEscape(url)}" target="_blank" rel="noopener noreferrer"><img src="${aiOriginalEscape(url)}" alt="变体图片" loading="lazy" referrerpolicy="no-referrer"></a>`).join("")}</span>` : "";
}

function collectAiOriginalEditorVariations() {
  const rows = Array.isArray(aiOriginalEditorState?.variations) ? aiOriginalEditorState.variations : [];
  return rows.map((variation, index) => {
    const next = {...variation};
    const value = field => document.querySelector(`[data-variant-index="${index}"][data-variant-field="${field}"]`)?.value?.trim() || "";
    const label = value("label");
    if (label) next.label = label; else delete next.label;
    if (label) {
      const previous = next.attribute_combinations || next.attributes || next.properties || [];
      next.attribute_combinations = label.split(/\s*\/\s*/).map((part, attributeIndex) => {
        const pieces = part.split(/[：:]/, 2).map(item => item.trim());
        const old = Array.isArray(previous) ? previous.find(attribute =>
          String(attribute?.name || attribute?.id || "").toLocaleLowerCase() === String(pieces.length > 1 ? pieces[0] : "").toLocaleLowerCase()
        ) || previous[attributeIndex] || {} : {};
        const name = pieces.length > 1 ? pieces[0] : String(old.name || old.id || `规格${attributeIndex + 1}`);
        const valueName = pieces.length > 1 ? pieces[1] : pieces[0];
        if (!name || !valueName) return null;
        const attribute = {...old, id: old.id || aiOriginalVariationAttributeId(name), name, value_name: valueName};
        const nameChanged = String(old.name || old.id || "") !== name;
        const valueChanged = String(old.value_name || old.value || "") !== valueName;
        if (valueChanged) delete attribute.value_id;
        if (nameChanged || valueChanged) {
          for (const key of ["name_es", "name_pt", "value_name_es", "value_name_pt", "value_es", "value_pt"]) delete attribute[key];
        }
        return attribute;
      }).filter(Boolean).slice(0, 20);
    }
    const sku = value("sku");
    if (sku) next.seller_sku = sku; else delete next.seller_sku;
    const price = value("price");
    if (price) { next.price_text = price; next.price = Number.isFinite(Number(price)) ? Number(price) : price; }
    else if (next.price_text) delete next.price_text;
    const stock = value("stock");
    if (stock) { next.stock_text = stock; next.available_quantity = Number.isFinite(Number(stock)) ? Number(stock) : stock; }
    else if (next.stock_text) delete next.stock_text;
    const extraPrice = value("extra-price");
    if (extraPrice) next.price_addition = Number.isFinite(Number(extraPrice)) ? Number(extraPrice) : extraPrice;
    else delete next.price_addition;
    const extraWeight = value("extra-weight");
    if (extraWeight) next.weight_addition_g = Number.isFinite(Number(extraWeight)) ? Number(extraWeight) : extraWeight;
    else delete next.weight_addition_g;
    const image = value("image");
    if (image) next.image_url = image; else if (next.image_url) delete next.image_url;
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
    const missingRequired = (row.ai_original?.missing_required_attributes || []).filter(item => !aiAttributes.some(attribute => attribute.id === item.id && (attribute.value_name || attribute.value_id)));
    const ready = status === "completed" && !missingRequired.length && aiAttributes.some(attribute => !["BRAND", "ITEM_CONDITION"].includes(String(attribute.id || "").toUpperCase()));
    const aiImageReady = ["ai_image_edit", "local_background_removal"].includes(row.ai_original?.image_generation_method) && aiImage.includes("-ai-white.jpg");
    const suggestedNet = Number(row.suggested_net_proceeds_usd || 0);
    const maxVariantPrice = Number(row.source_variation_max_price_cny || 0);
    const netProceedsValue = row.net_proceeds_usd || (suggestedNet > 0 ? suggestedNet : "");
    const netProceedsHint = suggestedNet > 0
      ? `1688最高变体价 ¥${maxVariantPrice.toFixed(2)} ÷ 6.7 向上取整 = USD ${suggestedNet}`
      : "未读取到变体价格，请手动填写 USD 净收益";
    return `<article class="ai-original-card">
      <input type="checkbox" value="${Number(row.id)}" ${aiOriginalSelected.has(Number(row.id)) ? "checked" : ""} onchange="toggleAiOriginalProduct(${Number(row.id)}, this.checked)">
      <div class="ai-original-image-pair">
        <figure>${sourceImage ? `<img src="${aiOriginalEscape(sourceImage)}" data-image-candidates="${aiOriginalEscape(JSON.stringify(aiOriginalSourceImageFallbacks(original)))}" alt="1688 原始主图" loading="lazy" referrerpolicy="no-referrer" onerror="aiOriginalImageError(this)">` : '<span class="ai-original-image-placeholder">暂无原图</span>'}<figcaption>1688 原图</figcaption></figure>
        <figure>${aiImage ? `<img src="${aiOriginalEscape(aiImage)}" alt="AI 美客多白底主图" loading="lazy" referrerpolicy="no-referrer" onerror="aiOriginalImageError(this)">` : '<span class="ai-original-image-placeholder">待生成</span>'}<figcaption>AI 白底主图</figcaption></figure>
      </div>
      <div class="ai-original-source"><span class="ai-original-section-label source">1688 原始资料</span><h4>${aiOriginalEscape(original.title || row.title)}</h4><p>1688 编号：${aiOriginalEscape(original.source_1688_item_id || row.source_item_id)}</p><p>采购价：${aiOriginalEscape(original.price ?? row.price ?? "-")} CNY</p><p>包装尺寸：${aiOriginalEscape([row.package_length_cm, row.package_width_cm, row.package_height_cm].some(value => value) ? `${row.package_length_cm || "-"} × ${row.package_width_cm || "-"} × ${row.package_height_cm || "-"} cm` : "未设置")}</p><a href="${aiOriginalEscape(original.source_url || row.source_url || "#")}" target="_blank" rel="noopener">打开 1688 详情页 ↗</a>${row.ai_error ? `<p class="bad">${aiOriginalEscape(row.ai_error)}</p>` : ""}</div>
      <div class="ai-original-copy"><span class="ai-original-section-label generated">AI 美客多刊登稿</span><strong>西语标题 ${titleEs.length}/60</strong><p class="${titleEs.length > 60 ? "bad" : ""}">${aiOriginalEscape(titleEs)}</p><strong>葡语标题 ${titlePt.length}/60</strong><p class="${titlePt.length > 60 ? "bad" : ""}">${aiOriginalEscape(titlePt)}</p><strong>AI 商品属性（${aiAttributes.length}）</strong>${renderAiOriginalAttributes(row.ai_original?.attributes)}${missingRequired.length ? `<p class="bad">必填属性待补充：${aiOriginalEscape(missingRequired.map(item => item.name || item.id).join("、"))}</p>` : ""}<details><summary>查看 AI 双语详情</summary><p>${aiOriginalEscape(row.description_es || "待生成西语详情")}</p><p>${aiOriginalEscape(row.description_pt || "待生成葡萄牙语详情")}</p></details><div class="ai-original-readiness"><span class="${ready ? "ok" : "pending"}">${ready ? "✓" : "!"} AI 属性</span><span class="${aiImageReady ? "ok" : "pending"}">${aiImageReady ? "✓" : "!"} AI 白底主图</span><span class="${row.category_id ? "ok" : "pending"}">${row.category_id ? "✓" : "!"} CBT 类目</span></div><button class="secondary ai-original-edit-button" type="button" onclick="openAiOriginalEditor(${Number(row.id)})">编辑分类、字段、详情与变体</button></div>
      <div class="ai-original-fields">
        <label>实重(g)<input id="ai-original-weight-${Number(row.id)}" type="number" min="1" step="1" value="${aiOriginalEscape(row.weight_g || "")}"></label>
        <label>净收益 USD<input id="ai-original-net-${Number(row.id)}" type="number" min="0.01" step="0.01" value="${aiOriginalEscape(netProceedsValue)}"><small class="ai-original-net-hint">${aiOriginalEscape(netProceedsHint)}</small></label>
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
    variations: Array.isArray(prepared.variations) ? JSON.parse(JSON.stringify(prepared.variations)) : (Array.isArray(original.variations) ? JSON.parse(JSON.stringify(original.variations)) : []),
    variationDimensions: Array.isArray(prepared.variation_dimensions) ? JSON.parse(JSON.stringify(prepared.variation_dimensions))
      : (Array.isArray(original.variation_dimensions) ? JSON.parse(JSON.stringify(original.variation_dimensions)) : aiOriginalVariationDimensionsFromRows(prepared.variations || original.variations || []))
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
  renderAiOriginalEditorDimensions();
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
  const variations = collectAiOriginalEditorVariations();
  const variationDimensions = collectAiOriginalEditorDimensions();
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
    variations,
    variation_dimensions: variationDimensions
  };
  aiOriginalEditorState.attributes = data.attributes;
  aiOriginalEditorState.variations = data.variations;
  aiOriginalEditorState.variationDimensions = data.variation_dimensions;
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

async function loadAiOriginalProducts({preserveTaskStatus = false} = {}) {
  const search = document.getElementById("ai-original-search")?.value?.trim() || "";
  if (!preserveTaskStatus) aiOriginalStatus("正在读取 1688 产品...");
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
    if (!aiOriginalLastTaskStatus || aiOriginalLastTaskStatus.status === "idle") {
      renderAiOriginalFailureLog(aiOriginalRows.filter(row => row.ai_status === "failed" && row.ai_error)
        .slice(0, 20).map(row => `产品 ${row.id}：${row.ai_error}`));
    }
    if (!preserveTaskStatus) aiOriginalStatus(`已加载 ${aiOriginalRows.length} 件 1688 产品`, "success");
  } catch (error) { if (!preserveTaskStatus) aiOriginalStatus(`读取失败：${error.message || error}`, "error"); }
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
  const publishSelection = document.getElementById("ai-original-publish-selection");
  if (publishSelection) publishSelection.textContent = `${count} 件`;
  const deleteButton = document.getElementById("ai-original-delete");
  if (deleteButton) deleteButton.disabled = !count || aiOriginalDeleteRunning;
  const filter = document.getElementById("ai-original-status-filter")?.value || "";
  const visible = aiOriginalRows.filter(row => !filter || row.ai_status === filter);
  const selectedCount = visible.filter(row => aiOriginalSelected.has(Number(row.id))).length;
  const selectAll = document.getElementById("ai-original-select-all");
  if (selectAll) {
    selectAll.checked = visible.length > 0 && selectedCount === visible.length;
    selectAll.indeterminate = selectedCount > 0 && selectedCount < visible.length;
  }
  const process = document.getElementById("ai-original-process");
  const publish = document.getElementById("ai-original-publish");
  if (process) process.disabled = !count;
  if (publish) publish.disabled = !count;
}

async function deleteSelectedAiOriginalProducts() {
  await deleteAiOriginalProductSelection([...aiOriginalSelected], aiOriginalStatus);
}

// Both areas display the same product records and must refresh together.
async function deleteAiOriginalProductSelection(ids, setStatus) {
  if (!ids.length || aiOriginalDeleteRunning) return;
  if (!window.confirm(`确定删除所选 ${ids.length} 件产品吗？删除后会同时从 AI 原创产品区和 1688 产品区移除，且无法撤销。`)) return;
  aiOriginalDeleteRunning = true;
  updateAiOriginalSelection();
  if (typeof products1688UpdateSelection === "function") products1688UpdateSelection();
  setStatus(`正在删除 ${ids.length} 件产品…`);
  try {
    const response = await fetch("/api/mercado-products", {
      method: "DELETE", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({product_item_ids: ids})
    });
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    const removed = new Set(ids);
    ids.forEach(id => aiOriginalSelected.delete(id));
    aiOriginalRows = aiOriginalRows.filter(row => !removed.has(Number(row.id)));
    if (aiOriginalEditorState && removed.has(Number(aiOriginalEditorState.id))) closeAiOriginalEditor();
    renderAiOriginalProducts();
    if (typeof products1688Rows !== "undefined") {
      ids.forEach(id => products1688Selected.delete(id));
      products1688Rows = products1688Rows.filter(row => !removed.has(Number(row.id)));
      products1688CloseDetail();
      products1688RenderRows();
    }
    try {
      const stored = JSON.parse(localStorage.getItem("mercado.aiOriginalSelected") || "[]");
      if (Array.isArray(stored)) localStorage.setItem("mercado.aiOriginalSelected", JSON.stringify(stored.filter(id => !removed.has(Number(id)))));
    } catch (_error) {}
    await Promise.all([loadAiOriginalProducts(), ...(typeof load1688Products === "function" ? [load1688Products(false)] : [])]);
    setStatus(`已删除 ${Number(payload.data?.deleted || 0)} 件产品`, "success");
  } catch (error) {
    setStatus(`删除失败：${error.message || error}`, "error");
  } finally {
    aiOriginalDeleteRunning = false;
    updateAiOriginalSelection();
    if (typeof products1688UpdateSelection === "function") products1688UpdateSelection();
  }
}

async function processSelectedAiOriginalProducts() {
  const ids = [...aiOriginalSelected];
  if (!ids.length) return;
  const button = document.getElementById("ai-original-process");
  button.disabled = true;
  aiOriginalStatus(`正在创建 ${ids.length} 件 AI 原创任务...`);
  try {
    const taskPayload = {
      product_item_ids: ids,
      token_id: aiOriginalEditorTokenId(),
      api_key: document.getElementById("ai-original-api-key")?.value || "",
      base_url: document.getElementById("ai-original-base-url")?.value || "",
      model: document.getElementById("ai-original-model")?.value || "",
      image_api_key: document.getElementById("ai-original-image-api-key")?.value || "",
      image_base_url: document.getElementById("ai-original-image-base-url")?.value || "",
      image_model: document.getElementById("ai-original-image-model")?.value || "",
    };
    if (window.canChangeTaskWorkers) {
      taskPayload.workers = Number(document.getElementById("ai-original-workers")?.value || 3);
    }
    const response = await fetch("/api/ai-original-products/process", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(taskPayload)});
    const payload = await aiOriginalReadJsonResponse(response, "启动 AI 任务");
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    renderAiOriginalTaskStatus(payload.data || {});
    loadAiOriginalTaskStatus();
  } catch (error) { aiOriginalStatus(`启动失败：${error.message || error}`, "error"); button.disabled = false; }
}

function renderAiOriginalTaskStatus(data) {
  aiOriginalLastTaskStatus = data;
  const failures = (Array.isArray(data.results) ? data.results : []).filter(item => item?.status === "failed");
  const firstFailure = failures[0];
  const failureDetail = firstFailure?.message ? `；失败示例：${String(firstFailure.message).slice(0, 240)}` : "";
  const text = `${data.message || "AI 原创任务"} · ${Number(data.processed_count || 0)}/${Number(data.requested_count || 0)} · 成功 ${Number(data.completed_count || 0)} · 失败 ${Number(data.failed_count || 0)}${failureDetail}`;
  const hasFailures = Number(data.failed_count || 0) > 0 || data.status === "error" || data.status === "partial";
  aiOriginalStatus(text, hasFailures ? "error" : (!data.running && data.status !== "idle" ? "success" : ""));
  const lines = failures.map(item => `产品 ${item.product_id || item.source_item_id || "未知"}：${item.message || "处理失败，查看服务日志"}`);
  if (!lines.length && data.status === "error") lines.push(data.message || "AI 原创任务失败，查看服务日志");
  if (data.status !== "idle") renderAiOriginalFailureLog(lines);
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
    else if (payload.data?.status !== "idle") await loadAiOriginalProducts({preserveTaskStatus: true});
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
    aiOriginalStoreRows = (payload.data?.rows || []).filter(row => Number(row.id) > 0 && row.enabled !== false && String(row.site_id || "").toUpperCase() === "CBT" && (row.status !== "expired" || row.has_refresh_token));
    select.dataset.loaded = "1";
    renderAiOriginalStoreFilters();
  } catch (error) {
    select.innerHTML = '<option value="">店铺加载失败</option>';
    const status = document.getElementById("ai-original-store-status");
    if (status) status.textContent = `店铺加载失败：${error.message || error}`;
  }
}

function aiOriginalSelectedSiteIds() {
  return [...document.querySelectorAll('input[name="ai-original-site"]:checked')].map(input => input.value);
}

function aiOriginalAccountSettings(account, siteIds = aiOriginalSelectedSiteIds()) {
  const settings = Array.isArray(account?.site_settings) ? account.site_settings : [];
  return siteIds.length
    ? settings.filter(setting => siteIds.includes(String(setting.site_id || "").trim().toUpperCase()))
    : settings;
}

function aiOriginalSettingMatchesFilters(setting, salesperson, groupName) {
  const owner = String(setting?.salesperson || "").trim();
  const group = String(setting?.group_name || "").trim();
  if (salesperson && (salesperson === "__unassigned__" ? Boolean(owner) : owner !== salesperson)) return false;
  if (groupName && (groupName === "__ungrouped__" ? Boolean(group) : group !== groupName)) return false;
  return true;
}

function renderAiOriginalStoreFilters() {
  const salespersonSelect = document.getElementById("ai-original-salesperson");
  const groupSelect = document.getElementById("ai-original-store-group");
  const select = document.getElementById("ai-original-stores");
  if (!select) return;
  const previousSalesperson = String(salespersonSelect?.value || "");
  const previousGroup = String(groupSelect?.value || "");
  const previousStores = new Set([...select.selectedOptions].map(option => option.value));
  const siteIds = aiOriginalSelectedSiteIds();
  const settings = aiOriginalStoreRows.flatMap(account => aiOriginalAccountSettings(account, siteIds));
  const salespeople = [...new Set(settings.map(setting => String(setting.salesperson || "").trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-CN"));
  const groupSalesperson = previousSalesperson;
  const groups = [...new Set(settings.filter(setting => {
    const owner = String(setting.salesperson || "").trim();
    return !groupSalesperson || (groupSalesperson === "__unassigned__" ? !owner : owner === groupSalesperson);
  }).map(setting => String(setting.group_name || "").trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-CN"));
  if (salespersonSelect) {
    salespersonSelect.innerHTML = ['<option value="">全部业务员</option>', '<option value="__unassigned__">未分配</option>', ...salespeople.map(name => `<option value="${aiOriginalEscape(name)}">${aiOriginalEscape(name)}</option>`)].join("");
    if ([...salespersonSelect.options].some(option => option.value === previousSalesperson)) salespersonSelect.value = previousSalesperson;
  }
  if (groupSelect) {
    groupSelect.innerHTML = ['<option value="">全部店铺组</option>', '<option value="__ungrouped__">未分组</option>', ...groups.map(name => `<option value="${aiOriginalEscape(name)}">${aiOriginalEscape(name)}</option>`)].join("");
    if ([...groupSelect.options].some(option => option.value === previousGroup)) groupSelect.value = previousGroup;
  }
  const salesperson = String(salespersonSelect?.value || "");
  const groupName = String(groupSelect?.value || "");
  const accounts = aiOriginalStoreRows.filter(account => {
    const applicable = aiOriginalAccountSettings(account, siteIds);
    if (!applicable.length) return !salesperson && !groupName;
    return applicable.some(setting => aiOriginalSettingMatchesFilters(setting, salesperson, groupName));
  });
  select.innerHTML = accounts.map(account => {
    const value = String(Number(account.id));
    const label = String(account.display_name || account.nickname || `账号 ${value}`);
    const matching = aiOriginalAccountSettings(account, siteIds).filter(setting => aiOriginalSettingMatchesFilters(setting, salesperson, groupName));
    const ownerGroups = [...new Set(matching.map(setting => `${String(setting.salesperson || "未分配").trim() || "未分配"} · ${String(setting.group_name || "未分组").trim() || "未分组"}`))];
    const suffix = ownerGroups.length ? ` · ${ownerGroups.join(" / ")}` : "";
    return `<option value="${value}" data-label="${aiOriginalEscape(label)}" ${previousStores.has(value) ? "selected" : ""}>${aiOriginalEscape(label + suffix)}</option>`;
  }).join("") || '<option value="">当前筛选下暂无店铺</option>';
  renderAiOriginalStoreRatioSummary();
}

function renderAiOriginalStoreRatioSummary() {
  const node = document.getElementById("ai-original-store-status");
  const select = document.getElementById("ai-original-stores");
  if (!node || !select) return;
  const selected = new Set([...select.selectedOptions].map(option => Number(option.value)).filter(Boolean));
  if (!selected.size) {
    node.textContent = "店铺比例沿用各店铺的站点设置；上架记录会写入“产品上架记录”。";
    return;
  }
  const siteIds = aiOriginalSelectedSiteIds();
  const summaries = aiOriginalStoreRows.filter(row => selected.has(Number(row.id))).map(account => {
    const rates = aiOriginalAccountSettings(account, siteIds).map(setting => {
      const site = String(setting.site_id || "").trim().toUpperCase();
      const rate = Number(setting.discount_rate || 100);
      return `${site} ${Number.isFinite(rate) ? rate : 100}%`;
    }).filter(Boolean);
    const label = String(account.display_name || account.nickname || `账号 ${account.id}`);
    return `${label}${rates.length ? `（${rates.join("，")}）` : "（站点比例默认 100%）"}`;
  });
  node.textContent = `各店铺站点比例：${summaries.join("；")}。最终净收益 = 1688换算净收益 × AI比例 × 店铺站点比例。`;
}

function renderAiOriginalPublishFormula() {
  const node = document.getElementById("ai-original-publish-formula-note");
  const value = Number(document.getElementById("ai-original-net-ratio")?.value || 0);
  if (node) node.textContent = `当前 AI 比例：${Number.isFinite(value) ? value : 0}%；店铺站点比例沿用各店铺设置。`;
}

async function publishSelectedAiOriginalProducts() {
  const ids = [...aiOriginalSelected];
  const tokenIds = [...(document.getElementById("ai-original-stores")?.selectedOptions || [])].map(option => Number(option.value)).filter(Boolean);
  const siteIds = aiOriginalSelectedSiteIds();
  const status = document.getElementById("ai-original-publish-status");
  if (!ids.length || !tokenIds.length || !siteIds.length) { status.textContent = "请选择产品、对应店铺和至少一个目标站点"; status.className = "ai-original-status error"; return; }
  const netProceedsRatio = Number(document.getElementById("ai-original-net-ratio")?.value || 200);
  if (!(netProceedsRatio > 0) || netProceedsRatio > 10000) { status.textContent = "AI 原创比例必须大于 0 且不超过 10000%"; status.className = "ai-original-status error"; return; }
  if (!window.confirm(`确定把 ${ids.length} 件产品上架到 ${tokenIds.length} 个店铺、${siteIds.length} 个站点吗？AI 原创比例 ${netProceedsRatio}% 将再乘以每个店铺的站点比例。`)) return;
  try {
    const publishPayload = {
      product_item_ids: ids, selection_mode: "accounts", token_ids: tokenIds, site_ids: siteIds,
      quantity: Number(document.getElementById("ai-original-quantity")?.value || 500),
      net_proceeds_ratio: netProceedsRatio
    };
    if (window.canChangeTaskWorkers) {
      publishPayload.worker_count = Number(document.getElementById("ai-original-publish-workers")?.value || 8);
    }
    const response = await fetch("/api/mercado-products/publish", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(publishPayload)});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    status.textContent = payload.data?.message || "已启动批量上架"; status.className = "ai-original-status success";
    loadAiOriginalPublishStatus();
  } catch (error) { status.textContent = `上架失败：${error.message || error}`; status.className = "ai-original-status error"; }
}

async function bulkSetAiOriginalDimensions() {
  const ids = [...aiOriginalSelected];
  if (!ids.length) { aiOriginalStatus("请先选择要批量设置尺寸的 AI 原创产品", "error"); return; }
  const fields = [
    ["package_length_cm", "ai-original-bulk-length"],
    ["package_width_cm", "ai-original-bulk-width"],
    ["package_height_cm", "ai-original-bulk-height"],
  ];
  const changes = {};
  for (const [field, inputId] of fields) {
    const raw = String(document.getElementById(inputId)?.value || "").trim();
    if (!raw) continue;
    const value = Number(raw);
    if (!(value > 0) || !Number.isFinite(value)) { aiOriginalStatus("长、宽、高必须填写大于 0 的厘米数", "error"); return; }
    changes[field] = value;
  }
  if (!Object.keys(changes).length) { aiOriginalStatus("请至少填写长、宽、高中的一项；仅会填写尚无长宽高的产品", "error"); return; }
  const button = document.getElementById("ai-original-bulk-dimensions-button");
  if (button) button.disabled = true;
  aiOriginalStatus(`正在为所选产品中尚无长宽高的商品填写尺寸…`);
  try {
    const response = await fetch("/api/ai-original-products/bulk-dimensions", {
      method: "PATCH", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({product_item_ids: ids, changes}),
    });
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    await loadAiOriginalProducts();
    aiOriginalStatus(`已为 ${Number(payload.data?.changed || 0)} 件原本没有长宽高的产品填写包装尺寸`, "success");
  } catch (error) {
    aiOriginalStatus(`批量设置尺寸失败：${error.message || error}`, "error");
  } finally {
    if (button) button.disabled = false;
  }
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
  document.getElementById("ai-original-salesperson")?.addEventListener("change", renderAiOriginalStoreFilters);
  document.getElementById("ai-original-store-group")?.addEventListener("change", renderAiOriginalStoreFilters);
  document.getElementById("ai-original-stores")?.addEventListener("change", renderAiOriginalStoreRatioSummary);
  document.getElementById("ai-original-net-ratio")?.addEventListener("input", renderAiOriginalPublishFormula);
  renderAiOriginalPublishFormula();
  document.querySelectorAll('input[name="ai-original-site"]').forEach(input => input.addEventListener("change", renderAiOriginalStoreFilters));
  loadAiOriginalStores();
  if (new URLSearchParams(location.search).get("tab") === "ai-original-products" && typeof switchTab === "function") switchTab("ai-original-products");
});
