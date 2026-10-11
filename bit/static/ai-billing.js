(() => {
  'use strict';
  const $ = id => document.getElementById(`ai-billing-${id}`);
  const form = $('filters');
  if (!form) return;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
  const number = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) && Number(value) >= 0 ? Number(value) : null;
  const count = value => number(value) === null ? '待确认' : number(value).toLocaleString('zh-CN');
  const money = value => {
    const amount = number(value);
    if (amount === null) return '待确认';
    if (amount > 0 && amount < .0001) return '¥<0.0001';
    return `¥${amount.toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 4})}`;
  };
  const timestamp = value => {
    if (!value) return ['时间待确认', ''];
    let normalized = value;
    if (typeof value === 'number') normalized = value < 1e12 ? value * 1000 : value;
    else if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(value) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(value)) normalized = `${value.replace(' ', 'T')}+08:00`;
    const date = new Date(normalized);
    if (Number.isNaN(date.getTime())) return [String(value), ''];
    const formatted = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'}).format(date);
    return formatted.split(' ');
  };
  let page = 1, pageSize = 20, total = 0, loading = false, controller, sequence = 0;
  let filters = {};
  let taskLabels = new Map();
  const filterNames = ['start_date', 'end_date', 'salesperson', 'task_type', 'model', 'keyword'];

  function message(text) {
    $('message').textContent = text || '';
    $('message').hidden = !text;
  }
  function setLoading(value) {
    loading = value;
    $('refresh').disabled = value;
    $('apply').disabled = value;
    $('page-size').disabled = value;
    $('metrics').setAttribute('aria-busy', String(value));
    $('table-wrap').setAttribute('aria-busy', String(value));
    $('prev').disabled = value || page <= 1;
    $('next').disabled = value || page * pageSize >= total;
  }
  function renderSummary(summary = {}) {
    const calls = number(summary.calls ?? summary.record_count);
    const unknown = number(summary.unknown_usage_count);
    const unpriced = number(summary.unpriced_count ?? summary.unpriced_record_count);
    $('calls').textContent = count(calls);
    $('tokens').textContent = calls > 0 && unknown === calls ? '待确认' : count(summary.total_tokens);
    $('cost').textContent = calls > 0 && unpriced === calls ? '待确认' : money(summary.estimated_cost_cny);
    // A call can have both missing usage and an unknown price; show each count separately.
    $('pending').textContent = unknown === null || unpriced === null ? '待确认' : `${count(unknown)} / ${count(unpriced)}`;
    $('pending-hint').textContent = '用量待确认 / 费用待估值';
    $('token-hint').textContent = unknown > 0 ? `已确认用量合计，另有 ${count(unknown)} 条待确认` : '输入与输出 Token 合计';
    $('cost-hint').textContent = unpriced > 0 ? `已估值费用合计，另有 ${count(unpriced)} 条待估值` : '按调用时的模型单价估算';
  }
  function renderOptions(options = {}) {
    options = {...options, salespeople: options.salespeople ?? options.salespersons};
    const specs = [['salesperson', 'salespeople', '全部业务员'], ['task-type', 'task_types', '全部任务类型'], ['model', 'models', '全部模型']];
    for (const [id, key, label] of specs) {
      if (!Array.isArray(options[key])) continue;
      const select = $(id), selected = select.value;
      const values = options[key].map(item => typeof item === 'object' && item !== null ? {value: String(item.value ?? item.salesperson ?? item.id ?? item.name ?? ''), label: String(item.label ?? item.salesperson ?? item.actor_name ?? item.name ?? item.value ?? '')} : {value: String(item ?? ''), label: String(item ?? '')}).filter(item => item.value);
      if (key === 'task_types') taskLabels = new Map(values.map(item => [item.value, item.label]));
      if (selected && !values.some(item => item.value === selected)) values.push({value: selected, label: selected});
      select.replaceChildren(new Option(label, ''), ...values.map(item => new Option(item.label, item.value)));
      select.value = selected;
    }
  }
  function renderRows(items) {
    $('records').innerHTML = items.map(item => {
      const [date, time] = timestamp(item.created_at);
      const known = item.usage_known !== false && number(item.total_tokens) !== null;
      const usage = known ? `<strong>${esc(count(item.total_tokens))}</strong>` : '<span class="pending">待确认</span>';
      const price = number(item.estimated_cost_cny);
      const details = known ? `<div class="token-detail"><small>输入 ${esc(count(item.input_tokens))}</small><small>输出 ${esc(count(item.output_tokens))}</small></div>${number(item.cached_tokens) > 0 ? `<small>其中缓存 ${esc(count(item.cached_tokens))}</small>` : ''}` : '';
      const taskType = taskLabels.get(String(item.task_type)) || item.task_type || '其他任务';
      const missingOriginalActor = item.task_type === 'ai_original' && !Number(item.actor_id) &&
        (!item.salesperson || item.salesperson === '系统任务');
      const salesperson = missingOriginalActor ? '业务员待核实' : (item.salesperson || item.actor_name || '系统任务');
      const actor = !missingOriginalActor && item.actor_name && item.actor_name !== salesperson ? `<small>操作人：${esc(item.actor_name)}</small>` : '';
      const taskId = item.task_id ? `<small>任务编号：${esc(item.task_id)}</small>` : '';
      const request = item.request_id ? `<small>请求：${esc(item.request_id)}</small>` : '';
      return `<tr><td class="time-cell">${esc(date)}${time ? `<small>${esc(time)}</small>` : ''}</td><td class="task-cell"><span class="task-type">${esc(taskType)}</span><br><strong>${esc(item.task_name || taskType)}</strong>${taskId}${request}</td><td>${esc(salesperson)}${actor}</td><td class="model-cell">${esc(item.model || '模型待确认')}${item.provider ? `<small>${esc(item.provider)}</small>` : ''}</td><td class="number-cell">${usage}${details}</td><td class="number-cell">${price === null ? '<span class="pending">待确认</span>' : `<strong>${esc(money(price))}</strong>`}</td></tr>`;
    }).join('') || `<tr><td colspan="6" class="state-cell"><strong>${Object.keys(filters).length ? '没有符合筛选条件的账单' : '暂无 AI 调用记录'}</strong><span>${Object.keys(filters).length ? '可以调整筛选条件，或清空筛选查看全部记录。' : '产生 Token 用量后，调用记录会显示在这里。'}</span></td></tr>`;
  }
  function renderPagination() {
    const pages = Math.max(1, Math.ceil(total / pageSize));
    $('count').textContent = `共 ${count(total)} 条记录`;
    $('page-info').textContent = total ? `第 ${page} / ${pages} 页 · ${count((page - 1) * pageSize + 1)}–${count(Math.min(page * pageSize, total))} 条` : '共 0 条记录';
    $('page-size').value = String(pageSize);
  }
  async function load() {
    controller?.abort();
    controller = new AbortController();
    const current = ++sequence;
    setLoading(true);
    message('');
    $('records').innerHTML = '<tr><td colspan="6" class="state-cell"><span class="loading-dot" aria-hidden="true"></span>正在加载账单…</td></tr>';
    $('count').textContent = '正在加载…';
    const params = new URLSearchParams({...filters, page: String(page), page_size: String(pageSize)});
    try {
      const response = await fetch(`/api/ai-billing?${params}`, {credentials: 'same-origin', headers: {Accept: 'application/json'}, cache: 'no-store', signal: controller.signal});
      if (response.status === 401 || (response.redirected && new URL(response.url, location.href).pathname === '/login')) throw new Error('登录已过期，请返回工作台重新登录后查看账单。');
      let payload;
      try { payload = await response.json(); } catch (_) { throw new Error('账单服务返回异常，请稍后刷新重试。'); }
      if (!response.ok || payload.status !== 'success') throw new Error(payload.message || payload.error || '账单加载失败，请稍后刷新重试。');
      if (current !== sequence) return;
      const data = payload.data || {};
      if (!Array.isArray(data.items)) throw new Error('账单数据格式异常，请稍后刷新重试。');
      total = number(data.total) ?? data.items.length;
      page = Math.max(1, Number(data.page) || page);
      pageSize = Number(data.page_size) || pageSize;
      renderOptions(data.options);
      renderSummary(data.summary);
      renderRows(data.items);
      renderPagination();
    } catch (error) {
      if (current !== sequence || error.name === 'AbortError') return;
      message(error.message || '账单加载失败，请刷新重试。');
      $('records').innerHTML = '<tr><td colspan="6" class="state-cell"><strong>账单暂时无法加载</strong><span>请点击“刷新账单”重试。</span></td></tr>';
      $('count').textContent = '加载失败';
      $('page-info').textContent = '—';
      total = 0;
      renderSummary();
    } finally {
      if (current === sequence) setLoading(false);
    }
  }
  function applyFilters() {
    const values = new FormData(form);
    const start = String(values.get('start_date') || ''), end = String(values.get('end_date') || '');
    if (start && end && start > end) {
      message('开始日期不能晚于结束日期，请调整后重新筛选。');
      $('end-date').focus();
      return;
    }
    filters = Object.fromEntries(filterNames.map(name => [name, String(values.get(name) || '').trim()]).filter(([, value]) => value));
    page = 1;
    load();
  }
  form.addEventListener('submit', event => { event.preventDefault(); applyFilters(); });
  $('clear').addEventListener('click', () => { form.reset(); filters = {}; page = 1; load(); });
  $('refresh').addEventListener('click', () => load());
  $('prev').addEventListener('click', () => { if (!loading && page > 1) { page--; load(); } });
  $('next').addEventListener('click', () => { if (!loading && page * pageSize < total) { page++; load(); } });
  $('page-size').addEventListener('change', () => { pageSize = Number($('page-size').value) || 20; page = 1; load(); });
  load();
})();
