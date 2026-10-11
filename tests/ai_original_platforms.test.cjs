const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(platform = 'amazon') {
  const nodes = new Map();
  const requests = [];
  const context = vm.createContext({
    document: {
      addEventListener() {}, querySelectorAll() { return []; },
      getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, {value: '', selectedOptions: [], showModal() {}, close() {}});
        return nodes.get(id);
      },
    },
    window: {confirm() { throw new Error('Unexpected publishing confirmation'); }},
    localStorage: {getItem() { return null; }, removeItem() {}},
    fetch: async (url, options = {}) => {
      requests.push({url, body: options.body ? JSON.parse(options.body) : null});
      return {ok: true, json: async () => ({status: 'success', data: {rows: []}})};
    },
  });
  const run = code => vm.runInContext(code, context);
  for (const file of ['ai-original-products.js', 'ai-original-workbench.js']) {
    run(fs.readFileSync(path.join(__dirname, '../bit/static', file), 'utf8'));
  }
  run(`document.getElementById('ai-original-platform').value = ${JSON.stringify(platform)};
    aiOriginalRows = [{id: 1, category_id: 'CBT123', title: 'Source title', ai_status: 'completed', review_status: 'approved',
      title_es: 'Mercado only text', title_pt: 'Mercado only PT', main_image_url: '/mercado-only.jpg',
      ai_original: {main_image_url: '/mercado-only.jpg'}, marketplace_listings: {}}];
    aiOriginalSelected = new Set([1]);`);
  return {run, nodes, requests};
}

test('a completed Mercado listing is pending on Amazon, without Mercado copy or image fallback', () => {
  const {run, nodes} = setup();
  assert.equal(run('aiOriginalPlatformStatus(aiOriginalRows[0])'), 'pending');
  assert.equal(run('aiOriginalListingReady(aiOriginalRows[0])'), false);
  run('renderAiOriginalProducts(); decorateAiOriginalWorkbench()');
  const markup = nodes.get('ai-original-grid').innerHTML;
  assert.match(markup, /本平台尚无草稿/);
  assert.doesNotMatch(markup, /Mercado only|mercado-only.jpg|净收益|CBT 类目/);
  assert.equal(nodes.get('ai-original-count-approved').textContent, 0);
  assert.equal(nodes.get('ai-original-count-unreviewed').textContent, 1);
});

test('explicitly cleared source net stays blank while a later manually saved AI net is shown', () => {
  const {run, nodes} = setup('mercadolibre');
  run(`aiOriginalRows[0].net_proceeds_usd = null;
    aiOriginalRows[0].suggested_net_proceeds_usd = 12;
    aiOriginalRows[0].source_1688_overrides = {net_proceeds_usd: null};
    renderAiOriginalProducts();`);
  assert.match(nodes.get('ai-original-grid').innerHTML, /id="ai-original-net-1"[^>]*value=""/);
  run('aiOriginalRows[0].net_proceeds_usd = 15; renderAiOriginalProducts();');
  assert.match(nodes.get('ai-original-grid').innerHTML, /id="ai-original-net-1"[^>]*value="15"/);
});

test('platform status filtering and select all use independent draft statuses', () => {
  const {run} = setup('yandex');
  run(`aiOriginalRows.push({id: 2, ai_status: 'pending', marketplace_listings: {yandex: {status:'completed',title:'Товар'}}});
    aiOriginalSelected.clear();
    document.getElementById('ai-original-status-filter').value = 'failed';
    toggleAllAiOriginalProducts(true);`);
  assert.deepEqual(Array.from(run('[...aiOriginalSelected]')), [2]);
  assert.equal(run('aiOriginalPlatformStatus(aiOriginalRows[0], "mercadolibre")'), 'completed');
});

test('generation sends chosen platform and template and excludes Mercado store token', async () => {
  const {run, requests} = setup();
  run(`document.getElementById('ai-original-template').value = 'features';
    document.getElementById('ai-original-stores').selectedOptions = [{value:'77'}];
    loadAiOriginalTaskStatus = async () => {};`);
  await run('processSelectedAiOriginalProducts()');
  assert.equal(requests[0].url, '/api/ai-original-products/process');
  assert.equal(requests[0].body.target_platform, 'amazon');
  assert.equal(requests[0].body.template_id, 'features');
  assert.equal(requests[0].body.token_id, '');
});

test('Mercado publishing, approval, translation and parameter mutations are guarded off-platform', async () => {
  const {run, nodes, requests} = setup('yandex');
  run('updateAiOriginalSelection(); syncAiOriginalWorkbenchSelection()');
  assert.equal(nodes.get('ai-original-publish').disabled, true);
  assert.equal(nodes.get('ai-original-review-submit').disabled, true);
  await run('Promise.all([publishSelectedAiOriginalProducts(), updateSelectedAiOriginalReviewStatus(), saveAiOriginalProduct(1), translateAiOriginalEditor("es")])');
  assert.equal(requests.length, 0);
});

test('Amazon editor uses current limits and PATCH preserves target platform and Item Highlights', async () => {
  const {run, nodes, requests} = setup();
  run(`aiOriginalRows[0].marketplace_listings.amazon = {status:'completed', title:'Lamp', description:'Metal lamp', bullet_points:['Metal body'], item_highlights:'Adjustable shade'};
    loadAiOriginalProducts = async () => {};
    openAiOriginalPlatformEditor(1);`);
  assert.equal(nodes.get('ai-original-platform-editor-title').maxLength, 75);
  assert.equal(nodes.get('ai-original-platform-editor-highlights').maxLength, 125);
  assert.equal(nodes.get('ai-original-platform-editor-highlights-field').hidden, false);
  nodes.get('ai-original-platform-editor-bullets').value = ' First point \n\n Second point ';
  await run('saveAiOriginalPlatformEditor()');
  assert.deepEqual(requests[0].body, {target_platform:'amazon', title:'Lamp', description:'Metal lamp', bullet_points:['First point', 'Second point'], item_highlights:'Adjustable shade'});
  assert.equal(requests[0].url, '/api/ai-original-products/1/listing');
});

test('switching platforms clears selection and includes platform in backend businessperson filter', async () => {
  const {run, requests} = setup('yandex');
  run(`document.getElementById('ai-original-generator-filter').value = '业务员甲';`);
  await run('changeAiOriginalPlatform()');
  assert.equal(run('aiOriginalSelected.size'), 0);
  assert.match(requests[0].url, /target_platform=yandex/);
  assert.match(requests[0].url, /generator_salesperson=/);
});

test('platform rules show readable language and safely escape validation content', () => {
  const {run, nodes} = setup();
  run(`aiOriginalProfiles.platforms[2].language = 'en_US';
    aiOriginalProfiles.platforms[2].language_label = '英语';
    aiOriginalRows[0].marketplace_listings.amazon = {status:'completed',title:'Lamp', validation:{errors:[{message:'<script>alert(1)</script>'}],warnings:['Check brand']}};
    renderAiOriginalPlatformSettings(); renderAiOriginalProducts();`);
  assert.match(nodes.get('ai-original-platform-rules').innerHTML, /英语/);
  assert.doesNotMatch(nodes.get('ai-original-platform-rules').innerHTML, /en_US/);
  assert.match(nodes.get('ai-original-grid').innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(nodes.get('ai-original-grid').innerHTML, /<script>/);
});

test('manual checklist distinguishes suggested or edited net from persisted net and updates on input', () => {
  const {run} = setup('mercadolibre');
  run(`
    aiOriginalRows[0].net_proceeds_usd = null;
    const netInput = {id: 'ai-original-net-1', value: '12', classList: {toggle() {}},
      setAttribute() {}, removeAttribute() {}};
    const manualList = {innerHTML: ''};
    const card = {querySelector(selector) {
      if (selector === '#ai-original-net-1') return netInput;
      if (selector === '.ai-original-manual-checks') return manualList;
      return null;
    }};
    netInput.closest = selector => selector === "label" ? null : card;
    updateAiOriginalManualChecks({target: netInput});
  `);
  assert.match(run('manualList.innerHTML'), /净收益<small>已填写待保存/);
  assert.equal(run('aiOriginalManualListingChecks(aiOriginalRows[0]).find(c => c.label === "净收益").ok'), false);
  run('aiOriginalRows[0].net_proceeds_usd = 12; updateAiOriginalManualChecks({target: netInput})');
  assert.match(run('manualList.innerHTML'), /净收益<small>已填写<\/small>/);
  run('netInput.value = "15"; updateAiOriginalManualChecks({target: netInput})');
  assert.match(run('manualList.innerHTML'), /净收益<small>已填写待保存/);
  for (const value of ['', '0', '-1', 'invalid']) {
    run(`netInput.value = ${JSON.stringify(value)}; updateAiOriginalManualChecks({target: netInput})`);
    assert.match(run('manualList.innerHTML'), /净收益<small>待补齐/);
  }
  assert.equal(run('aiOriginalRows[0].net_proceeds_usd'), 12);
});

test('parameter save rejects stale net or incomplete approval in the server readback', async () => {
  for (const saved of [
    {id: 1, net_proceeds_usd: 99, review_status: 'approved'},
    {id: 1, net_proceeds_usd: 14.75, review_status: 'unreviewed'},
    {id: 2, net_proceeds_usd: 14.75, review_status: 'approved'},
  ]) {
    const {run, nodes} = setup('mercadolibre');
    run(`aiOriginalRows[0].net_proceeds_usd = 10;
      document.getElementById('ai-original-weight-1').value = '100';
      document.getElementById('ai-original-net-1').value = '14.75';
      fetch = async () => ({ok:true, json: async () => ({status:'success', data:${JSON.stringify(saved)}})});
      loadAiOriginalProducts = async () => {throw new Error('Unexpected refresh after unverified save');};`);
    await run('saveAiOriginalProduct(1)');
    assert.match(nodes.get('ai-original-task-status').textContent, /保存失败：保存后回读的净收益、实重或审核状态不一致/);
    assert.equal(run('aiOriginalRows[0].net_proceeds_usd'), 10);
    assert.equal(nodes.get('ai-original-net-1').value, '14.75');
  }
});
