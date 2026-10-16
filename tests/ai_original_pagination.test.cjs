const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup() {
  const nodes = new Map();
  const context = vm.createContext({document: {addEventListener() {}, getElementById: id => nodes.get(id) || null}});
  const run = code => vm.runInContext(code, context);
  run(fs.readFileSync('bit/static/ai-original-products.js', 'utf8'));
  return {run, nodes};
}

test('50 rows per page, final page and bounds after filtering', () => {
  const {run, nodes} = setup();
  run('aiOriginalRows = Array.from({length: 121}, (_, i) => ({id: i + 1, review_status: i > 99 ? "failed" : "unreviewed"}));');
  assert.equal(run('aiOriginalPageRows().length'), 50);
  assert.equal(run('aiOriginalPage = 2; aiOriginalPageRows()[0].id'), 51);
  assert.equal(run('aiOriginalPage = 3; aiOriginalPageRows().length'), 21);
  nodes.set('ai-original-status-filter', {value: 'failed'});
  assert.equal(run('aiOriginalPageRows().length'), 21);
  assert.equal(run('aiOriginalPage'), 1);
  nodes.set('ai-original-status-filter', {value: 'approved'});
  assert.equal(run('aiOriginalPageRows().length'), 0);
  assert.equal(run('aiOriginalPage'), 1);
});

test('select all affects only the displayed page', () => {
  const {run} = setup();
  run('aiOriginalRows = Array.from({length: 121}, (_, i) => ({id: i + 1})); renderAiOriginalProducts = () => {}; aiOriginalPage = 2; toggleAllAiOriginalProducts(true);');
  assert.equal(run('aiOriginalSelected.size'), 50);
  assert.equal(run('aiOriginalSelected.has(51) && aiOriginalSelected.has(100)'), true);
  assert.equal(run('aiOriginalSelected.has(1) || aiOriginalSelected.has(101)'), false);
});

test('manually failed overrides completed and pending generation', () => {
  const {run} = setup();
  for (const status of ['completed', 'pending', 'processing']) {
    assert.equal(run(`aiOriginalProductStatus({review_status: 'failed', ai_status: '${status}'})`), 'failed');
  }
});

test('new source rows default to pending production and can be filtered', () => {
  const {run, nodes} = setup();
  run('aiOriginalRows = [{id:1}, {id:2, ai_status:"pending", review_status:"approved"}, {id:3, ai_status:"processing"}];');
  assert.equal(run('aiOriginalProductStatusLabels.pending'), '待制作');
  assert.equal(run('aiOriginalProductStatus(aiOriginalRows[0])'), 'pending');
  nodes.set('ai-original-status-filter', {value:'pending'});
  assert.equal(run('JSON.stringify(aiOriginalPageRows().map(row => row.id))'), '[1,2]');
});
