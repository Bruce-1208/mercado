const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('bit/static/1688-products.js', 'utf8');
const fields = ['weight_g', 'package_length_cm', 'package_width_cm', 'package_height_cm', 'net_proceeds_usd', 'price'];
const row = () => ({
  id: 17, title: '测试货源', collector_salesperson: '业务员甲', management_category_name: '居家',
  source_purchase_price_cny: 12.5, source_weight_g: 800, source_package_length_cm: 10,
  source_package_width_cm: 20, source_package_height_cm: 30, net_proceeds_usd: 4.5,
  original_1688: {title: '测试货源', price: 99, weight_g: 999, package_length_cm: 99,
    package_width_cm: 99, package_height_cm: 99}
});
const response = data => ({ok: true, status: 200, json: async () => ({status: 'success', data})});

function harness(custom) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', textContent: '', innerHTML: '', dataset: {}, disabled: false, open: false,
      validity: {badInput: false}, classList: {contains: () => true}, listeners: {},
      addEventListener(name, fn) { this.listeners[name] = fn; },
      showModal() { this.open = true; }, close() { this.open = false; }, focus() { this.focused = true; }
    });
    return elements.get(id);
  };
  const calls = [];
  let saved = row();
  const context = vm.createContext({
    URL, URLSearchParams, Intl, console, location: {search: ''}, window: {location: {origin: 'https://erp.test'}},
    localStorage: {getItem: () => null, setItem() {}}, aiOriginalDeleteRunning: false,
    document: {getElementById: get, addEventListener() {}},
    fetch: async (url, options = {}) => {
      calls.push({url, options});
      const override = custom && await custom(url, options);
      if (override) return override;
      if (url === '/api/1688-products/options') return response({collector_salespeople: ['业务员甲', '不在当前页的业务员乙']});
      if (url === '/api/mercado-management-categories') return response({rows: [{id: 3, name: '居家'}, {id: 4, name: '<新品>'}]});
      if (url === '/api/mercado-management-categories/assign') return response({changed: 1});
      if (options.method === 'PATCH') {
        const patch = JSON.parse(options.body);
        saved = {...saved, source_1688_overrides: {...saved.source_1688_overrides, ...patch}};
        if ('net_proceeds_usd' in patch) saved.net_proceeds_usd = patch.net_proceeds_usd;
        return response(saved);
      }
      return response({rows: [saved], total: 1});
    }
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run(`products1688Rows = [${JSON.stringify(saved)}]`);
  return {get, calls, run};
}

test('all collector options and shared categories remain independent of visible product rows', async () => {
  const h = harness();
  await h.run('load1688Products()');
  assert.match(h.get('products-1688-salesperson').innerHTML, /不在当前页的业务员乙/);
  assert.match(h.get('products-1688-category-filter').innerHTML, /&lt;新品&gt;/);
  h.get('products-1688-salesperson').value = '不在当前页的业务员乙';
  h.get('products-1688-category-filter').value = '3';
  await h.run('load1688Products()');
  const query = new URL(h.calls.at(-1).url, 'https://erp.test').searchParams;
  assert.equal(query.get('collector_salesperson'), '不在当前页的业务员乙');
  assert.equal(query.get('management_category_id'), '3');
  assert.equal(h.get('products-1688-salesperson').value, '不在当前页的业务员乙');
});

test('bulk category assignment uses product IDs and clearing sends an explicit null', async () => {
  const h = harness();
  await h.run('load1688Products()');
  assert.equal(h.get('products-1688-batch-category').disabled, true);
  h.run('products1688Toggle(17, true)');
  h.get('products-1688-batch-category').value = '4';
  h.run('products1688UpdateSelection()');
  assert.equal(h.get('products-1688-category-submit').disabled, false);
  await h.run('products1688ApplyCategory()');
  h.get('products-1688-batch-category').value = 'uncategorized';
  await h.run('products1688ApplyCategory()');
  const assignments = h.calls.filter(call => call.url.endsWith('/assign')).map(call => JSON.parse(call.options.body));
  assert.deepEqual(assignments, [
    {item_type: 'products', item_ids: [17], category_id: 4},
    {item_type: 'products', item_ids: [17], category_id: null}
  ]);
});

test('single edits send only changed fields and display saved overrides including clear', async () => {
  const h = harness();
  h.run('products1688OpenEdit(17)');
  assert.equal(h.get('products-1688-edit-weight_g').value, '800');
  assert.equal(h.get('products-1688-edit-price').value, '12.5');
  h.get('products-1688-edit-weight_g').value = '812.5';
  h.get('products-1688-edit-package_width_cm').value = '';
  h.get('products-1688-edit-price').value = '12.500';
  await h.run('products1688SaveEdit()');
  const writes = h.calls.filter(call => call.options.method === 'PATCH');
  assert.equal(writes.length, 1);
  assert.deepEqual(JSON.parse(writes[0].options.body), {weight_g: 812.5, package_width_cm: null});
  assert.equal(h.run('products1688Weight(products1688Rows[0])'), '812.5 g');
  assert.match(h.run('products1688Dimensions(products1688Rows[0])'), /宽：未填写/);
  h.run('products1688OpenDetail(17)');
  assert.match(h.get('products-1688-detail-content').innerHTML, /812.5 g/);
  assert.doesNotMatch(h.get('products-1688-detail-content').innerHTML, /999 g/);
  assert.equal(h.get('products-1688-edit-dialog').open, false);
});

test('saving source net closes immediately while AI products refresh in the background', async () => {
  const h = harness();
  h.run(`let aiRefreshes = 0;
    loadAiOriginalProducts = async options => {
      if (!options.preserveTaskStatus) throw new Error('task status must be preserved');
      if (products1688Rows[0].net_proceeds_usd !== 15) throw new Error('source save is missing');
      if (document.getElementById('products-1688-edit-dialog').open) throw new Error('dialog must already be closed');
      aiRefreshes++;
      return new Promise(() => {});
    };`);
  h.run('products1688OpenEdit(17)');
  h.get('products-1688-edit-net_proceeds_usd').value = '15';
  await h.run('products1688SaveEdit()');
  assert.equal(h.run('aiRefreshes'), 1);
  assert.equal(h.get('products-1688-edit-dialog').open, false);
  assert.equal(h.get('products-1688-edit-save').disabled, false);
  assert.equal(h.calls.filter(call => call.options.method !== 'PATCH').length, 0);
  assert.equal(h.get('products-1688-status').textContent, '商品资料已保存');
});

test('invalid numeric typing cannot accidentally clear existing data and failed saves remain editable', async () => {
  const h = harness((url, options) => options.method === 'PATCH'
    ? {ok: false, status: 400, json: async () => ({status: 'error', message: '资料保存失败'})} : null);
  h.run('products1688OpenEdit(17)');
  const weight = h.get('products-1688-edit-weight_g');
  weight.value = '';
  weight.validity.badInput = true;
  await h.run('products1688SaveEdit()');
  assert.equal(h.calls.length, 0);
  assert.match(h.get('products-1688-edit-message').textContent, /重量（g）.*有效数字/);
  weight.validity.badInput = false;
  weight.value = '900';
  await h.run('products1688SaveEdit()');
  assert.match(h.get('products-1688-edit-message').textContent, /资料保存失败/);
  assert.equal(h.get('products-1688-edit-dialog').open, true);
  assert.equal(h.get('products-1688-edit-save').disabled, false);
  assert.equal(h.get('products-1688-edit-weight_g').value, '900');
});

test('positive finite numeric validation rejects zero, negative and infinity without writes', async () => {
  for (const value of ['0', '-1', 'Infinity']) {
    const h = harness();
    h.run('products1688OpenEdit(17)');
    h.get('products-1688-edit-price').value = value;
    await h.run('products1688SaveEdit()');
    assert.equal(h.calls.length, 0);
    assert.match(h.get('products-1688-edit-message').textContent, /采购价（CNY）.*大于 0/);
  }
});

test('a saved authoritative row needs no source list refresh even when the read service is unavailable', async () => {
  const h = harness(url => url.startsWith('/api/1688-products?')
    ? {ok: false, status: 503, json: async () => ({status: 'error', message: '读取服务暂不可用'})} : null);
  h.run('products1688OpenEdit(17)');
  h.get('products-1688-edit-price').value = '18';
  await h.run('products1688SaveEdit()');
  assert.equal(h.run('products1688Rows.length'), 1);
  assert.equal(h.run('products1688SourceValue(products1688Rows[0], "price")'), 18);
  assert.equal(h.get('products-1688-status').textContent, '商品资料已保存');
  assert.equal(h.calls.length, 1);
});

test('AI refresh failure cannot turn a committed save into an edit failure', async () => {
  const h = harness();
  h.run(`let syncWarnings = [];
    console = {warn: (...args) => syncWarnings.push(args)};
    loadAiOriginalProducts = () => { throw new Error('AI unavailable'); };`);
  h.run('products1688OpenEdit(17)');
  h.get('products-1688-edit-price').value = '18';
  await h.run('products1688SaveEdit()');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.get('products-1688-edit-dialog').open, false);
  assert.equal(h.get('products-1688-status').textContent, '商品资料已保存');
  assert.equal(h.run('syncWarnings.length'), 1);
  assert.doesNotMatch(h.get('products-1688-edit-message').textContent, /保存失败/);
});

test('a list started before persistence cannot overwrite the saved row', async () => {
  let finishList;
  let listCalls = 0;
  const h = harness(url => {
    if (url.startsWith('/api/1688-products?') && ++listCalls === 1) {
      return new Promise(resolve => { finishList = () => resolve(response({rows: [row()], total: 1})); });
    }
    return null;
  });
  const oldList = h.run('load1688Products()');
  h.run('products1688OpenEdit(17)');
  h.get('products-1688-edit-price').value = '18';
  await h.run('products1688SaveEdit()');
  assert.equal(h.get('products-1688-edit-dialog').open, false);
  finishList();
  await oldList;
  assert.equal(h.run('products1688SourceValue(products1688Rows[0], "price")'), 18);
  assert.equal(listCalls, 2);
  assert.equal(h.run('products1688Loading'), false);
});

test('a committed category change remains visible when the following list refresh fails', async () => {
  const h = harness(url => url.startsWith('/api/1688-products?')
    ? {ok: false, status: 503, json: async () => ({status: 'error', message: '读取服务暂不可用'})} : null);
  await h.run('products1688LoadOptions()');
  h.run('products1688Toggle(17, true)');
  h.get('products-1688-batch-category').value = '4';
  await h.run('products1688ApplyCategory()');
  assert.equal(h.run('products1688Rows.length'), 1);
  assert.equal(h.run('products1688Rows[0].management_category_id'), 4);
  assert.match(h.get('products-1688-body').innerHTML, /&lt;新品&gt;/);
  assert.match(h.get('products-1688-status').textContent, /已更新.*分类.*列表刷新失败.*读取服务暂不可用/);
});

test('save busy state prevents duplicate PATCH calls and closing before completion', async () => {
  let finish;
  const h = harness((url, options) => options.method === 'PATCH'
    ? new Promise(resolve => { finish = () => resolve(response({...row(), source_1688_overrides: {weight_g: 900}})); }) : null);
  h.run('products1688OpenEdit(17)');
  h.get('products-1688-edit-weight_g').value = '900';
  const pending = h.run('products1688SaveEdit()');
  assert.equal(h.get('products-1688-edit-save').disabled, true);
  assert.ok(fields.every(field => h.get(`products-1688-edit-${field}`).disabled));
  h.run('products1688CloseEdit()');
  await h.run('products1688SaveEdit()');
  assert.equal(h.get('products-1688-edit-dialog').open, true);
  assert.equal(h.calls.filter(call => call.options.method === 'PATCH').length, 1);
  finish();
  await pending;
  assert.equal(h.get('products-1688-edit-save').disabled, false);
});

 test('row blank space opens edit while controls and text selection do not', () => {
  const h = harness();
  h.run('products1688RowClick({target: {closest: () => null}}, 17)');
  assert.equal(h.get('products-1688-edit-dialog').open, true);
  h.run('products1688CloseEdit()');
  h.run('products1688RowClick({target: {closest: () => ({})}}, 17)');
  assert.equal(h.get('products-1688-edit-dialog').open, false);
  h.run('window.getSelection = () => ({toString: () => "selected text"})');
  h.run('products1688RowClick({target: {closest: () => null}}, 17)');
  assert.equal(h.get('products-1688-edit-dialog').open, false);
});

test('workflow transfer accepts valid items and leaves missing source fields selected', () => {
  const h = harness();
  h.run(`
    var workflowWrites = [], workflowMessages = [], workflowOpened = 0;
    var aiOriginalSelected = new Set();
    localStorage.setItem = (key, value) => workflowWrites.push([key, value]);
    products1688OpenAiWorkflow = () => workflowOpened++;
    products1688SetStatus = (...args) => workflowMessages.push(args);
    products1688Rows = [
      {...products1688Rows[0], id: 1},
      {...products1688Rows[0], id: 2, source_1688_overrides: {weight_g: null}},
      {...products1688Rows[0], id: 3, source_1688_overrides: {net_proceeds_usd: null}},
      {...products1688Rows[0], id: 4, source_weight_g: 0},
      {...products1688Rows[0], id: 5, net_proceeds_usd: 'NaN'}
    ];
    products1688Selected = new Set([1, 2, 3, 4, 5]);
    products1688SendSelectedToWorkflow();
  `);
  assert.equal(h.run('JSON.stringify([...aiOriginalSelected])'), '[1]');
  assert.equal(h.run('JSON.stringify([...products1688Selected])'), '[2,3,4,5]');
  assert.equal(h.run('workflowWrites[0][1]'), '[1]');
  assert.equal(h.run('workflowOpened'), 1);
  assert.equal(h.run('workflowMessages.at(-1)[1]'), 'partial');
  assert.match(h.run('workflowMessages.at(-1)[0]'), /已转入 1 件.*未通过 4 件.*重量.*净收益/);
});

test('all invalid transfers and single editor entry stay in the 1688 area', async () => {
  const h = harness();
  h.run(`
    var workflowOpened = 0, workflowWrites = 0, workflowMessages = [];
    localStorage.setItem = () => workflowWrites++;
    products1688OpenAiWorkflow = () => workflowOpened++;
    products1688SetStatus = (...args) => workflowMessages.push(args);
    products1688Rows[0].source_1688_overrides = {weight_g: null, net_proceeds_usd: ''};
    products1688Selected = new Set([17]);
    products1688SendSelectedToWorkflow();
  `);
  await h.run('products1688OpenAiEditor(17)');
  assert.equal(h.run('workflowOpened'), 0);
  assert.equal(h.run('workflowWrites'), 0);
  assert.equal(h.run('products1688Selected.size'), 1);
  assert.equal(h.run('workflowMessages.at(-1)[1]'), 'error');
});

test('positive source overrides allow transfer despite missing raw values', () => {
  const h = harness();
  h.run(`products1688Rows[0].source_weight_g = null;
    products1688Rows[0].net_proceeds_usd = null;
    products1688Rows[0].source_1688_overrides = {weight_g: '250', net_proceeds_usd: '5'};`);
  assert.equal(h.run('products1688WorkflowIssue(products1688Rows[0])'), '');
});

test('slow filter metadata never blocks rows and concurrent loads share metadata', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const h = harness(async url => {
    if (url === '/api/1688-products/options' || url === '/api/mercado-management-categories') await gate;
  });
  const loaded = h.run('load1688Products()');
  await Promise.race([loaded, new Promise((_, reject) => setTimeout(() => reject(new Error('rows waited for metadata')), 500))]);
  assert.match(h.get('products-1688-status').textContent, /已加载 1 件/);
  await h.run('load1688Products(false)');
  assert.equal(h.calls.filter(call => call.url === '/api/1688-products/options').length, 1);
  release();
  await h.run('products1688OptionsPromise');
  await h.run('load1688Products(false)');
  assert.equal(h.calls.filter(call => call.url === '/api/1688-products/options').length, 1);
  h.run('products1688OptionsLoadedAt = Date.now() - PRODUCTS_1688_OPTIONS_TTL_MS - 1');
  await h.run('load1688Products(false)');
  assert.equal(h.calls.filter(call => call.url === '/api/1688-products/options').length, 2);
});

test('failed metadata can retry without discarding loaded products', async () => {
  let fail = true;
  const h = harness(url => fail && url === '/api/1688-products/options'
    ? {ok: false, status: 500, json: async () => ({message: '选项暂不可用'})} : null);
  await h.run('load1688Products()');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(h.get('products-1688-status').textContent, /已加载.*选项暂不可用/);
  assert.equal(h.run('products1688Rows.length'), 1);
  fail = false;
  await h.run('load1688Products()');
  assert.equal(h.calls.filter(call => call.url === '/api/1688-products/options').length, 2);
});
