const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const html = fs.readFileSync(require('node:path').join(__dirname, '../bit/templates/index.html'), 'utf8');
const source = html.slice(html.indexOf('        async function customerServiceAggregateRequest'), html.indexOf('        function customerServiceQueryKey'));
function setup(respond) {
    const requests = [], displays = [];
    const c = {
        Date, console, customerServiceLoadGeneration: 0, canChangeTaskWorkers: false,
        customerServiceHasSnapshot: false, customerServiceAllRows: [], customerServiceQueryMetrics: null,
        window: {setTimeout: fn => fn()}, setCustomerServiceMessage() {},
        customerServicePostSaleRows: result => result.stores,
        customerServiceApplyFilters: () => displays.push([...c.customerServiceAllRows]),
        fetch: async (url, options) => {
            const payload = JSON.parse(options.body); requests.push(payload);
            return {ok: true, json: async () => ({status: 'success', data: await respond(payload, c)})};
        },
    };
    vm.createContext(c); vm.runInContext(source, c);
    return {c, requests, displays};
}
test('completed stores display while pending stores poll without restarting work', async () => {
    const {c, requests, displays} = setup(payload => payload.token_ids.length === 2
        ? {stores: [{token_id: 1, updated_at: '2026-10-08T00:00:00Z'}], pending_token_ids: [2]}
        : {stores: [{token_id: 2, updated_at: '2026-10-08T00:01:00Z'}], pending_token_ids: []});
    const result = await c.customerServiceAggregateRequest([{id: 1}, {id: 2}], {mode: 'post-sale', refresh: true});
    assert.deepEqual(requests.map(row => row.token_ids), [[1, 2], [2]]);
    assert.deepEqual(requests.map(row => row.refresh), [true, false]);
    assert.deepEqual(displays.map(rows => rows.length), [1, 2]);
    assert.equal(result.stores.length, 2);
    assert.equal(c.customerServiceQueryMetrics.updatedAt, '2026-10-08T00:00:00Z');
});
test('a response from a previous filter cannot replace the current view', async () => {
    const {c, displays} = setup((payload, ctx) => {
        ctx.customerServiceLoadGeneration++;
        return {stores: [{token_id: 1}], pending_token_ids: []};
    });
    await assert.rejects(c.customerServiceAggregateRequest([{id: 1}], {mode: 'post-sale'}), /售后筛选已切换/);
    assert.equal(displays.length, 0);
    assert.equal(c.customerServiceQueryMetrics, null);
});
test('claims render completed stores before a slow store finishes', async () => {
    const {c, displays} = setup(payload => payload.token_ids.length === 2
        ? {stores: [{token_id: 1, rows: [{id: 101}]}], pending_token_ids: [2]}
        : {stores: [{token_id: 2, rows: [{id: 102}]}], pending_token_ids: []});
    c.customerServicePagination = {hidden: false};
    const result = await c.customerServiceAggregateRequest([{id: 1}, {id: 2}], {mode: 'claims'});
    assert.deepEqual(displays.map(rows => rows.length), [1, 2]);
    assert.equal(result.stores.length, 2);
    assert.equal(c.customerServicePagination.hidden, true);
});
