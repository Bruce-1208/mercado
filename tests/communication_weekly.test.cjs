const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(
    process.env.COMMUNICATION_TEMPLATE_PATH || path.join(__dirname, '../bit/templates/index.html'),
    'utf8',
);
function sourceFor(name) {
    const declaration = new RegExp(`^        (?:async )?function ${name}\\(`, 'm').exec(html);
    assert.ok(declaration, `Missing function ${name}`);
    const remaining = html.slice(declaration.index);
    const ending = /^        }/m.exec(remaining);
    assert.ok(ending, `Missing boundary after ${name}`);
    return remaining.slice(0, ending.index + ending[0].length);
}

function setup() {
    let now = new Date(2026, 9, 6, 12).getTime();
    class ClockDate extends Date {
        constructor(...args) { super(...(args.length ? args : [now])); }
        static now() { return now; }
    }
    const controls = new Map();
    for (const kind of ['pre-sale', 'customer-service']) {
        for (const suffix of ['start-date', 'end-date', 'search']) {
            controls.set(`${kind}-${suffix}`, {value: ''});
        }
    }
    const requests = {preSale: [], customerService: []};
    const c = vm.createContext({
        console, Date: ClockDate, currentWorkbenchUser: {id: 7},
        document: {getElementById: id => controls.get(id) || null, querySelectorAll: () => []},
        window: {},
        mercadoStoreTokensLoaded: true, mercadoStoreTokensLoading: false,
        mercadoStoreTokensLoadError: '', mercadoStoreTokenRows: [{id: 7}],
        preSaleStore: {}, preSaleLoad: {}, preSaleList: {}, preSalePagination: {},
        customerServiceStore: {}, customerServiceLoad: {}, customerServiceList: {}, customerServicePagination: {},
        canChangeTaskWorkers: false,
        escapeHtml: value => String(value),
        loadMercadoStoreTokens: async () => true,
        preSaleMatchedStores: () => c.mercadoStoreTokenRows,
        customerServiceTargetStores: () => c.mercadoStoreTokenRows,
        restoreCommunicationSnapshot: () => false,
        preSaleAggregateRequest: async (stores, filters) => {
            requests.preSale.push(JSON.parse(JSON.stringify({stores: stores.map(row => row.id), filters})));
            return {stores: stores.map(row => ({token_id: row.id, questions: [], official_total: 0}))};
        },
        customerServiceAggregateRequest: async (stores, options) => {
            requests.customerService.push(JSON.parse(JSON.stringify({stores: stores.map(row => row.id), ...options})));
            return {stores: stores.map(row => ({token_id: row.id, conversations: [], rows: [], total: 0}))};
        },
        customerServiceFailureDetails: () => [],
        translatePreSalePageRows: async () => 0,
        refreshPreSaleSummary: async () => {},
    });
    for (const name of [
        'renderPreSaleScopeFilters', 'renderPreSaleStores', 'renderPreSaleRows',
        'renderPreSalePagination', 'updatePreSaleBulkDelete', 'saveCommunicationSnapshot',
        'renderCustomerServiceScopeFilters', 'renderCustomerServiceStores',
        'restoreCustomerServiceLastUpdated', 'renderCustomerServiceQuickFilters',
        'scheduleCustomerServiceAutoUpdate', 'renderCustomerServiceTable',
        'renderCustomerServicePagination', 'setPreSaleMessage', 'setCustomerServiceMessage',
        'resetPreSaleCounts', 'warmCustomerServiceOtherMode',
    ]) c[name] = () => {};
    c.recordCustomerServiceUpdatedAt = () => vm.runInContext('customerServiceLastUpdatedAt = new Date().toISOString()', c);
    vm.runInContext(html.slice(html.indexOf('        let preSaleLoaded ='), html.indexOf('        let storeLinksLoaded =')), c);
    const functions = [
        'customerServiceDateInputValue', 'customerServiceCalendarDate', 'setCommunicationDefaultDateRange',
        'setPreSaleDefaultDateRange', 'setCustomerServiceDefaultDateRange',
        'preSaleFilterPayload', 'preSaleDataIsStale', 'customerServiceDataIsStale',
        'loadPreSaleModule', 'loadPreSaleQuestions', 'resetPreSaleView',
        'loadCustomerService', 'loadCustomerServiceData', 'resetCustomerServiceView',
        'customerServiceQueryKey', 'customerServiceLoadPostSale', 'customerServicePostSaleRows', 'customerServiceApplyFilters',
    ];
    vm.runInContext(functions.map(sourceFor).join('\n'), c);
    return {
        context: c, controls, requests,
        value: expression => vm.runInContext(expression, c),
        advance: milliseconds => { now += milliseconds; },
    };
}

test('both modules automatically request all statuses in the inclusive recent seven days', async () => {
    const app = setup();
    await Promise.all([app.context.loadPreSaleModule(), app.context.loadCustomerService()]);
    for (const kind of ['pre-sale', 'customer-service']) {
        assert.equal(app.controls.get(`${kind}-start-date`).value, '2026-09-30');
        assert.equal(app.controls.get(`${kind}-end-date`).value, '2026-10-06');
    }
    assert.equal(app.value('preSaleStatus'), '');
    assert.deepEqual(app.requests.preSale, [{stores: [7], filters: {
        item_id: '', user_id: '', date_from: '2026-09-30', date_to: '2026-10-06',
    }}]);
    assert.deepEqual(app.requests.customerService, [{stores: [7], mode: 'claims', refresh: false, required_rows: 20, status_filter: '', filters: {
        claim_id: '', claim_type: '', order_id: '', date_from: '2026-09-30', date_to: '2026-10-06',
    }}]);
});

test('claims and post-sale send the same recent-week date boundaries', async () => {
    const app = setup();
    app.value("customerServiceMode = 'post-sale'");
    await app.context.loadCustomerService();
    assert.equal(app.requests.customerService[0].mode, 'post-sale');
    assert.equal(app.requests.customerService[0].filters.date_from, '2026-09-30');
    assert.equal(app.requests.customerService[0].filters.date_to, '2026-10-06');
});

test('CBT replies use the marketplace seller in both summary and detail', () => {
    const app = setup();
    const c = app.context;
    c.customerServiceSelectedStore = () => ({id: 7, meli_user_id: '3502155126'});
    c.customerServiceDetail = {innerHTML: ''};
    c.hasWorkbenchPermission = () => false;
    c.customerServiceDate = value => value;
    c.customerServiceText = value => value;
    vm.runInContext([sourceFor('customerServiceConversationRow'), sourceFor('renderPostSaleDetail')].join('\n'), c);
    app.value(`
        var bundle = {seller_id: '3502163832', messages: [
            {from: {user_id: '88'}, to: {user_id: '3502163832'}, text: '买家提问', message_date: {created: '2026-10-01T00:00:00Z'}},
            {from: {user_id: '3502163832'}, to: {user_id: '88'}, text: '卖家回复', message_date: {created: '2026-10-02T00:00:00Z'}}
        ]};
        var row = customerServiceConversationRow('123', 0, bundle, {}, 7);
        renderPostSaleDetail('123', row.messages, row.conversation_status, row.token_id, row.seller_id);
    `);
    assert.equal(app.value('row.reply_state'), 'replied');
    assert.equal(app.value('row.buyer_name'), '88');
    assert.equal((c.customerServiceDetail.innerHTML.match(/customer-service-bubble outgoing/g) || []).length, 1);
    assert.match(c.customerServiceDetail.innerHTML, /卖家/);
});

test('post-sale includes Shanghai week boundaries and excludes the adjacent instants', () => {
    const app = setup();
    app.context.setCustomerServiceDefaultDateRange();
    app.value(`customerServiceMode = 'post-sale'; customerServiceAllRows = [
        {id: 'before', latest_at: '2026-09-29T15:59:59Z', last_updated: '2026-10-02T00:00:00Z'},
        {id: 'first', latest_at: '2026-09-29T16:00:00Z', last_updated: '2026-10-07T00:00:00Z'},
        {id: 'last', latest_at: '2026-10-06T15:59:59Z', last_updated: '2026-09-29T00:00:00Z'},
        {id: 'after', latest_at: '2026-10-06T16:00:00Z', last_updated: '2026-10-02T00:00:00Z'},
    ]`);
    app.context.customerServiceApplyFilters();
    assert.deepEqual(JSON.parse(app.value('JSON.stringify(customerServiceRows.map(row => row.id).sort())')), ['first', 'last']);
    assert.equal(app.value('customerServiceTotal'), 2);
});

test('claims filter by Shanghai creation dates even when updates fall in a different week', () => {
    const app = setup();
    app.context.setCustomerServiceDefaultDateRange();
    app.value(`customerServiceMode = 'claims'; customerServiceAllRows = [
        {id: 'before', date_created: '2026-09-29T15:59:59Z', last_updated: '2026-10-02T00:00:00Z'},
        {id: 'first', date_created: '2026-09-29T16:00:00Z', last_updated: '2026-10-07T00:00:00Z'},
        {id: 'last', date_created: '2026-10-06T15:59:59Z', last_updated: '2026-10-08T00:00:00Z'},
        {id: 'after', date_created: '2026-10-06T16:00:00Z', last_updated: '2026-10-02T00:00:00Z'},
    ]`);
    app.context.customerServiceApplyFilters();
    assert.deepEqual(JSON.parse(app.value('JSON.stringify(customerServiceRows.map(row => row.id).sort())')), ['first', 'last']);
    assert.equal(app.value('customerServiceTotal'), 2);
});

test('a successful empty preload is reused when opening either module', async () => {
    const app = setup();
    await Promise.all([app.context.loadPreSaleModule(), app.context.loadCustomerService()]);
    assert.ok(app.value('preSaleLastUpdatedAt'));
    assert.ok(app.value('customerServiceLastUpdatedAt'));
    assert.equal(app.value('preSaleRows.length'), 0);
    assert.equal(app.value('customerServiceAllRows.length'), 0);
    await Promise.all([app.context.loadPreSaleModule(), app.context.loadCustomerService()]);
    assert.equal(app.requests.preSale.length, 1);
    assert.equal(app.requests.customerService.length, 1);
});

test('after-sales cache expires hourly while pre-sale keeps its twelve-hour interval', async () => {
    const app = setup();
    app.advance(-12 * 60 * 60 * 1000);
    await Promise.all([app.context.loadPreSaleModule(), app.context.loadCustomerService()]);
    app.advance(60 * 60 * 1000 - 1);
    await Promise.all([app.context.loadPreSaleModule(), app.context.loadCustomerService()]);
    assert.equal(app.requests.preSale.length, 1);
    assert.equal(app.requests.customerService.length, 1);
    app.advance(1);
    await Promise.all([app.context.loadPreSaleModule(), app.context.loadCustomerService()]);
    assert.equal(app.requests.preSale.length, 1);
    assert.equal(app.requests.customerService.length, 2);
    app.advance(11 * 60 * 60 * 1000);
    await Promise.all([app.context.loadPreSaleModule(), app.context.loadCustomerService()]);
    assert.equal(app.requests.preSale.length, 2);
    assert.equal(app.requests.customerService.length, 3);
});

test('a new day rolls the default week and reloads both modules', async () => {
    const app = setup();
    app.advance((11 * 60 + 50) * 60 * 1000);
    await Promise.all([app.context.loadPreSaleModule(), app.context.loadCustomerService()]);
    app.advance(20 * 60 * 1000);
    await Promise.all([app.context.loadPreSaleModule(), app.context.loadCustomerService()]);
    for (const kind of ['pre-sale', 'customer-service']) {
        assert.equal(app.controls.get(`${kind}-start-date`).value, '2026-10-01');
        assert.equal(app.controls.get(`${kind}-end-date`).value, '2026-10-07');
    }
    assert.equal(app.requests.preSale[1].filters.date_from, '2026-10-01');
    assert.equal(app.requests.customerService[1].filters.date_to, '2026-10-07');
    assert.equal(app.requests.preSale.length, 2);
    assert.equal(app.requests.customerService.length, 2);
});

test('custom date ranges survive default initialization and a day rollover', () => {
    const app = setup();
    for (const kind of ['pre-sale', 'customer-service']) {
        const setter = kind === 'pre-sale' ? app.context.setPreSaleDefaultDateRange : app.context.setCustomerServiceDefaultDateRange;
        setter();
        app.controls.get(`${kind}-start-date`).value = '2026-08-01';
        app.controls.get(`${kind}-end-date`).value = '2026-08-31';
    }
    app.advance(24 * 60 * 60 * 1000);
    app.context.setPreSaleDefaultDateRange();
    app.context.setCustomerServiceDefaultDateRange();
    for (const kind of ['pre-sale', 'customer-service']) {
        assert.equal(app.controls.get(`${kind}-start-date`).value, '2026-08-01');
        assert.equal(app.controls.get(`${kind}-end-date`).value, '2026-08-31');
    }
});

test('pre-sale authorization-only initialization does not recursively query', async () => {
    const app = setup();
    await app.context.loadPreSaleModule(false, {loadData: false});
    assert.equal(app.requests.preSale.length, 0);
    assert.equal(app.value('preSaleLoaded'), true);
    await app.context.loadPreSaleQuestions(1);
    assert.equal(app.requests.preSale.length, 1);
});

function preloadSetup({permission = true, idle = true} = {}) {
    const callbacks = [];
    const loaded = [];
    const c = vm.createContext({
        Promise,
        hasWorkbenchPermission: name => permission && name === 'customer_service.view',
        loadPreSaleModule: async () => loaded.push('pre-sale'),
        refreshCustomerServiceInBackground: async () => loaded.push('customer-service'),
        window: {setTimeout: (callback, delay) => callbacks.push({callback, delay})},
    });
    if (idle) c.window.requestIdleCallback = (callback, options) => callbacks.push({callback, options});
    vm.runInContext(sourceFor('scheduleCommunicationPreload'), c);
    c.scheduleCommunicationPreload();
    return {callbacks, loaded};
}

test('startup defers both data preloads until the browser is idle', async () => {
    const app = preloadSetup();
    assert.deepEqual(app.loaded, []);
    assert.equal(app.callbacks.length, 1);
    assert.equal(app.callbacks[0].options.timeout, 3000);
    await app.callbacks[0].callback();
    assert.deepEqual(app.loaded, ['pre-sale', 'customer-service']);
});

test('startup falls back to a deferred timer when idle callbacks are unavailable', async () => {
    const app = preloadSetup({idle: false});
    assert.equal(app.callbacks.length, 1);
    assert.equal(app.callbacks[0].delay, 0);
    await app.callbacks[0].callback();
    assert.deepEqual(app.loaded, ['pre-sale', 'customer-service']);
});

test('users without customer service access do not preload communication data', () => {
    const app = preloadSetup({permission: false});
    assert.deepEqual(app.callbacks, []);
    assert.deepEqual(app.loaded, []);
});
