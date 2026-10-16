const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup() {
  const nodes = new Map();
  const requests = [];
  const context = vm.createContext({
    AbortController,
    currentWorkbenchUser: {role_key: 'super_admin'},
    document: {addEventListener() {}, getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, {value: '', textContent: ''});
      return nodes.get(id);
    }},
    localStorage: {getItem() {return null;}},
    fetch(url, options) {return new Promise(resolve => requests.push({url, options, resolve}));}
  });
  const run = code => vm.runInContext(code, context);
  run(fs.readFileSync('bit/static/ai-original-products.js', 'utf8'));
  run(`let renders = 0; renderAiOriginalProducts = () => {renders++; updateAiOriginalTotal();};
    updateAiOriginalSelection = () => {}; aiOriginalStatus = () => {}; renderAiOriginalFailureLog = () => {};`);
  const reply = (index, data) => requests[index].resolve({ok: true, json: async () => ({status: 'success', data})});
  return {run, requests, reply, nodes};
}

test('products display before cost statistics finish; stats do not rebuild editable cards', async () => {
  const {run, requests, reply, nodes} = setup();
  const loading = run('loadAiOriginalProducts()');
  assert.match(requests[0].url, /include_cost_summary=0/);
  reply(0, {rows: [{id: 1}]});
  await loading;
  assert.equal(run('renders'), 1);
  assert.equal(run('aiOriginalRows.length'), 1);
  assert.match(requests[1].url, /include_generator_options=1/);
  reply(1, {generator_salespeople: []});
  await new Promise(setImmediate);
  assert.match(requests[2].url, /cost-summary$/);
  reply(2, {spent_yuan: 1, recorded_products: 1, total_products: 1});
  await new Promise(setImmediate);
  assert.equal(run('renders'), 1);
  assert.match(nodes.get('ai-original-total').textContent, /已记录 1\/1/);
});

test('new list request aborts old work and ignores stale cost responses', async () => {
  const {run, requests, reply} = setup();
  const first = run('loadAiOriginalProducts()');
  reply(0, {rows: [{id: 1}]});
  await first;
  reply(1, {generator_salespeople: []});
  await new Promise(setImmediate);
  const second = run('loadAiOriginalProducts()');
  assert.equal(requests[0].options.signal.aborted, true);
  assert.equal(requests[1].options.signal.aborted, true);
  reply(3, {rows: [{id: 2}]});
  await second;
  reply(2, {spent_yuan: 999});
  await new Promise(setImmediate);
  assert.equal(run('aiOriginalTotalCost'), null);
  assert.equal(run('aiOriginalRows[0].id'), 2);
  reply(4, {generator_salespeople: []});
  await new Promise(setImmediate);
  reply(5, {spent_yuan: 2});
  await new Promise(setImmediate);
  assert.equal(run('aiOriginalTotalCost.spent_yuan'), 2);
});

test('a confirmed review save cannot be overwritten by an older list request', async () => {
  const {run, requests, reply, nodes} = setup();
  run(`aiOriginalRows = [{id:1, ai_status:'completed', review_status:'suspected',
    weight_g:100, weight_basis:'ai_original_manual', net_proceeds_usd:5,
    package_length_cm:10, package_width_cm:10, package_height_cm:10,
    category_id:'CBT123', title_es:'Producto', title_pt:'Produto',
    description_es:'Descripción', description_pt:'Descrição',
    main_image_url:'/product-ai-white.jpg', ai_original:{
      status:'completed', category_id:'CBT123', image_generation_method:'ai_image_edit',
      attributes:[{id:'MATERIAL', value_name:'Metal'}], validation:{errors:[],warnings:[]}}}];`);
  nodes.set('ai-original-weight-1', {value:'100'});
  nodes.set('ai-original-net-1', {value:'15'});
  nodes.set('ai-original-length-1', {value:'10'});
  nodes.set('ai-original-width-1', {value:'10'});
  nodes.set('ai-original-height-1', {value:'10'});
  nodes.set('ai-original-category-1', {value:'CBT123'});
  const loading = run('loadAiOriginalProducts()');
  const saving = run('saveAiOriginalProduct(1, true)');
  reply(1, {id:1, ai_status:'completed', review_status:'unreviewed', weight_g:100,
    net_proceeds_usd:15, package_length_cm:10, package_width_cm:10,
    package_height_cm:10, category_id:'CBT123'});
  await new Promise(setImmediate);
  reply(2, {id:1, ai_status:'completed', review_status:'approved', weight_g:100,
    net_proceeds_usd:15, package_length_cm:10, package_width_cm:10,
    package_height_cm:10, category_id:'CBT123'});
  await saving;
  assert.equal(requests[0].options.signal.aborted, true);
  reply(0, {rows:[{id:1, ai_status:'completed', review_status:'suspected', net_proceeds_usd:5}]});
  await loading;
  assert.equal(run('aiOriginalRows[0].review_status'), 'approved');
  assert.equal(run('aiOriginalRows[0].net_proceeds_usd'), 15);
  assert.equal(run('renders'), 1);
  assert.equal(requests.length, 3);
});


test('loads only 50 initially and requests each subsequent batch on next page', async () => {
  const {run, requests, reply} = setup();
  const loading = run('loadAiOriginalProducts()');
  assert.match(requests[0].url, /limit=50&include_generator_options=0/);
  reply(0, {rows: Array.from({length:50}, (_, i) => ({id:i+1})), has_more:true});
  await loading;
  assert.equal(run('aiOriginalRows.length'), 50);
  assert.equal(requests.length, 2);
  assert.match(requests[1].url, /include_generator_options=1/);
  const next = run('changeAiOriginalPage(1)');
  assert.match(requests[2].url, /limit=50&offset=50&include_generator_options=0/);
  await run('changeAiOriginalPage(1)');
  assert.equal(requests.length, 3);
  reply(2, {rows:[{id:51}], has_more:true});
  await next;
  assert.equal(run('aiOriginalPage'), 2);
  assert.equal(run('aiOriginalRows.length'), 51);
  assert.equal(requests.length, 3);
});

test('old next-page response cannot enter a new search result', async () => {
  const {run, requests, reply} = setup();
  const first = run('loadAiOriginalProducts()');
  reply(0, {rows: [{id:500}], has_more:true});
  await first;
  const next = run('changeAiOriginalPage(1)');
  const second = run('loadAiOriginalProducts()');
  reply(3, {rows: [{id:1}], has_more:false});
  await second;
  reply(2, {rows: [{id:499}]});
  await next;
  assert.equal(run('aiOriginalRows.length'), 1);
  assert.equal(run('aiOriginalRows[0].id'), 1);
  assert.equal(run('aiOriginalPage'), 1);
});

test('historical completed task does not show errors or reload products', async () => {
  const {run, requests, reply} = setup();
  run('let taskRenders = 0; renderAiOriginalTaskStatus = () => {taskRenders++;};');
  const loading = run('loadAiOriginalTaskStatus()');
  reply(0, {status:'error', running:false, message:'old failure'});
  await loading;
  assert.equal(run('taskRenders'), 0);
  assert.equal(requests.length, 1);
});
