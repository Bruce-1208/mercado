const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('bit/static/collection-ai.js', 'utf8');

function harness(fetcher) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: id === 'collection-ai-target' ? 'agent' : '', open: false,
      showModal() { this.open = true; },
      replaceChildren(...options) { this.options = options; this.value = options[0]?.value || ''; },
      add(option) { this.options.push(option); this.value = option.value; },
    });
    return elements.get(id);
  };
  const feedback = [];
  let reloads = 0;
  const context = vm.createContext({
    document: {getElementById: element},
    Option: function(label, value) { this.label = label; this.value = value; },
    selectedMercadoCollectionIds: new Set([1, 2]), fetch: fetcher,
    clearTimeout() {}, setTimeout() {},
    loadMercadoList: async () => { reloads++; },
    setMercadoListFeedback: (...args) => feedback.push(args),
  });
  vm.runInContext(source, context);
  return {context, element, feedback, reloads: () => reloads};
}
const response = data => ({ok: true, json: async () => ({status: 'success', data})});

test('chooses an online Agent and sends explicit target with selected collection ids', async () => {
  const calls = [];
  const h = harness(async (url, init = {}) => {
    calls.push([url, init]);
    if (url.includes('execution-agents')) return response({agents: [
      {agent_id: 'offline', online: false}, {agent_id: 'office', online: true, name: 'Office'}]});
    if (init.method === 'POST') return response({message: 'queued'});
    return response({running: false});
  });
  await h.context.openMercadoCollectionAiCheck();
  assert.equal(h.element('collection-ai-agent').value, 'office');
  assert.equal(h.element('collection-ai-start').disabled, false);
  await h.context.startMercadoCollectionAiCheck();
  const body = JSON.parse(calls.find(([, options]) => options.method === 'POST')[1].body);
  assert.deepEqual(body, {collection_item_ids: [1, 2], execution_target: 'agent', agent_id: 'office'});
});

test('no online Agent disables start; server choice enables it without an Agent', async () => {
  const h = harness(async url => response(url.includes('execution-agents') ? {agents: []} : {running: false}));
  await h.context.openMercadoCollectionAiCheck();
  assert.equal(h.element('collection-ai-start').disabled, true);
  h.element('collection-ai-target').value = 'server';
  h.context.syncCollectionAiTarget();
  assert.equal(h.element('collection-ai-start').disabled, false);
  assert.equal(h.element('collection-ai-agent-controls').hidden, true);
});

test('running batch prevents resubmission and completion refreshes collection list', async () => {
  let running = true;
  const h = harness(async url => response(url.includes('execution-agents') ? {agents: [
    {agent_id: 'office', online: true, name: 'Office'}]} :
    {running, run: {run_id: 'r', outcome: running ? 'running' : 'completed', max_items: 2}}));
  await h.context.openMercadoCollectionAiCheck();
  assert.equal(h.element('collection-ai-start').disabled, true);
  assert.equal(h.element('collection-ai-stop').disabled, false);
  running = false;
  await h.context.refreshCollectionAiStatus();
  assert.equal(h.element('collection-ai-start').disabled, false);
  assert.equal(h.element('collection-ai-stop').disabled, true);
  assert.equal(h.reloads(), 1);
  assert.match(h.element('collection-ai-status').textContent, /已完成/);
});
