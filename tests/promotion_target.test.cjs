const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('bit/templates/mercado_promotions.html', 'utf8');
const itemCode = html.slice(html.indexOf('function clearCampaignItems'), html.indexOf('async function loadJobs'));
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
    state, $: field, URLSearchParams, window: {location: {search: ''}},
    esc: value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char])),
    fmtMoney: value => value == null ? '—' : Number(value).toFixed(2),
    fmtDate: value => String(value || ''), typeLabel: value => value,
    statusPill: value => `<span>${value}</span>`,
    feedback: (message, type, timeout) => messages.push({message, type, timeout}),
    showView: name => views.push(name), previewDialog: field('previewDialog'),
    api: async (url, options = {}) => {calls.push({url, options}); return api ? api(url, options) : {promotion: campaign, rows: []};},
    loadStores: loadStores || (async () => {}), loadPromotions: loadPromotions || (async () => {}),
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
  assert.equal(s.calls[0].url, '/api/promotions/12/items?page=1&page_size=50&target_item_id=MLM123');
  await new Promise(setImmediate);
  assert.deepEqual([...s.state.selectedItems], [91]);
  assert.equal(s.state.currentCampaign.id, 12);
  stores.resolve(); list.resolve(); await boot;
  assert.equal(s.calls.length, 1);
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
    assert.equal(s.calls.length, 0); assert.equal(storeLoads, 1); assert.equal(listLoads, 1);
    assert.equal(s.messages.at(-1).type, 'bad');
  }
});

test('explicit preview sends only the selected target and edited price, without a job submission', async () => {
  const s = setup({api: async url => /\/items(?:\?|$)/.test(url) ? {promotion: campaign, rows: [item(1, 'MLM123'), item(2, 'MLM456')]} :
    {preview_id: 'new-preview', ready_count: 1, blocked_count: 0, rows: [{item_id: 'MLM123', status: 'ready', deal_price: 75}]}});
  await s.context.openCampaign(12, 'MLM123');
  s.field('price-1').value = '75';
  await s.context.previewAction('enroll');
  assert.equal(s.calls[1].url, '/api/promotions/preview');
  const body = JSON.parse(s.calls[1].options.body);
  assert.equal(body.promotion_id, 12); assert.equal(body.action, 'enroll');
  assert.deepEqual(body.rows.map(row => row.item_local_id), [1]);
  assert.equal(body.rows[0].deal_price, '75');
  assert.equal(s.field('previewDialog').open, true);
  assert.equal(s.state.preview.preview_id, 'new-preview');
  assert.equal(s.calls.some(call => call.url === '/api/promotions/jobs'), false);
});

test('a preview that arrives after opening another activity cannot restore a stale submission', async () => {
  const preview = deferred();
  const s = setup({api: url => url.endsWith('/preview') ? preview.promise : Promise.resolve({promotion: campaign, rows: [item(1, 'MLM123')]} )});
  await s.context.openCampaign(12, 'MLM123');
  const previewing = s.context.previewAction('enroll');
  await s.context.openCampaign(12);
  preview.resolve({preview_id: 'stale', ready_count: 1, blocked_count: 0, rows: []});
  await previewing;
  assert.equal(s.state.preview, null);
  assert.equal(s.field('previewDialog').open, false);
  assert.equal(s.field('confirmBtn').disabled, true);
});

test('submission results remain visible after refreshing the activity (mocked job API)', async () => {
  const s = setup({api: async url => /\/items(?:\?|$)/.test(url) ? {promotion: campaign, rows: [item(1, 'MLM123')]} :
    {results: [{status: 'succeeded'}, {status: 'unknown'}, {status: 'failed'}]}});
  await s.context.openCampaign(12, 'MLM123');
  s.state.preview = {preview_id: 'confirmed', ready_count: 1};
  await s.context.submitPreview();
  assert.equal(s.calls.at(-1).url, '/api/promotions/12/items?page=1&page_size=50');
  assert.equal(s.messages.at(-1).message, '提交完成：成功 1，待核实 1，失败 1');
  assert.equal(s.messages.at(-1).type, 'info');
  assert.equal(s.state.preview, null);
  assert.equal(s.field('confirmBtn').disabled, true);
});

test('failed submissions use an error or partial-result style instead of success styling', async () => {
  for (const [statuses, type] of [[['failed'], 'bad'], [['succeeded', 'failed'], 'info'], [['succeeded'], 'ok']]) {
    const s = setup({api: async url => /\/items(?:\?|$)/.test(url) ? {promotion: campaign, rows: [item(1, 'MLM123')]} :
      {results: statuses.map(status => ({status}))}});
    await s.context.openCampaign(12, 'MLM123');
    s.state.preview = {preview_id: 'confirmed', ready_count: 1};
    await s.context.submitPreview();
    assert.equal(s.messages.at(-1).type, type);
  }
});

test('paging invalidates an in-flight preview and retains the selected offscreen price', async () => {
  const preview = deferred();
  const s = setup({api: async url => {
    if (url.endsWith('/preview')) return preview.promise;
    const page = Number(new URLSearchParams(url.split('?')[1]).get('page'));
    return {promotion: campaign, rows: [item(page, `MLM${page}`)], page, page_size: 50, total: 100, pages: 2};
  }});
  await s.context.openCampaign(12);
  s.context.toggleItem(1, true); s.field('price-1').value = '75';
  const previewing = s.context.previewAction('enroll');
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
