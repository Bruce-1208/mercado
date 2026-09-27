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
