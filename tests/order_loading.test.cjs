const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('bit/templates/index.html', 'utf8');
const fn = html.slice(html.indexOf('        async function loadOrders('), html.indexOf('        function setOrderCountry('));
function setup() {
    const timers = new Map();
    const requests = [];
    const context = vm.createContext({
        AbortController, URLSearchParams,
        orderTableBody: {}, orderStartDate: {}, orderEndDate: {},
        orderLoadSequence: 0, orderLoadController: null, orderSummaryController: null, orderStatusLoadTimer: null,
        updateOrderSortIndicators() {},
        orderRefreshButton: {}, orderPaginationSummary: {},
        ordersLoading: false, orderPages: 1, orderRows: [],
        clearOrderSelection() {}, orderQueryParams: () => new URLSearchParams(),
        escapeHtml: String,
        setTimeout: cb => { const id = timers.size + 1; timers.set(id, cb); return id; },
        clearTimeout: id => timers.delete(id),
        fetch: (url, options) => new Promise((resolve, reject) => {
            requests.push({resolve, signal: options.signal});
            options.signal.addEventListener('abort', () => reject(new Error('aborted')));
        }),
    });
    vm.runInContext(fn, context);
    return {context, timers, requests};
}
test('timeout releases loading state and allows retry', async () => {
    const {context, timers} = setup();
    const loading = context.loadOrders();
    [...timers.values()][0]();
    await loading;
    assert.match(context.orderTableBody.innerHTML, /订单查询超时/);
    assert.match(context.orderTableBody.innerHTML, /重新加载/);
    assert.equal(context.ordersLoading, false);
    assert.equal(context.orderRefreshButton.disabled, false);
    assert.equal(timers.size, 0);
});
test('superseded request cannot overwrite current loading state', async () => {
    const {context, timers, requests} = setup();
    const old = context.loadOrders();
    const current = context.loadOrders();
    await old;
    assert.equal(requests[0].signal.aborted, true);
    assert.equal(context.ordersLoading, true);
    assert.match(context.orderTableBody.innerHTML, /正在读取订单数据/);
    [...timers.values()][0]();
    await current;
    assert.equal(context.ordersLoading, false);
});

const summaryFn = html.slice(html.indexOf('        async function loadOrderSummary('), html.indexOf('        let orderStatusLoadTimer'));
test('summary arriving after a new filter cannot overwrite current metrics', async () => {
    const pending = [], rendered = [];
    const context = vm.createContext({
        AbortController, URLSearchParams, orderLoadSequence: 1, orderSummaryController: null,
        setTimeout: () => 1, clearTimeout() {},
        renderOrderMetrics: data => rendered.push(data),
        fetch: () => new Promise(resolve => pending.push(resolve)),
    });
    vm.runInContext(summaryFn, context);
    const old = context.loadOrderSummary(1, new URLSearchParams('country=old'), 100);
    context.orderLoadSequence = 2;
    const current = context.loadOrderSummary(2, new URLSearchParams('country=new'), 20);
    pending[1]({ok: true, json: async () => ({status: 'success', data: {total: 20, summary: {cost: 7}}})});
    await current;
    pending[0]({ok: true, json: async () => ({status: 'success', data: {total: 100, summary: {cost: 999}}})});
    await old;
    assert.equal(rendered.length, 1);
    assert.equal(rendered[0].total, 20);
});
test('summary failure displays unavailable state instead of a fabricated zero', async () => {
    const rendered = [];
    const context = vm.createContext({
        AbortController, URLSearchParams, orderLoadSequence: 1, orderSummaryController: null,
        setTimeout: () => 1, clearTimeout() {}, renderOrderMetrics: data => rendered.push(data),
        fetch: async () => ({ok: false, json: async () => ({status: 'error'})}),
    });
    vm.runInContext(summaryFn, context);
    await context.loadOrderSummary(1, new URLSearchParams(), 45);
    assert.equal(rendered[0].summary_error, true);
    assert.equal(rendered[0].total, 45);
});
