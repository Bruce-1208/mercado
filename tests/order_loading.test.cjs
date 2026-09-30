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
        orderLoadSequence: 0, orderLoadController: null,
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
