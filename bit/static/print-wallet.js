(() => {
  'use strict';
  const root = document.getElementById('print-wallet');
  const host = root || document.body;
  if (host.dataset.walletInitialized === 'true') return;
  host.dataset.walletInitialized = 'true';
  const $ = id => document.getElementById(root ? `print-wallet-${id}` : id);
  if (!root) document.body.classList.add('print-wallet');
  const admin = root ? root.dataset.walletAdmin === 'true' : document.body.dataset.admin === 'true';
  const storageKey = `print-wallet.pending-recharges.${root?.dataset.userId || document.body.dataset.userId}`;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const cents = value => Number.isSafeInteger(Number(value)) ? Number(value) : 0;
  const money = value => `¥${(cents(value) / 100).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2})}`;
  const accountName = account => account?.display_name || account?.username || '业务员账户';
  const sameId = (left, right) => String(left) === String(right);
  const date = value => value ? String(value).replace('T', ' ').slice(0, 19) : '—';
  let accounts = [], selectedId = null, unitPrice = 300, canRecharge = admin;
  const historyLimit = 50;
  let historyOffset = 0, historyTotal = 0, historyLoading = false;
  let historySequence = 0, busy = false, editing = null, refunding = null, pending = Object.create(null);
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || '{}');
    if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
      for (const [id, item] of Object.entries(saved)) {
        if (item && typeof item.amount === 'string' && typeof item.note === 'string' && typeof item.request_id === 'string') pending[id] = item;
      }
    }
  } catch (_) { /* Retain in-memory retry protection when session storage is unavailable. */ }
  function savePending() { try { sessionStorage.setItem(storageKey, JSON.stringify(pending)); } catch (_) {} }
  function message(text, success = false) { $('message').textContent = text; $('message').className = `message${success ? ' success' : ''}`; }
  function apiError(text, outcomeUnknown = false) { const error = new Error(text); error.outcomeUnknown = outcomeUnknown; return error; }
  const readRequests = new Map();
  function api(path, options = {}) {
    const read = !options.method || options.method === 'GET';
    if (read && readRequests.has(path)) return readRequests.get(path);
    if (!read) readRequests.clear();
    const pending = request(path, options).finally(() => {
      if (!read) readRequests.clear();
      else if (readRequests.get(path) === pending) readRequests.delete(path);
    });
    if (read) readRequests.set(path, pending);
    return pending;
  }
  async function request(path, options = {}) {
    let response;
    try { response = await fetch(path, {credentials:'same-origin', ...options, headers:{'Content-Type':'application/json'}}); }
    catch (_) { throw apiError('网络连接失败，请重试。', options.method === 'POST'); }
    let payload;
    try { payload = await response.json(); }
    catch (_) { throw apiError('服务返回异常，请刷新或重试。', options.method === 'POST'); }
    if (!payload || typeof payload !== 'object') throw apiError('服务返回异常，请刷新或重试。', options.method === 'POST');
    if (!response.ok || payload.status !== 'success') throw apiError(payload.message || '操作失败，请重试。', options.method === 'POST' && response.status >= 500);
    return payload.data;
  }
  function selectedAccount() { return accounts.find(account => sameId(account.user_id, selectedId)); }
  function accountStatus(account) {
    if (!account.is_active) return {kind:'inactive', label:'账户已停用'};
    return cents(account.balance_cents) < unitPrice ? {kind:'low', label:'余额不足'} : {kind:'ready', label:'可打印'};
  }
  function renderSummary() {
    const account = selectedAccount();
    $('selected-name').textContent = account ? accountName(account) : '暂无账户';
    $('selected-detail').textContent = account ? [account.username, account.organization_key].filter(Boolean).join(' · ') : '暂无可查看的业务员账户。';
    $('selected-status').hidden = !account;
    if (account) {
      const status = accountStatus(account);
      $('selected-status').textContent = status.label; $('selected-status').className = `badge ${status.kind}`;
    }
    $('balance').textContent = account ? money(account.balance_cents) : '—';
    $('print-count').textContent = account ? Math.max(0, Math.floor(cents(account.balance_cents) / unitPrice)).toLocaleString('zh-CN') : '—';
    $('total-recharge').textContent = account ? money(account.total_recharge_cents) : '—';
    $('total-charge').textContent = account ? money(account.total_charge_cents) : '—';
    $('balance-hint').textContent = account && cents(account.balance_cents) < unitPrice ? `余额低于 ${money(unitPrice)}，请联系管理员充值` : '每次打印前校验账户余额';
    $('refresh-records').disabled = !account;
  }
  function renderAccounts() {
    const query = $('search').value.trim().toLowerCase(), lowOnly = $('insufficient-only').checked;
    const rows = accounts.filter(account => [account.username, account.display_name, account.organization_key].join(' ').toLowerCase().includes(query) && (!lowOnly || cents(account.balance_cents) < unitPrice));
    $('accounts').innerHTML = rows.map(account => {
      const status = accountStatus(account), id = esc(account.user_id);
      return `<tr class="${sameId(account.user_id, selectedId) ? 'selected' : ''}"><td><strong>${esc(accountName(account))}</strong><small>${esc([account.username, account.organization_key].filter(Boolean).join(' · '))}</small></td><td><strong>${money(account.balance_cents)}</strong></td><td>${Math.max(0, Math.floor(cents(account.balance_cents) / unitPrice)).toLocaleString('zh-CN')}</td><td><span class="badge ${status.kind}">${status.label}</span></td><td><div class="row-actions"><button type="button" data-action="records" data-id="${id}" aria-label="查看 ${esc(accountName(account))} 的流水">查看流水</button>${canRecharge ? `<button type="button" data-action="recharge" data-id="${id}" aria-label="为 ${esc(accountName(account))} 登记充值">${pending[String(account.user_id)] ? '重试充值' : '登记充值'}</button>` : ''}</div></td></tr>`;
    }).join('') || '<tr><td colspan="5">暂无匹配的业务员账户</td></tr>';
    $('account-count').textContent = `显示 ${rows.length} / ${accounts.length} 个账户`;
  }
  function renderPagination() {
    if (!$('record-page')) return;
    $('record-page').textContent = historyLoading ? '正在加载明细…' : historyTotal ? `第 ${Math.floor(historyOffset / historyLimit) + 1} / ${Math.ceil(historyTotal / historyLimit)} 页 · 共 ${historyTotal} 条` : '暂无匹配的明细';
    $('record-prev').disabled = historyLoading || historyOffset <= 0;
    $('record-next').disabled = historyLoading || historyOffset + historyLimit >= historyTotal;
  }
  function historyQuery() {
    const query = new URLSearchParams({limit:String(historyLimit), offset:String(historyOffset)});
    const filters = {'record-order':'order_id', 'record-printer':'printer', 'record-date-from':'date_from', 'record-date-to':'date_to', 'record-kind':'kind'};
    Object.entries(filters).forEach(([id, name]) => { const value = $(id)?.value.trim(); if (value) query.set(name, value); });
    return query.toString();
  }
  function recordResult(record, charge) {
    if (record.kind === 'recharge') return {kind:'completed', label:'已到账'};
    if (record.kind === 'refund') return {kind:'refunded', label:'已退款'};
    const labels = {pending:'待完成', completed:'面单已生成', refunded:'生成失败，已退款', cancelled:'已取消，已退款', unknown:'历史结果未记录'};
    const kind = record.print_result || (charge && record.request_status === 'reserved' ? 'pending' : record.request_status === 'cancelled' ? 'cancelled' : 'unknown');
    return {kind, label:labels[kind] || '结果未记录'};
  }
  async function history(id) {
    const sequence = ++historySequence, account = accounts.find(item => sameId(item.user_id, id));
    $('records-title').textContent = `${accountName(account)} · 打印与余额明细`;
    $('records').innerHTML = '<tr><td colspan="8">正在加载…</td></tr>';
    $('records-meta').textContent = '按订单显示打印时间、仓库打印人员及结果，充值和退款也保留记录 · 北京时间';
    $('refresh-records').disabled = true;
    historyLoading = true; renderPagination();
    try {
      const data = await api(`/api/print-wallet/accounts/${encodeURIComponent(id)}/records?${historyQuery()}`);
      if (sequence !== historySequence || !sameId(id, selectedId)) return;
      const rows = Array.isArray(data) ? data : data.records || [];
      if (data.account) {
        const index = accounts.findIndex(item => sameId(item.user_id, id));
        if (index >= 0) accounts[index] = data.account;
        renderSummary(); renderAccounts();
      }
      const kinds = {recharge:'充值到账', print:'打印扣费', charge:'打印扣费', refund:'打印退款'};
      const refundActions = new Set();
      $('records').innerHTML = rows.map(record => {
        const amount = cents(record.amount_cents), charge = record.kind === 'print' || record.kind === 'charge';
        const sign = amount < 0 || charge ? '−' : amount > 0 ? '+' : '';
        const reserved = charge && record.request_status === 'reserved';
        const showRefund = canRecharge && reserved && record.request_id && !refundActions.has(record.request_id);
        if (showRefund) refundActions.add(record.request_id);
        const isPrint = charge || record.kind === 'refund', result = recordResult(record, charge);
        const printer = isPrint ? record.printer_name || record.printer_username || '历史人员未记录' : '—';
        const printTime = record.printed_at ? `${esc(date(record.printed_at))}${record.print_time_source === 'settled' ? '<small>历史结算时间</small>' : ''}` : '—';
        const requested = isPrint && record.requested_at ? `<small>发起：${esc(date(record.requested_at))}</small>` : '';
        const actor = !charge && record.actor_name ? `<small>${record.kind === 'refund' ? '退款操作人' : '登记人员'}：${esc(record.actor_name)}</small>` : '';
        const orderMeta = [record.shop_name, record.site_id].filter(Boolean).join(' · ');
        return `<tr><td>${esc(date(record.created_at))}</td><td><span class="record-kind ${esc(record.kind)}">${esc(kinds[record.kind] || record.kind)}</span></td><td><strong>${esc(record.order_id || '—')}</strong>${orderMeta ? `<small>${esc(orderMeta)}</small>` : ''}</td><td>${printTime}${requested}</td><td>${esc(printer)}${isPrint && record.printer_name && record.printer_username && record.printer_username !== record.printer_name ? `<small>${esc(record.printer_username)}</small>` : ''}</td><td class="${sign === '−' ? 'amount-negative' : 'amount-positive'}">${sign}${money(Math.abs(amount))}</td><td>${money(record.balance_after_cents)}</td><td><span class="print-result ${esc(result.kind)}">${esc(result.label)}</span>${actor}<small>${esc(record.note || '无备注')}</small>${showRefund ? `<button class="refund-action" type="button" data-refund-request="${esc(record.request_id)}">退回未完成扣费</button>` : ''}</td></tr>`;
      }).join('') || '<tr><td colspan="8">暂无匹配的充值、扣费或退款记录</td></tr>';
      historyTotal = Math.max(0, Number(data.total ?? rows.length) || 0);
      $('records-meta').textContent = `本页 ${rows.length} 条 / 共 ${historyTotal} 条 · 打印结果指面单生成结果 · 北京时间`;
    } catch (error) {
      if (sequence === historySequence) $('records').innerHTML = `<tr><td colspan="8">${esc(error.message)} 可点击「刷新流水」重试。</td></tr>`;
    } finally {
      if (sequence === historySequence) { $('refresh-records').disabled = !selectedAccount(); historyLoading = false; renderPagination(); }
    }
  }
  async function load() {
    $('refresh').disabled = true;
    try {
      const data = await api('/api/print-wallet/accounts');
      accounts = Array.isArray(data) ? data : data.accounts || [];
      if (Number.isSafeInteger(data.unit_price_cents) && data.unit_price_cents > 0) unitPrice = data.unit_price_cents;
      canRecharge = admin && data.can_recharge !== false;
      $('unit-price').textContent = money(unitPrice);
      if (!selectedAccount()) { selectedId = accounts[0]?.user_id ?? null; historyOffset = 0; }
      renderSummary(); renderAccounts();
      if (selectedId !== null) await history(selectedId);
      else { ++historySequence; historyTotal = 0; historyLoading = false; renderPagination(); $('records').innerHTML = '<tr><td colspan="8">暂无可查看的账户流水</td></tr>'; }
      if (canRecharge && accounts.some(account => pending[String(account.user_id)])) message('有一笔充值结果待确认，请打开对应账户的「重试充值」，使用原记录重试，避免重复登记。');
    } catch (error) {
      message(error.message);
      if (!accounts.length) $('accounts').innerHTML = '<tr><td colspan="5">加载失败，请点击「刷新数据」重试</td></tr>';
      if (!accounts.length) { $('selected-name').textContent = '数据加载失败'; $('records').innerHTML = '<tr><td colspan="8">加载失败，请点击「刷新数据」重试</td></tr>'; historyTotal = 0; historyLoading = false; renderPagination(); }
    } finally { $('refresh').disabled = false; }
  }
  function selectAccount(id) { selectedId = id; historyOffset = 0; renderSummary(); renderAccounts(); return history(id); }
  $('refresh').addEventListener('click', () => { message(''); load(); });
  $('refresh-records').addEventListener('click', () => { if (selectedId !== null) history(selectedId); });
  $('search').addEventListener('input', renderAccounts);
  $('insufficient-only').addEventListener('change', renderAccounts);
  $('accounts').addEventListener('click', event => {
    const button = event.target.closest('button[data-action="records"]');
    if (button) selectAccount(button.dataset.id);
  });
  if ($('record-filters')) {
    $('record-filters').addEventListener('submit', event => {
      event.preventDefault();
      const from = $('record-date-from').value, to = $('record-date-to').value;
      if (from && to && from > to) { message('开始日期不能晚于结束日期。'); return; }
      message(''); historyOffset = 0; if (selectedId !== null) history(selectedId);
    });
    $('reset-record-filters').addEventListener('click', () => { $('record-filters').reset(); message(''); historyOffset = 0; if (selectedId !== null) history(selectedId); });
    $('record-prev').addEventListener('click', () => { if (!historyLoading && historyOffset > 0) { historyOffset = Math.max(0, historyOffset - historyLimit); history(selectedId); } });
    $('record-next').addEventListener('click', () => { if (!historyLoading && historyOffset + historyLimit < historyTotal) { historyOffset += historyLimit; history(selectedId); } });
  }

  if (admin) {
    const form = $('recharge-form'), field = name => form.elements.namedItem(root ? `wallet_${name}` : name);
    // Parse decimal currency without floating point multiplication or rounding.
    function parseAmount(value) {
      const match = /^(\d{1,7})(?:\.(\d{1,2}))?$/.exec(value.trim());
      if (!match) return null;
      const amount = Number(match[1]) * 100 + Number((match[2] || '').padEnd(2, '0'));
      return amount >= 1 && amount <= 100000000 ? amount : null;
    }
    function updatePreview() {
      const amount = parseAmount(field('amount').value);
      const account = accounts.find(item => sameId(item.user_id, editing?.user_id));
      $('recharge-preview').textContent = amount === null ? '输入金额后查看充值结果。' : `本次充值 ${money(amount)}；充值后余额约 ${money(cents(account?.balance_cents) + amount)}，可打印 ${Math.floor((cents(account?.balance_cents) + amount) / unitPrice).toLocaleString('zh-CN')} 次。`;
    }
    function setEditorState() {
      const retrying = editing && Boolean(pending[String(editing.user_id)]);
      field('amount').disabled = busy || retrying; field('note').disabled = busy || retrying;
      $('submit-recharge').disabled = busy; $('cancel-recharge').disabled = busy; $('close-recharge').disabled = busy;
      $('submit-recharge').textContent = busy ? '正在登记…' : retrying ? '使用原记录重试' : '确认已收款并充值';
      $('cancel-recharge').textContent = retrying ? '稍后重试' : '取消';
      $('retry-notice').hidden = !retrying;
    }
    function openRecharge(id) {
      if (!canRecharge || busy) return;
      const account = accounts.find(item => sameId(item.user_id, id));
      if (!account) return;
      const previous = pending[String(id)];
      editing = {user_id:account.user_id, request_id:previous?.request_id || crypto.randomUUID()};
      form.reset(); field('amount').value = previous?.amount || ''; field('note').value = previous?.note || '';
      $('recharge-account').textContent = `${accountName(account)} · 当前余额 ${money(account.balance_cents)}`;
      $('recharge-error').textContent = ''; updatePreview(); setEditorState(); $('recharge-dialog').showModal();
      if (!previous) field('amount').focus();
    }
    function closeRecharge() { if (!busy) $('recharge-dialog').close(); }
    $('accounts').addEventListener('click', event => {
      const button = event.target.closest('button[data-action="recharge"]');
      if (!button) return;
      openRecharge(button.dataset.id);
    });
    field('amount').addEventListener('input', updatePreview);
    $('cancel-recharge').addEventListener('click', closeRecharge);
    $('close-recharge').addEventListener('click', closeRecharge);
    $('recharge-dialog').addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (busy || !editing) return;
      const id = String(editing.user_id), previous = pending[id];
      const amount = previous?.amount || field('amount').value.trim(), note = previous?.note || field('note').value.trim();
      if (parseAmount(amount) === null) { $('recharge-error').textContent = '请输入 ¥0.01 至 ¥1,000,000.00 的金额，最多两位小数。'; return; }
      if (!note) { $('recharge-error').textContent = '请填写收款备注或凭证编号。'; return; }
      const payload = previous || {amount, note, request_id:editing.request_id};
      pending[id] = payload; savePending(); busy = true; setEditorState(); $('recharge-error').textContent = '';
      try {
        const result = await api(`/api/print-wallet/accounts/${encodeURIComponent(id)}/recharges`, {method:'POST', body:JSON.stringify(payload)});
        delete pending[id]; savePending(); $('recharge-dialog').close(); selectedId = id; historyOffset = 0;
        message(result.duplicate ? '这笔充值已登记，未重复入账。' : `已为 ${accountName(accounts.find(account => sameId(account.user_id, id)))} 充值 ${money(parseAmount(amount))}。`, true);
        await load();
      } catch (error) {
        if (!error.outcomeUnknown) { delete pending[id]; savePending(); }
        $('recharge-error').textContent = error.outcomeUnknown ? `${error.message} 充值结果尚未确认，请使用原记录重试。` : error.message;
        renderAccounts();
      } finally { busy = false; setEditorState(); }
    });
    function setRefundState() {
      ['refund-note', 'submit-refund', 'cancel-refund', 'close-refund'].forEach(id => { $(id).disabled = busy; });
      $('submit-refund').textContent = busy ? '正在退回…' : '取消请求并退回余额';
    }
    function closeRefund() { if (!busy) $('refund-dialog').close(); }
    $('records').addEventListener('click', event => {
      const button = event.target.closest('button[data-refund-request]');
      if (!button || !canRecharge || busy) return;
      refunding = {request_id:button.dataset.refundRequest, user_id:selectedId};
      $('refund-form').reset(); $('refund-error').textContent = '';
      $('refund-account').textContent = `${accountName(selectedAccount())} · 打印请求 ${refunding.request_id}`;
      setRefundState(); $('refund-dialog').showModal(); $('refund-note').focus();
    });
    $('cancel-refund').addEventListener('click', closeRefund);
    $('close-refund').addEventListener('click', closeRefund);
    $('refund-dialog').addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    $('refund-form').addEventListener('submit', async event => {
      event.preventDefault(); if (busy || !refunding) return;
      const note = $('refund-note').value.trim();
      if (!note) { $('refund-error').textContent = '请填写退款原因。'; return; }
      busy = true; setRefundState(); $('refund-error').textContent = '';
      try {
        const result = await api(`/api/print-wallet/requests/${encodeURIComponent(refunding.request_id)}/cancel`, {method:'POST', body:JSON.stringify({note})});
        $('refund-dialog').close(); selectedId = refunding.user_id; historyOffset = 0;
        message(result.duplicate ? '该打印请求已经取消，余额未重复退回。' : `已取消未完成的打印请求，退回 ${money(result.refund_cents)}。`, true);
        await load();
      } catch (error) { $('refund-error').textContent = error.outcomeUnknown ? `${error.message} 请重试同一请求，退款不会重复入账。` : error.message; }
      finally { busy = false; setRefundState(); }
    });
  }
  load();
})();
