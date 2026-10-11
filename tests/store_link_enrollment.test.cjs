const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('bit/templates/index.html', 'utf8');
const code = html.slice(html.indexOf('        function storeLinkPromotionEnrollmentReason('), html.indexOf('        function storeLinkQuality('));

function setup() {
  const frame = {src: '', dataset: {loaded: '1'}};
  const row = {id: 42, item_id: 'MLM123', available_promotions: [
    {promotion_fk: 7, promotion_id: 'PLATFORM-7', promotion_type: 'DEAL', status_raw: 'candidate', name: '<活动>', price: 15, currency_id: 'USD'},
    {promotion_fk: 8, promotion_type: 'MARKETPLACE_CAMPAIGN', status_raw: 'candidate'},
  ], enrolled_promotions: [{name: '已参与', promotion_type: 'DEAL', status_raw: 'started'}]};
  const permissions = new Set(['promotions.view', 'promotions.execute']);
  const context = vm.createContext({
    window: {addEventListener(){}},
    URLSearchParams, storeLinkRows: [row], storeLinkPage: 3, storeLinkPendingRefreshPage: null,
    storeLinkMessage: {textContent: ''}, hasWorkbenchPermission: p => permissions.has(p),
    document: {getElementById: id => id === 'promotions-frame' ? frame : null},
    enterpriseScopedUrl: path => `${path}&organization_key=enterprise-test`,
    switchTab: tab => context.activeTab = tab,
    escapeHtml: s => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
    storeLinkDateTime: () => '-', storeLinkNumber: (value, digits) => Number(value).toFixed(digits),
  });
  vm.runInContext(code, context);
  return {context, row, frame, permissions};
}

test('each eligible activity has its own entry and enrolled records have none', () => {
  const {context, row} = setup();
  const result = context.storeLinkPromotionDetails(row, true);
  assert.match(result, /&lt;活动>/);
  assert.match(result, /onclick="openStoreLinkPromotion\(42, 7\)"/);
  assert.match(result, /onclick="openStoreLinkPromotion\(42, 8\)"/);
  assert.doesNotMatch(result, /disabled/);
  assert.doesNotMatch(context.storeLinkPromotionDetails(row), /openStoreLinkPromotion|>报名</);
});

test('entry uses local campaign identity and the current product, even when frame is already loaded', () => {
  const {context, row, frame} = setup();
  row.item_id = 'MLM123 &special';
  context.openStoreLinkPromotion(42, 7);
  const url = new URL(frame.src, 'https://console.test');
  assert.equal(url.pathname, '/promotions');
  assert.equal(url.searchParams.get('promotion_id'), '7');
  assert.equal(url.searchParams.get('item_id'), row.item_id);
  assert.equal(url.searchParams.get('embedded'), '1');
  assert.equal(url.searchParams.get('organization_key'), 'enterprise-test');
  assert.equal(context.activeTab, 'promotions');
  assert.equal(context.storeLinkPendingRefreshPage, 3);
  context.openStoreLinkPromotion(42, 8);
  assert.equal(new URL(frame.src, 'https://console.test').searchParams.get('promotion_id'), '8');
});

test('read-only types, missing metadata, stale candidates and missing permissions cannot open enrollment', () => {
  for (const change of [
    ({row}) => row.available_promotions[0].promotion_type = 'CLEARANCE',
    ({row}) => row.available_promotions[0].promotion_fk = 'PLATFORM-7',
    ({row}) => row.item_id = '',
    ({row}) => row.available_promotions[0].status_raw = 'started',
    ({permissions}) => permissions.delete('promotions.view'),
    ({permissions}) => permissions.delete('promotions.execute'),
  ]) {
    const state = setup();
    change(state);
    const output = state.context.storeLinkPromotionDetails({...state.row, available_promotions: [state.row.available_promotions[0]]}, true);
    assert.match(output, /disabled title=/);
    assert.doesNotMatch(output, /onclick=/);
    state.context.openStoreLinkPromotion(42, 7);
    assert.equal(state.frame.src, '');
    assert.ok(state.context.storeLinkMessage.textContent);
  }
});

test('an entry for an activity removed from current rows cannot redirect to another campaign', () => {
  const {context, row, frame} = setup();
  row.available_promotions = [];
  context.openStoreLinkPromotion(42, 7);
  assert.equal(frame.src, '');
  assert.match(context.storeLinkMessage.textContent, /刷新店铺链接/);
});
