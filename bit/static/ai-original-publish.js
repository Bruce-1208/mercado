"use strict";

// Credentials and unsaved listing inputs stay in this page's memory only.
const AiOriginalPublisher = (() => {
  const state = {
    platform: "", stores: [], fields: [], inputs: new Map(), signature: "", revision: 0,
    preview: null, previewRequest: null, busy: false, storeSequence: 0, schemaSequence: 0,
    jobs: [], jobTimer: null, jobSequence: 0, activeJobActions: new Set()
  };
  const node = id => document.getElementById(`aip-${id}`);
  const escape = value => aiOriginalEscape(value);
  const label = platform => platform === "amazon" ? "亚马逊美国站" : "Yandex Market";
  const selectedRows = () => aiOriginalRows.filter(row => aiOriginalSelected.has(Number(row.id)));
  const key = id => `${state.platform}:${Number(id)}`;
  const messageText = value => typeof value === "string" ? value : value?.message || value?.description || value?.code || "";
  const messages = values => (Array.isArray(values) ? values : values ? [values] : []).map(messageText).filter(Boolean);

  function status(message, kind = "") {
    const target = node("status");
    if (target) { target.textContent = message; target.className = `aip-status ${kind}`.trim(); }
  }

  async function request(path, options = {}) {
    const response = await fetch(`/api/ai-original-products/publish${path}`, {
      cache: "no-store", ...options,
      ...(options.body ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(options.body)} : {})
    });
    let payload;
    try { payload = await response.json(); } catch (_) { throw new Error(`发布服务返回无效数据（HTTP ${response.status}），请刷新登录状态后重试`); }
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || `HTTP ${response.status}`);
    return payload.data || {};
  }

  function invalidatePreview(message = "发布资料已变化，请重新检查并生成预览") {
    state.revision += 1;
    state.preview = null;
    state.previewRequest = null;
    if (node("preview")) node("preview").hidden = true;
    if (node("confirm")) node("confirm").disabled = true;
    if (message && node("status")) status(message);
  }

  function initialSettings(row) {
    return {
      sku: "", brand: "", gtin: "", gtin_exemption: false, price: "", quantity: "",
      currency: state.platform === "amazon" ? "USD" : "RUR", condition: "",
      weight_g: row.weight_g || "", package_length_cm: row.package_length_cm || "",
      package_width_cm: row.package_width_cm || "", package_height_cm: row.package_height_cm || "",
      image_url: "", additional_attributes: "{}"
    };
  }

  function settingsFor(row) {
    if (!state.inputs.has(key(row.id))) state.inputs.set(key(row.id), initialSettings(row));
    return state.inputs.get(key(row.id));
  }

  function inputField(id, field, title, settings, options = {}) {
    const {type = "text", min, step, placeholder = "", required = false} = options;
    return `<label><span>${escape(title)}${required ? " *" : ""}</span><input data-product-id="${id}" data-field="${field}" type="${type}" value="${escape(settings[field] ?? "")}" ${min !== undefined ? `min="${min}"` : ""} ${step ? `step="${step}"` : ""} placeholder="${escape(placeholder)}" autocomplete="off"></label>`;
  }

  function renderProducts(rows) {
    const container = node("products");
    if (!container) return;
    container.innerHTML = rows.length ? rows.map(row => {
      const settings = settingsFor(row);
      const id = Number(row.id);
      const draft = aiOriginalPlatformDraft(row, state.platform);
      const currencies = state.platform === "amazon" ? ["USD"] : ["RUR", "CNY"];
      const example = state.platform === "amazon" ? '{"material":[{"value":"Metal","language_tag":"en_US","marketplace_id":"ATVPDKIKX0DER"}]}' : '{"123":"真实材质","124":{"value":"2","unitId":3}}';
      return `<details class="aip-product" data-product="${id}" open><summary><span><b>商品 ${id}</b> ${escape(row.original_1688?.title || row.title || draft?.title || "")}</span><small>${draft?.status === "completed" ? "已生成本平台文案" : "请先完成本平台 AI 生成"}</small></summary><div class="aip-product-body"><p class="aip-listing-title">${escape(draft?.title || "尚无目标平台标题")}</p><div class="aip-fields">
        ${inputField(id, "sku", "卖家 SKU", settings, {placeholder: "为此商品填写唯一 SKU", required: true})}
        ${inputField(id, "brand", "真实品牌", settings, {placeholder: "与实物及品牌授权一致", required: true})}
        ${inputField(id, "gtin", "商品条码 GTIN / EAN / UPC", settings, {placeholder: "此商品独有的真实条码"})}
        ${state.platform === "amazon" ? `<label class="aip-checkbox"><input type="checkbox" data-product-id="${id}" data-field="gtin_exemption" ${settings.gtin_exemption ? "checked" : ""}><span>此商品已获得平台 GTIN 豁免</span></label>` : '<p class="aip-field-note">条码留空是否允许，由所选类目要求决定。</p>'}
        ${inputField(id, "price", state.platform === "amazon" ? "销售价格" : "销售价格（整数）", settings, {type: "number", min: state.platform === "amazon" ? "0.01" : "1", step: state.platform === "amazon" ? "0.01" : "1", required: true})}
        <label><span>币种 *</span><select data-product-id="${id}" data-field="currency">${currencies.map(currency => `<option ${settings.currency === currency ? "selected" : ""}>${currency}</option>`).join("")}</select></label>
        ${inputField(id, "quantity", "真实可售库存", settings, {type: "number", min: "1", step: "1", placeholder: "填写实际可售数量", required: true})}
        <label><span>商品状况 *</span><select data-product-id="${id}" data-field="condition"><option value="">请选择商品状况</option><option value="new_new" ${settings.condition === "new_new" ? "selected" : ""}>全新</option></select></label>
        ${inputField(id, "weight_g", "包装重量（g）", settings, {type: "number", min: "0.01", step: "any", required: true})}
        ${inputField(id, "package_length_cm", "包装长（cm）", settings, {type: "number", min: "0.01", step: "any", required: true})}
        ${inputField(id, "package_width_cm", "包装宽（cm）", settings, {type: "number", min: "0.01", step: "any", required: true})}
        ${inputField(id, "package_height_cm", "包装高（cm）", settings, {type: "number", min: "0.01", step: "any", required: true})}
        <label class="wide"><span>公开主图地址（选填）</span><input data-product-id="${id}" data-field="image_url" type="url" value="${escape(settings.image_url)}" placeholder="https://；留空使用此平台已生成主图"><small>需能被平台直接访问，并真实展示本商品。</small></label>
        <label class="wide"><span>其他类目字段（JSON 对象）</span><textarea data-product-id="${id}" data-field="additional_attributes" rows="4" spellcheck="false" placeholder="${escape(example)}">${escape(settings.additional_attributes)}</textarea><small>${state.platform === "amazon" ? "按照所选商品类型的官方属性结构填写；示例只用于说明格式，请使用真实资料。" : "填写字段编号对应的真实值；也支持 parameterValues 官方数组格式。"}</small><code>${escape(example)}</code></label>
      </div></div></details>`;
    }).join("") : '<div class="aip-empty">先在上方列表勾选要上架的商品，再逐件确认真实品牌、条码、价格及库存。</div>';
    node("selected-count").textContent = `已选择 ${rows.length} 件`;
    updateButtons();
  }

  function updateButtons() {
    const rows = selectedRows();
    if (node("prepare")) node("prepare").disabled = state.busy || !rows.length || rows.length > 20 || !node("store")?.value;
    if (node("confirm")) node("confirm").disabled = state.busy || !state.preview?.valid || !state.preview?.preview_id;
    if (node("schema-load")) node("schema-load").disabled = state.busy || !node("store")?.value;
  }

  function syncSelection() {
    if (!node("panel")) return;
    const platform = aiOriginalTargetPlatform();
    if (platform !== state.platform) { syncPlatform(); return; }
    if (platform === "mercadolibre") return;
    const rows = selectedRows();
    // Rebuild only after selection/draft changes, never while typing an input.
    const signature = JSON.stringify(rows.map(row => [row.id, aiOriginalPlatformDraft(row, platform)]));
    if (signature !== state.signature) {
      state.signature = signature;
      invalidatePreview("");
      renderProducts(rows);
    }
    updateButtons();
  }

  function syncPlatform() {
    const panel = node("panel");
    if (!panel) return;
    const platform = aiOriginalTargetPlatform();
    panel.hidden = platform === "mercadolibre";
    if (platform === state.platform) { syncSelection(); return; }
    state.platform = platform;
    state.signature = "";
    state.fields = [];
    state.stores = [];
    state.storeSequence += 1;
    state.schemaSequence += 1;
    state.jobSequence += 1;
    if (state.jobTimer) clearTimeout(state.jobTimer);
    invalidatePreview("");
    if (platform === "mercadolibre") return;
    node("platform-name").textContent = `自动上架到 ${label(platform)}`;
    node("category-label").textContent = platform === "amazon" ? "亚马逊商品类型" : "Yandex 类目编号";
    node("category").value = "";
    node("category").placeholder = platform === "amazon" ? "例如 HOME_ORGANIZER；以店铺可用类型为准" : "输入数字类目编号";
    node("amazon-config-actions")?.toggleAttribute("hidden", platform !== "amazon");
    node("yandex-authorize").hidden = platform !== "yandex";
    node("schema").innerHTML = "选择店铺并填写类目后，读取平台当前字段要求。";
    node("jobs").innerHTML = '<p class="aip-empty">正在读取发布记录…</p>';
    status("完善每件商品的真实资料，先检查预览，再确认提交平台。");
    syncSelection();
    loadStores();
    loadJobs();
  }

  async function loadStores() {
    const platform = state.platform;
    if (!["amazon", "yandex"].includes(platform)) return;
    const sequence = ++state.storeSequence;
    const previous = node("store").value;
    node("store").innerHTML = '<option value="">正在读取授权店铺…</option>';
    updateButtons();
    try {
      const data = await request(`/stores?target_platform=${encodeURIComponent(platform)}`);
      if (sequence !== state.storeSequence || platform !== state.platform) return;
      state.stores = Array.isArray(data.rows) ? data.rows : [];
      node("store").innerHTML = '<option value="">请选择授权店铺</option>' + state.stores.map(store => `<option value="${escape(store.id)}">${escape(store.label || store.name || store.id)}</option>`).join("");
      if (state.stores.some(store => String(store.id) === previous)) node("store").value = previous;
      else if (state.stores.length === 1) node("store").value = String(state.stores[0].id);
      if (node("store").value !== previous) { state.schemaSequence += 1; state.fields = []; invalidatePreview(""); }
      const configuration = data.configuration || {};
      const details = [state.stores.length ? `已读取 ${state.stores.length} 个授权店铺` : platform === "amazon" ? "暂无亚马逊店铺，请管理员先配置 SP-API 授权。" : "暂无 Yandex 店铺，请先在 Yandex 店铺模块完成授权。"];
      if (data.image_base_url_configured === false) details.push("本机图片尚未配置公网地址；可逐商品填写可公开访问的 HTTPS 主图地址，预检会校验图片来源。");
      if (configuration.message) details.push(configuration.message);
      node("store-hint").textContent = details.join(" ");
    } catch (error) {
      if (sequence !== state.storeSequence || platform !== state.platform) return;
      node("store").innerHTML = '<option value="">店铺读取失败</option>';
      node("store-hint").textContent = error.message;
    } finally { if (sequence === state.storeSequence) updateButtons(); }
  }

  async function loadSchema() {
    const platform = state.platform;
    const storeId = node("store").value;
    const categoryId = node("category").value.trim();
    if (!storeId || !categoryId) { status("请先选择店铺并填写类目或商品类型", "error"); return; }
    const sequence = ++state.schemaSequence;
    node("schema").textContent = "正在读取平台当前字段要求…";
    try {
      const query = new URLSearchParams({target_platform: platform, store_id: storeId, category_id: categoryId});
      const data = await request(`/schema?${query}`);
      if (sequence !== state.schemaSequence || platform !== state.platform) return;
      state.fields = Array.isArray(data.fields) ? data.fields : [];
      node("schema").innerHTML = state.fields.length ? `<p>下列字段由当前平台返回。通用字段在商品表单中填写，其余按字段编号填入各商品的 JSON；最终以预检结果为准。</p><div class="aip-schema-fields">${state.fields.map(field => {
        const choices = (Array.isArray(field.choices) ? field.choices : []).slice(0, 30).map(choice => typeof choice === "object" ? `${choice.label || choice.name || choice.value} (${choice.value ?? choice.id ?? ""})` : choice).join("、");
        return `<div><strong>${escape(field.label || field.id)} ${field.required ? '<b class="aip-required">必填</b>' : ""}</strong><code>${escape(field.id)}</code><small>${escape(field.type || "文本")}${field.multivalue ? " · 支持多个值" : ""}${choices ? ` · 可选：${escape(choices)}` : ""}</small>${field.description ? `<p>${escape(field.description)}</p>` : ""}</div>`;
      }).join("")}</div>` : "已读取类目要求。此类目没有可直接展示的字段；提交预检后会显示平台返回的缺项。";
    } catch (error) { if (sequence === state.schemaSequence) node("schema").textContent = `类目要求读取失败：${error.message}`; }
  }

  function collectRequest() {
    const rows = selectedRows();
    if (!rows.length) throw new Error("请先选择要上架的商品");
    if (rows.length > 20) throw new Error("每批最多 20 件商品；建议先选择 10 件以内完成预检");
    const storeId = node("store").value;
    const categoryId = node("category").value.trim();
    if (!storeId || !categoryId) throw new Error("请先选择店铺并填写类目或商品类型");
    const skuSet = new Set();
    const barcodeSet = new Set();
    const items = rows.map(row => {
      const settings = {...settingsFor(row), category_id: categoryId};
      for (const field of ["sku", "brand", "gtin", "image_url"]) settings[field] = String(settings[field] || "").trim();
      if (!settings.sku || !settings.brand || !settings.condition) throw new Error(`商品 ${row.id} 请填写唯一 SKU、真实品牌和商品状况`);
      if (skuSet.has(settings.sku)) throw new Error(`SKU ${settings.sku} 重复，不同商品必须使用不同 SKU`);
      skuSet.add(settings.sku);
      if (settings.gtin && barcodeSet.has(settings.gtin)) throw new Error(`商品 ${row.id} 的条码与其他商品重复，请分别填写真实条码`);
      if (settings.gtin) barcodeSet.add(settings.gtin);
      if (state.platform === "amazon" && !settings.gtin && !settings.gtin_exemption) throw new Error(`商品 ${row.id} 需填写真实条码，或确认已获 GTIN 豁免`);
      for (const field of ["price", "quantity", "weight_g", "package_length_cm", "package_width_cm", "package_height_cm"]) {
        settings[field] = Number(settings[field]);
        if (!Number.isFinite(settings[field]) || settings[field] <= 0) throw new Error(`商品 ${row.id} 的售价、真实库存、重量和包装尺寸必须大于 0`);
      }
      if (!Number.isInteger(settings.quantity)) throw new Error(`商品 ${row.id} 的库存必须为整数`);
      if (state.platform === "yandex" && !Number.isInteger(settings.price)) throw new Error(`商品 ${row.id} 的 Yandex 售价必须为整数`);
      try { settings.additional_attributes = JSON.parse(settings.additional_attributes || "{}"); }
      catch (_) { throw new Error(`商品 ${row.id} 的类目字段 JSON 格式不正确`); }
      if (!settings.additional_attributes || typeof settings.additional_attributes !== "object" || Array.isArray(settings.additional_attributes)) throw new Error(`商品 ${row.id} 的类目字段必须是 JSON 对象`);
      return {product_id: Number(row.id), settings};
    });
    return {target_platform: state.platform, store_id: storeId, items};
  }

  function renderPreview(data, submitted) {
    const store = state.stores.find(item => String(item.id) === String(submitted.store_id));
    const items = Array.isArray(data.items) ? data.items : [];
    const expires = data.expires_at ? new Date(data.expires_at).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false}) : "以服务端有效期为准";
    node("preview-title").textContent = `${label(submitted.target_platform)} · ${data.store_label || store?.label || submitted.store_id} · ${items.length} 件`;
    node("preview-expiry").textContent = `预览有效期至 ${expires}（北京时间）；修改资料后需重新预检。`;
    node("preview-items").innerHTML = items.map(item => {
      const requested = submitted.items.find(row => Number(row.product_id) === Number(item.product_id));
      const settings = item.settings || requested?.settings || {};
      const errors = messages(item.errors);
      const warnings = messages(item.warnings);
      const imageUrl = /^https:\/\//i.test(settings.image_url || "") ? settings.image_url : "";
      const payloadView = item.price_payload || item.stock_payload ? {listing: item.payload || {}, price: item.price_payload, stock: item.stock_payload} : item.payload || {};
      return `<article class="aip-preview-item ${errors.length ? "has-errors" : ""}"><header><strong>${escape(item.title || `商品 ${item.product_id}`)}</strong><span>${errors.length ? `发现 ${errors.length} 项问题` : "预检通过"}</span></header>${imageUrl ? `<a class="aip-preview-image" href="${escape(imageUrl)}" target="_blank" rel="noopener noreferrer"><img src="${escape(imageUrl)}" alt="将提交的商品主图" loading="lazy"><span>查看将提交的主图 ↗</span></a>` : ""}<dl><div><dt>商品 / SKU</dt><dd>${escape(item.product_id)} / ${escape(item.sku || settings.sku)}</dd></div><div><dt>销售价格</dt><dd>${escape(settings.price)} ${escape(settings.currency)}</dd></div><div><dt>真实库存</dt><dd>${escape(settings.quantity)} 件</dd></div><div><dt>品牌 / 条码</dt><dd>${escape(settings.brand)} · ${escape(settings.gtin || (settings.gtin_exemption ? "已确认获豁免" : "未填写"))}</dd></div><div><dt>类目 / 商品状况</dt><dd>${escape(settings.category_id)} · ${settings.condition === "new_new" ? "全新" : escape(settings.condition)}</dd></div><div><dt>包装</dt><dd>${escape(settings.weight_g)} g · ${escape(settings.package_length_cm)} × ${escape(settings.package_width_cm)} × ${escape(settings.package_height_cm)} cm</dd></div></dl>${errors.length ? `<ul class="aip-errors">${errors.map(error => `<li>${escape(error)}</li>`).join("")}</ul>` : ""}${warnings.length ? `<ul class="aip-warnings">${warnings.map(warning => `<li>${escape(warning)}</li>`).join("")}</ul>` : ""}<details><summary>查看将提交的平台完整资料（含图片地址）</summary><pre>${escape(JSON.stringify(payloadView, null, 2))}</pre></details></article>`;
    }).join("");
    node("preview").hidden = false;
    updateButtons();
  }

  async function prepare() {
    if (state.busy) return;
    let body;
    try { body = collectRequest(); } catch (error) { status(error.message, "error"); return; }
    invalidatePreview("");
    const revision = state.revision;
    state.busy = true;
    status("正在读取平台要求并逐商品预检，完成后会展示将提交的报价、库存及资料…");
    updateButtons();
    try {
      const data = await request("/preview", {method: "POST", body});
      if (revision !== state.revision || body.target_platform !== state.platform) { status("预检期间资料已变化，请重新生成预览", "error"); return; }
      state.preview = data;
      state.previewRequest = body;
      renderPreview(data, body);
      status(data.valid ? "预检通过。请核对每件商品的图片、报价和库存，再确认提交真实平台。" : "预检发现待修正资料，请按每件商品的提示修改后重新预检。", data.valid ? "success" : "error");
    } catch (error) { status(`预检失败：${error.message}`, "error"); }
    finally { state.busy = false; updateButtons(); }
  }

  async function confirmPublish() {
    if (state.busy || !state.preview?.valid || !state.preview?.preview_id) return;
    const preview = state.preview;
    const expires = Date.parse(preview.expires_at);
    if (Number.isFinite(expires) && expires <= Date.now()) { invalidatePreview("此预览已过期，请重新预检后提交"); return; }
    state.busy = true;
    updateButtons();
    status("正在提交已确认的上架任务…");
    try {
      const data = await request("", {method: "POST", body: {preview_id: preview.preview_id}});
      const job = data.job || data;
      invalidatePreview("");
      if (job.id) upsertJob(job);
      status("上架任务已提交。平台接收后仍需回查处理结果，只有“已可售”表示平台确认可售。", "success");
      await loadJobs();
    } catch (error) {
      // The server owns idempotency. Never repeat a possibly accepted publish automatically.
      invalidatePreview("");
      status(`提交结果未确认：${error.message}。请刷新发布记录并回查，避免重复提交。`, "error");
      await loadJobs();
    } finally { state.busy = false; updateButtons(); }
  }

  function stateLabel(value) {
    return {queued: "等待提交", pending: "等待处理", running: "正在提交", processing: "平台处理中", submitted: "平台已接收", live: "平台已处理", published: "平台已发布，待确认可售", completed: "任务完成，需回查可售状态", partial: "部分步骤失败", failed: "提交失败", unknown: "结果未确认，先回查", interrupted: "任务中断，先回查"}[value] || value || "等待处理";
  }

  function upsertJob(job) {
    state.jobs = [job, ...state.jobs.filter(item => String(item.id) !== String(job.id))];
    renderJobs();
  }

  function renderJobs() {
    const jobs = state.jobs.filter(job => (job.platform || job.target_platform) === state.platform);
    node("jobs").innerHTML = jobs.length ? jobs.slice(0, 20).map(job => {
      const items = Array.isArray(job.items) ? job.items : [];
      const busy = Boolean(job.running) || state.activeJobActions.has(String(job.id));
      const unknown = ["unknown", "interrupted"].includes(job.state) || items.some(item => ["unknown", "interrupted"].includes(item.state));
      const retryable = !unknown && (job.state === "failed" || items.some(item => ["failed", "partial"].includes(item.state)));
      return `<article class="aip-job"><header><div><strong>${escape(job.store_label || label(job.platform))}</strong><small>任务 ${escape(job.id)} · ${escape(job.created_at || "")}</small></div><span class="aip-job-state">${escape(stateLabel(job.state))}</span></header><ul>${items.map(item => {
        const issues = messages(item.issues || item.errors);
        const live = item.live === true;
        return `<li><div><strong>${escape(item.sku || `商品 ${item.product_id}`)}</strong><span class="${live ? "aip-live" : ""}">${live ? "已可售" : escape(stateLabel(item.state))}</span></div><p>${escape(item.title || "")}${item.remote_id ? ` · 平台编号 ${escape(item.remote_id)}` : ""}</p>${item.message ? `<p>${escape(item.message)}</p>` : ""}${item.remote_status || item.card_status ? `<p>平台状态：${escape(typeof item.remote_status === "object" ? JSON.stringify(item.remote_status) : item.remote_status || "")} ${escape(typeof item.card_status === "object" ? JSON.stringify(item.card_status) : item.card_status || "")}</p>` : ""}${issues.length ? `<ul class="aip-errors">${issues.map(issue => `<li>${escape(issue)}</li>`).join("")}</ul>` : ""}</li>`;
      }).join("")}</ul><footer><button type="button" class="secondary" data-job-action="refresh" data-job-id="${escape(job.id)}" ${busy ? "disabled" : ""}>回查平台状态</button><button type="button" class="secondary" data-job-action="retry" data-job-id="${escape(job.id)}" ${busy || !retryable ? "disabled" : ""}>重试失败步骤</button>${unknown ? '<small>提交结果不明，先回查确认；不会自动重复创建商品。</small>' : '<small>平台接收不代表已经可售。</small>'}</footer></article>`;
    }).join("") : '<p class="aip-empty">当前平台暂无发布记录。已提交的任务可在刷新页面后继续查看。</p>';
  }

  async function loadJobs() {
    if (state.jobTimer) clearTimeout(state.jobTimer);
    if (!["amazon", "yandex"].includes(state.platform)) return;
    const platform = state.platform;
    const sequence = ++state.jobSequence;
    try {
      const data = await request(`/jobs?target_platform=${encodeURIComponent(platform)}`);
      if (sequence !== state.jobSequence || platform !== state.platform) return;
      state.jobs = Array.isArray(data.rows) ? data.rows : [];
      renderJobs();
      if (state.jobs.some(job => job.running && (job.platform || job.target_platform) === platform)) state.jobTimer = setTimeout(pollJobs, 2500);
    } catch (error) { if (sequence === state.jobSequence) node("jobs").textContent = `发布记录读取失败：${error.message}`; }
  }

  async function pollJobs() {
    if (state.jobTimer) clearTimeout(state.jobTimer);
    const platform = state.platform;
    const sequence = state.jobSequence;
    const pending = state.jobs.filter(job => job.running && (job.platform || job.target_platform) === platform);
    const results = await Promise.allSettled(pending.map(job => request(`/jobs/${encodeURIComponent(job.id)}`)));
    if (platform !== state.platform || sequence !== state.jobSequence) return;
    results.forEach(result => {
      if (result.status !== "fulfilled") return;
      const job = result.value.job || result.value;
      if (job.id) state.jobs = state.jobs.map(existing => String(existing.id) === String(job.id) ? job : existing);
    });
    renderJobs();
    if (state.jobs.some(job => job.running && (job.platform || job.target_platform) === platform)) state.jobTimer = setTimeout(pollJobs, 2500);
  }

  async function jobAction(id, action) {
    if (!["refresh", "retry"].includes(action) || state.activeJobActions.has(String(id))) return;
    const job = state.jobs.find(item => String(item.id) === String(id));
    if (!job || job.running) return;
    const items = Array.isArray(job.items) ? job.items : [];
    if (action === "retry" && (["unknown", "interrupted"].includes(job.state) || items.some(item => ["unknown", "interrupted"].includes(item.state)))) { status("此任务结果未确认，请先回查平台状态", "error"); return; }
    state.activeJobActions.add(String(id));
    renderJobs();
    try {
      const data = await request(`/jobs/${encodeURIComponent(id)}/${action}`, {method: "POST", body: {}});
      const updated = data.job || data;
      if (updated.id) upsertJob(updated);
      status(action === "refresh" ? "已请求回查平台状态，请查看每件商品的处理结果。" : "已启动失败步骤重试；已成功的商品不会重复提交。", "success");
      await loadJobs();
    } catch (error) { status(`${action === "refresh" ? "回查" : "重试"}失败：${error.message}`, "error"); }
    finally { state.activeJobActions.delete(String(id)); renderJobs(); }
  }

  function openAmazonConfig() { node("amazon-config")?.showModal(); }
  function closeAmazonConfig() { node("amazon-form")?.reset(); node("amazon-config")?.close(); }

  async function saveAmazonConfig(event) {
    event?.preventDefault();
    const form = node("amazon-form");
    if (!form || !form.reportValidity()) return;
    const button = node("amazon-save");
    if (button.disabled) return;
    const body = {};
    ["label", "seller_id", "client_id", "client_secret", "refresh_token"].forEach(field => { body[field] = form.elements.namedItem(field).value.trim(); });
    button.disabled = true;
    node("amazon-status").textContent = "正在保存店铺授权…";
    try {
      await request("/amazon-store", {method: "POST", body});
      closeAmazonConfig();
      status("亚马逊店铺授权已保存，密钥不会回显在页面。", "success");
      await loadStores();
    } catch (error) { node("amazon-status").textContent = `保存失败：${error.message}`; }
    finally {
      ["client_secret", "refresh_token"].forEach(field => { form.elements.namedItem(field).value = ""; });
      button.disabled = false;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const panel = node("panel");
    if (!panel) return;
    panel.addEventListener("input", event => {
      const input = event.target.closest?.("[data-product-id][data-field]");
      if (!input) return;
      const row = aiOriginalRows.find(item => Number(item.id) === Number(input.dataset.productId));
      if (!row) return;
      settingsFor(row)[input.dataset.field] = input.type === "checkbox" ? input.checked : input.value;
      invalidatePreview();
    });
    node("store").addEventListener("change", () => { state.schemaSequence += 1; state.fields = []; node("schema").textContent = "店铺已变化，请重新读取类目要求。"; invalidatePreview(); updateButtons(); });
    node("category").addEventListener("input", () => { state.schemaSequence += 1; state.fields = []; node("schema").textContent = "类目已变化，请重新读取字段要求。"; invalidatePreview(); });
    node("jobs").addEventListener("click", event => { const button = event.target.closest?.("[data-job-action]"); if (button && !button.disabled) jobAction(button.dataset.jobId, button.dataset.jobAction); });
    node("amazon-form")?.addEventListener("submit", saveAmazonConfig);
    node("amazon-config")?.addEventListener("cancel", closeAmazonConfig);
    syncPlatform();
  });
  return {syncPlatform, syncSelection, loadStores, loadSchema, prepare, confirmPublish, loadJobs, jobAction, openAmazonConfig, closeAmazonConfig, collectRequest};
})();
globalThis.AiOriginalPublisher = AiOriginalPublisher;
