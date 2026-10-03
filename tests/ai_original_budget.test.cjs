const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(platform = 'mercadolibre') {
  const nodes = new Map();
  const requests = [];
  const timers = [];
  let responseData = {status: 'idle', running: false};
  const context = vm.createContext({
    document: {
      addEventListener() {}, querySelectorAll() { return []; },
      getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, {value: '', checked: false, selectedOptions: []});
        return nodes.get(id);
      },
    },
    window: {location: {origin: 'http://localhost'}},
    localStorage: {getItem() { return null; }},
    setTimeout(callback, delay) { timers.push({callback, delay}); return timers.length; },
    clearTimeout() {},
    fetch: async (url, options = {}) => {
      requests.push({url, body: options.body ? JSON.parse(options.body) : null});
      const payload = {status: 'success', data: responseData};
      return {ok: true, json: async () => payload, text: async () => JSON.stringify(payload)};
    },
  });
  const run = code => vm.runInContext(code, context);
  run(fs.readFileSync(path.join(__dirname, '../bit/static/ai-original-products.js'), 'utf8'));
  run(`document.getElementById('ai-original-platform').value = ${JSON.stringify(platform)};
    aiOriginalRows = [{id: 1, title: 'Source', ai_status: 'pending', original_1688: {}, ai_original: {}}];
    aiOriginalSelected = new Set([1]);`);
  return {run, nodes, requests, timers, setResponse(value) { responseData = value; }};
}

test('Mercado enforces Flash official endpoint and excludes stale paid-image fields', async () => {
  const {run, requests} = setup();
  run(`document.getElementById('ai-original-model').value = 'deepseek-v4-pro';
    document.getElementById('ai-original-base-url').value = 'https://other.example';
    document.getElementById('ai-original-image-model').value = 'paid-model';
    document.getElementById('ai-original-image-base-url').value = 'https://images.example';
    document.getElementById('ai-original-image-api-key').value = 'stale-key';
    loadAiOriginalTaskStatus = async () => {};`);
  await run('processSelectedAiOriginalProducts()');
  assert.equal(requests[0].body.model, 'deepseek-flash');
  assert.equal(requests[0].body.base_url, 'https://api.deepseek.com');
  for (const field of ['image_model', 'image_base_url', 'image_api_key']) assert.equal(requests[0].body[field], '');
});

test('Mercado locks settings while other-platform values survive a platform round trip', () => {
  const {run, nodes} = setup();
  run('renderAiOriginalPlatformSettings()');
  assert.equal(nodes.get('ai-original-model').value, 'deepseek-flash');
  assert.equal(nodes.get('ai-original-model').readOnly, true);
  assert.equal(nodes.get('ai-original-image-model').disabled, true);
  run(`document.getElementById('ai-original-platform').value = 'amazon'; renderAiOriginalPlatformSettings();`);
  assert.equal(nodes.get('ai-original-model').value, 'deepseek-v4-pro');
  assert.equal(nodes.get('ai-original-model').readOnly, false);
  run(`document.getElementById('ai-original-model').value = 'custom-model';
    document.getElementById('ai-original-base-url').value = 'https://other.example';
    document.getElementById('ai-original-platform').value = 'mercadolibre'; renderAiOriginalPlatformSettings();
    document.getElementById('ai-original-platform').value = 'yandex'; renderAiOriginalPlatformSettings();`);
  assert.equal(nodes.get('ai-original-model').value, 'custom-model');
  assert.equal(nodes.get('ai-original-base-url').value, 'https://other.example');
  assert.equal(nodes.get('ai-original-image-model').disabled, false);
});

test('non-Mercado payload preserves custom model and paid image settings', async () => {
  const {run, requests} = setup('amazon');
  run(`document.getElementById('ai-original-model').value = 'custom-model';
    document.getElementById('ai-original-base-url').value = 'https://other.example';
    document.getElementById('ai-original-image-model').value = 'paid-model';
    loadAiOriginalTaskStatus = async () => {};`);
  await run('processSelectedAiOriginalProducts()');
  assert.equal(requests[0].body.model, 'custom-model');
  assert.equal(requests[0].body.base_url, 'https://other.example');
  assert.equal(requests[0].body.image_model, 'paid-model');
});

test('budget displays estimates and unresolved reserves separately without inventing legacy costs', () => {
  const {run, nodes} = setup();
  assert.equal(run('aiOriginalBudgetSummary(undefined)'), '');
  assert.equal(run('aiOriginalBudgetSummary({})'), '');
  assert.equal(run('aiOriginalBudgetSummary({cap_yuan: 0.03, spent_yuan: null})'), '');
  run(`aiOriginalRows[0].ai_original.generation_budget = {cap_yuan: 0.03, spent_yuan: 0.01425, reserved_yuan: 0.005, cache_hits: 2};
    renderAiOriginalProducts();`);
  assert.match(nodes.get('ai-original-grid').innerHTML, /模型计费估算 ¥0.01425（含已记录尝试）/);
  assert.match(nodes.get('ai-original-grid').innerHTML, /待核对预留 ¥0.005/);
  assert.match(nodes.get('ai-original-grid').innerHTML, /复用结果 2 次/);
  run('delete aiOriginalRows[0].ai_original.generation_budget; renderAiOriginalProducts();');
  assert.doesNotMatch(nodes.get('ai-original-grid').innerHTML, /模型计费估算/);
});

test('blocked budget recovery keeps saved work and advises manual completion instead of blind retries', () => {
  const {run} = setup();
  const message = run('aiOriginalRecoveryMessage({ai_original: {workflow_status: "needs_budget_review", generation_budget: {status: "blocked"}}})');
  assert.match(message, /已保留生成结果/);
  assert.match(message, /金额上限已取消/);
  assert.match(message, /重新提交继续/);
  assert.doesNotMatch(message, /重新生成所选产品/);
});

test('off-peak waiting stays active, uses Beijing resume time and polls even when running is false', async () => {
  const {run, nodes, requests, timers, setResponse} = setup();
  setResponse({status: 'waiting_off_peak', running: false, message: '已排队', resume_at: '2026-10-05T04:00:00+00:00', requested_count: 1});
  await run('loadAiOriginalTaskStatus()');
  assert.match(nodes.get('ai-original-task-status').textContent, /北京时间 10\/05 12:00/);
  assert.doesNotMatch(nodes.get('ai-original-task-status').className, /success/);
  assert.equal(nodes.get('ai-original-process').textContent, '已排队，等待闲时');
  run('updateAiOriginalSelection()');
  assert.equal(nodes.get('ai-original-process').disabled, true);
  assert.equal(timers.at(-1).delay, 15000);
  await run('processSelectedAiOriginalProducts()');
  assert.equal(requests.length, 1);
});

test('failure log includes known budget estimate and unresolved charge reserve', () => {
  const {run, nodes} = setup();
  run(`renderAiOriginalTaskStatus({status: 'error', failed_count: 1, requested_count: 1, processed_count: 1,
    results: [{product_id: 1, status: 'failed', message: '人工补齐',
      generation_budget: {cap_yuan: 0.03, spent_yuan: 0.01, reserved_yuan: 0.015}}]});`);
  assert.match(nodes.get('ai-original-task-log').textContent, /模型计费估算 ¥0.01/);
  assert.match(nodes.get('ai-original-task-log').textContent, /待核对预留 ¥0.015/);
});


test('overall cost remains separate from visible rows and shows recorded coverage', () => {
  const {run, nodes} = setup();
  run(`aiOriginalTotalCost = {spent_yuan: 0.05878744, total_tokens: 51109, recorded_products: 11, total_products: 38, budget_limit_enabled: false}; renderAiOriginalProducts();`);
  assert.match(nodes.get('ai-original-total').textContent, /全部商品/);
  assert.match(nodes.get('ai-original-total').textContent, /51,109 Token/);
  assert.match(nodes.get('ai-original-total').textContent, /11\/38/);
  assert.match(nodes.get('ai-original-total').textContent, /金额不限额/);
});
