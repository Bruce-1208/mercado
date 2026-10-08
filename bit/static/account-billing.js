(() => {
  'use strict';
  if (document.body.dataset.billingInitialized === 'true') return;
  document.body.dataset.billingInitialized = 'true';
  const sections = ['subscription', 'print-wallet'];
  function activateSection(section, updateUrl = false, focus = false) {
    sections.forEach(name => {
      const selected = name === section, tab = document.getElementById(`${name}-tab`);
      document.getElementById(name).hidden = !selected;
      tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) tab.focus();
    });
    if (updateUrl) {
      const url = new URL(window.location.href);
      if (section === 'print-wallet') url.searchParams.set('section', section);
      else url.searchParams.delete('section');
      url.hash = '';
      window.history.pushState({}, '', url);
    }
  }
  function sectionFromUrl() {
    const url = new URL(window.location.href);
    return url.searchParams.get('section') === 'print-wallet' || url.hash === '#print-wallet' ? 'print-wallet' : 'subscription';
  }
  sections.forEach((section, index) => {
    const tab = document.getElementById(`${section}-tab`);
    tab.addEventListener('click', () => activateSection(section, true));
    tab.addEventListener('keydown', event => {
      let target;
      if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') target = sections[(index + 1) % sections.length];
      else if (event.key === 'Home') target = sections[0];
      else if (event.key === 'End') target = sections[sections.length - 1];
      if (target) { event.preventDefault(); activateSection(target, true, true); }
    });
  });
  window.addEventListener('popstate', () => activateSection(sectionFromUrl()));
  window.addEventListener('hashchange', () => activateSection(sectionFromUrl()));
  activateSection(sectionFromUrl());
  const $ = id => document.getElementById(id), admin = document.body.dataset.admin === 'true';
  const labels = {active:'使用中', expired:'已到期', unpaid:'未开通', over_limit:'站点超额'};
  let accounts = [], editing, mode, busy = false, historySequence = 0;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = cents => `¥${(Number(cents || 0) / 100).toFixed(2)}`;
  const date = value => value ? String(value).replace('T',' ') : '未设置';
  const form = $('edit-form'), field = name => form.elements.namedItem(name);
  const readRequests = new Map();
  function api(path, options = {}) {
    const read = !options.method || options.method === 'GET';
    if (read && readRequests.has(path)) return readRequests.get(path);
    if (!read) readRequests.clear();
    const pending = (async () => {
      const res = await fetch(path, {credentials:'same-origin', ...options, headers:{'Content-Type':'application/json'}});
      const data = await res.json();
      if (!res.ok || data.status !== 'success') throw new Error(data.message || '操作失败，请重试');
      return data.data;
    })().finally(() => {
      if (!read) readRequests.clear();
      else if (readRequests.get(path) === pending) readRequests.delete(path);
    });
    if (read) readRequests.set(path, pending);
    return pending;
  }
  function render() {
    const query = $('search').value.trim().toLowerCase(), status = $('status').value;
    const rows = accounts.filter(a => [a.username,a.display_name,a.organization_key].join(' ').toLowerCase().includes(query) && (!status || (status === 'soon' ? a.status === 'active' && a.days_remaining <= 7 : a.status === status)));
    $('accounts').innerHTML = rows.map(a => `<tr><td><strong>${esc(a.display_name || a.username)}</strong><small>${esc(a.username)} · ${esc(a.organization_key)}</small>${a.is_active ? '' : '<small>账户已停用</small>'}</td><td>${esc(a.plan_name)}</td><td>${a.actual_sites} / ${a.capacity}<small>计费站点：${a.site_count}</small></td><td>${money(a.monthly_cents)}</td><td>${esc(date(a.expires_at))}</td><td>${a.status === 'expired' ? `逾期 ${a.overdue_days} 天` : `${a.days_remaining} 天`}</td><td><span class="badge ${esc(a.status)}">${esc(labels[a.status])}</span></td><td>${admin ? `<button data-action="recharge" data-id="${a.user_id}">登记充值</button> <button data-action="adjust" data-id="${a.user_id}">调整</button> ` : ''}<button data-action="history" data-id="${a.user_id}">记录</button></td></tr>`).join('') || '<tr><td colspan="8">暂无匹配的账户</td></tr>';
  }
  async function load() {
    $('refresh').disabled = true;
    try {
      const data = await api(admin ? '/api/billing/accounts' : '/api/billing/me');
      accounts = admin ? data : [data]; render();
      if (!admin && data.status !== 'active') $('message').textContent = '账户未开通、已到期或站点超额，请联系管理员充值或调整套餐。';
    } catch(e) { $('message').textContent = e.message; $('accounts').innerHTML = '<tr><td colspan="8">加载失败，请点击刷新重试</td></tr>'; }
    finally { $('refresh').disabled = false; }
  }
  async function history(id) {
    const sequence = ++historySequence;
    $('records-panel').hidden = false; $('records').innerHTML = '<tr><td colspan="6">正在加载…</td></tr>';
    const account = accounts.find(a => a.user_id === id);
    $('records-title').textContent = `${account?.display_name || account?.username || ''} · 充值与调整记录`;
    try {
      const rows = await api(`/api/billing/accounts/${id}/records`);
      if (sequence !== historySequence) return;
      const summary = a => `${a.plan === 'enterprise' ? '企业版' : '个人版'}，${a.site_count} 站点，到期 ${date(a.expires_at)}`;
      $('records').innerHTML = rows.map(r => `<tr><td>${esc(date(r.created_at))}</td><td>${r.kind === 'recharge' ? '充值续费' : '手动调整'}</td><td>${r.kind === 'recharge' ? money(r.amount_cents) : '—'}</td><td>${r.months || '—'}</td><td><small>原：${esc(summary(r.before))}</small>现：${esc(summary(r.after))}</td><td>${esc(r.actor_name)}<small>${esc(r.note)}</small></td></tr>`).join('') || '<tr><td colspan="6">暂无充值或调整记录</td></tr>';
    } catch(e) { if (sequence === historySequence) $('records').innerHTML = `<tr><td colspan="6">${esc(e.message)}</td></tr>`; }
  }
  function updateQuote(resetAmount = false) {
    const sites = Number(field('site_count').value), enterprise = field('plan').value === 'enterprise';
    field('site_count').max = '10000';
    const price = (enterprise ? 59900 + Math.max(0, sites - 30) * 1000 : 29900);
    const total = price * Number(field('months').value || 1);
    $('quote').textContent = `标准月费 ${money(price)}${mode === 'recharge' ? `，本次应收 ${money(total)}` : ''}。额度 ${Math.max(enterprise ? 30 : 10, sites)} 个站点。`;
    if (resetAmount) field('amount').value = (total / 100).toFixed(2);
  }
  async function openEditor(id, action) {
    $('message').textContent = '';
    try {
      const account = await api(`/api/billing/accounts/${id}`);
      editing = {...account, request_id: Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('')}; mode = action;
      form.reset(); field('plan').value = account.plan; field('site_count').value = account.site_count;
      field('expires_at').value = account.expires_at || '';
      $('editor-title').textContent = action === 'recharge' ? '登记充值并续费' : '手动调整套餐与到期时间';
      $('editor-account').textContent = `${account.display_name || account.username} · 已分配 ${account.actual_sites} 个站点`;
      document.querySelectorAll('.recharge-field').forEach(el => { el.hidden = action !== 'recharge'; el.querySelector('input').disabled = action !== 'recharge'; });
      document.querySelectorAll('.adjust-field').forEach(el => { el.hidden = action !== 'adjust'; el.querySelector('input').disabled = action !== 'adjust'; });
      field('amount').required = action === 'recharge';
      $('edit-error').textContent = ''; updateQuote(true); $('editor').showModal();
    } catch(e) { $('message').textContent = e.message; }
  }
  $('accounts').addEventListener('click', event => { const button = event.target.closest('button[data-action]'); if (!button) return; const id = Number(button.dataset.id); if (button.dataset.action === 'history') history(id); else openEditor(id, button.dataset.action); });
  ['plan','site_count','months'].forEach(name => field(name).addEventListener('input', () => updateQuote(true)));
  $('search').addEventListener('input',render); $('status').addEventListener('change',render);
  $('refresh').addEventListener('click', () => { $('message').textContent=''; load(); });
  const close = () => { if (!busy) $('editor').close(); };
  $('cancel').addEventListener('click',close); $('close-editor').addEventListener('click',close);
  $('editor').addEventListener('cancel', e => { if (busy) e.preventDefault(); });
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return; busy = true; $('save').disabled = true; $('edit-error').textContent = '';
    const payload = {request_id:editing.request_id, revision:editing.revision || 0, plan:field('plan').value, site_count:Number(field('site_count').value), note:field('note').value, months:Number(field('months').value), amount:field('amount').value, expires_at:field('expires_at').value};
    try {
      await api(`/api/billing/accounts/${editing.user_id}${mode === 'recharge' ? '/recharges' : ''}`, {method:mode === 'recharge' ? 'POST' : 'PUT', body:JSON.stringify(payload)});
      $('editor').close(); $('message').className = 'success'; $('message').textContent = '已保存，充值和调整记录已更新。';
      await load(); await history(editing.user_id);
    } catch(e) { $('edit-error').textContent = e.message; }
    finally { busy = false; $('save').disabled = false; }
  });
  load();
})();
