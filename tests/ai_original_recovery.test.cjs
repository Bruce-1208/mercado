const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(prepared = {}) {
  const nodes = new Map();
  const context = vm.createContext({
    document: {
      addEventListener() {}, querySelectorAll() { return []; }, querySelector() { return null; },
      getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, {value: '', checked: false, selectedOptions: [],
          showModal() {}, close() {}, querySelectorAll() { return []; }, querySelector() { return null; }});
        return nodes.get(id);
      },
    },
    window: {location: {origin: 'http://localhost'}},
    localStorage: {getItem() { return null; }, removeItem() {}},
  });
  const run = code => vm.runInContext(code, context);
  run(fs.readFileSync(path.join(__dirname, '../bit/static/ai-original-products.js'), 'utf8'));
  run(`loadAiOriginalCategoryAttributes = async () => {};
    aiOriginalRows = [{id: 1, category_id: 'CBT123', category_name: 'Costumes', ai_status: 'failed',
      original_1688: {title: 'Source title', category_id: 'MLM456'},
      ai_original: ${JSON.stringify(prepared)}}];`);
  return {run, nodes};
}

test('existing imported CBT category opens unlocked and payload sends an explicit boolean', () => {
  const {run, nodes} = setup();
  nodes.set('ai-original-editor-category-locked', {checked: true});
  run('openAiOriginalEditor(1)');
  assert.equal(nodes.get('ai-original-editor-category-locked').checked, false);
  const payload = run('collectAiOriginalEditorPayload()');
  assert.equal(payload.category_id, 'CBT123');
  assert.equal(payload.category_locked, false);
});

test('manual search selection locks category and reopening reflects saved lock', async () => {
  const {run, nodes} = setup({category_locked: true});
  run('openAiOriginalEditor(1)');
  assert.equal(nodes.get('ai-original-editor-category-locked').checked, true);
  await run('selectAiOriginalCategory("CBT999", "String lights")');
  const payload = run('collectAiOriginalEditorPayload()');
  assert.equal(payload.category_id, 'CBT999');
  assert.equal(payload.category_name, 'String lights');
  assert.equal(payload.category_locked, true);
});

test('failure cards explain required recovery and saved partial steps without dropping failed status', () => {
  const {run, nodes} = setup({status: 'failed', workflow_status: 'needs_variations',
    error: '<script>unsafe</script>', preparation: {image_ready: true, copy_ready: true, category_ready: true}});
  run('renderAiOriginalProducts()');
  const markup = nodes.get('ai-original-grid').innerHTML;
  assert.match(markup, /待处理规格/);
  assert.match(markup, /已保存白底主图、双语文案、类目/);
  assert.match(markup, /&lt;script&gt;unsafe&lt;\/script&gt;/);
  assert.doesNotMatch(markup, /<script>/);
  assert.equal(run('aiOriginalPlatformStatus(aiOriginalRows[0])'), 'failed');
});

test('recovery text distinguishes category and required attribute work', () => {
  const {run} = setup();
  assert.match(run('aiOriginalRecoveryMessage({ai_original: {workflow_status:"needs_category"}})'), /待重试选类.*AI 会根据标题和描述/);
  assert.match(run('aiOriginalRecoveryMessage({ai_original: {workflow_status:"needs_attributes"}})'), /待补充必填属性/);
  assert.equal(run('aiOriginalRecoveryMessage({ai_original: {status:"completed"}})'), '');
});

test('partial category checkpoint populates editor and loads its schema ahead of imported category', () => {
  const {run, nodes} = setup({category_id: 'CBT987', category_name: 'Prepared costume category'});
  run(`aiOriginalRows[0].category_id = '';
    aiOriginalRows[0].category_name = '';
    globalThis.loadedCategoryId = '';
    loadAiOriginalCategoryAttributes = async categoryId => { loadedCategoryId = categoryId; };
    openAiOriginalEditor(1);`);
  assert.equal(nodes.get('ai-original-editor-category-id').value, 'CBT987');
  assert.equal(nodes.get('ai-original-editor-category-name').value, 'Prepared costume category');
  assert.equal(run('loadedCategoryId'), 'CBT987');
  assert.match(nodes.get('ai-original-editor-category-hint').textContent, /CBT987/);
});
