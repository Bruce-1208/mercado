const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup() {
  const nodes = new Map();
  const context = vm.createContext({
    URLSearchParams,
    document: {addEventListener() {}, getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, {value: '', dataset: {}, close() {}});
      return nodes.get(id);
    }},
    window: {confirm: () => true},
    localStorage: {getItem: () => '[1,3]', setItem(key, value) {context.saved = value;}},
    fetch: async (url, options) => {
      context.requests.push({url, options});
      return {ok: true, json: async () => ({status: 'success', data: {deleted: 2}})};
    }, requests: [],
  });
  for (const file of ['ai-original-products.js', '1688-products.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../bit/static', file), 'utf8'), context);
  }
  vm.runInContext(`
    aiOriginalRows = [{id: 1}, {id: 2}, {id: 3}];
    products1688Rows = [{id: 1}, {id: 2}, {id: 3}];
    aiOriginalSelected = new Set([1, 2]);
    products1688Selected = new Set([1, 2]);
    renderAiOriginalProducts = () => updateAiOriginalSelection();
    products1688RenderRows = () => products1688UpdateSelection();
    originalLoad1688Products = load1688Products;
    loadAiOriginalProducts = async () => {};
    load1688Products = async () => {};
  `, context);
  return {context, nodes, run: code => vm.runInContext(code, context)};
}

for (const action of ['deleteSelectedAiOriginalProducts()', 'products1688DeleteSelected()']) {
  test(`${action} deletes selection and synchronizes both areas`, async () => {
    const {context, run, nodes} = setup();
    await run(action);
    assert.equal(context.requests.length, 1);
    assert.equal(context.requests[0].url, '/api/mercado-products');
    assert.equal(context.requests[0].options.method, 'DELETE');
    assert.deepEqual(JSON.parse(context.requests[0].options.body), {product_item_ids: [1, 2]});
    assert.equal(run('aiOriginalRows.map(row => row.id).join()'), '3');
    assert.equal(run('products1688Rows.map(row => row.id).join()'), '3');
    assert.equal(run('aiOriginalSelected.size + products1688Selected.size'), 0);
    assert.equal(context.saved, '[3]');
    assert.equal(nodes.get('ai-original-delete').disabled, true);
    assert.equal(nodes.get('products-1688-delete').disabled, true);
  });
}

test('cancellation and empty selection never send deletion', async () => {
  const {context, run} = setup();
  context.window.confirm = () => false;
  await run('deleteSelectedAiOriginalProducts()');
  assert.equal(run('aiOriginalSelected.size'), 2);
  context.window.confirm = () => {throw new Error('should not prompt');};
  await run('aiOriginalSelected.clear(); deleteSelectedAiOriginalProducts()');
  assert.equal(context.requests.length, 0);
});

test('failed deletion preserves rows and selection and allows retry', async () => {
  const {context, run, nodes} = setup();
  context.fetch = async () => ({ok: false, status: 409, json: async () => ({message: '批量上架正在运行'})});
  await run('products1688DeleteSelected()');
  assert.equal(run('products1688Rows.length'), 3);
  assert.equal(run('products1688Selected.size'), 2);
  assert.equal(nodes.get('products-1688-delete').disabled, false);
  assert.match(nodes.get('products-1688-status').textContent, /批量上架正在运行/);
});

test('pending request disables both delete buttons and prevents duplicates', async () => {
  const {context, run, nodes} = setup();
  let finish;
  context.fetch = () => new Promise(resolve => {finish = resolve;});
  const pending = run('deleteSelectedAiOriginalProducts()');
  assert.equal(nodes.get('products-1688-delete').disabled, true);
  assert.equal(nodes.get('ai-original-delete').disabled, true);
  await run('products1688DeleteSelected()');
  finish({ok: true, json: async () => ({status: 'success', data: {deleted: 2}})});
  await pending;
  assert.equal(run('aiOriginalDeleteRunning'), false);
});

test('AI select-all reflects only visible filtered rows', () => {
  const {run, nodes} = setup();
  run('aiOriginalRows = [{id:1, ai_status:"pending"}, {id:2, ai_status:"completed"}]; aiOriginalSelected = new Set([2]); updateAiOriginalSelection()');
  nodes.get('ai-original-status-filter').value = 'pending';
  run('updateAiOriginalSelection()');
  assert.equal(nodes.get('ai-original-select-all').checked, false);
  assert.equal(nodes.get('ai-original-select-all').indeterminate, false);
  run('toggleAiOriginalProduct(1, true)');
  assert.equal(nodes.get('ai-original-select-all').checked, true);
});


test('deleting last page returns to the last remaining page', async () => {
  const {context, run} = setup();
  const urls = [];
  context.fetch = async url => {
    urls.push(url);
    return {ok: true, json: async () => ({status: 'success', data: {
      total: 50, rows: urls.length === 1 ? [] : [{id: 3}]
    }})};
  };
  await run('products1688Page = 2; load1688Products = originalLoad1688Products; load1688Products(false)');
  assert.equal(urls.length, 2);
  assert.match(urls[0], /offset=50/);
  assert.match(urls[1], /offset=0/);
  assert.equal(run('products1688Page'), 1);
  assert.equal(run('products1688Rows[0].id'), 3);
});
