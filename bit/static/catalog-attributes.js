/* Shared, read-only list checks and explicit batch attribute edits. */
(() => {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const cache = new Map();
  const pending = new Map();
  const summaries = new Map();
  const versions = new Map();
  let busy = false, scheduled = false, state = null;
  function badge(kind, row) {
    const value = JSON.stringify([row.updated_at, row.source_json, row.source_snapshot_json, row.original_1688, row.ai_original]);
    let revision = 0; for (const c of value) revision = (Math.imul(revision, 31) + c.charCodeAt(0)) | 0;
    return `<div class="zs-attribute-status" data-catalog-attributes="${kind}" data-catalog-id="${Number(row.id)}" ${kind === 'collection' ? `data-collection-attribute-check="${Number(row.id)}"` : ''} data-catalog-revision="${revision}" aria-live="polite">属性核验中…</div>`;
  }
  function updateSummary() {
    for (const [kind, {host, ids}] of summaries) {
      host.hidden = !ids.length;
      const statuses = ids.map(id => document.querySelector(`[data-catalog-attributes="${kind}"][data-catalog-id="${id}"]`)?.dataset.status);
      const count = status => statuses.filter(s => s === status).length;
      const text = `本页属性：缺失 ${count('missing')} 件 · 齐全 ${count('complete')} 件 · 待确认/暂未核验 ${count('pending') + count('unavailable')} 件 · 核验中 ${statuses.filter(s => !s).length} 件`;
      if (host.textContent !== text) host.textContent = text;
    }
  }
  function summary(kind, rows, host) { if (host) { summaries.set(kind, {host, ids:rows.map(r => Number(r.id))}); updateSummary(); } }

  const key = host => `${host.dataset.catalogAttributes}:${host.dataset.catalogId}:${host.dataset.catalogRevision}`;
  function paint(host, report) {
    host.dataset.status = report.status;
    const status = host.closest('tr')?.querySelector('[data-collection-data-status="checking"]');
    if (status) {
      status.textContent = report.status === 'complete' ? '已采集资料可用' : report.status === 'missing' ? '资料待补充' : '属性待核验';
      status.className = `market-status-tag ${report.status === 'complete' ? 'ok' : 'partial'}`;
    }
    const names = [...new Set((report.missing || []).map(e => `${e.name} (${e.attribute_id})${e.is_choice ? ' · 需选择' : ''}`))];
    host.innerHTML = `<span class="market-status-tag ${report.status === 'complete' ? 'ok' : 'partial'}">${esc(report.message || '属性暂未核验')}</span>${names.length ? `<small title="${esc((report.missing || []).map(e => `${e.scope}：${e.name} (${e.attribute_id})`).join('；'))}">缺少：${esc(names.join('、'))}</small>` : ''}${report.pending?.length ? `<small>${esc(report.pending.map(e => `${e.name}：${e.reason}`).join('；'))}</small>` : ''}`;
  }
  async function check(kind, ids) {
    const response = await fetch(`/api/catalog/attributes/check?kind=${encodeURIComponent(kind)}&ids=${ids.join(',')}`, {cache:'no-store', signal:AbortSignal.timeout(60000)});
    const body = await response.json().catch(() => ({}));
    if (!response.ok || body.status !== 'success' || !Array.isArray(body.data?.results)) throw new Error(body.message || '属性暂未核验，请刷新重试');
    return body.data.results;
  }
  async function scan() {
    scheduled = false;
    for (const host of document.querySelectorAll('[data-catalog-attributes]:not([data-check-started])')) {
      host.dataset.checkStarted = 'true';
      const cached = cache.get(key(host));
      if (cached && Date.now() - cached.at < 60000) { paint(host, cached.report); continue; }
      const kind = host.dataset.catalogAttributes;
      if (!pending.has(kind)) pending.set(kind, new Set());
      pending.get(kind).add(Number(host.dataset.catalogId));
    }
    updateSummary();
    if (busy) return;
    busy = true;
    try {
      while (pending.size) {
        const [kind, set] = pending.entries().next().value;
        const ids = [...set].slice(0, 5);
        ids.forEach(id => set.delete(id));
        if (!set.size) pending.delete(kind);
        const revisions = new Set([...document.querySelectorAll(`[data-catalog-attributes="${kind}"]`)].filter(host => ids.includes(Number(host.dataset.catalogId))).map(key));
        const version = versions.get(kind);
        let reports;
        try { reports = await check(kind, ids); }
        catch (error) { reports = ids.map(id => ({id, status:'unavailable', message:error.message})); }
        for (const id of ids) {
          const report = reports.find(r => Number(r.id) === id) || {id, status:'unavailable', message:'属性暂未核验，请刷新重试'};
          for (const host of document.querySelectorAll(`[data-catalog-attributes="${kind}"][data-catalog-id="${id}"]`)) {
            if (version === versions.get(kind) && revisions.has(key(host))) {
              if (report.status !== 'unavailable') cache.set(key(host), {at:Date.now(), report});
              paint(host, report);
            }
          }
        }
        updateSummary();
      }
    } finally { busy = false; }
  }
  function schedule() { if (!scheduled) { scheduled = true; queueMicrotask(scan); } }
  const dialog = () => document.getElementById('catalog-attributes-dialog');
  const node = id => document.getElementById(`catalog-attributes-${id}`);
  function selected(kind) {
    if (kind === 'supplier') return [...products1688Selected];
    if (kind === 'original') return [...aiOriginalSelected];
    return [...selectedMercadoCollectionIds];
  }
  function setBusy(value) {
    state.saving = value;
    dialog().querySelectorAll('button, input, select').forEach(el => { el.disabled = value; });
  }
  function showReports(reports) {
    node('results').innerHTML = reports.map(r => `<article><strong>产品 #${Number(r.id)} · ${esc(r.category_id || '未分类')}</strong><div class="zs-attribute-status" data-status="${esc(r.status)}"><span>${esc(r.message)}</span>${r.missing?.length ? `<ul>${r.missing.map(e => `<li>${esc(e.scope)}：${esc(e.name)} (${esc(e.attribute_id)})${e.is_choice ? ' · 需手动选择' : ''}</li>`).join('')}</ul>` : ''}${r.pending?.length ? `<p>${esc(r.pending.map(e => `${e.name}：${e.reason}`).join('；'))}</p>` : ''}</div></article>`).join('');
    const fields = new Map();
    for (const report of reports) for (const field of report.fields || []) {
      fields.set(`${report.category_id}:${field.attribute_id}`, {...field, category_id:report.category_id});
    }
    state.fields = [...fields.values()];
    const previous = node('field').value;
    const previousValue = node('value')?.value || '';
    node('field').innerHTML = '<option value="">选择属性及适用类目</option>' + state.fields.map((f,i) => `<option value="${i}">${esc(f.category_id)} · ${esc(f.name)} (${esc(f.attribute_id)})</option>`).join('');
    if ([...node('field').options].some(o => o.value === previous)) node('field').value = previous;
    fieldChanged();
    if (node('field').value === previous) node('value').value = previousValue;
  }
  function fieldChanged() {
    const field = node('field').value === '' ? null : state.fields[Number(node('field').value)];
    node('value-wrap').innerHTML = field?.is_choice
      ? `<select id="catalog-attributes-value" aria-label="属性值"><option value="">请选择有效选项</option>${field.choices.map(c => `<option value="${esc(c.id || c.name)}">${esc(c.name)}</option>`).join('')}</select>`
      : '<input id="catalog-attributes-value" aria-label="属性值" maxlength="255" placeholder="填写属性值，例如 generic">';
  }
  async function refresh(kind) {
    versions.set(kind, (versions.get(kind) || 0) + 1);
    cache.clear();
    if (kind === 'supplier') await load1688Products(false);
    else if (kind === 'original') await loadAiOriginalProducts({preserveTaskStatus:true});
    else await loadMercadoList();
  }
  async function apply(mode) {
    if (!state || state.saving) return;
    const payload = {kind:state.kind, ids:state.ids, mode};
    if (mode === 'set') {
      const field = node('field').value === '' ? null : state.fields[Number(node('field').value)];
      const value = node('value').value.trim();
      if (!field || !value) { node('message').textContent = '请选择属性并填写有效值。'; return; }
      Object.assign(payload, {category_id:field.category_id, attribute_id:field.attribute_id, value, only_missing:node('only-missing').checked});
    }
    setBusy(true);
    node('message').textContent = '正在保存属性…';
    try {
      const response = await fetch('/api/catalog/attributes', {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
      const body = await response.json().catch(() => ({}));
      if (!response.ok || body.status !== 'success') throw new Error(body.message || '属性保存失败');
      showReports(body.data.results || []);
      const remaining = (body.data.results || []).filter(r => r.status !== 'complete').length;
      node('message').textContent = `已修改 ${Number(body.data.changed || 0)} 件产品${remaining ? `；${remaining} 件仍有缺失或待确认属性，请查看下方明细。` : '，属性检查已更新。'}`;
      try { await refresh(state.kind); } catch (_) { node('message').textContent += ' 列表刷新失败，请手动刷新。'; }
    } catch (error) { node('message').textContent = error.message; }
    finally { setBusy(false); }
  }
  async function open(kind, mode='set') {
    const ids = selected(kind).map(Number);
    state = {kind, ids, fields:[], saving:false};
    node('message').textContent = ids.length ? `已选择 ${ids.length} 件，正在读取当前属性…` : '请先勾选需要处理的产品。';
    node('results').replaceChildren();
    node('field').innerHTML = '<option value="">选择属性及适用类目</option>';
    node('only-missing').checked = true;
    fieldChanged();
    dialog().showModal();
    if (!ids.length || ids.length > 100) {
      if (ids.length > 100) node('message').textContent = '每次最多处理 100 件，请减少所选产品。';
      node('save').disabled = node('generic').disabled = true; return;
    }
    setBusy(true);
    try {
      const reports = [];
      for (let i=0;i<ids.length;i+=10) reports.push(...await check(kind, ids.slice(i,i+10)));
      showReports(reports);
      node('message').textContent = `已选择 ${ids.length} 件产品。仅检查必填属性和已有空值；generic 补全保留已有值并跳过选择项。`;
      setBusy(false);
      if (mode === 'generic') await apply('generic');
    } catch (error) { node('message').textContent = error.message; setBusy(false); }
  }
  window.CatalogAttributes = {badge, open, apply, summary};
  document.addEventListener('DOMContentLoaded', () => {
    document.body.insertAdjacentHTML('beforeend', `<dialog id="catalog-attributes-dialog" aria-labelledby="catalog-attributes-title"><form method="dialog"><header><h3 id="catalog-attributes-title">批量设置 / 补全产品属性</h3><button type="button" id="catalog-attributes-close" class="secondary" aria-label="关闭属性设置">关闭</button></header><p id="catalog-attributes-message" role="status"></p><div class="zs-attribute-controls"><label>属性<select id="catalog-attributes-field"></select></label><label>属性值<span id="catalog-attributes-value-wrap"></span></label><label class="zs-attribute-checkbox"><input id="catalog-attributes-only-missing" type="checkbox" checked>仅补空值（取消后统一替换所选属性）</label><button type="button" id="catalog-attributes-save" class="primary">应用属性值</button><button type="button" id="catalog-attributes-generic" class="secondary">一键补全 generic</button></div><div id="catalog-attributes-results"></div></form></dialog>`);
    node('close').addEventListener('click', () => dialog().close());
    dialog().querySelector('form').addEventListener('submit', event => { event.preventDefault(); apply('set'); });
    node('field').addEventListener('change', fieldChanged);
    node('save').addEventListener('click', () => apply('set'));
    node('generic').addEventListener('click', () => apply('generic'));
    dialog().addEventListener('cancel', event => { if (state?.saving) event.preventDefault(); });
    new MutationObserver(schedule).observe(document.body, {childList:true, subtree:true});
    schedule();
  });
})();
