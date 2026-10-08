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
      return {ok: true, json: async () => ({status: 'success', data: {
        id: Number(url.split('/').pop()), ...JSON.parse(options.body),
      }})};
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
      package_length_cm: 10, package_width_cm: 10, package_height_cm: 10,
      weight_basis: 'ai_original_manual', ai_original: {
        attributes: [{id: 'MATERIAL', value_name: 'Plastic'}], image_generation_method: 'ai_image_edit'
      }};
    aiOriginalRows = [ready, {...ready, id: 2, review_status: 'unreviewed'},
      {...ready, id: 3, review_status: 'unreviewed', ai_original: {...ready.ai_original, attributes: []}}];
    aiOriginalSelected = new Set([1, 2, 3]);
    aiOriginalSelectedSiteIds = () => ['MLM'];
    loadAiOriginalPublishStatus = () => {};
    document.getElementById('ai-original-stores').selectedOptions = [{value: '7'}];
  `);
  return {run, nodes, requests, confirmations};
}

test('mixed selection enables publishing and sends only products with unified passed status', async () => {
  const {run, nodes, requests, confirmations} = setup();
  run('updateAiOriginalSelection(); syncAiOriginalWorkbenchSelection()');
  assert.equal(nodes.get('ai-original-publish').disabled, false);
  assert.match(nodes.get('ai-original-selection-hint').textContent, /通过 1 件 · 将忽略 2 件/);
  await run('publishSelectedAiOriginalProducts()');
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].body.product_item_ids, [1]);
  assert.match(confirmations[0], /自动忽略 2 件/);
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

test('a pending publish cannot be submitted twice or unlocked by presentation updates', async () => {
  const {run, nodes} = setup();
  run(`
    var writes = 0, finish;
    fetch = () => { writes++; return new Promise(resolve => { finish = () => resolve({ok:true,json:async()=>({status:'success'})}); }); };
    var pending = publishSelectedAiOriginalProducts();
    publishSelectedAiOriginalProducts();
    syncAiOriginalWorkbenchSelection();
  `);
  assert.equal(run('writes'), 1);
  assert.equal(nodes.get('ai-original-publish').disabled, true);
  await run('finish(); pending');
  assert.equal(run('aiOriginalSubmitting'), false);
});

test('status filtering removes hidden selections, including after switching back', () => {
  const {run, nodes} = setup();
  run(`aiOriginalRows[1].ai_status = 'pending';
    document.getElementById('ai-original-status-filter').value = 'approved';
    renderAiOriginalProducts();`);
  assert.deepEqual(Array.from(run('[...aiOriginalSelected]')), [1]);
  assert.equal(nodes.get('ai-original-selection').textContent, '当前列表已选择 1 件');
  run(`document.getElementById('ai-original-status-filter').value = ''; renderAiOriginalProducts()`);
  assert.deepEqual(Array.from(run('[...aiOriginalSelected]')), [1]);
});

test('bulk approval saves current inputs and review status atomically', async () => {
  const {run, requests} = setup();
  run(`aiOriginalSelected = new Set([1]);
    aiOriginalRows[0].weight_g = null;
    aiOriginalRows[0].net_proceeds_usd = null;
    document.getElementById('ai-original-weight-1').value = '250';
    document.getElementById('ai-original-net-1').value = '3.25';
    document.getElementById('ai-original-review-status').value = 'approved';
    loadAiOriginalProducts = async () => {};`);
  await run('updateSelectedAiOriginalReviewStatus()');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/ai-original-products/1');
  assert.deepEqual(requests[0].body, {weight_g: 250, net_proceeds_usd: 3.25, category_id: '', review_status: 'approved'});
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
  assert.match(nodes.get('ai-original-task-status').textContent, /失败 1 件：产品 1：保存失败/);
  assert.equal(run('aiOriginalReviewRunning'), false);
});

test('five page states do not alter the AI generation result', () => {
  const {run} = setup();
  assert.equal(run('aiOriginalProductStatus(aiOriginalRows[0])'), 'approved');
  assert.equal(run('aiOriginalProductStatus(aiOriginalRows[1])'), 'unreviewed');
  assert.equal(run('aiOriginalProductStatus(aiOriginalRows[2])'), 'incomplete');
  for (const field of ['package_length_cm', 'package_width_cm', 'package_height_cm', 'weight_g', 'net_proceeds_usd']) {
    run(`var original = aiOriginalRows[0]; var changed = {...original, ${field}: null};`);
    assert.equal(run('aiOriginalProductStatus(changed)'), 'approved', field);
    assert.equal(run('aiOriginalProductStatus({...changed, review_status:"unreviewed"})'), 'incomplete', field);
    assert.equal(run('aiOriginalPlatformStatus(changed)'), 'completed', field);
    assert.equal(run('aiOriginalListingChecks(changed).every(check => check.ok)'), true, field);
  }
  assert.equal(run(`aiOriginalProductStatus({...original, ai_status:'failed', weight_g:null, review_status:'unreviewed'})`), 'failed');
  assert.equal(run(`aiOriginalProductStatus({...original, ai_status:'processing', review_status:'unreviewed'})`), 'unreviewed');
  for (const review of ['suspected', 'infringing', 'risk']) {
    assert.equal(run(`aiOriginalProductStatus({...original, ai_status:'failed', review_status:'${review}'})`), 'suspected');
  }
});

test('counts, filter and selection agree on the same five statuses', () => {
  const {run, nodes} = setup();
  run(`aiOriginalRows.push({...aiOriginalRows[0], id:4, ai_status:'failed', review_status:'unreviewed'},
      {...aiOriginalRows[0], id:5, review_status:'suspected'});
    decorateAiOriginalWorkbench();`);
  for (const status of ['approved', 'unreviewed', 'failed', 'incomplete', 'suspected']) {
    assert.equal(nodes.get('ai-original-count-' + status).textContent, 1);
    run(`document.getElementById('ai-original-status-filter').value = '${status}';
      aiOriginalSelected.clear(); toggleAllAiOriginalProducts(true);`);
    assert.equal(run('aiOriginalSelected.size'), 1);
    assert.equal(run('aiOriginalProductStatus(aiOriginalRows.find(row => aiOriginalSelected.has(row.id)))'), status);
  }
});
