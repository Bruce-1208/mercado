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

test('mixed selection enables publishing and sends only eligible products', async () => {
  const {run, nodes, requests, confirmations} = setup();
  run('updateAiOriginalSelection(); syncAiOriginalWorkbenchSelection()');
  assert.equal(nodes.get('ai-original-publish').disabled, false);
  assert.match(nodes.get('ai-original-selection-hint').textContent, /可上架 1 件 · 将忽略 2 件/);
  await run('publishSelectedAiOriginalProducts()');
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].body.product_item_ids, [1]);
  assert.match(confirmations[0], /自动忽略 2 件/);
  assert.equal(run('aiOriginalSelected.size'), 3);
});

test('no eligible products disables publishing and sends no request', async () => {
  const {run, nodes, requests, confirmations} = setup();
  run('aiOriginalSelected = new Set([2, 3]); updateAiOriginalSelection()');
  assert.equal(nodes.get('ai-original-publish').disabled, true);
  run('syncAiOriginalWorkbenchSelection()');
  assert.equal(nodes.get('ai-original-publish').disabled, true);
  await run('publishSelectedAiOriginalProducts()');
  assert.equal(requests.length, 0);
  assert.equal(confirmations.length, 0);
  assert.match(nodes.get('ai-original-publish-status').textContent, /暂无可上架产品/);
});

test('empty selection disables publishing', () => {
  const {run, nodes} = setup();
  run('aiOriginalSelected.clear(); updateAiOriginalSelection(); syncAiOriginalWorkbenchSelection()');
  assert.equal(nodes.get('ai-original-publish').disabled, true);
});
