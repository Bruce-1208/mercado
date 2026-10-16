const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('bit/static/1688-products.js', 'utf8');
const fields = ['weight_g', 'package_length_cm', 'package_width_cm', 'package_height_cm', 'net_proceeds_usd', 'price'];
const row = () => ({
  id: 17, source_item_id: '701168800017', title: '测试货源', collector_salesperson: '业务员甲', management_category_name: '居家',
  source_purchase_price_cny: 12.5, source_weight_g: 800, source_package_length_cm: 10,
  source_package_width_cm: 20, source_package_height_cm: 30, net_proceeds_usd: 4.5,
  original_1688: {title: '测试货源', price: 99, weight_g: 999, package_length_cm: 99,
    package_width_cm: 99, package_height_cm: 99}
});
const response = data => ({ok: true, status: 200, json: async () => ({status: 'success', data})});

function harness(custom) {
  const elements = new Map();
  const skuControls = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', textContent: '', innerHTML: '', dataset: {}, open: false,
      get disabled() { return Boolean(this._disabled); }, set disabled(value) { this._disabled = Boolean(value); },
      validity: {badInput: false}, classList: {contains: () => true}, listeners: {},
      addEventListener(name, fn) { this.listeners[name] = fn; },
      showModal() { this.open = true; }, close() { this.open = false; }, focus() { this.focused = true; }
    });
    return elements.get(id);
  };
  const calls = [];
  let saved = row();
  const context = vm.createContext({
    URL, URLSearchParams, Intl, console, structuredClone, location: {search: ''}, window: {location: {origin: 'https://erp.test'}, catalogOperationMarkup(_row, markup) { return markup; }},
    localStorage: {getItem: () => null, setItem() {}}, aiOriginalDeleteRunning: false,
    document: {getElementById: get, querySelector: selector => skuControls.get(selector) || null,
      querySelectorAll: selector => selector.startsWith('#products-1688-sku-editor') ? [...skuControls.values()] : [],
      addEventListener() {}},
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
  vm.runInContext(fs.readFileSync('bit/static/sku-matrix.js', 'utf8'), context);
  vm.runInContext('globalThis.ZeshunSku = window.ZeshunSku', context);
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run(`products1688Rows = [${JSON.stringify(saved)}]`);
  const setSku = (index, field, value) => {
    const control = {value, disabled: false, validity: {badInput: false}, focus() { this.focused = true; }};
    skuControls.set(`[data-1688-sku="${index}"][data-field="${field}"]`, control);
    return control;
  };
  return {get, calls, run, setSku};
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
  assert.equal(h.get('products-1688-edit-weight_g').value, '812.5');
  assert.equal(h.get('products-1688-edit-package_width_cm').value, '');
  assert.equal(h.get('products-1688-detail-dialog').open, true);
});

test('saving source net closes immediately while AI products refresh in the background', async () => {
  const h = harness();
  h.run(`let aiRefreshes = 0;
    loadAiOriginalProducts = async options => {
      if (!options.preserveTaskStatus) throw new Error('task status must be preserved');
      if (products1688Rows[0].net_proceeds_usd !== 15) throw new Error('source save is missing');
      if (document.getElementById('products-1688-detail-dialog').open) throw new Error('dialog must already be closed');
      aiRefreshes++;
      return new Promise(() => {});
    };`);
  h.run('products1688OpenEdit(17)');
  h.get('products-1688-edit-net_proceeds_usd').value = '15';
  await h.run('products1688SaveEdit()');
  assert.equal(h.run('aiRefreshes'), 1);
  assert.equal(h.get('products-1688-detail-dialog').open, false);
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
  assert.equal(h.get('products-1688-detail-dialog').open, true);
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
  assert.equal(h.get('products-1688-detail-dialog').open, false);
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
  assert.equal(h.get('products-1688-detail-dialog').open, false);
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
  assert.equal(h.get('products-1688-detail-dialog').open, true);
  assert.equal(h.calls.filter(call => call.options.method === 'PATCH').length, 1);
  finish();
  await pending;
  assert.equal(h.get('products-1688-edit-save').disabled, false);
});

 test('row blank space opens unified detail while controls and text selection do not', () => {
  const h = harness();
  h.run('products1688RowClick({target: {closest: () => null}}, 17)');
  assert.equal(h.get('products-1688-detail-dialog').open, true);
  h.run('products1688CloseEdit()');
  h.run('products1688RowClick({target: {closest: () => ({})}}, 17)');
  assert.equal(h.get('products-1688-detail-dialog').open, false);
  h.run('window.getSelection = () => ({toString: () => "selected text"})');
  h.run('products1688RowClick({target: {closest: () => null}}, 17)');
  assert.equal(h.get('products-1688-detail-dialog').open, false);
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

test('bulk measurement edits snapshot the selection and omit unchanged blank fields', async () => {
  const h = harness((url) => url.endsWith('/bulk-dimensions') ? response({changed: 1}) : null);
  h.run('products1688Toggle(17, true); products1688OpenBulkEdit()');
  h.get('products-1688-bulk-weight_g').value = '680';
  h.get('products-1688-bulk-package_width_cm').value = '12.5';
  h.run('products1688Selected.clear()');
  await h.run('products1688SaveBulkEdit()');
  const call = h.calls.find(call => call.url.endsWith('/bulk-dimensions'));
  assert.deepEqual(JSON.parse(call.options.body), {product_item_ids: [17], changes: {weight_g: 680, package_width_cm: 12.5}});
  assert.equal(h.get('products-1688-bulk-dialog').open, false);
});

test('invalid or empty bulk edits do not write and failed writes retain user input', async () => {
  const h = harness(url => url.endsWith('/bulk-dimensions') ? {ok: false, status: 500, json: async () => ({status:'error', message:'写入失败'})} : null);
  h.run('products1688Toggle(17, true); products1688OpenBulkEdit()');
  await h.run('products1688SaveBulkEdit()');
  h.get('products-1688-bulk-weight_g').value = '-1';
  await h.run('products1688SaveBulkEdit()');
  assert.equal(h.calls.length, 0);
  h.get('products-1688-bulk-weight_g').value = '700';
  await h.run('products1688SaveBulkEdit()');
  assert.equal(h.get('products-1688-bulk-dialog').open, true);
  assert.equal(h.get('products-1688-bulk-weight_g').value, '700');
  assert.match(h.get('products-1688-bulk-message').textContent, /写入失败/);
});

test('weight range reaches the paginated query, validates bounds and resets', async () => {
  const h = harness();
  h.get('products-1688-weight-min').value = '200';
  h.get('products-1688-weight-max').value = '800';
  await h.run('load1688Products()');
  const query = new URL(h.calls.find(call => call.url.startsWith('/api/1688-products?')).url, 'https://erp.test').searchParams;
  assert.equal(query.get('weight_min'), '200');
  assert.equal(query.get('weight_max'), '800');
  const before = h.calls.length;
  h.get('products-1688-weight-min').value = '900';
  await h.run('load1688Products()');
  assert.equal(h.calls.length, before);
  assert.match(h.get('products-1688-status').textContent, /最低重量不能大于最高重量/);
  h.run('products1688ResetFilters()');
  assert.equal(h.get('products-1688-weight-min').value, '');
  assert.equal(h.get('products-1688-weight-max').value, '');
});

test('sending to AI keeps source rows and collected details in the 1688 library', () => {
  const h = harness();
  const original = h.run('JSON.stringify(products1688Rows)');
  h.run('var aiOriginalSelected = new Set(); products1688OpenAiWorkflow = () => {}; products1688Toggle(17, true); products1688SendSelectedToWorkflow()');
  assert.equal(h.run('JSON.stringify(products1688Rows)'), original);
  assert.equal(h.run('aiOriginalSelected.has(17)'), true);
  assert.match(h.get('products-1688-status').textContent, /原始资料继续保留/);
});

test('product cards expose the 1688 source number and only the unified detail entry', () => {
  const h = harness();
  h.run('products1688RenderRows()');
  assert.match(h.get('products-1688-body').innerHTML, /701168800017/);
  assert.match(h.get('products-1688-body').innerHTML, /查看详情/);
  assert.doesNotMatch(h.get('products-1688-body').innerHTML, /修改资料|编辑刊登内容/);
  h.run('products1688OpenDetail(17)');
  assert.equal(h.get('products-1688-detail-dialog').open, true);
  assert.match(h.get('products-1688-detail-subtitle').textContent, /1688 编号：701168800017/);
  h.run('products1688CloseEdit()');
  assert.equal(h.get('products-1688-detail-dialog').open, false);
});

test('detail SKU edits preserve untouched source data and validate stock before saving', async () => {
  const h = harness();
  h.run(`products1688Rows[0].original_1688.variations = [{sku_id: 'red', seller_sku: 'CUP-RED',
    price: 12.5, available_quantity: 0, weight_g: 150, images: ['https://img.test/red.jpg']}];
    window.sourceSnapshot = products1688Rows[0].original_1688;
    products1688OpenDetail(17);`);
  const before = h.run('JSON.stringify(window.sourceSnapshot)');
  h.setSku(0, 'seller_sku', 'CUP-RED-NEW');
  const stock = h.setSku(0, 'available_quantity', '2.5');
  await h.run('products1688SaveEdit()');
  assert.equal(h.calls.length, 0);
  assert.match(h.get('products-1688-edit-message').textContent, /SKU 1.*库存.*整数/);
  assert.equal(stock.focused, true);
  stock.value = '7';
  h.setSku(0, 'price', '25');
  h.setSku(0, 'weight_g', '180');
  await h.run('products1688SaveEdit()');
  const patch = JSON.parse(h.calls.find(call => call.options.method === 'PATCH').options.body);
  assert.deepEqual(patch, {variations: [{sku_id: 'red', seller_sku: 'CUP-RED-NEW', price: 25,
    available_quantity: 7, weight_g: 180, images: ['https://img.test/red.jpg'], net_proceeds_usd: 5}]});
  assert.equal(h.run('JSON.stringify(window.sourceSnapshot)'), before);
  assert.equal(h.get('products-1688-detail-dialog').open, false);
});

test('viewing another member product disables detail editing and rejects save attempts', async () => {
  const h = harness();
  const sku = h.setSku(0, 'seller_sku', 'LOCKED');
  h.run('products1688Rows[0].can_operate = false; products1688OpenDetail(17)');
  assert.equal(h.get('products-1688-detail-dialog').open, true);
  assert.ok(fields.every(field => h.get(`products-1688-edit-${field}`).disabled));
  assert.equal(sku.disabled, true);
  assert.equal(h.get('products-1688-edit-save').disabled, true);
  assert.equal(h.get('products-1688-edit-close').disabled, false);
  h.get('products-1688-edit-price').value = '99';
  await h.run('products1688SaveEdit()');
  assert.equal(h.calls.length, 0);
  assert.match(h.get('products-1688-edit-message').textContent, /仅可查看/);
  h.run('products1688CloseDetail()');
  assert.equal(h.get('products-1688-detail-dialog').open, false);
});


test('source identifiers prefer original offer IDs and supplier URLs before removing canonical prefixes', () => {
  const h = harness();
  h.run(`products1688Rows[0].source_item_id = '1688_701168800017'; products1688RenderRows();`);
  assert.equal(h.run('products1688SourceId(products1688Rows[0])'), '701168800017');
  h.run('products1688OpenDetail(17)');
  assert.match(h.get('products-1688-detail-subtitle').textContent, /1688 编号：701168800017/);
  assert.doesNotMatch(h.get('products-1688-detail-subtitle').textContent, /1688_701/);
  assert.equal(h.run(`products1688SourceId({source_item_id: '16881041447098398', original_1688: {}})`), '1041447098398');
  assert.equal(h.run(`products1688SourceId({source_item_id: '1688_701168800017',
    original_1688: {source_url: 'https://detail.1688.com/offer/1041447098398.html'}})`), '1041447098398');
  assert.equal(h.run(`products1688SourceId({source_item_id: '1688_701168800017',
    original_1688: {source_1688_item_id: '701168800018', source_url: 'https://detail.1688.com/offer/1041447098398.html'}})`), '701168800018');
  h.run(`products1688Rows[0].source_item_id = '';
    products1688Rows[0].original_1688.source_1688_item_id = '701168800018';`);
  assert.equal(h.run('products1688SourceId(products1688Rows[0])'), '701168800018');
});
