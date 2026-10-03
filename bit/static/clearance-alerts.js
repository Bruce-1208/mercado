(function (root) {
  'use strict';
  const STATUS_LABELS = {risk:'清退风险', pending_data:'待补资料', exempt:'符合豁免', normal:'当前正常', not_applicable:'不在检测范围'};
  const FIELD_LABELS = {
    sales_7d:'过去 7 天 SKU 销量', sales_14d:'过去 14 天 SKU 销量', orders:'订单销售数据', orders_14d:'完整 14 天订单数据',
    sales_complete:'完整近 14 天订单检测', visits_complete:'完整且截至今天的 30 天 SKU 访问量', fresh_snapshot:'最新平台检测数据', inventory_complete:'最新平台在售状态及库存',
    first_inbound_at:'首次入仓日期', inbound_history_confirmed:'入仓历史核实情况', restock_after_stockout:'补货前是否缺货', last_restock_at:'最近补货入仓日期',
    visits_30d:'该 SKU 过去 30 天访问总量', visits_as_of:'访问量统计截至日期', available_quantity:'SKU 可售库存', variation_id:'SKU 规格标识',
    notice_at:'平台邮件通知日期', promotion_start_at:'实际促销开始日期', return_order_at:'平台退仓单日期'
  };
  const DATE_FIELDS = ['first_inbound_at','last_restock_at','visits_as_of','notice_at','promotion_start_at','return_order_at'];

  function chinaToday(now = new Date()) {
    const parts = new Intl.DateTimeFormat('en-US', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit'}).formatToParts(now);
    const get = type => parts.find(part => part.type === type).value;
    return `${get('year')}-${get('month')}-${get('day')}`;
  }
  function dateInput(value) {
    if (!value) return '';
    let raw = String(value).replace(' ', 'T');
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(raw)) raw += '+08:00';
    const date = new Date(raw);
    return Number.isFinite(date.getTime()) ? chinaToday(date) : '';
  }
  function dateLabel(value, includeTime = false) {
    if (!value) return '未录入';
    if (!includeTime) return dateInput(value) || String(value);
    let raw = String(value).replace(' ', 'T');
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(raw)) raw += '+08:00';
    const date = new Date(raw);
    return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false}) : String(value);
  }
  function numberLabel(value, decimals = 0) {
    if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) return '待同步';
    return Number(value).toLocaleString('zh-CN', {maximumFractionDigits:decimals});
  }
  function reasons(values) {
    return (Array.isArray(values) ? values : []).map(value => typeof value === 'string' ? value : value && (value.message || value.summary || value.code)).filter(Boolean);
  }
  function missingLabel(field) { return FIELD_LABELS[field] || String(field); }
  function referenceVisits(row) {
    const value = row.visits_api_30d ?? row.facts?.visits_api_30d;
    const asOf = row.visits_api_as_of ?? row.facts?.visits_api_as_of;
    return value === null || value === undefined ? '' : `链接级 API 参考：${numberLabel(value)} 次${asOf ? `，截至 ${dateLabel(asOf)}` : ''}；可能有延迟，不能直接证明该 SKU 低曝光。`;
  }
  function factValues(facts = {}) {
    const values = {};
    for (const field of DATE_FIELDS) values[field] = dateInput(facts[field]);
    values.inbound_history_confirmed = typeof facts.inbound_history_confirmed === 'boolean' ? String(facts.inbound_history_confirmed) : '';
    values.restock_after_stockout = typeof facts.restock_after_stockout === 'boolean' ? String(facts.restock_after_stockout) : '';
    values.visits_30d = facts.visits_30d === null || facts.visits_30d === undefined ? '' : String(facts.visits_30d);
    return values;
  }
  function validatedFacts(values, skuKey, today = chinaToday()) {
    if (!['true','false'].includes(values.inbound_history_confirmed)) throw new Error('请选择入仓历史核实情况；尚未核实的记录请选择“尚未核实”。');
    if (!['','true','false'].includes(values.restock_after_stockout || '')) throw new Error('请选择补货前缺货情况，未知时保留“未知 / 尚未核实”。');
    const result = {sku_key:skuKey, inbound_history_confirmed:values.inbound_history_confirmed === 'true', restock_after_stockout:values.restock_after_stockout === 'true' ? true : values.restock_after_stockout === 'false' ? false : null};
    for (const field of DATE_FIELDS) {
      const value = values[field] || '';
      if (value && (!/^\d{4}-\d{2}-\d{2}$/.test(value) || Number.isNaN(Date.parse(value)) || new Date(`${value}T00:00:00Z`).toISOString().slice(0,10) !== value)) throw new Error(`${missingLabel(field)}格式不正确。`);
      if (value && value > today && field !== 'promotion_start_at') throw new Error(`${missingLabel(field)}请填写已经发生的实际日期，不能晚于今天。`);
      result[field] = value || null;
    }
    if (result.restock_after_stockout === true && !result.last_restock_at) throw new Error('选择缺货后补货时，请填写该补货批次实际入仓日期。');
    if (result.first_inbound_at && result.last_restock_at && result.last_restock_at < result.first_inbound_at) throw new Error('最近补货入仓日期不能早于首次入仓日期。');
    const visits = String(values.visits_30d ?? '').trim();
    if (visits && (!/^\d+$/.test(visits) || !Number.isSafeInteger(Number(visits)))) throw new Error('30 天访问总量必须为大于等于 0 的整数；未知时请留空。');
    result.visits_30d = visits ? Number(visits) : null;
    if (result.visits_30d !== null && !result.visits_as_of) throw new Error('填写访问量时，请同时填写统计截至日期。');
    if (result.visits_30d === null && result.visits_as_of) throw new Error('填写统计截至日期时，请同时填写访问总量，或将两项一并清空。');
    if (result.visits_30d !== null && result.visits_as_of !== today) throw new Error('请核实截至今天的过去 30 天 SKU 访问量，并将统计截至日期设为今天；过期数据不能用于低曝光豁免。');
    if (result.notice_at && result.promotion_start_at && result.promotion_start_at < result.notice_at) throw new Error('实际促销开始日期不能早于平台邮件通知日期。');
    if (result.promotion_start_at && result.return_order_at && result.return_order_at < result.promotion_start_at) throw new Error('平台退仓单日期不能早于实际促销开始日期。');
    return result;
  }

  function boot(document, window) {
    const panel = document.getElementById('clearance-alerts');
    if (!panel) return;
    const $ = id => document.getElementById(`ca-${id}`);
    const canManage = panel.dataset.canManage === 'true';
    const state = {page:1, pageSize:25, total:0, rows:[], checking:false, refreshing:false, loading:false, requestId:0, controller:null, timer:null, selected:null, saving:false, toastTimer:null};
    function node(tag, text, className) {
      const element = document.createElement(tag);
      if (text !== null && text !== undefined) element.textContent = String(text);
      if (className) element.className = className;
      return element;
    }
    function error(message) { $('error').textContent = message || ''; $('error').hidden = !message; }
    function update(message) { $('update').textContent = message || ''; $('update').hidden = !message; }
    function toast(message) {
      window.clearTimeout(state.toastTimer);
      $('toast').textContent = message;
      $('toast').hidden = false;
      state.toastTimer = window.setTimeout(() => { $('toast').hidden = true; }, 4000);
    }
    function updateControls() {
      $('refresh').disabled = !canManage || state.refreshing || state.checking;
      $('refresh').textContent = state.refreshing || state.checking ? '检测中…' : '立即检测';
      $('reload').disabled = state.loading;
      $('prev').disabled = state.loading || state.page <= 1;
      $('next').disabled = state.loading || state.page * state.pageSize >= state.total;
      panel.setAttribute('aria-busy', String(state.loading || state.refreshing || state.checking));
    }
    function emptyRows(message) { const row = node('tr'); const cell = node('td', message, 'ca-empty'); cell.colSpan = 7; row.appendChild(cell); $('rows').replaceChildren(row); }
    function appendEvidence(cell, row) {
      const alert = row.alert || {};
      const lines = [
        ...reasons(alert.trigger_reasons).map(reason => `触发：${reason}`),
        ...reasons(alert.exemption_reasons).map(reason => `豁免：${reason}`),
        ...reasons(alert.missing_fields).map(field => `待补：${missingLabel(field)}`),
        ...reasons(alert.details)
      ];
      const facts = row.facts || {};
      lines.push(`入仓历史：${facts.inbound_history_confirmed === true ? '已核实' : '尚未完整核实'}；首次入仓：${dateLabel(facts.first_inbound_at)}`);
      lines.push(`补货前缺货：${facts.restock_after_stockout === true ? '是' : facts.restock_after_stockout === false ? '否' : '未知'}；最近补货入仓：${dateLabel(facts.last_restock_at)}`);
      if (facts.visits_as_of) lines.push(`人工 SKU 访问量统计截至：${dateLabel(facts.visits_as_of)}`);
      if (row.checked_at) lines.push(`本 SKU 检测时间：${dateLabel(row.checked_at, true)}（北京时间）`);
      const details = node('details', null, 'ca-evidence');
      details.appendChild(node('summary', '查看依据与缺失资料'));
      const list = node('ul'); lines.forEach(line => list.appendChild(node('li', line))); details.appendChild(list); cell.appendChild(details);
    }
    function renderWorkflow(cell, row) {
      const workflow = row.workflow || {};
      const facts = row.facts || {};
      cell.appendChild(node('div', workflow.summary || '尚未录入平台流程日期', 'ca-workflow'));
      if (workflow.deadline_at) {
        const days = workflow.days_remaining;
        const suffix = days === null || days === undefined ? '' : Number(days) < 0 ? ` · 已逾期 ${Math.abs(Number(days))} 天` : Number(days) === 0 ? ' · 今天截止' : ` · 还剩 ${Number(days)} 天`;
        cell.appendChild(node('div', `${dateLabel(workflow.deadline_at)}${suffix}`, `ca-deadline${Number(days) < 0 ? ' ca-deadline-overdue' : ''}`));
      }
      const dates = [];
      if (facts.notice_at) dates.push(`邮件 ${dateLabel(facts.notice_at)}`);
      if (facts.promotion_start_at) dates.push(`促销 ${dateLabel(facts.promotion_start_at)}`);
      if (facts.return_order_at) dates.push(`退仓单 ${dateLabel(facts.return_order_at)}`);
      if (dates.length) {
        cell.appendChild(node('div', `已录入：${dates.join(' · ')}`, 'ca-workflow-note'));
        cell.appendChild(node('div', '时限按录入日期推算，请以平台通知为准。', 'ca-workflow-note'));
      } else cell.appendChild(node('div', '风险检测不会自动确认平台已发出邮件。', 'ca-workflow-note'));
    }
    function renderRows() {
      if (!state.rows.length) { emptyRows('当前筛选下没有在售且有库存的半托管 SKU。可调整筛选，或点击“立即检测”更新销售与商品数据。'); return; }
      const fragment = document.createDocumentFragment();
      for (const row of state.rows) {
        const tr = node('tr');
        const productCell = node('td');
        const layout = node('div', null, 'ca-product-layout');
        const media = node('div', null, 'ca-product-media');
        const placeholder = node('span', '暂无主图', 'ca-image-placeholder');
        media.appendChild(placeholder);
        const imageUrl = String(row.thumbnail_url || '').trim();
        if (/^https?:\/\//i.test(imageUrl)) {
          const image = node('img', null, 'ca-product-image');
          image.alt = row.title || '产品主图';
          image.loading = 'lazy';
          image.decoding = 'async';
          image.addEventListener('error', () => { image.hidden = true; placeholder.hidden = false; });
          placeholder.hidden = true;
          image.src = imageUrl;
          media.appendChild(image);
        }
        layout.appendChild(media);
        const product = node('div', null, 'ca-product-info');
        product.appendChild(node('div', row.title || '商品标题待同步', 'ca-product'));
        product.appendChild(node('div', `SKU：${row.seller_sku || row.sku_key || '未设置商家 SKU'}`, 'ca-sku'));
        product.appendChild(node('div', `${row.store_name || `店铺 #${row.token_id ?? '—'}`} · ${row.site_id || '站点待同步'}`, 'ca-meta'));
        product.appendChild(node('div', `商品 ${row.item_id || '—'} · ${row.variation_id === null || row.variation_id === undefined || row.variation_id === '' ? '无规格商品' : `规格 ${row.variation_id}`}`, 'ca-meta'));
        layout.appendChild(product); productCell.appendChild(layout); tr.appendChild(productCell);
        tr.appendChild(node('td', numberLabel(row.available_quantity), 'ca-number'));
        const sales = node('td', null, 'ca-number');
        sales.appendChild(node('div', `7 天：${numberLabel(row.sales_7d)} 件`));
        sales.appendChild(node('div', `14 天：${numberLabel(row.sales_14d)} 件`));
        tr.appendChild(sales);
        const visits = node('td');
        visits.appendChild(node('div', `总量：${numberLabel(row.visits_30d)} 次`, 'ca-number'));
        visits.appendChild(node('div', `日均：${numberLabel(row.visits_daily_avg, 2)}`, 'ca-number'));
        if (row.facts?.visits_as_of) visits.appendChild(node('div', `人工 SKU 数据 · 截至 ${dateLabel(row.facts.visits_as_of)}`, 'ca-meta'));
        const reference = referenceVisits(row); if (reference) visits.appendChild(node('div', reference, 'ca-meta'));
        tr.appendChild(visits);
        const conclusion = node('td');
        const status = Object.hasOwn(STATUS_LABELS, row.alert?.status) ? row.alert.status : 'pending_data';
        conclusion.appendChild(node('span', STATUS_LABELS[status], `ca-badge ca-badge-${status}`));
        conclusion.appendChild(node('div', row.alert?.summary || '等待完整数据后判断', 'ca-conclusion'));
        appendEvidence(conclusion, row); tr.appendChild(conclusion);
        const workflow = node('td'); renderWorkflow(workflow, row); tr.appendChild(workflow);
        const action = node('td'); const edit = node('button', canManage ? '补充资料' : '查看资料', 'ca-button'); edit.type = 'button';
        edit.addEventListener('click', () => openFacts(row)); action.appendChild(edit); tr.appendChild(action);
        fragment.appendChild(tr);
      }
      $('rows').replaceChildren(fragment);
    }
    function render(data) {
      state.rows = Array.isArray(data.rows) ? data.rows : [];
      state.total = Number.isFinite(Number(data.total)) ? Math.max(0, Number(data.total)) : state.rows.length;
      state.page = Math.max(1, Number(data.page) || state.page);
      state.pageSize = Math.max(1, Number(data.page_size) || state.pageSize);
      state.checking = Boolean(data.checking);
      for (const status of ['risk','pending_data','exempt','normal']) $('count-' + status).textContent = numberLabel(data.counts?.[status]);
      const selectedStore = $('store').value;
      const options = [node('option', '全部半托管店铺')]; options[0].value = '';
      for (const store of Array.isArray(data.stores) ? data.stores : []) { const option = node('option', store.store_name || `店铺 #${store.token_id}`); option.value = String(store.token_id); options.push(option); }
      if (selectedStore && !options.some(option => option.value === selectedStore)) { const option = node('option', `店铺 #${selectedStore}`); option.value = selectedStore; options.push(option); }
      $('store').replaceChildren(...options); $('store').value = selectedStore;
      $('checked').textContent = data.checked_at ? `最近检测：${dateLabel(data.checked_at, true)}（北京时间）` : '尚无已完成的检测';
      $('page-summary').textContent = `共 ${state.total.toLocaleString('zh-CN')} 个 SKU · 第 ${state.page} / ${Math.max(1, Math.ceil(state.total / state.pageSize))} 页`;
      if (data.last_error) error(`最近检测未成功：${data.last_error}。当前显示已保存结果，请核实数据时间并重试检测。`);
      else error('');
      update(state.checking ? '正在后台检测半托管 SKU，销售和商品数据更新后将自动刷新本页。' : '');
      renderRows();
      for (const metric of panel.querySelectorAll('[data-status]')) metric.setAttribute('aria-pressed', String(metric.dataset.status === $('status').value));
    }
    function clearPoll() { window.clearTimeout(state.timer); state.timer = null; }
    function schedulePoll() { clearPoll(); if (state.checking && !document.hidden) state.timer = window.setTimeout(() => loadData(), 5000); }
    async function request(url, options = {}) {
      const controller = options.signal ? null : new AbortController();
      const timeout = controller ? window.setTimeout(() => controller.abort(), 30000) : null;
      try {
        const response = await window.fetch(url, {credentials:'same-origin', cache:'no-store', ...options, signal:options.signal || controller.signal});
        if (response.status === 401) throw new Error('登录状态已失效，请重新登录后重试。');
        if (response.status === 403) throw new Error('当前账号没有该店铺的访问或管理权限。');
        let payload; try { payload = await response.json(); } catch (_) { throw new Error(`服务返回异常 (${response.status})，请稍后重试。`); }
        if (!response.ok || payload.status !== 'success') throw new Error(payload.message || `请求失败 (${response.status})`);
        return payload.data || {};
      } catch (err) {
        if (err.name === 'AbortError' && controller) throw new Error('请求超时，请检查网络后重试。');
        throw err;
      } finally { if (timeout) window.clearTimeout(timeout); }
    }
    async function loadData() {
      clearPoll();
      if (state.controller) state.controller.abort();
      const controller = new AbortController(); state.controller = controller;
      const requestId = ++state.requestId;
      const timeout = window.setTimeout(() => controller.abort(), 30000);
      state.loading = true; updateControls();
      const params = new URLSearchParams({token_id:$('store').value, site_id:$('site').value, search:$('search').value.trim(), alert_status:$('status').value, page:String(state.page), page_size:String(state.pageSize)});
      try {
        const data = await request(`/api/clearance-alerts?${params}`, {signal:controller.signal});
        if (requestId !== state.requestId) return;
        render(data);
      } catch (err) {
        if (requestId !== state.requestId) return;
        error(`加载告警失败：${err.name === 'AbortError' ? '请求超时，请检查网络后重试。' : err.message || err}`);
        state.checking = false; update('');
        if (!state.rows.length) { emptyRows('暂时无法读取清退告警，请点击“刷新列表”重试。'); $('page-summary').textContent = '加载失败'; }
        else $('checked').textContent += ' · 列表刷新失败，当前为已加载结果';
      } finally {
        window.clearTimeout(timeout);
        if (requestId === state.requestId) { state.controller = null; state.loading = false; updateControls(); schedulePoll(); }
      }
    }
    async function refresh() {
      if (!canManage || state.refreshing || state.checking) return;
      state.refreshing = true; error(''); updateControls();
      update('正在启动检测，完成后将更新清退判断。');
      try {
        const data = await request('/api/clearance-alerts/refresh', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token_id:$('store').value ? Number($('store').value) : null, site_id:$('site').value})});
        state.checking = Boolean(data.checking);
        await loadData();
        if (data.started) toast('已启动半托管 SKU 检测');
      } catch (err) { state.checking = false; update(''); error(`无法启动检测：${err.message || err}`); }
      finally { state.refreshing = false; updateControls(); schedulePoll(); }
    }
    function openFacts(row) {
      state.selected = row;
      $('form-error').hidden = true; $('form-error').textContent = '';
      $('dialog-title').textContent = canManage ? '补充 SKU 资料' : '查看 SKU 资料';
      $('dialog-sku').textContent = `${row.store_name || '半托管店铺'} · ${row.site_id || ''} · ${row.seller_sku || row.sku_key || row.item_id || ''}${row.variation_id ? ` · 规格 ${row.variation_id}` : ''}`;
      const values = factValues(row.facts);
      for (const [name, value] of Object.entries(values)) {
        const field = $('facts-form').elements.namedItem(name); field.value = value; field.disabled = !canManage;
        if (DATE_FIELDS.includes(name)) field.max = name === 'promotion_start_at' ? '' : chinaToday();
      }
      $('reference-visits').textContent = referenceVisits(row);
      $('reference-visits').hidden = !$('reference-visits').textContent;
      $('save').hidden = !canManage; $('save').disabled = false;
      $('dialog-cancel').textContent = canManage ? '取消' : '关闭';
      $('facts-dialog').showModal();
    }
    function closeFacts() { if (!state.saving) { $('facts-dialog').close(); state.selected = null; } }
    async function saveFacts(event) {
      event.preventDefault();
      if (!canManage || state.saving || !state.selected) return;
      const row = state.selected;
      const values = {};
      for (const name of [...DATE_FIELDS,'inbound_history_confirmed','restock_after_stockout','visits_30d']) values[name] = $('facts-form').elements.namedItem(name).value;
      let body;
      try { body = validatedFacts(values, row.sku_key); }
      catch (err) { $('form-error').textContent = err.message; $('form-error').hidden = false; $('form-error').scrollIntoView({block:'nearest'}); return; }
      state.saving = true; $('save').disabled = true; $('save').textContent = '正在保存…'; $('form-error').hidden = true;
      try {
        await request(`/api/clearance-alerts/${encodeURIComponent(row.link_id)}/facts`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
        $('facts-dialog').close(); state.selected = null;
        toast('资料已保存，正在更新该 SKU 判断');
        await loadData();
      } catch (err) { $('form-error').textContent = `保存失败：${err.message || err}`; $('form-error').hidden = false; }
      finally { state.saving = false; $('save').disabled = false; $('save').textContent = '保存并重新判断'; }
    }
    function filteredLoad() { state.page = 1; loadData(); }
    $('filters').addEventListener('submit', event => { event.preventDefault(); filteredLoad(); });
    for (const id of ['store','site','status']) $(id).addEventListener('change', filteredLoad);
    $('reset').addEventListener('click', () => { $('store').value = ''; $('site').value = ''; $('status').value = ''; $('search').value = ''; filteredLoad(); });
    for (const metric of panel.querySelectorAll('[data-status]')) metric.addEventListener('click', () => { $('status').value = $('status').value === metric.dataset.status ? '' : metric.dataset.status; filteredLoad(); });
    $('page-size').addEventListener('change', () => { state.pageSize = Number($('page-size').value) || 25; filteredLoad(); });
    $('prev').addEventListener('click', () => { if (state.page > 1) { state.page--; loadData(); } });
    $('next').addEventListener('click', () => { if (state.page * state.pageSize < state.total) { state.page++; loadData(); } });
    $('reload').addEventListener('click', loadData); $('refresh').addEventListener('click', refresh);
    $('facts-form').addEventListener('submit', saveFacts);
    $('dialog-close').addEventListener('click', closeFacts); $('dialog-cancel').addEventListener('click', closeFacts);
    $('facts-dialog').addEventListener('cancel', event => { if (state.saving) event.preventDefault(); else state.selected = null; });
    document.addEventListener('visibilitychange', () => { if (document.hidden) clearPoll(); else loadData(); });
    window.addEventListener('pagehide', () => { clearPoll(); window.clearTimeout(state.toastTimer); if (state.controller) { state.requestId++; state.controller.abort(); } });
    loadData();
    return {state, loadData, refresh, openFacts, saveFacts};
  }
  const api = {boot, chinaToday, dateInput, dateLabel, numberLabel, reasons, missingLabel, referenceVisits, factValues, validatedFacts};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else if (root.document) {
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', () => boot(root.document, root));
    else boot(root.document, root);
  }
})(typeof window === 'undefined' ? globalThis : window);
