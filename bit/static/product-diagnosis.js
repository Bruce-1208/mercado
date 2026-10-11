(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const root = $('product-diagnosis');
  if (!root) return;
  const labels = {high:'优先处理',medium:'需要关注',low:'改进机会',pending:'待核对',healthy:'当前正常'};
  const sourceNames = {item:'商品状态与库存',quality:'刊登质量',experience:'购物体验'};
  const sourceLabels = {ok:'已读取',partial:'部分数据',error:'读取失败',no_data:'数据不足',restricted:'接口受限',unsupported:'暂不支持'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const href = value => { try { const u = new URL(value); return u.protocol === 'https:' && !u.username ? u.href : ''; } catch (_) { return ''; } };
  const date = value => { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.valueOf()) ? '—' : d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}); };
  const points = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? `${value.toFixed(1).replace(/\.0$/, '')}` : '—';
  let page = 1, total = 0, detailRow = null, timer, sequence = 0, controller, running = false, starting = false;
  const permissionManage = root.dataset.canManage === 'true';
  const permissionTask = root.dataset.canTask === 'true';
  const initialLink = new URLSearchParams(location.search).get('link_id');
  let openedInitial = false;

  function enterpriseUrl(url) {
    const u = new URL(url, location.origin);
    const organization = new URLSearchParams(location.search).get('organization_key');
    if (organization) u.searchParams.set('organization_key', organization);
    return u.pathname + u.search;
  }
  async function api(url, options = {}) {
    const response = await fetch(enterpriseUrl(url), {credentials:'same-origin', ...options});
    const payload = await response.json();
    if (!response.ok || payload.status !== 'success') throw new Error(payload.message || '商品诊断请求失败');
    return payload.data;
  }
  function showError(message) { $('pd-error').textContent = message || ''; $('pd-error').hidden = !message; }
  function badge(risk) { return `<span class="pd-badge ${esc(labels[risk] ? risk : 'pending')}">${esc(labels[risk] || labels.pending)}</span>`; }
  function sourcesIncomplete(row) {
    return Object.entries(row.sources || {}).filter(([, value]) => value.status !== 'ok').map(([key,value]) => `${sourceNames[key] || key}：${sourceLabels[value.status] || '待核对'}`).join('；');
  }
  function renderRows(rows) {
    if (!rows.length) {
      $('pd-results').innerHTML = `<div class="pd-empty"><strong>${total ? '当前页没有结果' : '当前范围没有诊断结果'}</strong>首次使用请先同步店铺链接，再点击“诊断当前店铺范围”；也可以调整筛选条件。</div>`;
      return;
    }
    $('pd-results').innerHTML = rows.map(row => {
      const first = (row.issues || [])[0], image = href(row.thumbnail_url);
      return `<article class="pd-product">
        <div class="pd-product-info">${image ? `<img src="${esc(image)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : '<span class="pd-no-image">暂无图片</span>'}<div><div class="pd-product-title">${esc(row.title || row.item_id)}</div><div class="pd-meta">${esc(row.item_id)} · ${esc(row.seller_sku || '未提供 SKU')}<br>${esc(row.store_name)} · ${esc(row.site_id)}</div></div></div>
        <div class="pd-product-risk">${badge(row.risk)}<div class="pd-meta">${row.stale ? '结果已过期，请重新诊断' : row.data_status === 'partial' ? '部分数据待核对' : `${row.issue_count} 项待处理`}</div></div>
        <div class="pd-score">刊登质量 <b>${points(row.sources?.quality?.status === 'ok' ? row.quality_score : null)}</b><br>购物体验 <b>${points(row.sources?.experience?.status === 'ok' ? row.experience_score : null)}</b></div>
        <div class="pd-product-issue"><div class="pd-issue-title">${esc(first?.title || (row.risk === 'healthy' ? '本次未发现待处理问题' : '需要补充平台数据'))}</div><div class="pd-suggestion">${esc(first?.suggestion || sourcesIncomplete(row) || (row.stale ? '请重新诊断以确认最新状态。' : '持续关注商品经营表现。'))}</div><div class="pd-meta">${date(row.checked_at)}</div></div>
        <div class="pd-product-action"><button class="pd-button" data-detail="${esc(row.link_id)}">查看诊断</button></div>
      </article>`;
    }).join('');
    $('pd-results').querySelectorAll('img').forEach(img => img.addEventListener('error', () => { img.hidden = true; }));
  }
  function updateStores(stores) {
    const select = $('pd-store'), selected = select.value;
    select.innerHTML = '<option value="">全部授权店铺</option>' + (stores || []).map(row => `<option value="${esc(row.id)}">${esc(row.name)}</option>`).join('');
    if (selected && [...select.options].some(row => row.value === selected)) select.value = selected;
    else if (selected) { const missing = document.createElement('option'); missing.value=selected; missing.textContent='原店铺范围不可用'; select.add(missing); select.value=selected; }
  }
  function updateRuns(runs) {
    const active = (runs || []).find(row => ['queued','running'].includes(row.status));
    const latest = active || (runs || [])[0];
    running = Boolean(active);
    $('pd-start').disabled = !permissionManage || running || starting;
    const box = $('pd-progress');
    box.hidden = !latest;
    if (latest) {
      const status = {queued:'已排队',running:'诊断中',succeeded:'诊断完成',partial:'诊断完成，部分数据待核对',failed:'诊断未完成'}[latest.status] || '待核对';
      box.textContent = `${status} · 已诊断 ${latest.processed} 件${latest.failed ? `，${latest.failed} 件数据不完整` : ''} · ${date(latest.updated_at)}${latest.message ? ` · ${latest.message}` : ''}`;
    }
    clearTimeout(timer);
    if (running && !document.hidden) timer = setTimeout(load, 3000);
  }
  async function load() {
    const current = ++sequence;
    controller?.abort(); controller = new AbortController();
    const query = new URLSearchParams({page:String(page),page_size:'50'});
    for (const [id,key] of [['pd-store','token_id'],['pd-site','site_id'],['pd-risk','risk'],['pd-category','category'],['pd-search','search']]) if ($(id).value) query.set(key,$(id).value);
    try {
      const data = await api(`/api/product-diagnosis?${query}`, {signal:controller.signal});
      if (current !== sequence) return;
      total = data.total || 0;
      const pages = Math.max(1, Math.ceil(total / 50));
      if (page > pages) { page=pages; return load(); }
      showError(''); updateStores(data.stores);
      const summary = data.summary || {};
      Object.keys(labels).forEach(key => { $('pd-'+key).textContent=String(summary[key] || 0); });
      document.querySelectorAll('.pd-metric').forEach(el => el.setAttribute('aria-pressed',String(el.dataset.risk === $('pd-risk').value)));
      $('pd-count').textContent=`当前筛选 ${total} 件商品 · 已诊断范围 ${summary.total || 0} 件`;
      $('pd-page-label').textContent=`第 ${page} / ${pages} 页 · 共 ${total} 件`;
      $('pd-prev').disabled=page<=1; $('pd-next').disabled=page>=pages;
      renderRows(data.rows || []); updateRuns(data.runs);
      if (!openedInitial && initialLink && /^\d+$/.test(initialLink)) { openedInitial=true; openDetail(initialLink); }
    } catch (error) {
      if (error.name !== 'AbortError' && current === sequence) { showError(error.message); if (running && !document.hidden) timer=setTimeout(load,5000); }
    }
  }
  async function start() {
    if (starting || !permissionManage) return;
    starting=true; $('pd-start').disabled=true; showError('');
    try {
      await api('/api/product-diagnosis/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token_ids:$('pd-store').value ? [Number($('pd-store').value)] : [],site_id:$('pd-site').value})});
      page=1; await load();
    } catch (error) { showError(error.message); }
    finally { starting=false; $('pd-start').disabled=!permissionManage || running; }
  }
  let detailSequence = 0;
  async function openDetail(id) {
    const current=++detailSequence;
    detailRow=null; $('pd-today').disabled=true; $('pd-detail').innerHTML='<p>正在读取平台诊断依据…</p>';
    $('pd-detail-message').textContent=''; $('pd-detail-title').textContent='正在读取…';
    if (!$('pd-dialog').open) $('pd-dialog').showModal();
    try {
      const row=await api(`/api/product-diagnosis/${id}`);
      if (current !== detailSequence) return;
      detailRow=row; $('pd-detail-title').textContent=row.title || row.item_id;
      const live=href(row.permalink);
      $('pd-detail').innerHTML=`<div class="pd-detail-overview">${badge(row.risk)}<span class="pd-meta">${esc(row.store_name)} · ${esc(row.site_id)} · ${esc(row.item_id)}</span>${live ? `<a class="pd-button" target="_blank" rel="noopener noreferrer" href="${esc(live)}">查看平台商品 ↗</a>` : ''}</div><p class="pd-meta">诊断时间：${date(row.checked_at)}${row.stale ? ' · 已过期，请重新诊断' : ''}</p>
      <h3 class="pd-section-title">数据完整性</h3><div class="pd-source-grid">${Object.entries(sourceNames).map(([key,name]) => { const s=(row.sources || {})[key] || {}; const value=s.status !== 'ok' ? null : key==='quality' ? row.quality_score : key==='experience' ? row.experience_score : null; return `<div><strong>${name}</strong>${esc(sourceLabels[s.status] || '待核对')}${value !== null && value !== undefined ? ` · ${points(value)} 分` : ''}<div>${esc(s.message || (key==='item' ? `可售库存：${row.available_quantity ?? '未提供'}；状态：${row.listing_status || '未提供'}` : '平台数据已保存'))}</div></div>`; }).join('')}</div>
      <h3 class="pd-section-title">待处理问题 · ${(row.issues || []).length} 项</h3>${(row.issues || []).map(issue => `<section class="pd-detail-issue"><span class="pd-badge ${esc(issue.severity)}">${esc(labels[issue.severity])}</span> <span class="pd-meta">${issue.source==='official' ? '平台诊断' : '本地规则 · 平台实时字段'}</span><h3>${esc(issue.title)}</h3><p>${esc(issue.suggestion)}</p><details><summary>查看平台依据与原文</summary><p>${esc(issue.evidence)}</p>${issue.official_suggestion ? `<p>平台建议原文：${esc(issue.official_suggestion)}</p>` : ''}${issue.freeze_notice ? `<p>平台保护提示：${esc(issue.freeze_notice)}</p>` : ''}${href(issue.official_url) ? `<a href="${esc(href(issue.official_url))}" target="_blank" rel="noopener noreferrer">打开平台处理入口 ↗</a>` : ''}</details></section>`).join('') || '<div class="pd-empty">本次没有已确认的待处理问题；请同时核对数据完整性。</div>'}`;
      $('pd-today').textContent='加入今日必办'; $('pd-today').disabled=!permissionTask || row.stale || !(row.issues || []).length;
    } catch(error) { if (current===detailSequence) { $('pd-detail-title').textContent='诊断详情暂不可用'; $('pd-detail').textContent=error.message; } }
  }
  $('pd-today').addEventListener('click',async () => {
    if (!detailRow || !permissionTask) return;
    const id=detailRow.link_id; $('pd-today').disabled=true;
    try { await api(`/api/product-diagnosis/${id}/today`,{method:'POST'}); if (detailRow?.link_id===id) { $('pd-detail-message').textContent='已加入今日必办，可在工作台分派负责人并记录处理结果。24 小时为内部建议处理时限。'; $('pd-today').textContent='已加入今日必办'; } }
    catch(error) { if (detailRow?.link_id===id) { $('pd-detail-message').textContent=error.message; $('pd-today').disabled=false; } }
  });
  $('pd-results').addEventListener('click',event => { const button=event.target.closest('[data-detail]'); if(button) openDetail(button.dataset.detail); });
  $('pd-close').addEventListener('click',() => { ++detailSequence; $('pd-dialog').close(); detailRow=null; });
  $('pd-dialog').addEventListener('cancel',() => { ++detailSequence; detailRow=null; });
  $('pd-start').addEventListener('click',start); $('pd-refresh').addEventListener('click',load);
  $('pd-filters').addEventListener('submit',event => {event.preventDefault(); page=1; load();});
  ['pd-store','pd-site','pd-risk','pd-category'].forEach(id => $(id).addEventListener('change',() => {page=1; load();}));
  document.querySelectorAll('.pd-metric').forEach(el => el.addEventListener('click',() => {$('pd-risk').value=$('pd-risk').value===el.dataset.risk ? '' : el.dataset.risk; page=1; load();}));
  $('pd-prev').addEventListener('click',() => {if(page>1){--page;load();}}); $('pd-next').addEventListener('click',() => {if(page*50<total){++page;load();}});
  document.addEventListener('visibilitychange',() => {clearTimeout(timer);if(!document.hidden)load();});
  load();
})();
