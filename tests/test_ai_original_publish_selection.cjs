const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup() {
  const nodes = new Map();
  const requests = [];
  const confirmations = [];
  const context = vm.createContext({
    document: {
      addEventListener() {}, querySelectorAll() { return []; },
      getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, {value: '', selectedOptions: []});
        return nodes.get(id);
      },
    },
    window: {confirm(message) { confirmations.push(message); return true; }},
    fetch: async (url, options) => {
      requests.push({url, body: JSON.parse(options.body)});
      return {ok: true, json: async () => ({status: 'success'})};
    },
  });
  const run = code => vm.runInContext(code, context);
  for (const file of ['ai-original-products.js', 'ai-original-workbench.js']) {
    run(fs.readFileSync(path.join(__dirname, '../bit/static', file), 'utf8'));
  }
  run(`
    const ready = {id: 1, ai_status: 'completed', review_status: 'approved',
      category_id: 'CBT123', title_es: 'Producto', title_pt: 'Produto',
      description_es: 'Descripcion', description_pt: 'Descricao',
      main_image_url: '/product-ai-white.jpg', weight_g: 100, net_proceeds_usd: 2,
      weight_basis: 'ai_original_manual', ai_original: {
        attributes: [{id: 'MATERIAL', value_name: 'Plastic'}], image_generation_method: 'ai_image_edit'
      }};
    aiOriginalRows = [ready, {...ready, id: 2, review_status: 'unreviewed'},
      {...ready, id: 3, ai_original: {...ready.ai_original, attributes: []}}];
    aiOriginalSelected = new Set([1, 2, 3]);
    aiOriginalSelectedSiteIds = () => ['MLM'];
    loadAiOriginalPublishStatus = () => {};
    document.getElementById('ai-original-stores').selectedOptions = [{value: '7'}];
  `);
  return {run, nodes, requests, confirmations};
}

test('mixed selection enables publishing and sends all approved products regardless of completeness', async () => {
  const {run, nodes, requests, confirmations} = setup();
  run('updateAiOriginalSelection(); syncAiOriginalWorkbenchSelection()');
  assert.equal(nodes.get('ai-original-publish').disabled, false);
  assert.match(nodes.get('ai-original-selection-hint').textContent, /审核通过 2 件 · 将忽略 1 件/);
  await run('publishSelectedAiOriginalProducts()');
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].body.product_item_ids, [1, 3]);
  assert.match(confirmations[0], /自动忽略 1 件/);
  assert.equal(run('aiOriginalSelected.size'), 3);
});

test('all unapproved products keep button enabled and are ignored', async () => {
  const {run, nodes, requests, confirmations} = setup();
  run('aiOriginalSelected = new Set([2]); updateAiOriginalSelection()');
  assert.equal(nodes.get('ai-original-publish').disabled, false);
  run('syncAiOriginalWorkbenchSelection()');
  assert.equal(nodes.get('ai-original-publish').disabled, false);
  await run('publishSelectedAiOriginalProducts()');
  assert.equal(requests.length, 0);
  assert.equal(confirmations.length, 0);
  assert.match(nodes.get('ai-original-publish-status').textContent, /已全部忽略，本次上架 0 件/);
});

test('empty selection disables publishing', () => {
  const {run, nodes} = setup();
  run('aiOriginalSelected.clear(); updateAiOriginalSelection(); syncAiOriginalWorkbenchSelection()');
  assert.equal(nodes.get('ai-original-publish').disabled, true);
});

test('status filtering removes hidden selections, including after switching back', () => {
  const {run, nodes} = setup();
  run(`aiOriginalRows[1].ai_status = 'pending';
    document.getElementById('ai-original-status-filter').value = 'completed';
    renderAiOriginalProducts();`);
  assert.deepEqual(Array.from(run('[...aiOriginalSelected]')), [1, 3]);
  assert.equal(nodes.get('ai-original-selection').textContent, '已选择 2 件');
  run(`document.getElementById('ai-original-status-filter').value = ''; renderAiOriginalProducts()`);
  assert.deepEqual(Array.from(run('[...aiOriginalSelected]')), [1, 3]);
});

test('bulk approval saves current inputs before sending review status', async () => {
  const {run, requests} = setup();
  run(`aiOriginalSelected = new Set([1]);
    aiOriginalRows[0].weight_g = null;
    aiOriginalRows[0].net_proceeds_usd = null;
    document.getElementById('ai-original-weight-1').value = '250';
    document.getElementById('ai-original-net-1').value = '3.25';
    document.getElementById('ai-original-review-status').value = 'approved';
    loadAiOriginalProducts = async () => {};`);
  await run('updateSelectedAiOriginalReviewStatus()');
  assert.equal(requests.length, 2);
  assert.equal(requests[0].url, '/api/ai-original-products/1');
  assert.deepEqual(requests[0].body, {weight_g: 250, net_proceeds_usd: 3.25});
  assert.equal(requests[1].url, '/api/mercado-products/review-status');
  assert.deepEqual(requests[1].body.product_item_ids, [1]);
});

test('cleared input prevents approval even when saved data is valid', async () => {
  const {run, requests} = setup();
  run(`aiOriginalSelected = new Set([1]);
    document.getElementById('ai-original-weight-1').value = '';
    document.getElementById('ai-original-net-1').value = '2';
    document.getElementById('ai-original-review-status').value = 'approved';`);
  await run('updateSelectedAiOriginalReviewStatus()');
  assert.equal(requests.length, 0);
});

test('parameter save failure prevents bulk approval', async () => {
  const {run, requests, nodes} = setup();
  run(`aiOriginalSelected = new Set([1]);
    document.getElementById('ai-original-weight-1').value = '100';
    document.getElementById('ai-original-net-1').value = '2';
    document.getElementById('ai-original-review-status').value = 'approved';
    fetch = async () => ({ok: false, json: async () => ({message: '保存失败'})});`);
  await run('updateSelectedAiOriginalReviewStatus()');
  assert.equal(requests.length, 0);
  assert.match(nodes.get('ai-original-task-status').textContent, /产品 1 参数保存失败/);
  assert.equal(run('aiOriginalReviewRunning'), false);
});
