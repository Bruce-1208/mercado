const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('bit/templates/mercado_promotions.html', 'utf8');
const code = html.slice(html.indexOf('function renderCampaignEnrollment'), html.indexOf('function applyBatchDiscount'));

function setup() {
  const fields = {};
  const context = {
    state: {items: [], preview: null, currentJob: null},
    $: id => fields[id],
    esc: value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char])),
    fmtDate: value => String(value || ''),
    fmtMoney: (value, currency = '') => value == null || value === '' || !Number.isFinite(Number(value)) ? '—' : `${currency} ${Number(value).toFixed(2)}`.trim(),
  };
  vm.createContext(context);
  vm.runInContext(code, context);
  return {context, fields};
}

test('activity enrollment summary separates latest successes, failures and unresolved results', () => {
  const {context: c} = setup();
  const result = c.renderCampaignEnrollment({total: 7, succeeded: 3, failed: 2, unknown: 2});
  assert.match(result, /成功 3/);
  assert.match(result, /失败 2/);
  assert.match(result, /待核实 2/);
  assert.match(result, /每件商品最近一次报名结果/);
  assert.match(c.renderCampaignEnrollment({total: 0}), /暂无报名记录/);
});

test('a saved failed enrollment stays visible despite an official participating state and escapes the reason', () => {
  const {context: c} = setup();
  const result = c.renderEnrollmentResult({status_raw: 'started', enrollment_result: {
    status: 'failed', message: '价格无效 <img src=x onerror="alert(1)"> & "原因"', created_at: '2026-10-07T02:00:00Z',
  }});
  assert.match(result, /报名失败/);
  assert.match(result, /价格无效 &lt;img/);
  assert.match(result, /&amp; &quot;原因&quot;/);
  assert.match(result, /2026-10-07/);
  assert.doesNotMatch(result, /<img|已参与（官方）/);
});

test('official participation is distinguished from local enrollment and unresolved submissions remain unresolved', () => {
  const {context: c} = setup();
  assert.match(c.renderEnrollmentResult({status_raw: 'started'}), /已参与（官方）/);
  assert.match(c.renderEnrollmentResult({status_raw: 'candidate'}), /未报名/);
  assert.match(c.renderEnrollmentResult({status_raw: 'scheduled'}), /待生效（官方）/);
  assert.match(c.renderEnrollmentResult({status_raw: 'pending_approval'}), /待批准（官方）/);
  const unresolved = c.renderEnrollmentResult({status_raw: 'candidate', enrollment_result: {status: 'unknown'}});
  assert.match(unresolved, /待核实/);
  assert.match(unresolved, /平台结果尚未确认/);
  assert.doesNotMatch(unresolved, /报名成功|报名失败/);
});

test('persistent submission panel includes each item, counts, action and safely escaped reasons', () => {
  const {context: c, fields} = setup();
  const classes = new Set(['hidden']);
  fields.enrollmentResults = {innerHTML: '', classList: {toggle(name, on) {on ? classes.add(name) : classes.delete(name);}}};
  c.state.currentJob = {job_id: 'test-batch-0001', action: 'enroll', results: [
    {item_id: 'MLM123', status: 'succeeded'},
    {item_id: 'MLM456', status: 'failed', message: '<script>failure</script>'},
    {item_id: 'MLM789', status: 'unknown'},
  ]};
  c.renderOperationResults();
  const result = fields.enrollmentResults.innerHTML;
  assert.equal(classes.has('hidden'), false);
  for (const text of ['最近报名结果', '成功 1', '失败 1', '待核实 1', 'MLM123', 'MLM456', 'MLM789']) assert.ok(result.includes(text), text);
  assert.match(result, /&lt;script&gt;failure&lt;\/script&gt;/);
  assert.doesNotMatch(result, /<script>/);
  c.state.currentJob.action = 'withdraw';
  c.renderOperationResults();
  assert.match(fields.enrollmentResults.innerHTML, /最近退出结果|退出成功/);
  c.state.currentJob = null;
  c.renderOperationResults();
  assert.equal(fields.enrollmentResults.innerHTML, '');
  assert.equal(classes.has('hidden'), true);
});

test('net proceeds show amount, its own currency, quote price currency and source without converting currencies', () => {
  const {context: c} = setup();
  const result = c.renderNetProceeds({amount: 32.5, currency_id: 'USD', source: 'official', applies_to_price: 450, price_currency_id: 'MXN'}, 'MXN', '450.00');
  assert.match(result, /USD 32\.50/);
  assert.match(result, /对应售价 MXN 450\.00/);
  assert.match(result, /平台数据/);
  assert.doesNotMatch(result, /MXN 32\.50/);
  assert.match(c.renderNetProceeds({amount: 0, currency_id: 'MXN'}), /MXN 0\.00/);
  for (const amount of [undefined, null, '', 'NaN', 'Infinity']) assert.match(c.renderNetProceeds({amount}), /待获取/);
});

test('changed activity prices hide a stale after-promotion net quote and invalidate confirmation', () => {
  const {context: c, fields} = setup();
  const net = {amount: 280.5, currency_id: 'MXN', source: 'promotion', applies_to_price: 450};
  c.state.items = [{id: 7, currency_id: 'MXN', net_proceeds_after: net}];
  c.state.preview = {preview_id: 'old-price-preview'};
  fields['price-7'] = {value: '400'};
  fields['net-after-7'] = {innerHTML: 'MXN 280.50'};
  fields.confirmBtn = {disabled: false};
  c.updateNetForPrice(7);
  assert.match(fields['net-after-7'].innerHTML, /报价已变更，待重新核价/);
  assert.doesNotMatch(fields['net-after-7'].innerHTML, /280\.50/);
  assert.equal(c.state.preview, null);
  assert.equal(fields.confirmBtn.disabled, true);
  fields['price-7'].value = '450.00';
  c.updateNetForPrice(7);
  assert.match(fields['net-after-7'].innerHTML, /MXN 280\.50/);
  assert.match(c.renderNetProceeds(net, 'MXN', ''), /报价已变更/);
});

test('zero-price candidates use the official suggested discount price before falling back to the maximum', () => {
  const {context: c} = setup();
  assert.equal(c.defaultDealPrice({price: 0, raw: {suggested_discounted_price: 420}, max_price: 450}), 420);
  assert.equal(c.defaultDealPrice({price: 0, max_price: 450}), 450);
  assert.equal(c.defaultDealPrice({price: 400, raw: {suggested_discounted_price: 420}, max_price: 450}), 400);
  assert.equal(c.defaultDealPrice({price: null, raw: {suggested_discounted_price: 0}, max_price: 450}), 450);
  assert.equal(c.defaultDealPrice({price: 0, max_price: null}), '');
});

test('matching official net options are used for edited prices only in the same sale currency', () => {
  const {context: c} = setup();
  const baseline = {amount: 300, applies_to_price: 450, currency_id: 'MXN'};
  const matching = {amount: 28, applies_to_price: 400, currency_id: 'USD', price_currency_id: 'MXN'};
  const wrongCurrency = {amount: 99, applies_to_price: 400, currency_id: 'USD', price_currency_id: 'BRL'};
  const row = {currency_id: 'MXN', net_proceeds_after: baseline, net_proceeds_options: [wrongCurrency, matching]};
  assert.equal(c.netForPrice(row, '400.00'), matching);
  assert.equal(c.netForPrice(row, '410.00'), baseline);
  assert.equal(c.netForPrice(row, ''), baseline);
  assert.equal(c.netForPrice({...row, net_proceeds_options: [wrongCurrency]}, 400), baseline);
  assert.match(c.renderNetProceeds(c.netForPrice(row, 400), 'MXN', 400), /USD 28\.00/);
  assert.match(c.renderNetProceeds(c.netForPrice(row, 410), 'MXN', 410), /报价已变更/);
});
