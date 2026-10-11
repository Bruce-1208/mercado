const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../bit/templates/index.html'), 'utf8');
function sourceBetween(start, end) {
    const first = template.indexOf(start);
    const last = template.indexOf(end, first);
    assert.ok(first >= 0 && last > first, `Template markers missing: ${start}`);
    return template.slice(first, last);
}
const source = sourceBetween('        let storeLinkSelectionScope =', '        function toggleStoreLink(')
    + sourceBetween('        function renderStoreLinkRemoteUpdateStatus(data)', '        async function startStoreLinkSync(')
    + sourceBetween('        function switchTab(tabName)', '        function setStatus(text, state)');

function classList() {
    const names = new Set();
    return {
        contains: name => names.has(name),
        toggle: (name, enabled) => enabled ? names.add(name) : names.delete(name),
    };
}
function harness({honorAbort = true} = {}) {
    const calls = [];
    const timers = new Map();
    const tableStates = [];
    let timerId = 0;
    const page = {id: 'tab-store-links', classList: classList()};
    page.classList.toggle('active', true);
    const buttons = ['store-links', 'overview'].map(tab => ({dataset: {tab}, classList: classList()}));
    const input = value => ({value});
    const context = {
        URLSearchParams, AbortController, Boolean, Number, Math, Set,
        currentWorkbenchUser: {role_key: "member"},
        storeLinkDateTime: value => value || "-",
        storeLinkBody: {dataset: {}, innerHTML: ''},
        storeLinkLoadRequestId: 0, storeLinkLoadController: null,
        storeLinkRefreshButton: {}, storeLinkSalesSort: input('sold_quantity:desc'),
        storeLinkSalespersonFilter: input(''), storeLinkSearch: input(''), storeLinkSiteFilter: input(''), storeLinkGroupFilter: input(''),
        storeLinkStatusFilter: input(''), storeLinkPromotionFilter: input(''),
        storeLinkAdvertisingFilter: input(''), storeLinkVideoFilter: input(''),
        storeLinkProductCategoryFilter: input(''), storeLinkMercadoCategoryFilter: input(''),
        storeLinkPageSize: input('50'), storeLinkStoreFilter: input(''),
        selectedStoreLinkIds: new Set(), storeLinkSyncState: {}, storeLinkSyncLog: null,
        storeLinkSyncPollTimer: null, storeLinkSyncStatusLoading: false,
        storeLinkSyncRunning: false, storeLinkLastRenderedProgress: -1,
        storeLinkRemoteUpdateRunning: false, storeLinkRemoteUpdatePollTimer: null,
        storeLinkRemoteUpdateStatusLoading: false, storeLinkRemoteUpdateLog: {},
        storeLinkPendingRefreshPage: null, storeLinkPage: 1, storeLinkPages: 1,
        storeLinksLoaded: false, storeLinkRows: [], storeLinkAllCount: {},
        storeLinkCurrentCount: {}, storeLinkStoreCount: {}, storeLinkLastSync: {},
        storeLinkPaginationSummary: {}, storeLinkResultSummary: {}, storeLinkMessage: {}, storeLinkBulkButton: {},
        storeLinkSyncAllButton: {}, storeLinkSyncButton: {}, canChangeTaskWorkers: false,
        mercadoSelectedValues: () => [], updateStoreLinkSortButtons: () => {},
        updateStoreLinkSelection: () => {}, renderStoreLinkRows: rows => {
            context.storeLinkRows = rows;
            context.storeLinkBody.innerHTML = rows.map(row => `row:${row.id}`).join(',');
        },
        renderStoreLinkStores: () => {}, renderStoreLinkSites: () => {},
        renderStoreLinkSalespeople: () => {}, renderStoreLinkGroups: () => {}, renderStoreLinkPagination: () => {},
        loadMercadoManagementCategories: () => {}, syncWorkspaceTabs: () => {},
        escapeHtml: value => String(value),
        WorkbenchUI: {
            tableState: (body, state) => { tableStates.push(state); body.innerHTML = state; },
            errorKind: error => error.status === 403 ? 'denied' : 'error',
            feedback: () => {},
        },
        window: {
            scrollTo: () => {},
            clearTimeout: id => timers.delete(id),
            setTimeout: (callback, delay) => {
                timers.set(++timerId, {callback, delay});
                return timerId;
            },
        },
        document: {
            getElementById: id => id === 'tab-store-links' ? page : null,
            querySelector: selector => buttons.find(button => selector.includes(`"${button.dataset.tab}"`)),
            querySelectorAll: selector => selector === '.tab-button' ? buttons : selector === '.tab-page' ? [page] : [],
        },
    };
    context.fetch = (url, options) => new Promise((resolve, reject) => {
        const call = {
            url, signal: options?.signal,
            respond: (data, {status = 'success', code = 200, html = false} = {}) => resolve({
                ok: code >= 200 && code < 300, status: code,
                json: async () => {
                    if (html) throw new SyntaxError('HTML gateway body must not be parsed');
                    return {status, data, message: 'test response'};
                },
            }),
            fail: message => reject(new Error(message)),
        };
        calls.push(call);
        if (honorAbort) options?.signal?.addEventListener('abort', () => {
            reject(Object.assign(new Error('aborted'), {name: 'AbortError'}));
        });
    });
    vm.createContext(context);
    vm.runInContext(source, context);
    return {
        context, calls, timers, tableStates,
        lists: () => calls.filter(call => call.url.startsWith('/api/store-links?')),
        syncs: () => calls.filter(call => call.url === '/api/store-links/sync/status'),
        updates: () => calls.filter(call => call.url === '/api/store-links/bulk-update/status'),
        active: enabled => page.classList.toggle('active', enabled),
        state: name => vm.runInContext(name, context),
    };
}
const flush = () => new Promise(setImmediate);
const runningSync = discovered_count => ({running: true, status: 'running', discovered_count});

test('opening twice during sync keeps one uninterrupted first-page request', async () => {
    const h = harness();
    h.context.switchTab('store-links');
    h.context.switchTab('store-links');
    assert.equal(h.lists().length, 1);
    assert.equal(h.syncs().length, 1);
    assert.equal(h.updates().length, 1);
    h.syncs()[0].respond(runningSync(1000));
    h.updates()[0].respond({running: false, status: 'idle'});
    await flush();
    assert.equal(h.lists().length, 1);
    assert.equal(h.lists()[0].signal.aborted, false);
    h.lists()[0].respond({rows: [{id: 1}]});
    await flush();
    assert.equal(h.context.storeLinkBody.innerHTML, 'row:1');
    assert.equal(h.lists().length, 1);
    assert.equal(h.timers.size, 1);
});

test('manual filter changes still cancel old requests and discard late responses', async () => {
    const h = harness({honorAbort: false});
    const oldList = h.context.loadStoreLinks(1);
    const status = h.context.loadStoreLinkSyncStatus();
    h.syncs()[0].respond(runningSync(2000));
    await status;
    assert.equal(h.lists().length, 1);
    h.context.storeLinkSearch.value = 'new filter';
    const newList = h.context.loadStoreLinks(1);
    assert.equal(h.context.storeLinkResultSummary.textContent, '正在查询…');
    assert.equal(h.lists()[0].signal.aborted, true);
    assert.match(h.lists()[1].url, /search=new\+filter/);
    h.lists()[1].respond({rows: [{id: 2}], total: 12345});
    await newList;
    h.lists()[0].respond({rows: [{id: 1}], total: 99999});
    await oldList;
    assert.equal(h.context.storeLinkBody.innerHTML, 'row:2');
    assert.equal(h.context.storeLinkResultSummary.textContent, '筛选结果共 12,345 条 · 本页 1 条');
    assert.match(h.context.storeLinkPaginationSummary.textContent, /筛选结果共 12,345 条/);
    assert.equal(h.context.storeLinkRefreshButton.disabled, false);
});

test('both status endpoints deduplicate overlapping calls and keep one timer each', async () => {
    const h = harness();
    const firstSync = h.context.loadStoreLinkSyncStatus();
    const duplicateSync = h.context.loadStoreLinkSyncStatus();
    const firstUpdate = h.context.loadStoreLinkRemoteUpdateStatus();
    const duplicateUpdate = h.context.loadStoreLinkRemoteUpdateStatus();
    assert.equal(h.syncs().length, 1);
    assert.equal(h.updates().length, 1);
    h.syncs()[0].respond(runningSync(0));
    h.updates()[0].respond({running: true});
    await Promise.all([firstSync, duplicateSync, firstUpdate, duplicateUpdate]);
    assert.equal(h.timers.size, 2);
    const nextSync = h.context.loadStoreLinkSyncStatus();
    const nextUpdate = h.context.loadStoreLinkRemoteUpdateStatus();
    assert.equal(h.timers.size, 0);
    h.syncs()[1].respond(runningSync(0));
    h.updates()[1].respond({running: true});
    await Promise.all([nextSync, nextUpdate]);
    assert.equal(h.timers.size, 2);
    assert.equal(h.context.storeLinkSyncStatusLoading, false);
    assert.equal(h.context.storeLinkRemoteUpdateStatusLoading, false);
});

test('switching away cancels polling; reactivation refreshes stale rows only once', async () => {
    const h = harness();
    h.context.storeLinksLoaded = true;
    h.context.storeLinkBody.innerHTML = 'existing rows';
    h.context.storeLinkLastRenderedProgress = 1000;
    const sync = h.context.loadStoreLinkSyncStatus();
    const update = h.context.loadStoreLinkRemoteUpdateStatus();
    h.syncs()[0].respond(runningSync(1000));
    h.updates()[0].respond({running: true});
    await Promise.all([sync, update]);
    h.context.switchTab('overview');
    assert.equal(h.timers.size, 0);
    await h.context.loadStoreLinkSyncStatus();
    await h.context.loadStoreLinkRemoteUpdateStatus();
    await h.context.loadStoreLinks(1, {automatic: true});
    assert.equal(h.calls.length, 2);
    h.context.switchTab('store-links');
    h.context.switchTab('store-links');
    assert.equal(h.lists().length, 1);
    assert.equal(h.syncs().length, 2);
    assert.equal(h.updates().length, 2);
    assert.equal(h.context.storeLinkBody.innerHTML, 'existing rows');
    h.syncs()[1].respond(runningSync(1000));
    h.updates()[1].respond({running: true});
    h.lists()[0].respond({rows: [{id: 9}]});
    await flush();
    assert.equal(h.lists().length, 1);
    assert.equal(h.timers.size, 2);
});

test('status responses received after switching away cannot refresh or resume polling', async () => {
    const h = harness();
    const sync = h.context.loadStoreLinkSyncStatus();
    const update = h.context.loadStoreLinkRemoteUpdateStatus();
    h.context.switchTab('overview');
    h.syncs()[0].respond(runningSync(1000));
    h.updates()[0].respond({running: true});
    await Promise.all([sync, update]);
    assert.equal(h.lists().length, 0);
    assert.equal(h.timers.size, 0);
    assert.equal(h.context.storeLinkPendingRefreshPage, 1);
});

test('automatic refresh preserves existing rows through loading and a transient failure', async () => {
    const h = harness();
    h.context.storeLinksLoaded = true;
    h.context.storeLinkBody.innerHTML = 'existing rows';
    const refresh = h.context.loadStoreLinks(1, {automatic: true});
    assert.equal(h.context.storeLinkBody.innerHTML, 'existing rows');
    assert.equal(h.tableStates.length, 0);
    h.lists()[0].fail('network failed');
    await refresh;
    assert.equal(h.context.storeLinkBody.innerHTML, 'existing rows');
    assert.match(h.context.storeLinkMessage.textContent, /network failed/);
    assert.equal(h.context.storeLinkRefreshButton.disabled, false);
});

test('automatic refresh clears stale data when access is denied', async () => {
    const h = harness();
    h.context.storeLinksLoaded = true;
    h.context.storeLinkBody.innerHTML = 'existing rows';
    const refresh = h.context.loadStoreLinks(1, {automatic: true});
    h.lists()[0].respond({}, {status: 'error', code: 403});
    await refresh;
    assert.equal(h.context.storeLinkBody.innerHTML, 'denied');
    assert.equal(h.context.storeLinkResultSummary.textContent, '查询失败');
    assert.match(h.context.storeLinkMessage.textContent, /权限不足/);
});

for (const kind of ['sync', 'update']) {
    test(`${kind} completion during a list request queues one refresh without cancelling filters`, async () => {
        const h = harness();
        h.context.storeLinkPage = 3;
        const firstList = h.context.loadStoreLinks(3);
        h.context.storeLinkSyncRunning = kind === 'sync';
        h.context.storeLinkRemoteUpdateRunning = kind === 'update';
        const status = kind === 'sync' ? h.context.loadStoreLinkSyncStatus() : h.context.loadStoreLinkRemoteUpdateStatus();
        const calls = kind === 'sync' ? h.syncs() : h.updates();
        calls[0].respond({running: false, status: 'completed'});
        await status;
        assert.equal(h.lists().length, 1);
        assert.equal(h.lists()[0].signal.aborted, false);
        h.lists()[0].respond({page: 3, rows: [{id: 3}]});
        await firstList;
        assert.equal(h.lists().length, 2);
        const refreshedPage = kind === 'sync' ? 1 : 3;
        assert.equal(new URL(h.lists()[1].url, 'http://console.test').searchParams.get('page'), String(refreshedPage));
        assert.equal(h.context.storeLinkBody.innerHTML, 'row:3');
        h.lists()[1].respond({page: refreshedPage, rows: [{id: 4}]});
        await flush();
        assert.equal(h.context.storeLinkBody.innerHTML, 'row:4');
        assert.equal(h.lists().length, 2);
        assert.equal(h.context.storeLinkPendingRefreshPage, null);
        assert.equal(h.timers.size, 0);
    });
}

test('failed status requests release their lock and cannot restart hidden-page retries', async () => {
    const h = harness();
    const first = h.context.loadStoreLinkSyncStatus();
    h.syncs()[0].fail('offline');
    await first;
    assert.equal(h.context.storeLinkSyncStatusLoading, false);
    assert.equal(h.timers.size, 1);
    assert.equal([...h.timers.values()][0].delay, 3000);
    const retry = h.context.loadStoreLinkSyncStatus();
    h.context.switchTab('overview');
    h.syncs()[1].fail('still offline');
    await retry;
    assert.equal(h.timers.size, 0);
    assert.equal(h.context.storeLinkSyncStatusLoading, false);
});


test('changing salesperson scopes pagination and clears the previous selection', async () => {
    const h = harness();
    h.context.storeLinkSalespersonFilter.value = '张三';
    const first = h.context.loadStoreLinks(2);
    let query = new URL(h.lists()[0].url, 'http://localhost').searchParams;
    assert.equal(query.get('salesperson'), '张三');
    assert.equal(query.get('page'), '2');
    h.lists()[0].respond({rows: [{id: 1}], total: 5});
    await first;
    h.context.selectedStoreLinkIds.add(1);
    h.context.storeLinkSalespersonFilter.value = '李四';
    const second = h.context.loadStoreLinks(1);
    assert.equal(h.context.selectedStoreLinkIds.size, 0);
    query = new URL(h.lists()[1].url, 'http://localhost').searchParams;
    assert.equal(query.get('salesperson'), '李四');
    assert.equal(query.get('page'), '1');
    h.lists()[1].respond({rows: [], total: 0});
    await second;
});

async function fireGatewayRetry(h) {
    await flush();
    const entry = [...h.timers].find(([, timer]) => [1500, 3000].includes(timer.delay));
    assert.ok(entry, 'gateway retry scheduled');
    h.timers.delete(entry[0]);
    entry[1].callback();
    await flush();
}

test('transient gateway failure retries the same list then renders recovered data', async () => {
    const h = harness();
    const loading = h.context.loadStoreLinks(2);
    h.lists()[0].respond({}, {code: 502, html: true});
    await fireGatewayRetry(h);
    assert.equal(h.lists()[1].url, h.lists()[0].url);
    h.lists()[1].respond({rows: [{id: 42}], page: 2});
    await loading;
    assert.equal(h.context.storeLinkBody.innerHTML, 'row:42');
    assert.equal(h.context.storeLinkRefreshButton.disabled, false);
});

test('persistent gateway failure stops after three attempts with actionable feedback', async () => {
    const h = harness();
    const loading = h.context.loadStoreLinks();
    for (let i = 0; i < 3; i++) {
        h.lists()[i].respond({}, {code: 502, html: true});
        if (i < 2) await fireGatewayRetry(h);
    }
    await loading;
    assert.equal(h.lists().length, 3);
    assert.match(h.context.storeLinkMessage.textContent, /自动重试后仍未恢复/);
    assert.equal(h.context.storeLinkRefreshButton.disabled, false);
});

test('changing filters cancels the gateway retry delay', async () => {
    const h = harness();
    const old = h.context.loadStoreLinks();
    h.lists()[0].respond({}, {code: 503});
    await flush();
    h.context.storeLinkSearch.value = 'new';
    const current = h.context.loadStoreLinks();
    h.lists()[1].respond({rows: [{id: 9}]});
    await Promise.all([old, current]);
    assert.equal(h.timers.size, 0);
    assert.equal(h.lists().length, 2);
    assert.equal(h.context.storeLinkBody.innerHTML, 'row:9');
});
