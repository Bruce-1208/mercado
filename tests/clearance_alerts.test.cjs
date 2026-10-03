const {test} = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../bit/static/clearance-alerts.js');

const today = '2026-09-30';
const blank = () => ({...ui.factValues({}), inbound_history_confirmed:'false'});
test('unknown evidence stays unknown while confirmed zero remains zero', () => {
  assert.equal(ui.factValues({}).restock_after_stockout, '');
  assert.equal(ui.factValues({restock_after_stockout:false}).restock_after_stockout, 'false');
  const body = ui.validatedFacts(blank(), 'MLM123:variation:41', today);
  assert.equal(body.restock_after_stockout, null);
  assert.equal(body.visits_30d, null);
  assert.equal(body.first_inbound_at, null);
  assert.equal(body.inbound_history_confirmed, false);
  const zero = ui.validatedFacts({...blank(), visits_30d:'0', visits_as_of:today}, 'SKU-0', today);
  assert.equal(zero.visits_30d, 0);
  assert.equal(ui.numberLabel(0), '0');
  assert.equal(ui.numberLabel(null), '待同步');
  assert.equal(ui.numberLabel(undefined), '待同步');
});
test('evidence requires deliberate warehouse confirmation and current SKU visits', () => {
  assert.throws(() => ui.validatedFacts(ui.factValues({}), 'sku', today), /核实情况/);
  assert.throws(() => ui.validatedFacts({...blank(), restock_after_stockout:'true'}, 'sku', today), /实际入仓日期/);
  assert.throws(() => ui.validatedFacts({...blank(), visits_30d:'0'}, 'sku', today), /统计截至日期/);
  assert.throws(() => ui.validatedFacts({...blank(), visits_30d:'100', visits_as_of:'2026-09-29'}, 'sku', today), /过期数据/);
  assert.throws(() => ui.validatedFacts({...blank(), visits_30d:'-1', visits_as_of:today}, 'sku', today), /整数/);
  assert.throws(() => ui.validatedFacts({...blank(), visits_30d:'0.1', visits_as_of:today}, 'sku', today), /整数/);
  assert.throws(() => ui.validatedFacts({...blank(), visits_as_of:today}, 'sku', today), /一并清空/);
  assert.throws(() => ui.validatedFacts({...blank(), first_inbound_at:'2026-02-30'}, 'sku', today), /格式/);
  assert.throws(() => ui.validatedFacts({...blank(), first_inbound_at:'2026-10-01'}, 'sku', today), /不能晚于今天/);
  assert.equal(ui.validatedFacts({...blank(), promotion_start_at:'2026-10-03'}, 'sku', today).promotion_start_at, '2026-10-03');
  assert.throws(() => ui.validatedFacts({...blank(), first_inbound_at:'2026-09-10', last_restock_at:'2026-09-09'}, 'sku', today), /不能早于首次/);
});
test('date boundaries use Beijing time and listing visits remain labeled as reference', () => {
  assert.equal(ui.chinaToday(new Date('2026-09-29T16:00:00Z')), today);
  assert.equal(ui.dateInput('2026-09-09 18:30:00'), '2026-09-09');
  assert.equal(ui.dateInput('2026-09-29T16:00:00Z'), '2026-09-30');
  assert.equal(ui.dateLabel('2026-09-29T16:00:00Z'), '2026-09-30');
  assert.match(ui.referenceVisits({visits_api_30d:0, visits_api_as_of:'2026-09-28'}), /链接级 API 参考：0 次/);
  assert.match(ui.referenceVisits({visits_api_30d:45}), /不能直接证明该 SKU 低曝光/);
  assert.equal(ui.referenceVisits({}), '');
  assert.equal(ui.missingLabel('visits_as_of'), '访问量统计截至日期');
});

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.dataset = {}; this.value = ''; this.disabled = false; this.hidden = false; this._text = ''; }
  set innerHTML(_) { throw new Error('Unsafe innerHTML used'); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  appendChild(child) { if (child.tagName === '#fragment') this.children.push(...child.children); else this.children.push(child); return child; }
  replaceChildren(...children) { this.children = []; this._text = ''; children.forEach(child => this.appendChild(child)); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelectorAll() { return this.metrics || []; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  scrollIntoView() {}
}
function harness(fetcher, canManage = true) {
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const panel = get('clearance-alerts'); panel.dataset.canManage = String(canManage);
  panel.metrics = ['risk','pending_data','exempt','normal'].map(status => { const metric = new Element('button'); metric.dataset.status = status; return metric; });
  const fields = new Map();
  get('ca-facts-form').elements = {namedItem(name) { if (!fields.has(name)) fields.set(name, new Element('input')); return fields.get(name); }};
  const document = {hidden:false, listeners:{}, getElementById:get, createElement:tag => new Element(tag), createDocumentFragment:() => new Element('#fragment'), addEventListener(name, callback) { this.listeners[name] = callback; }};
  const timers = new Map(); let timerId = 0;
  const window = {listeners:{}, fetch:fetcher, setTimeout(callback, delay) { const id = ++timerId; timers.set(id, {callback,delay}); return id; }, clearTimeout(id) { timers.delete(id); }, addEventListener(name, callback) { this.listeners[name] = callback; }};
  const app = ui.boot(document, window);
  return {get, fields, document, window, timers, app};
}
const tick = () => new Promise(setImmediate);
const response = data => ({ok:true, status:200, json:async () => ({status:'success', data})});
const sample = overrides => ({rows:[{link_id:17, token_id:2, site_id:'MLM', item_id:'MLM123', variation_id:41, sku_key:'MLM123:41', seller_sku:'SKU41', title:'规格商品', store_name:'半托管 A', available_quantity:3, sales_7d:0, sales_14d:2, visits_30d:null, visits_daily_avg:null, alert:{status:'pending_data', summary:'销量触发，豁免资料不完整', trigger_reasons:['过去 7 天销量为 0'], missing_fields:['visits_30d']}, facts:{}, workflow:{summary:'待录入平台通知日期'}}], total:1, page:1, page_size:25, counts:{risk:0,pending_data:1,exempt:0,normal:0}, stores:[{token_id:2,store_name:'半托管 A'}], checked_at:'2026-09-30T08:00:00+08:00', checking:false, ...overrides});

test('renders per-variation evidence with DOM text, without treating missing metrics as zero', async () => {
  const data = sample(); data.rows[0].title = '<img src=x onerror=alert(1)>';
  const h = harness(async () => response(data)); await tick();
  const text = h.get('ca-rows').textContent;
  assert.match(text, /<img src=x onerror=alert\(1\)>/);
  assert.match(text, /规格 41/);
  assert.match(text, /7 天：0 件/);
  assert.match(text, /总量：待同步/);
  assert.match(text, /待补：该 SKU 过去 30 天访问总量/);
  assert.match(text, /补货前缺货：未知/);
  assert.equal(h.get('ca-refresh').disabled, false);
});
test('a late previous request cannot replace newer filter results or their loading state', async () => {
  let completeFirst; let requests = 0;
  const h = harness(async () => { requests++; if (requests === 1) return new Promise(resolve => { completeFirst = resolve; }); return response(sample({rows:[], total:0})); });
  await tick(); await h.app.loadData();
  assert.match(h.get('ca-rows').textContent, /当前筛选下没有/);
  completeFirst(response(sample())); await tick();
  assert.equal(h.app.state.rows.length, 0);
  assert.equal(h.get('ca-reload').disabled, false);
  assert.equal(h.get('clearance-alerts').attributes['aria-busy'], 'false');
});
test('failed load and failed remote scan release controls and display actionable errors', async () => {
  let failing = true;
  const h = harness(async (_, options) => { if (failing || options.method === 'POST') throw new Error('网络不可用'); return response(sample()); });
  await tick();
  assert.equal(h.get('ca-error').hidden, false); assert.match(h.get('ca-error').textContent, /网络不可用/);
  assert.equal(h.get('ca-reload').disabled, false); assert.equal(h.get('ca-refresh').disabled, false);
  failing = false; await h.app.loadData(); await h.app.refresh();
  assert.equal(h.get('ca-refresh').disabled, false); assert.equal(h.get('ca-update').hidden, true);
  assert.match(h.get('ca-error').textContent, /无法启动检测/);
});
test('background scan polls only while this page is visible and stops after completion', async () => {
  let checking = true; let requests = 0;
  const h = harness(async () => { requests++; return response(sample({checking})); }); await tick();
  assert.equal(h.get('ca-refresh').disabled, true);
  assert.equal([...h.timers.values()].filter(timer => timer.delay === 5000).length, 1);
  h.document.hidden = true; h.document.listeners.visibilitychange();
  assert.equal([...h.timers.values()].filter(timer => timer.delay === 5000).length, 0);
  checking = false; h.document.hidden = false; h.document.listeners.visibilitychange(); await tick();
  assert.equal(requests, 2); assert.equal(h.get('ca-refresh').disabled, false);
  assert.equal([...h.timers.values()].filter(timer => timer.delay === 5000).length, 0);
});
test('read-only accounts cannot submit facts or request a scan', async () => {
  const requests = [];
  const h = harness(async (url, options) => { requests.push({url,options}); return response(sample()); }, false); await tick();
  assert.equal(h.get('ca-refresh').disabled, true);
  h.app.openFacts(sample().rows[0]);
  assert.equal(h.get('ca-save').hidden, true);
  assert.equal(h.fields.get('restock_after_stockout').disabled, true);
  await h.app.refresh(); await h.app.saveFacts({preventDefault(){}});
  assert.equal(requests.length, 1);
});
test('facts save sends the exact SKU key and preserves unknown stockout at variation grain', async () => {
  const requests = [];
  const h = harness(async (url, options) => { requests.push({url,options}); return response(options.method === 'PATCH' ? {} : sample()); }); await tick();
  h.app.openFacts(sample().rows[0]);
  h.fields.get('inbound_history_confirmed').value = 'false';
  await h.app.saveFacts({preventDefault(){}});
  const patch = requests.find(request => request.options.method === 'PATCH');
  assert.equal(patch.url, '/api/clearance-alerts/17/facts');
  const body = JSON.parse(patch.options.body);
  assert.equal(body.sku_key, 'MLM123:41'); assert.equal(body.restock_after_stockout, null); assert.equal(body.visits_30d, null);
  assert.equal(h.get('ca-facts-dialog').open, false); assert.equal(h.get('ca-save').disabled, false);
});
test('manual scan sends JSON with the selected store and site scope', async () => {
  const requests = [];
  const h = harness(async (url, options) => { requests.push({url,options}); return response(options.method === 'POST' ? {started:true,checking:false} : sample()); }); await tick();
  h.get('ca-store').value = '2'; h.get('ca-site').value = 'MLM'; await h.app.refresh();
  const scan = requests.find(request => request.options.method === 'POST');
  assert.equal(scan.options.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(scan.options.body), {token_id:2, site_id:'MLM'});
  assert.equal(h.get('ca-refresh').disabled, false);
});

test('product main images load lazily and failed or unsafe images keep a placeholder', async () => {
  const data = sample(); data.rows[0].thumbnail_url = 'https://example.com/main.jpg';
  const h = harness(async () => response(data)); await tick();
  const media = h.get('ca-rows').children[0].children[0].children[0].children[0];
  const [placeholder, image] = media.children;
  assert.equal(image.tagName, 'img'); assert.equal(image.src, data.rows[0].thumbnail_url);
  assert.equal(image.loading, 'lazy'); assert.equal(image.alt, data.rows[0].title);
  assert.equal(placeholder.hidden, true);
  image.listeners.error(); assert.equal(image.hidden, true); assert.equal(placeholder.hidden, false);
  data.rows[0].thumbnail_url = 'javascript:alert(1)'; await h.app.loadData();
  const unsafeMedia = h.get('ca-rows').children[0].children[0].children[0].children[0];
  assert.equal(unsafeMedia.children.length, 1); assert.equal(unsafeMedia.textContent, '暂无主图');
});
