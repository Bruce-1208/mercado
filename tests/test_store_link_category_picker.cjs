const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync(require('node:path').join(__dirname, '../bit/templates/index.html'), 'utf8');
const source = template.slice(template.indexOf('        async function loadStoreLinkCategoryPaths()'), template.indexOf('        function toggleStoreLinkCategoryPicker()'));

function setup(responses, rows = [{category_id: 'CBT2'}]) {
    const calls = [];
    const context = vm.createContext({
        storeLinkCategoryRows: rows,
        storeLinkCategoryPaths: {},
        storeLinkCategoryPathLoading: false,
        storeLinkCategoryOptionsLoading: false,
        storeLinkCategoryColumns: {}, storeLinkMessage: {},
        escapeHtml: String,
        renderStoreLinkMercadoCategories(rows) { context.storeLinkCategoryRows = rows; },
        renderStoreLinkCategoryTree() {},
        async fetch(url, options) {
            calls.push({url, ids: options.body ? JSON.parse(options.body).category_ids : []});
            const result = responses.shift();
            if (result instanceof Error) throw result;
            return {ok: true, json: async () => ({status: 'success', data: result})};
        },
    });
    vm.runInContext(source, context);
    return {context, calls};
}
const path = [{id: 'CBT2', name: 'Refrigerators'}];

test('reopening retries failed name loading even when category options are cached', async () => {
    const {context, calls} = setup([new Error('timeout'), {CBT2: path}]);
    await context.loadStoreLinkCategoryOptions();
    assert.match(context.storeLinkMessage.textContent, /timeout/);
    await context.loadStoreLinkCategoryOptions();
    assert.equal(context.storeLinkCategoryPaths.CBT2[0].name, 'Refrigerators');
    assert.equal(calls.length, 2);
    await context.loadStoreLinkCategoryOptions();
    assert.equal(calls.length, 2);
});

test('partial results and newly added categories fetch only missing names and retain existing paths', async () => {
    const {context, calls} = setup([{}, {CBT2: path}, {MLB3: [{id: 'MLB3', name: 'Test'}]}]);
    await context.loadStoreLinkCategoryOptions();
    await context.loadStoreLinkCategoryOptions();
    context.storeLinkCategoryRows.push({category_id: 'MLB3'});
    await context.loadStoreLinkCategoryOptions();
    assert.deepEqual(calls.map(call => call.ids), [['CBT2'], ['CBT2'], ['MLB3']]);
    assert.equal(context.storeLinkCategoryPaths.CBT2[0].name, 'Refrigerators');
});

test('initial options request loads names, concurrent opens do not duplicate requests', async () => {
    const {context, calls} = setup([{mercado_categories: [{category_id: 'CBT2'}]}, {CBT2: path}], []);
    await Promise.all([context.loadStoreLinkCategoryOptions(), context.loadStoreLinkCategoryOptions()]);
    assert.equal(calls.length, 2);
    assert.equal(context.storeLinkCategoryPaths.CBT2[0].name, 'Refrigerators');
});

test('a failed catalog does not discard names loaded from another catalog', async () => {
    const {context, calls} = setup([new Error('timeout'), {MLB3: [{id: 'MLB3', name: 'Test'}]}, {CBT2: path}], [{category_id: 'CBT2'}, {category_id: 'MLB3'}]);
    await context.loadStoreLinkCategoryOptions();
    assert.equal(context.storeLinkCategoryPaths.MLB3[0].name, 'Test');
    assert.match(context.storeLinkMessage.textContent, /还有 1 个/);
    await context.loadStoreLinkCategoryOptions();
    assert.deepEqual(calls.map(call => call.ids), [['CBT2'], ['MLB3'], ['CBT2']]);
    assert.equal(context.storeLinkMessage.textContent, '');
});

test('unresolved categories remain selectable without an artificial site level', () => {
    const context = vm.createContext({storeLinkCategoryRows: [{category_id: 'MLB3', category_name: '测试分类', link_count: 7}], storeLinkCategoryPaths: {}});
    vm.runInContext(template.slice(template.indexOf('        function storeLinkCategoryTree()'), template.indexOf('        function renderStoreLinkCategoryTree()')), context);
    const root = context.storeLinkCategoryTree();
    assert.equal(root.children.size, 1);
    assert.equal(root.children.get('MLB3').name, '测试分类');
    assert.equal(root.children.get('MLB3').selectable, true);
    assert.equal(root.children.get('MLB3').count, 7);
});

test('same category path merges across sites and retains every filter id', () => {
    const context = vm.createContext({storeLinkCategoryRows: [{category_id:'MLA2',link_count:3},{category_id:'MLM2',link_count:4}],storeLinkCategoryPaths:{MLA2:[{id:'MLA1',name:'Tools'},{id:'MLA2',name:'Hammers'}],MLM2:[{id:'MLM1',name:'Tools'},{id:'MLM2',name:'Hammers'}]}});
    vm.runInContext(template.slice(template.indexOf('        function storeLinkCategoryTree()'), template.indexOf('        function renderStoreLinkCategoryTree()')), context);
    const root=context.storeLinkCategoryTree();
    assert.equal(root.children.size,1);
    const parent=[...root.children.values()][0];
    assert.equal(parent.count,7);
    assert.equal(parent.children.size,1);
    assert.deepEqual(Array.from([...parent.children.values()][0].categoryIds),['MLA2','MLM2']);
});

test('categories without names or paths remain separate selectable options', () => {
    const context = vm.createContext({
        storeLinkCategoryRows: [{category_id: 'MLB3', link_count: 7}, {category_id: 'CBT2', category_name: '', link_count: 2}],
        storeLinkCategoryPaths: {},
    });
    vm.runInContext(template.slice(template.indexOf('        function storeLinkCategoryTree()'), template.indexOf('        function renderStoreLinkCategoryTree()')), context);
    const root = context.storeLinkCategoryTree();
    assert.equal(root.children.size, 2);
    for (const [id, count] of [['MLB3', 7], ['CBT2', 2]]) {
        const node = root.children.get(id);
        assert.equal(node.name, '分类名称待同步');
        assert.equal(node.selectable, true);
        assert.equal(node.count, count);
        assert.deepEqual(Array.from(node.categoryIds), [id]);
    }
});
