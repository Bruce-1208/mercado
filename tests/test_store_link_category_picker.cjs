const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../bit/static/mercado-category-picker.js'), 'utf8');
function setup(responses = [], rows = [{category_id: 'CBT2'}]) {
    const calls = [];
    const context = vm.createContext({window: {}, setTimeout, clearTimeout, async fetch(url, options = {}) {
        calls.push({url, ids: options.body ? JSON.parse(options.body).category_ids : []});
        const result = responses.shift();
        if (result instanceof Error) throw result;
        return {ok: true, json: async () => ({status: 'success', data: result})};
    }});
    vm.runInContext(source, context);
    const picker = Object.create(context.window.MercadoCategoryPicker.prototype);
    Object.assign(picker, {rows, paths: {}, generation: 0, panel: {hidden: true}, endpoint: '/options', rowsKey: 'rows',
        status(message) { this.message = message; }, render() { this.tree = context.window.buildMercadoCategoryTree(this.rows, this.paths); this.message = ''; }});
    return {picker, calls, build: context.window.buildMercadoCategoryTree};
}
const path = [{id: 'CBT1', name: '家电'}, {id: 'CBT2', name: '冰箱'}];
test('reopening retries failed loading, cached Chinese paths avoid duplicate requests', async () => {
    const {picker, calls} = setup([new Error('timeout'), {CBT2: path}]);
    await picker.load(); assert.match(picker.message, /timeout/);
    await picker.load(); assert.equal(picker.paths.CBT2[1].name, '冰箱');
    await picker.load(); assert.equal(calls.length, 2);
});
test('partial and newly added categories fetch only missing paths', async () => {
    const {picker, calls} = setup([{}, {CBT2: path}, {MLB3: [{id: 'MLB3', name: '工具'}]}]);
    await picker.load(); await picker.load();
    picker.rows.push({category_id: 'MLB3'}); await picker.load();
    assert.deepEqual(calls.map(call => call.ids), [['CBT2'], ['CBT2'], ['MLB3']]);
});
test('initial catalog and concurrent opens share one request', async () => {
    const {picker, calls} = setup([{rows: [{category_id: 'CBT2'}]}, {CBT2: path}], null);
    await Promise.all([picker.load(), picker.load()]);
    assert.equal(calls.length, 2);
});
test('one failed site preserves another site and retries only the failed site', async () => {
    const {picker, calls} = setup([new Error('timeout'), {MLB3: [{id: 'MLB3', name: '工具'}]}, {CBT2: path}], [{category_id: 'CBT2'}, {category_id: 'MLB3'}]);
    await picker.load(); assert.equal(picker.paths.MLB3[0].name, '工具');
    await picker.load(); assert.deepEqual(calls.map(call => call.ids), [['CBT2'], ['MLB3'], ['CBT2']]);
});
test('missing hierarchy and untranslated names never become bogus first-level options', () => {
    const {build} = setup();
    const tree = build([{category_id: 'CBT2', category_name: '冰箱'}, {category_id: 'MLB3'}], {MLB3: [{id: 'MLB3', name: 'Refrigerators'}]});
    assert.equal(tree.root.children.size, 0); assert.equal(tree.pending, 2);
});
test('same Chinese paths merge across sites and retain all parent and leaf filter IDs', () => {
    const {build} = setup();
    const tree = build([{category_id: 'CBT2'}, {category_id: 'MLB2'}], {CBT2: path, MLB2: [{id: 'MLB1', name: '家电'}, {id: 'MLB2', name: '冰箱'}]});
    assert.equal(tree.root.children.size, 1);
    const parent = tree.root.children.get('家电');
    assert.deepEqual([...parent.ids], ['CBT1', 'MLB1']);
    assert.deepEqual([...parent.children.get('冰箱').ids], ['CBT2', 'MLB2']);
});
test('untranslated paths refresh after background translation', async () => {
    const {picker, calls} = setup([{CBT2: path}]);
    picker.paths.CBT2 = [{id: 'CBT2', name: 'Refrigerators', name_source: 'Refrigerators'}];
    await picker.load(); await picker.load();
    assert.equal(picker.paths.CBT2[1].name, '冰箱'); assert.equal(calls.length, 1);
});
test('tree retains more than five levels', () => {
    const {build} = setup();
    const path = Array.from({length: 8}, (_, n) => ({id: `CBT${n + 1}`, name: `分类${n + 1}`}));
    let node = build([{category_id: 'CBT8'}], {CBT8: path}).root;
    for (const part of path) { node = node.children.get(part.name); assert.ok(node); }
});
