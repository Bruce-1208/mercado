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
    clearTimeout() {}, setTimeout() { return 0; },
    document: {
      addEventListener() {}, querySelectorAll() { return []; },
      getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, {value: '', selectedOptions: []});
        return nodes.get(id);
      },
    },
    window: {catalogOperationMarkup(_row, markup) { return markup; }, confirm(message) { confirmations.push(message); return true; }},
    fetch: async (url, options) => {
      requests.push({url, body: JSON.parse(options.body)});
      return {ok: true, json: async () => ({status: 'success', data: {
        id: Number(url.split('/').pop()), ...JSON.parse(options.body),
      }})};
    },
  });
  const run = code => vm.runInContext(code, context);
  run(fs.readFileSync(path.join(__dirname, '../bit/static/sku-matrix.js'), 'utf8'));
  run('globalThis.ZeshunSku = window.ZeshunSku');
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
        category_id: 'CBT123', attributes: [{id: 'MATERIAL', value_name: 'Plastic'}], image_generation_method: 'ai_image_edit',
        validation: {errors: [], warnings: []}
      }};
    aiOriginalRows = [ready, {...ready, id: 2, review_status: 'unreviewed'},
      {...ready, id: 3, review_status: 'unreviewed', net_proceeds_usd: null}];
    aiOriginalSelected = new Set([1, 2, 3]);
    aiOriginalSelectedSiteIds = () => ['MLM'];
    loadAiOriginalPublishStatus = () => {};
    document.getElementById('ai-original-stores').selectedOptions = [{value: '7'}];
    for (const row of aiOriginalRows) {
      for (const [prefix,field] of [['weight','weight_g'],['net','net_proceeds_usd'],['length','package_length_cm'],['width','package_width_cm'],['height','package_height_cm'],['category','category_id']])
        document.getElementById('ai-original-'+prefix+'-'+row.id).value=String(row[field] ?? '');
    }
  `);
  return {run, nodes, requests, confirmations};
}

test('unattempted categories are not errors and invalid completed categories can retry', () => {
  const {run} = setup();
  run(`globalThis.pendingCategory = {...ready, category_id:'', ai_status:'pending', ai_original:{}};
       globalThis.badCategory = {...ready, category_id:'', ai_status:'completed'};`);
  assert.equal(run('aiOriginalPlatformStatus(pendingCategory)'), 'pending');
  assert.equal(run('aiOriginalMissingCheckMessages(pendingCategory, aiOriginalListingChecks(pendingCategory)).length'), 0);
  assert.equal(run('aiOriginalPlatformStatus(badCategory)'), 'failed');
  assert.equal(run('aiOriginalProductStatus(badCategory)'), 'failed');
  assert.match(run('aiOriginalMissingCheckMessages(badCategory, aiOriginalListingChecks(badCategory)).join()'), /美客多类目生成失败，可重新生成/);
});

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

test('bulk approval saves inputs as pending review before the separate manual approval', async () => {
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
  assert.deepEqual(requests[0].body, {weight_g: 250, net_proceeds_usd: 3.25, review_status: 'unreviewed'});
  assert.deepEqual(requests[1].body, {review_status: 'approved'});
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

test('listing check failures do not alter the AI generation result', () => {
  const {run} = setup();
  assert.equal(run('aiOriginalProductStatus(aiOriginalRows[0])'), 'approved');
  assert.equal(run('aiOriginalProductStatus(aiOriginalRows[1])'), 'unreviewed');
  assert.equal(run('aiOriginalProductStatus(aiOriginalRows[2])'), 'incomplete');
  for (const field of ['package_length_cm', 'package_width_cm', 'package_height_cm', 'weight_g', 'net_proceeds_usd']) {
    run(`var original = aiOriginalRows[0]; var changed = {...original, ${field}: null};`);
    assert.equal(run('aiOriginalProductStatus(changed)'), 'incomplete', field);
    assert.equal(run('aiOriginalProductStatus({...changed, review_status:"unreviewed"})'), 'incomplete', field);
    assert.equal(run('aiOriginalPlatformStatus(changed)'), 'completed', field);
    assert.equal(run('aiOriginalListingChecks(changed).every(check => check.ok)'), true, field);
  }
  assert.equal(run(`aiOriginalProductStatus({...original, ai_status:'failed', weight_g:null, review_status:'unreviewed'})`), 'failed');
  assert.equal(run(`aiOriginalProductStatus({...original, ai_status:'processing', review_status:'unreviewed'})`), 'processing');
  for (const review of ['suspected', 'infringing', 'risk']) {
    assert.equal(run(`aiOriginalProductStatus({...original, ai_status:'failed', review_status:'${review}'})`), 'suspected');
  }
});

test('counts, filter and selection agree on the same listing statuses', () => {
  const {run, nodes} = setup();
  run(`aiOriginalRows[2].net_proceeds_usd = 2; aiOriginalRows[2].weight_g = null;
    aiOriginalRows.push({...aiOriginalRows[0], id:4, ai_status:'failed', review_status:'unreviewed'},
      {...aiOriginalRows[0], id:5, review_status:'suspected'});
    decorateAiOriginalWorkbench();`);
  for (const status of ['approved', 'unreviewed', 'incomplete', 'failed', 'suspected']) {
    assert.equal(nodes.get('ai-original-count-' + status).textContent, 1);
    run(`document.getElementById('ai-original-status-filter').value = '${status}';
      aiOriginalSelected.clear(); toggleAllAiOriginalProducts(true);`);
    assert.equal(run('aiOriginalSelected.size'), 1);
    assert.equal(run('aiOriginalProductStatus(aiOriginalRows.find(row => aiOriginalSelected.has(row.id)))'), status);
  }
});


test('generated content omissions are failures even after approval and with missing manual values', () => {
  const {run} = setup();
  for (const field of ['category_id', 'title_es', 'title_pt', 'description_es', 'description_pt', 'main_image_url']) {
    assert.equal(run(`aiOriginalProductStatus({...aiOriginalRows[0], ${field}: '', weight_g: null})`), 'failed', field);
  }
  assert.equal(run(`aiOriginalProductStatus({...aiOriginalRows[0], ai_original: {...aiOriginalRows[0].ai_original, attributes: []}})`), 'failed');
  assert.equal(run(`aiOriginalProductStatus({...aiOriginalRows[0], ai_original: {...aiOriginalRows[0].ai_original, missing_required_attributes:[{id:'GENDER'}]}})`), 'failed');
  assert.equal(run(`aiOriginalProductStatus({...aiOriginalRows[0], ai_original: {...aiOriginalRows[0].ai_original, category_id:'CBT999'}})`), 'failed');
  assert.equal(run(`aiOriginalProductStatus({...aiOriginalRows[0], ai_original: {...aiOriginalRows[0].ai_original, preparation:{required_ready:false}}})`), 'failed');
  assert.equal(run(`aiOriginalProductStatus({...aiOriginalRows[0], ai_original: {...aiOriginalRows[0].ai_original, validation:undefined}})`), 'failed');
  assert.equal(run(`aiOriginalProductStatus({...aiOriginalRows[0], ai_status:'failed'})`), 'failed');
});

test('generated attributes require values and accept populated multivalue attributes', () => {
  const {run} = setup();
  assert.equal(run(`aiOriginalProductStatus({...ready, review_status:'unreviewed',
    ai_original:{...ready.ai_original, attributes:[{id:'MATERIAL'}]}})`), 'failed');
  assert.equal(run(`aiOriginalProductStatus({...ready, review_status:'unreviewed',
    ai_original:{...ready.ai_original, attributes:[{id:'COLOR', values:['Red']}],
      missing_required_attributes:[{id:'COLOR'}]}})`), 'unreviewed');
});

test('target-platform cards require shared manual facts and keep generation failures failed', () => {
  const {run} = setup();
  run(`var amazonReady = {...ready, marketplace_listings:{amazon:{status:'completed', review_status:'approved',
    title:'Desktop organizer', description:'Storage for a desktop.', main_image_url:'https://cdn.example.com/a.jpg',
    validation:{errors:[]}}}};`);
  assert.equal(run(`aiOriginalProductStatus({...amazonReady, weight_g:null}, 'amazon')`), 'incomplete');
  assert.equal(run(`aiOriginalProductStatus(amazonReady, 'amazon')`), 'approved');
  assert.equal(run(`aiOriginalProductStatus({...amazonReady,
    marketplace_listings:{amazon:{...amazonReady.marketplace_listings.amazon, status:'failed'}}}, 'amazon')`), 'failed');
});

test('legacy manual-field failures move from pending completion to review after facts are filled', async () => {
  const {run, requests} = setup();
  run(`var legacyManualFailure = {...ready, ai_status:'failed', review_status:'unreviewed', weight_g:null,
    ai_original:{...ready.ai_original, status:'failed', failed_step:'manual_fields'}};`);
  assert.equal(run('aiOriginalProductStatus(legacyManualFailure)'), 'incomplete');
  assert.equal(run('aiOriginalProductStatus({...legacyManualFailure, weight_g:100})'), 'unreviewed');
  run(`aiOriginalRows = [{...legacyManualFailure, weight_g:100}]; renderAiOriginalProducts = () => {};
    document.getElementById('ai-original-review-1').value='approved';`);
  await run('updateAiOriginalReviewStatus(1)');
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].body, {review_status:'approved'});
});

test('unattempted and running rows keep their status; saved facts recover without regenerating', () => {
  const {run} = setup();
  for (const status of ['pending', 'processing', 'waiting_off_peak']) {
    assert.equal(run(`aiOriginalProductStatus({...ready, ai_status:'${status}', weight_g:null, title_es:''})`), status);
  }
  run(`var repaired = {...ready, weight_g:null}; var before = JSON.stringify(repaired);`);
  assert.equal(run('aiOriginalProductStatus(repaired)'), 'incomplete');
  assert.equal(run('JSON.stringify(repaired) === before'), true);
  assert.equal(run('aiOriginalProductStatus({...repaired, weight_g:100})'), 'approved');
  assert.equal(run('aiOriginalProductStatus({...repaired, review_status:"suspected"})'), 'suspected');
  assert.equal(run('aiOriginalProductStatus({...repaired, weight_g:100, review_status:"suspected"})'), 'suspected');
});


test('unfinished or missing SKU mappings cannot be pending manual fields', () => {
  const {run} = setup();
  assert.equal(run(`aiOriginalProductStatus({...aiOriginalRows[0], weight_g:null,
    original_1688:{variations:[{id:'one'}]}})`), 'failed');
  assert.equal(run(`aiOriginalProductStatus({...aiOriginalRows[0], weight_g:null,
    ai_original:{...aiOriginalRows[0].ai_original, preparation:{variations_ready:false}}})`), 'failed');
});

test('saving complete parameters waits for a separate manual approval', async () => {
  const {run, requests, nodes} = setup();
  run(`aiOriginalRows = [{...ready, review_status:'unreviewed'}];
    renderAiOriginalProducts = () => {};
    for (const [prefix, value] of Object.entries({weight:100,net:2,length:10,width:10,height:10,category:'CBT123'}))
      document.getElementById('ai-original-'+prefix+'-1').value=String(value);`);
  await run('saveAiOriginalProduct(1)');
  assert.equal(requests[0].body.review_status, 'unreviewed');
  assert.equal(run('aiOriginalProductStatus(aiOriginalRows[0])'), 'unreviewed');
  nodes.get('ai-original-review-1');
  run(`document.getElementById('ai-original-review-1').value='approved'`);
  await run('updateAiOriginalReviewStatus(1)');
  assert.equal(requests[1].body.review_status, 'approved');
  assert.equal(run('aiOriginalProductStatus(aiOriginalRows[0])'), 'approved');
  assert.equal(run('aiOriginalSelected.size'), 3);
});

test('unchanged Mercado approval preserves source weight basis and other platform approvals', async () => {
  const {run, requests} = setup();
  run(`aiOriginalRows = [{...ready, review_status:'unreviewed', weight_basis:'1688_source',
    marketplace_listings:{amazon:{status:'completed', review_status:'approved'}}}];
    renderAiOriginalProducts = () => {};
    document.getElementById('ai-original-review-1').value='approved';`);
  await run('updateAiOriginalReviewStatus(1)');
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].body, {review_status:'approved'});
  assert.equal(run('aiOriginalRows[0].weight_basis'), '1688_source');
  assert.equal(run('aiOriginalRows[0].marketplace_listings.amazon.review_status'), 'approved');
});

test('manual review changes only one product and rejects missing facts or unauthorized rows', async () => {
  const {run, requests} = setup();
  run(`renderAiOriginalProducts = () => {};
    document.getElementById('ai-original-review-3').value='approved';`);
  await run('updateAiOriginalReviewStatus(3)');
  assert.equal(requests.length, 0);
  run(`document.getElementById('ai-original-review-1').value='suspected'`);
  await run('updateAiOriginalReviewStatus(1)');
  assert.equal(requests.length, 1);
  assert.equal(run('aiOriginalProductStatus(aiOriginalRows[0])'), 'suspected');
  assert.equal(run('aiOriginalRows[1].review_status'), 'unreviewed');
  run(`aiOriginalRows[0].can_operate=false;
    document.getElementById('ai-original-review-1').value='failed'`);
  await run('updateAiOriginalReviewStatus(1)');
  assert.equal(requests.length, 1);
});
