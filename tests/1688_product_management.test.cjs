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

test('a saved authoritative row is retained when the following list refresh fails', async () => {
  const h = harness(url => url.startsWith('/api/1688-products?')
    ? {ok: false, status: 503, json: async () => ({status: 'error', message: '读取服务暂不可用'})} : null);
  h.run('products1688OpenEdit(17)');
  h.get('products-1688-edit-price').value = '18';
  await h.run('products1688SaveEdit()');
  assert.equal(h.run('products1688Rows.length'), 1);
  assert.equal(h.run('products1688SourceValue(products1688Rows[0], "price")'), 18);
  assert.match(h.get('products-1688-status').textContent, /已保存.*列表刷新失败.*读取服务暂不可用/);
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
