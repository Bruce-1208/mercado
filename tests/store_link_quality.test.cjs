const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync('bit/templates/index.html', 'utf8');
const context = vm.createContext({escapeHtml: s => String(s).replaceAll('<','&lt;').replaceAll('"','&quot;'), storeLinkNumber: (s,n) => Number(s).toFixed(n)});
vm.runInContext(template.slice(template.indexOf('        function storeLinkDateTime('), template.indexOf('        function renderStoreLinkRows(rows)')), context);
test('quality preserves zero and rejects missing or invalid scores', () => {
  assert.match(context.storeLinkQuality({quality_score:0}), /0.0 \/ 100/);
  assert.match(context.storeLinkQuality({quality_score:69}), /69.0 \/ 100/);
  for (const score of [null, undefined, '', true, -1, 101, 'NaN']) assert.match(context.storeLinkQuality({quality_score:score}), /未提供/);
});
test('available and enrolled activities are separate and escaped', () => {
  const row = {available_promotions:[{name:'<Candidate>',status_raw:'candidate',price:10,currency_id:'USD'}], enrolled_promotions:[{name:'Enrolled',status_raw:'started',price:20,currency_id:'MXN'}]};
  assert.match(context.storeLinkPromotionDetails(row,true), /&lt;Candidate>.*可报名/);
  assert.doesNotMatch(context.storeLinkPromotionDetails(row,true), /Enrolled/);
  assert.match(context.storeLinkPromotionDetails(row), /Enrolled.*进行中/);
  assert.match(context.storeLinkPromotionDetails({},true), /待同步/);
  assert.match(context.storeLinkPromotionDetails({available_promotions:[]},true), /暂无可报名活动/);
});
test('listing rows include all three new columns and aligned empty state', () => {
  Object.assign(context, {storeLinkBody:{innerHTML:''}, storeLinkRows:[], selectedStoreLinkIds:new Set(), updateStoreLinkSelection:()=>{}, storeLinkStatus:()=>'', storeLinkMarkers:()=>''});
  vm.runInContext(template.slice(template.indexOf('        function renderStoreLinkRows(rows)'), template.indexOf('        function updateStoreLinkMarkers(')), context);
  context.renderStoreLinkRows([{id:1,quality_score:88,enrolled_promotions:[],available_promotions:[]}]);
  assert.equal((context.storeLinkBody.innerHTML.match(/<td(?:\s|>)/g)||[]).length,19);
  assert.match(context.storeLinkBody.innerHTML,/待同步/);
  assert.match(context.storeLinkBody.innerHTML,/88.0 \/ 100/);
  assert.match(context.storeLinkBody.innerHTML,/暂无可报名活动/);
  assert.match(context.storeLinkBody.innerHTML,/暂无已参加活动记录/);
  context.renderStoreLinkRows([]);
  assert.match(context.storeLinkBody.innerHTML,/colspan="19"/);
});

 test('store link times share a stable seconds format', () => {
  for (const value of ['2026-10-07 01:02:03', '2026-10-07T01:02:03.123Z', 'Wed, 07 Oct 2026 01:02:03 GMT', '2026-10-07T04:02:03+03:00']) {
    assert.equal(context.storeLinkDateTime(value), '2026-10-07 01:02:03');
  }
  for (const value of [null, '', 'invalid', '2026-02-30 01:02:03']) assert.equal(context.storeLinkDateTime(value), '-');
  context.renderStoreLinkRows([{id:1, listed_at:'2024-03-02T12:30:45Z',last_synced_at:'2026-10-07 01:02:03'}]);
  assert.match(context.storeLinkBody.innerHTML, /<td class="store-link-time">2024-03-02 12:30:45<\/td>\s*<td class="store-link-time">2026-10-07 01:02:03<\/td>/);
});
