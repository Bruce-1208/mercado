const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('bit/templates/mercado_promotions.html', 'utf8');
const itemCode = html.slice(html.indexOf('function stopCampaignReason'), html.indexOf('function canStopCampaign')) + html.slice(html.indexOf('function clearCampaignItems'), html.indexOf('async function loadJobs'));
const bootCode = html.slice(html.indexOf('async function initializePromotions'), html.indexOf('\ninitializePromotions();'));
const campaign = {id: 12, name: 'Test campaign', store_name: 'Store A', site_id: 'MLM', promotion_type: 'DEAL', status_raw: 'started'};
const item = (id, itemId, status = 'candidate') => ({id, item_id: itemId, status_raw: status, original_price: 100, max_price: 80, currency_id: 'MXN'});

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => {resolve = res; reject = rej;});
  return {promise, resolve, reject};
}

function setup({api, loadStores, loadPromotions} = {}) {
  const fields = {}, messages = [], calls = [], views = [], scrolls = [];
  function field(id) {
    if (!fields[id]) {
      const classes = new Set();
      fields[id] = {
        value: '', innerHTML: '', textContent: '', checked: false, indeterminate: false, disabled: false,
        classList: {toggle(name, on) {on ? classes.add(name) : classes.delete(name);}, remove(name) {classes.delete(name);}, contains(name) {return classes.has(name);}},
        close() {this.open = false;}, showModal() {this.open = true;},
        scrollIntoView(options) {scrolls.push({id, options});},
      };
    }
    return fields[id];
  }
  const state = {currentCampaign: null, items: [], selectedItems: new Set(), preview: null, campaignRequest: 0, targetItemRowId: null};
  const context = {
    state, $: field, URLSearchParams, AbortController, setTimeout, clearTimeout, window: {location: {search: ''}},
    esc: value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char])),
    fmtMoney: value => value == null ? '—' : Number(value).toFixed(2),
    fmtDate: value => String(value || ''), typeLabel: value => value,
    statusPill: value => `<span>${value}</span>`,
    feedback: (message, type, timeout) => messages.push({message, type, timeout}),
    showView: name => views.push(name), previewDialog: field('previewDialog'),
    api: async (url, options = {}) => {calls.push({url, options}); return api ? api(url, options) : {promotion: campaign, rows: []};},
    loadStores: loadStores || (async () => {}), loadPromotions: loadPromotions || (async () => {}),
    window: {addEventListener(){},parent: {postMessage(){}}, location: {origin:'http://test'}},
    document: {querySelectorAll() {
      return [...field('itemRows').innerHTML.matchAll(/<input type="checkbox" value="(\d+)" ([^>]*)>/g)]
        .map(match => ({value: match[1], checked: /\bchecked\b/.test(match[2])}));
    }},
  };
  vm.createContext(context);
  vm.runInContext(itemCode + '\n' + bootCode, context);
  return {context, state, fields, field, messages, calls, views, scrolls};
}

test('shop entry opens the requested activity independently of store/list loading and filters', async () => {
  const stores = deferred(), list = deferred();
  const s = setup({loadStores: () => stores.promise, loadPromotions: () => list.promise,
    api: async () => ({promotion: campaign, rows: [item(91, 'MLM123')]})});
  s.field('searchFilter').value = 'an unrelated activity';
  const boot = s.context.initializePromotions('?embedded=1&promotion_id=12&item_id=MLM123&organization_key=enterprise-a');
  assert.equal(s.calls.find(call => /\/items\?/.test(call.url)).url, '/api/promotions/12/items?page=1&page_size=50&target_item_id=MLM123');
  await new Promise(setImmediate);
  assert.deepEqual([...s.state.selectedItems], [91]);
  assert.equal(s.state.currentCampaign.id, 12);
  stores.resolve(); list.resolve(); await boot;
  assert.equal(s.calls.filter(call => !call.url.startsWith('/api/promotions/jobs?')).length, 1);
});

test('target selects one candidate only, renders its checked state, and focuses the row', async () => {
  const s = setup({api: async () => ({promotion: campaign, rows: [
    item(1, 'MLM123', 'started'), item(2, 'MLM456'), item(3, 'MLM123'), item(4, 'MLM123'),
  ]})});
  await s.context.openCampaign(12, 'MLM123');
  assert.deepEqual([...s.state.selectedItems], [3]);
  assert.equal(s.state.targetItemRowId, 3);
  assert.equal((s.field('itemRows').innerHTML.match(/ checked /g) || []).length, 1);
  assert.match(s.field('itemRows').innerHTML, /id="item-row-3" class="target-item"/);
  assert.match(s.field('itemRows').innerHTML, /value="3" checked/);
  assert.equal(s.scrolls[0].id, 'item-row-3');
  assert.equal(s.field('enrollBtn').disabled, false);
  assert.equal(s.field('enrollBtn').textContent, '报名此商品');
  assert.equal(s.field('selectionText').textContent, '已选择 1 件商品');
  assert.equal(s.field('selectAll').checked, false);
  assert.equal(s.field('selectAll').indeterminate, true);
  assert.equal(s.messages.at(-1).type, 'ok');
  assert.match(s.messages.at(-1).message, /核对活动价/);
  assert.equal(s.calls.length, 1); // Opening an entry never previews or submits.
});

test('missing or no-longer-candidate target never selects another available product', async () => {
  for (const targetRows of [[], [item(1, 'MLM123', 'started')], [item(1, 'MLM123', 'pending_approval')]]) {
    const s = setup({api: async () => ({promotion: campaign, rows: [...targetRows, item(2, 'MLM456')]})});
    await s.context.openCampaign(12, 'MLM123');
    assert.deepEqual([...s.state.selectedItems], []);
    assert.equal(s.field('enrollBtn').disabled, true);
    assert.equal(s.state.targetItemRowId, null);
    assert.equal(s.messages.at(-1).type, 'bad');
    assert.match(s.messages.at(-1).message, /未找到商品|已不可报名/);
  }
});

test('ended, expired, and unsupported activities leave the target unselected with a clear reason', async () => {
  for (const promotion of [...['finished', 'ended', 'cancelled', 'canceled', 'deleted'].map(status_raw => ({...campaign, status_raw})),
    {...campaign, finish_date: '2000-01-01T00:00:00Z'}, {...campaign, promotion_type: 'DOD'}]) {
    const s = setup({api: async () => ({promotion, rows: [item(1, 'MLM123')]})});
    await s.context.openCampaign(12, 'MLM123');
    assert.equal(s.state.selectedItems.size, 0);
    assert.equal(s.field('enrollBtn').disabled, true);
    assert.match(s.messages.at(-1).message, /已结束|仅支持查看/);
    if (promotion.promotion_type === 'DOD') assert.equal(s.field('enrollBtn').classList.contains('hidden'), true);
  }
});

test('opening clears old selection, prices, preview, and controls immediately; failure stays empty', async () => {
  const response = deferred(), s = setup({api: () => response.promise});
  s.state.currentCampaign = {...campaign, id: 1};
  s.state.items = [item(1, 'OLD')]; s.state.selectedItems.add(1); s.state.preview = {preview_id: 'old-preview'};
  s.field('previewDialog').open = true; s.field('selectAll').checked = true; s.field('selectAll').indeterminate = true;
  s.field('batchDiscountValue').value = '20'; s.field('discountStart').value = '2026-10-07';
  s.field('discountFinish').value = '2026-10-08';
  const opening = s.context.openCampaign(12, 'MLM123');
  assert.equal(s.state.currentCampaign, null);
  assert.equal(s.state.items.length, 0);
  assert.equal(s.state.selectedItems.size, 0);
  assert.equal(s.state.preview, null);
  assert.equal(s.field('previewDialog').open, false);
  for (const id of ['enrollBtn', 'withdrawBtn', 'applyDiscountBtn', 'confirmBtn', 'selectAll']) assert.equal(s.field(id).disabled, true);
  assert.equal(s.field('selectAll').checked, false); assert.equal(s.field('selectAll').indeterminate, false);
  for (const id of ['batchDiscountValue', 'discountStart', 'discountFinish']) assert.equal(s.field(id).value, '');
  assert.match(s.field('itemRows').innerHTML, /正在读取/);
  assert.equal(s.messages[0].timeout, 0);
  response.reject(new Error('当前账号不能查看该店铺'));
  await opening;
  assert.equal(s.state.currentCampaign, null); assert.equal(s.state.preview, null);
  assert.equal(s.field('enrollBtn').disabled, true);
  assert.match(s.messages.at(-1).message, /当前账号不能查看该店铺/);
  assert.equal(s.messages.at(-1).timeout, 0);
});

test('late activity response cannot override the most recently opened target', async () => {
  const first = deferred(), second = deferred();
  const s = setup({api: url => url.includes('/12/') ? first.promise : second.promise});
  const openingFirst = s.context.openCampaign(12, 'MLM123');
  const openingSecond = s.context.openCampaign(13, 'MLM456');
  second.resolve({promotion: {...campaign, id: 13}, rows: [item(2, 'MLM456')]});
  await openingSecond;
  first.resolve({promotion: campaign, rows: [item(1, 'MLM123')]});
  await openingFirst;
  assert.equal(s.state.currentCampaign.id, 13);
  assert.deepEqual([...s.state.selectedItems], [2]);
  assert.equal(s.scrolls.length, 1);
});

test('ordinary activity browsing stays unselected and multi-product selection still works', async () => {
  const s = setup({api: async () => ({promotion: campaign, rows: [item(1, 'MLM123'), item(2, 'MLM456')]})});
  await s.context.openCampaign(12);
  assert.equal(s.state.selectedItems.size, 0);
  s.context.toggleItem(1, true); s.context.toggleItem(2, true);
  assert.deepEqual([...s.state.selectedItems], [1, 2]);
  assert.equal(s.field('enrollBtn').textContent, '批量报名');
  assert.equal(s.field('selectAll').checked, true);
  s.context.selectAllItems(false); assert.equal(s.state.selectedItems.size, 0);
  s.context.selectAllItems(true); assert.deepEqual([...s.state.selectedItems], [1, 2]);
  assert.equal(s.field('applyDiscountBtn').disabled, false);
});

test('invalid target parameters show feedback without requesting an unintended activity', async () => {
  for (const search of ['?item_id=MLM123', '?promotion_id=0&item_id=MLM123', '?promotion_id=12/no&item_id=MLM123',
    '?promotion_id=9007199254740993&item_id=MLM123', '?promotion_id=12&item_id=%20']) {
    let storeLoads = 0, listLoads = 0;
    const s = setup({loadStores: async () => storeLoads++, loadPromotions: async () => listLoads++});
    await s.context.initializePromotions(search);
    assert.equal(s.calls.filter(call => !call.url.startsWith('/api/promotions/jobs?')).length, 0); assert.equal(storeLoads, 1); assert.equal(listLoads, 1);
    assert.equal(s.messages.at(-1).type, 'bad');
  }
});

test('enrollment posts selected rows directly without opening the confirmation dialog', async () => {
  const watched = [];
  const s = setup({api: async url => /\/items(?:\?|$)/.test(url) ? {promotion: campaign, rows: [item(1, 'MLM123'), item(2, 'MLM456')]} :
    {status: 'running', job_id: 'job-1', submitted_count: 1, excluded_count: 0, excluded_reasons: []}});
  s.context.watchPromotionJob = (...args) => watched.push(args);
  await s.context.openCampaign(12, 'MLM123');
  s.field('price-1').value = '75';
  await s.context.startEnrollment();
  const request = s.calls.find(call => call.url === '/api/promotions/enroll');
  assert.ok(request);
  assert.equal(request.options.method, 'POST');
  const body = JSON.parse(request.options.body);
  assert.deepEqual(Object.keys(body).sort(), ['promotion_id', 'rows']);
  assert.equal(body.promotion_id, 12);
  assert.deepEqual(body.rows.map(row => row.item_local_id), [1]);
  assert.equal(body.rows[0].deal_price, '75');
  assert.equal(Boolean(s.field('previewDialog').open), false);
  assert.equal(s.state.preview, null);
  assert.deepEqual(watched, [['job-1', 12]]);
  assert.equal(s.calls.some(call => call.url === '/api/promotions/preview' || call.url === '/api/promotions/jobs'), false);
});

test('enrollment becomes busy synchronously and ignores a duplicate click', async () => {
  const enrollment = deferred(), watched = [];
  const s = setup({api: url => /\/items(?:\?|$)/.test(url) ? Promise.resolve({promotion: campaign, rows: [item(1, 'MLM123')]}) : enrollment.promise});
  s.context.watchPromotionJob = (...args) => watched.push(args);
  await s.context.openCampaign(12, 'MLM123');
  const first = s.context.startEnrollment();
  assert.equal(s.field('enrollBtn').disabled, true);
  assert.equal(s.field('withdrawBtn').disabled, true);
  assert.equal(s.field('applyDiscountBtn').disabled, true);
  assert.equal(s.field('selectAll').disabled, true);
  const duplicate = s.context.startEnrollment();
  assert.equal(s.calls.filter(call => call.url === '/api/promotions/enroll').length, 1);
  enrollment.resolve({status: 'running', job_id: 'job-1', submitted_count: 1, excluded_count: 0, excluded_reasons: []});
  await Promise.all([first, duplicate]);
  assert.equal(s.state.pendingJob.id, 'job-1');
  assert.equal(s.state.pendingJob.campaignId, 12);
  assert.deepEqual(watched, [['job-1', 12]]);
  assert.equal(s.field('enrollBtn').disabled, true);
});

test('enrollment reports products automatically excluded by server validation', async () => {
  const rows = [item(1, 'MLM1'), item(2, 'MLM2'), item(3, 'MLM3')], watched = [];
  const s = setup({api: async url => /\/items(?:\?|$)/.test(url) ? {promotion: campaign, rows} :
    {status: 'running', job_id: 'job-1', submitted_count: 1, excluded_count: 2, excluded_reasons: [{reason: '价格不符合', count: 2}]}});
  s.context.watchPromotionJob = (...args) => watched.push(args);
  await s.context.openCampaign(12);
  rows.forEach(row => s.context.toggleItem(row.id, true));
  await s.context.startEnrollment();
  const request = s.calls.find(call => call.url === '/api/promotions/enroll');
  assert.deepEqual(JSON.parse(request.options.body).rows.map(row => row.item_local_id), [1, 2, 3]);
  assert.equal(s.state.pendingJob.id, 'job-1');
  assert.equal(s.state.pendingJob.campaignId, 12);
  assert.deepEqual(watched, [['job-1', 12]]);
  assert.match(s.messages.at(-1).message, /自动屏蔽.*2/);
  assert.match(s.messages.at(-1).message, /价格不符合.*2/);
});

test('an all-excluded enrollment records a completed log without starting a watcher', async () => {
  const watched = [];
  const s = setup({api: async url => /\/items(?:\?|$)/.test(url) ? {promotion: campaign, rows: [item(1, 'MLM1'), item(2, 'MLM2')]} :
    {status: 'skipped', job_id: 'blocked-log', submitted_count: 0, excluded_count: 2, excluded_reasons: [{reason: '价格不符合', count: 2}]}});
  s.context.watchPromotionJob = (...args) => watched.push(args);
  await s.context.openCampaign(12);
  s.context.selectAllItems(true);
  await s.context.startEnrollment();
  assert.equal(s.calls.filter(call => call.url === '/api/promotions/enroll').length, 1);
  assert.equal(s.state.pendingJob ?? null, null);
  assert.deepEqual(watched, []);
  assert.equal(Boolean(s.field('previewDialog').open), false);
    assert.match(s.messages.at(-1).message, /报名日志已记录/);
  assert.match(s.messages.at(-1).message, /没有.*报名|全部.*排除/);
  assert.match(s.messages.at(-1).message, /价格不符合.*2/);
});

test('withdrawal still previews selected rows and submits the confirmed preview as a job', async () => {
  const s = setup({api: async url => {
    if (/\/items(?:\?|$)/.test(url)) return {promotion: campaign, rows: [item(1, 'MLM123')]};
    if (url === '/api/promotions/preview') return {preview_id: 'withdraw-preview', action: 'withdraw', ready_count: 1, blocked_count: 0, rows: [{item_id: 'MLM123', status: 'ready'}]};
    return {results: [{item_id: 'MLM123', status: 'succeeded'}]};
  }});
  await s.context.openCampaign(12, 'MLM123');
  await s.context.previewAction('withdraw');
  assert.equal(s.calls[1].url, '/api/promotions/preview');
  const previewBody = JSON.parse(s.calls[1].options.body);
  assert.equal(previewBody.promotion_id, 12);
  assert.equal(previewBody.action, 'withdraw');
  assert.deepEqual(previewBody.rows.map(row => row.item_local_id), [1]);
  assert.equal(s.field('previewDialog').open, true);
  await s.context.submitPreview();
  const jobRequest = s.calls.find(call => call.url === '/api/promotions/jobs');
  assert.ok(jobRequest);
  assert.deepEqual(JSON.parse(jobRequest.options.body), {preview_id: 'withdraw-preview', confirmed: true});
});

test('a withdrawal preview that arrives after opening another activity cannot restore a stale submission', async () => {
  const preview = deferred();
  const s = setup({api: url => url.endsWith('/preview') ? preview.promise : Promise.resolve({promotion: campaign, rows: [item(1, 'MLM123')]} )});
  await s.context.openCampaign(12, 'MLM123');
  const previewing = s.context.previewAction('withdraw');
  await s.context.openCampaign(12);
  preview.resolve({preview_id: 'stale', ready_count: 1, blocked_count: 0, rows: []});
  await previewing;
  assert.equal(s.state.preview, null);
  assert.equal(s.field('previewDialog').open, false);
  assert.equal(s.field('confirmBtn').disabled, true);
});

test('withdrawal submission results remain visible after refreshing the activity (mocked job API)', async () => {
  const s = setup({api: async url => /\/items(?:\?|$)/.test(url) ? {promotion: campaign, rows: [item(1, 'MLM123')]} :
    {results: [{status: 'succeeded'}, {status: 'unknown'}, {status: 'failed'}]}});
  await s.context.openCampaign(12, 'MLM123');
  s.state.preview = {preview_id: 'confirmed', action: 'withdraw', ready_count: 1};
  await s.context.submitPreview();
  assert.equal(s.calls.at(-1).url, '/api/promotions/12/items?page=1&page_size=50');
  assert.equal(s.messages.at(-1).message, '提交完成：成功 1，待核实 1，失败 1');
  assert.equal(s.messages.at(-1).type, 'info');
  assert.equal(s.state.preview, null);
  assert.equal(s.field('confirmBtn').disabled, true);
});

test('failed withdrawal submissions use an error or partial-result style instead of success styling', async () => {
  for (const [statuses, type] of [[['failed'], 'bad'], [['succeeded', 'failed'], 'info'], [['succeeded'], 'ok']]) {
    const s = setup({api: async url => /\/items(?:\?|$)/.test(url) ? {promotion: campaign, rows: [item(1, 'MLM123')]} :
      {results: statuses.map(status => ({status}))}});
    await s.context.openCampaign(12, 'MLM123');
    s.state.preview = {preview_id: 'confirmed', action: 'withdraw', ready_count: 1};
    await s.context.submitPreview();
    assert.equal(s.messages.at(-1).type, type);
  }
});

test('paging invalidates an in-flight withdrawal preview and retains the selected offscreen price', async () => {
  const preview = deferred();
  const s = setup({api: async url => {
    if (url.endsWith('/preview')) return preview.promise;
    const page = Number(new URLSearchParams(url.split('?')[1]).get('page'));
    return {promotion: campaign, rows: [item(page, `MLM${page}`)], page, page_size: 50, total: 100, pages: 2};
  }});
  await s.context.openCampaign(12);
  s.context.toggleItem(1, true); s.field('price-1').value = '75';
  const previewing = s.context.previewAction('withdraw');
  await s.context.loadCampaignPage(2);
  preview.resolve({preview_id: 'stale-page-preview', ready_count: 1, blocked_count: 0, rows: []});
  await previewing;
  assert.equal(s.state.preview, null);
  assert.equal(s.field('previewDialog').open, false);
  assert.equal(s.state.itemPage, 2);
  assert.equal(s.context.previewRequestRows()[0].deal_price, '75');
  assert.equal(s.field('selectAll').checked, false);
  assert.equal(s.field('selectAll').indeterminate, false);
});

test('platform-priced selections keep null deal prices on every page', async () => {
  const s = setup({api: async url => {
    const page = Number(new URLSearchParams(url.split('?')[1]).get('page'));
    return {promotion: {...campaign, promotion_type: 'MARKETPLACE_CAMPAIGN'}, rows: [item(page, `MLM${page}`)],
      page, page_size: 50, total: 100, pages: 2};
  }});
  await s.context.openCampaign(12); s.context.toggleItem(1, true);
  await s.context.loadCampaignPage(2); s.context.toggleItem(2, true);
  assert.deepEqual(Array.from(s.context.previewRequestRows(), row => row.deal_price), [null, null]);
  s.context.selectAllItems(false);
  assert.deepEqual([...s.state.selectedItems], [1]);
});

test('select all pages includes offscreen candidates and preserves their edited prices in preview', async () => {
  const s = setup({api: async url => {
    const page = Number(new URL(url, 'http://test').searchParams.get('page'));
    return {promotion: campaign, rows: page === 1 ? [item(1, 'MLM1'), item(2, 'MLM2', 'started')] : [item(3, 'MLM3')], total: 3, pages: 2, page, page_size: 100};
  }});
  await s.context.openCampaign(12);
  s.field('price-1').value = '75';
  await s.context.selectAllCampaignItems();
  assert.deepEqual([...s.state.selectedItems], [1, 3]);
  assert.equal(s.state.itemPage, 1);
  s.field('batchDiscountValue').value = '25';
  s.context.applyBatchDiscount();
  const rows = s.context.previewRequestRows();
  assert.deepEqual(Array.from(rows, row => row.item_local_id), [1, 3]);
  assert.equal(rows[1].deal_price, '75.00');
  assert.equal(s.state.allPagesLoading, false);
});

test('all pages read failure keeps existing selection without partially selecting pages', async () => {
  const s = setup({api: async url => {
    if (url.includes('page=2')) throw new Error('offline');
    return {promotion: campaign, rows: [item(1, 'MLM1')], total: 2, pages: 2, page: 1};
  }});
  await s.context.openCampaign(12);
  await s.context.selectAllCampaignItems();
  assert.equal(s.state.selectedItems.size, 0);
  assert.match(s.messages.at(-1).message, /全选所有页未完成/);
  assert.equal(s.state.allPagesLoading, false);
});

test('late all-pages response cannot select products in another campaign', async () => {
  const pending = deferred();
  const s = setup();
  await s.context.openCampaign(12);
  s.context.api = () => pending.promise;
  const selection = s.context.selectAllCampaignItems();
  s.state.campaignRequest++;
  s.context.clearCampaignItems();
  pending.resolve({rows: [item(1, 'MLM1')], total: 1, pages: 1});
  await selection;
  assert.equal(s.state.selectedItems.size, 0);
  assert.equal(s.state.allPagesLoading, false);
});

test('product and item filters reset selection, persist across pages, and reset for another campaign', async () => {
  const s = setup({api: async () => ({promotion: campaign, rows: [item(1, 'MLM123')], total: 80, pages: 2, page: 1})});
  await s.context.openCampaign(12);
  s.state.selectedItems.add(1);
  s.field('itemProductFilter').value = ' 蓝色 Cup ';
  s.field('itemIdFilter').value = ' mlm12 ';
  s.field('itemEnrollmentFilter').value = '0';
  await s.context.applyItemFilters();
  const query = new URL(s.calls.at(-1).url, 'http://test').searchParams;
  assert.equal(query.get('product'), '蓝色 Cup');
  assert.equal(query.get('item_id'), 'mlm12');
  assert.equal(query.get('enrolled'), '0');
  assert.equal(query.get('page'), '1');
  assert.equal(s.state.selectedItems.size, 0);
  await s.context.loadCampaignPage(2);
  assert.equal(new URL(s.calls.at(-1).url, 'http://test').searchParams.get('product'), '蓝色 Cup');
  await s.context.applyItemFilters(true);
  assert.equal(s.calls.at(-1).url, '/api/promotions/12/items?page=1&page_size=50');
  s.state.itemProductFilter = 'old';
  s.state.itemIdFilter = 'old';
  await s.context.openCampaign(13);
  assert.equal(s.calls.at(-1).url, '/api/promotions/13/items?page=1&page_size=50');
});

test('batch discount excludes products below official minimum from enrollment, including offscreen products', async () => {
  const rows = [
    {...item(1, 'MLM1'), max_price: 80},
    {...item(2, 'MLM2'), max_price: 75},
    {...item(3, 'MLM3'), max_price: 70},
  ];
  const s = setup({api: async () => ({promotion: campaign, rows: rows.slice(0, 2)})});
  await s.context.openCampaign(12);
  s.state.selectedItems = new Set([1, 2, 3]);
  s.state.selectedRows = new Map(rows.map(row => [row.id, row]));
  s.field('price-1').value = '80';
  s.field('price-2').value = '75';
  s.field('batchDiscountValue').value = '20';
  s.context.applyBatchDiscount();
  assert.deepEqual([...s.state.selectedItems], [1]);
  assert.deepEqual([...s.state.selectedRows.keys()], [1]);
  assert.equal(s.field('selectAll').checked, false);
  assert.match(s.messages.at(-1).message, /2 件低于官方最低折扣要求/);
  await s.context.startEnrollment();
  const sent = JSON.parse(s.calls.at(-1).options.body);
  assert.deepEqual(sent.rows.map(row => row.item_local_id), [1]);
  assert.equal(sent.rows[0].deal_price, '80.00');
});

test('all products below minimum leave no selection and never request direct enrollment', async () => {
  const s = setup({api: async () => ({promotion: campaign, rows: [{...item(1, 'MLM1'), max_price: 75}]})});
  await s.context.openCampaign(12);
  s.context.toggleItem(1, true);
  s.field('batchDiscountValue').value = '20';
  s.context.applyBatchDiscount();
  assert.equal(s.state.selectedItems.size, 0);
  assert.equal(s.field('enrollBtn').disabled, true);
  assert.match(s.messages.at(-1).message, /不会提交报名/);
  const count = s.calls.length;
  await s.context.startEnrollment();
  assert.equal(s.calls.length, count);
});

test('running enrollment refreshes store links as each new success arrives', async () => {
  const jobs = [
    {status: 'running', success_count: 1},
    {status: 'running', success_count: 1},
    {status: 'running', success_count: 2},
    {status: 'succeeded', success_count: 2},
  ];
  const timers = [], notifications = [];
  const s = setup({api: async () => ({rows: [{job_id: 'batch', ...jobs.shift()}]})});
  s.context.window.setTimeout = fn => timers.push(fn);
  s.context.window.parent.postMessage = message => notifications.push(message.type);
  s.context.watchPromotionJob('batch', 12);
  await timers.shift()();
  assert.deepEqual(notifications, ['promotions-changed']);
  await timers.shift()();
  assert.equal(notifications.length, 1);
  await timers.shift()();
  assert.equal(notifications.length, 2);
  await timers.shift()();
  assert.equal(notifications.length, 3);
  assert.equal(timers.length, 0);
});

test('terminal enrollment state survives a failed campaign refresh', async () => {
  const timers = [];
  const s = setup({api: async () => ({rows: [{
    job_id: 'batch', action: 'enroll', status: 'succeeded', success_count: 1,
  }]})});
  s.state.currentCampaign = campaign;
  s.state.currentJob = {job_id: 'batch', action: 'enroll', status: 'running'};
  s.state.pendingJob = {id: 'batch', campaignId: 12};
  s.context.window.setTimeout = fn => timers.push(fn);
  s.context.refreshCampaign = async () => {
    assert.equal(s.state.currentJob.status, 'succeeded');
  };

  s.context.watchPromotionJob('batch', 12);
  await timers.shift()();

  assert.equal(s.state.pendingJob, null);
  assert.equal(s.state.currentJob.status, 'succeeded');
  assert.equal(timers.length, 0);
});

test('withdrawn products are excluded from bulk selection but remain manually selectable for retry', async () => {
  const withdrawn={...item(1,'MLM1','withdrawn'),original_price:100,max_price:80};
  const s=setup({api:async()=>({promotion:campaign,rows:[withdrawn],page:1,pages:1,total:1,page_size:100})});
  await s.context.openCampaign(12);
  await s.context.selectAllCampaignItems();
  assert.deepEqual([...s.state.selectedItems],[]);
  s.context.toggleItem(1,true);
  assert.deepEqual([...s.state.selectedItems],[1]);
  assert.equal(s.field('enrollBtn').disabled,false);
});

test('full selection reads six pages concurrently and slow page does not block remaining pages', async()=>{
  const s=setup();await s.context.openCampaign(12);
  const held=deferred(),seen=[];let active=0,peak=0;
  s.context.api=async url=>{
    const q=new URL(url,'http://test').searchParams,p=Number(q.get('page'));
    assert.equal(q.get('selection'),'1');assert.equal(q.get('page_size'),'500');
    seen.push(p);active++;peak=Math.max(peak,active);
    if(p===2)await held.promise;
    else await Promise.resolve();
    active--;return {total:9,pages:9,page:p,rows:[item(p,'MLM'+p)]};
  };
  const work=s.context.selectAllCampaignItems();
  for(let i=0;i<30;i++)await Promise.resolve();
  assert.ok(seen.includes(9));assert.ok(peak>1&&peak<=6);
  assert.equal(s.state.selectedItems.size,0);
  held.resolve();await work;
  assert.deepEqual([...s.state.selectedItems],[1,2,3,4,5,6,7,8,9]);
});

test('cancel full selection exits immediately even when a page never responds',async()=>{
  const s=setup({api:async()=>({promotion:campaign,rows:[item(1,'MLM1')],page:1,pages:1,total:1})});
  await s.context.openCampaign(12);s.context.toggleItem(1,true);
  s.context.api=()=>new Promise(()=>{});
  const work=s.context.selectAllCampaignItems();
  assert.equal(s.field('selectAllPagesBtn').disabled,false);
  await s.context.selectAllCampaignItems();await work;
  assert.equal(s.state.allPagesLoading,false);
  assert.equal(s.state.selectedItems.size,1);
  assert.match(s.messages.at(-1).message,/已取消读取/);
});

test('page timeout releases all selection controls and preserves prior choices',async()=>{
  const s=setup();await s.context.openCampaign(12);
  let expire;
  s.context.setTimeout=fn=>{expire=fn;return 1;};s.context.clearTimeout=()=>{};
  s.context.api=()=>new Promise(()=>{});
  const work=s.context.selectAllCampaignItems();expire();await work;
  assert.equal(s.state.allPagesLoading,false);
  assert.match(s.messages.at(-1).message,/超过 20 秒/);
});
