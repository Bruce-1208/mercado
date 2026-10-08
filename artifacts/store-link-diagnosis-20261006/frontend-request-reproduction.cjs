// Isolate the real template functions; no business APIs are contacted.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const template = fs.readFileSync(path.join(__dirname, '../../bit/templates/index.html'), 'utf8');
function segment(start, end) {
    const first = template.indexOf(start);
    const last = template.indexOf(end, first);
    if (first < 0 || last < 0) throw new Error('Template function markers not found');
    return template.slice(first, last);
}

function createHarness() {
    const calls = [];
    const timers = [];
    const value = {value: ''};
    const context = {
        URLSearchParams, AbortController, Number, Math, Set, globalThis: {},
        storeLinkBody: {dataset: {}}, storeLinkLoadRequestId: 0,
        storeLinkLoadController: null, storeLinkRefreshButton: {},
        storeLinkSalesSort: {value: 'sold_quantity:desc'},
        storeLinkSearch: value, storeLinkSiteFilter: value, storeLinkGroupFilter: value,
        storeLinkStatusFilter: value, storeLinkPromotionFilter: value,
        storeLinkAdvertisingFilter: value, storeLinkVideoFilter: value,
        storeLinkProductCategoryFilter: value, storeLinkMercadoCategoryFilter: value,
        storeLinkPageSize: {value: '50'}, storeLinkStoreFilter: value,
        selectedStoreLinkIds: new Set(), storeLinkSyncState: {},
        storeLinkSyncPollTimer: null, storeLinkLastRenderedProgress: -1,
        storeLinkPage: 1, storeLinkSyncRunning: false, storeLinksLoaded: false,
        storeLinkRows: [], storeLinkAllCount: {}, storeLinkCurrentCount: {},
        storeLinkStoreCount: {}, storeLinkLastSync: {}, storeLinkPaginationSummary: {},
        mercadoSelectedValues: () => [], updateStoreLinkSortButtons: () => {},
        updateStoreLinkSelection: () => {}, renderStoreLinkRows: () => {},
        renderStoreLinkStores: () => {}, renderStoreLinkSites: () => {},
        renderStoreLinkGroups: () => {}, renderStoreLinkPagination: () => {},
        renderStoreLinkSyncStatus: data => {
            context.storeLinkSyncRunning = data.running;
            return false;
        },
        window: {
            clearTimeout: id => {
                const timer = timers.find(timer => timer.id === id);
                if (timer) timer.cancelled = true;
            },
            setTimeout: (callback, delay) => {
                const timer = {id: timers.length + 1, callback, delay, cancelled: false};
                timers.push(timer);
                return timer.id;
            },
        },
        document: {getElementById: () => ({classList: {contains: () => false}})},
    };
    context.fetch = (url, options) => new Promise((resolve, reject) => {
        const call = {
            url, signal: options?.signal,
            respond: data => resolve({ok: true, json: async () => ({status: 'success', data})}),
        };
        calls.push(call);
        options?.signal?.addEventListener('abort', () => {
            reject(Object.assign(new Error('aborted'), {name: 'AbortError'}));
        });
    });
    vm.createContext(context);
    vm.runInContext(
        segment('        let storeLinkSelectionScope =', '        function toggleStoreLink(')
        + segment('        async function loadStoreLinkSyncStatus()', '        async function startStoreLinkSync('),
        context,
    );
    return {calls, timers, context};
}

async function main() {
    const results = [];
    const first = createHarness();
    const listPromise = first.context.loadStoreLinks(1);
    const statusPromise = first.context.loadStoreLinkSyncStatus();
    first.calls.find(call => call.url.includes('/sync/status')).respond({running: true, discovered_count: 1000});
    await new Promise(setImmediate);
    const lists = first.calls.filter(call => call.url.startsWith('/api/store-links?'));
    results.push({
        scenario: 'Open store-links while the background sync is running',
        list_requests_before_first_result: lists.length,
        first_request_aborted: lists[0].signal.aborted,
        queries_identical: lists[0].url === lists[1].url,
    });
    lists[1].respond({rows: []});
    await statusPromise;
    await listPromise;
    results.push({
        scenario: 'The store-links tab is inactive when the sync status responds',
        next_status_poll_ms: first.timers.at(-1)?.delay,
        background_poll_scheduled: first.timers.some(timer => !timer.cancelled),
    });

    const overlap = createHarness();
    // Prevent list reloads so this scenario isolates status request concurrency.
    overlap.context.storeLinkLastRenderedProgress = 1000;
    const statusA = overlap.context.loadStoreLinkSyncStatus();
    const statusB = overlap.context.loadStoreLinkSyncStatus();
    overlap.calls.forEach(call => call.respond({running: true, discovered_count: 1000}));
    await Promise.all([statusA, statusB]);
    results.push({
        scenario: 'Two activations overlap before either sync status request returns',
        simultaneous_status_requests: overlap.calls.length,
        active_poll_timers_after_both_responses: overlap.timers.filter(timer => !timer.cancelled).length,
        referenced_poll_timer_id: overlap.context.storeLinkSyncPollTimer,
    });
    const output = {
        tested_at: new Date().toISOString(),
        template: 'bit/templates/index.html',
        method: 'Real function source executed in a Node VM with mocked DOM, fetch, and timers; zero API access',
        results,
    };
    fs.writeFileSync(path.join(__dirname, 'frontend-request-reproduction.json'), JSON.stringify(output, null, 2) + '\n');
    console.log(JSON.stringify(output, null, 2));
}

main().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
