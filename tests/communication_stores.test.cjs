const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(
    process.env.COMMUNICATION_TEMPLATE_PATH || path.join(__dirname, '../bit/templates/index.html'),
    'utf8',
);
const functionNames = [
    'loadMercadoStoreTokens', 'loadPreSaleModule', 'loadCustomerService',
    'loadPreSaleQuestions', 'loadCustomerServiceData',
];
const source = functionNames.map(name => {
    const declaration = new RegExp(`^        (?:async )?function ${name}\\(`, 'm').exec(template);
    assert.ok(declaration, `Missing function ${name}`);
    const remaining = template.slice(declaration.index + declaration[0].length);
    const nextDeclaration = /^        (?:async )?function /m.exec(remaining);
    assert.ok(nextDeclaration, `Missing boundary after ${name}`);
    return template.slice(declaration.index, declaration.index + declaration[0].length + nextDeclaration.index);
}).join('\n');

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return {promise, resolve};
}

const authorizedStores = [{id: 7, display_name: '已授权店铺'}];
const success = () => ({
    ok: true,
    json: async () => ({status: 'success', data: {rows: authorizedStores}}),
});
const failure = message => ({
    ok: false, status: 503,
    json: async () => ({status: 'error', message}),
});

function setup(respond, {withTokenTable = true} = {}) {
    const calls = [];
    const messages = {preSale: [], customerService: []};
    const storeReads = [];
    const queries = {preSale: [], customerService: []};
    const context = vm.createContext({
        console,
        document: {getElementById: () => null, querySelectorAll: () => []},
        mercadoStoreTokensLoaded: false,
        mercadoStoreTokensLoading: false,
        mercadoStoreTokensLoadPromise: null,
        mercadoStoreTokensLoadError: '',
        mercadoStoreTokenRows: [],
        mercadoTokenBody: withTokenTable ? {innerHTML: ''} : null,
        storeLinksLoaded: true,
        preSaleStore: {}, preSaleLoaded: false, preSaleQueryKey: '',
        preSaleLoading: false, preSaleLastUpdatedAt: '',
        preSaleLoad: {}, preSaleList: {}, preSalePagination: {},
        preSaleRows: [], preSalePageSize: 20, preSaleStatus: '',
        selectedPreSaleQuestionIds: new Set(),
        customerServiceStore: {}, customerServiceLoaded: false,
        customerServiceLoad: {}, customerServiceList: {}, customerServicePagination: {},
        customerServiceLoading: false, customerServiceHasSnapshot: true,
        customerServiceMode: 'post-sale', customerServiceLastQueryKey: '',
        customerServiceQueryMetrics: null, customerServiceLoadGeneration: 0,
        canChangeTaskWorkers: false,
        fetch: async (url, options) => {
            calls.push({url, options});
            return respond(calls.length);
        },
        escapeHtml: value => String(value),
        renderMercadoStoreTokens: rows => { context.mercadoStoreTokenRows = rows; },
        loadMercadoAccountGroups: async () => [],
        setPreSaleMessage: (message, error = false) => messages.preSale.push({message, error}),
        setCustomerServiceMessage: (message, error = false) => messages.customerService.push({message, error}),
        preSaleMatchedStores: () => {
            storeReads.push('preSale');
            return context.mercadoStoreTokenRows;
        },
        customerServiceTargetStores: () => {
            storeReads.push('customerService');
            return context.mercadoStoreTokenRows;
        },
        restoreCommunicationSnapshot: () => false,
        preSaleDataIsStale: () => false,
        customerServiceDataIsStale: () => false,
        preSaleFilterPayload: () => ({}),
        preSaleAggregateRequest: async stores => {
            queries.preSale.push(stores.map(store => store.id));
            return {stores: stores.map(store => ({token_id: store.id, questions: [], official_total: 0}))};
        },
        translatePreSalePageRows: async () => 0,
        refreshPreSaleSummary: async () => {},
        customerServiceQueryKey: stores => JSON.stringify(stores.map(store => store.id)),
        customerServiceLoadPostSale: async stores => {
            queries.customerService.push(stores.map(store => store.id));
            return [];
        },
    });
    for (const name of [
        'renderPreSaleScopeFilters', 'renderPreSaleStores', 'renderPreSaleRows',
        'renderPreSalePagination', 'updatePreSaleBulkDelete', 'saveCommunicationSnapshot',
        'renderCustomerServiceScopeFilters', 'renderCustomerServiceStores',
        'setPreSaleDefaultDateRange', 'setCustomerServiceDefaultDateRange', 'restoreCustomerServiceLastUpdated',
        'renderCustomerServiceQuickFilters', 'scheduleCustomerServiceAutoUpdate',
        'customerServiceApplyFilters', 'recordCustomerServiceUpdatedAt', 'warmCustomerServiceOtherMode',
    ]) context[name] = () => {};
    vm.runInContext(source, context);
    return {context, calls, messages, storeReads, queries};
}

function assertNoMissingStoreMessage(messages) {
    for (const moduleMessages of Object.values(messages)) {
        assert.ok(moduleMessages.every(({message}) => !/暂无可用授权店铺|没有可用授权店铺|没有可查询的授权店铺/.test(message)));
    }
}

test('both communication modules wait for one shared authorization request', async () => {
    const response = deferred();
    const app = setup(() => response.promise);
    const preSale = app.context.loadPreSaleModule();
    const customerService = app.context.loadCustomerService();
    await Promise.resolve();

    assert.equal(app.calls.length, 1);
    assert.equal(app.calls[0].url, '/api/mercado-tokens');
    assert.equal(app.context.mercadoStoreTokensLoading, true);
    assert.equal(app.context.preSaleLoaded, false);
    assert.equal(app.context.customerServiceLoaded, false);
    assert.deepEqual(app.storeReads, []);
    assertNoMissingStoreMessage(app.messages);

    response.resolve(success());
    await Promise.all([preSale, customerService]);
    assert.equal(app.context.preSaleLoaded, true);
    assert.equal(app.context.customerServiceLoaded, true);
    assert.deepEqual(app.context.mercadoStoreTokenRows, authorizedStores);
    assert.equal(app.context.mercadoStoreTokensLoading, false);
    assert.equal(app.context.mercadoStoreTokensLoadPromise, null);
    assertNoMissingStoreMessage(app.messages);
});

test('already initialized modules wait for an ongoing background authorization refresh', async () => {
    const response = deferred();
    const app = setup(() => response.promise);
    app.context.mercadoStoreTokensLoaded = true;
    app.context.preSaleLoaded = true;
    app.context.customerServiceLoaded = true;
    const refresh = app.context.loadMercadoStoreTokens(true, true);
    const preSale = app.context.loadPreSaleModule();
    const customerService = app.context.loadCustomerService();
    await Promise.resolve();

    assert.equal(app.calls.length, 1);
    assert.equal(app.context.mercadoStoreTokensLoading, true);
    assert.deepEqual(app.storeReads, []);
    assertNoMissingStoreMessage(app.messages);

    response.resolve(success());
    assert.equal(await refresh, true);
    await Promise.all([preSale, customerService]);
    assert.ok(app.storeReads.includes('preSale'));
    assert.ok(app.storeReads.includes('customerService'));
    assertNoMissingStoreMessage(app.messages);
});

test('authorization failure preserves the reason and both modules can retry', async () => {
    const response = deferred();
    const reason = '授权服务暂不可用';
    const app = setup(call => call === 1 ? response.promise : success());
    const authorization = app.context.loadMercadoStoreTokens();
    const preSale = app.context.loadPreSaleModule();
    const customerService = app.context.loadCustomerService();
    response.resolve(failure(reason));
    assert.equal(await authorization, false);
    await Promise.all([preSale, customerService]);

    assert.equal(app.calls.length, 1);
    assert.equal(app.context.mercadoStoreTokensLoaded, false);
    assert.equal(app.context.preSaleLoaded, false);
    assert.equal(app.context.customerServiceLoaded, false);
    assert.equal(app.context.mercadoStoreTokensLoadError, reason);
    assert.equal(app.context.mercadoStoreTokensLoading, false);
    assert.equal(app.context.mercadoStoreTokensLoadPromise, null);
    for (const moduleMessages of Object.values(app.messages)) {
        assert.ok(moduleMessages.some(({message, error}) => error && message.includes(`授权店铺加载失败：${reason}`)));
    }
    assertNoMissingStoreMessage(app.messages);

    await Promise.all([app.context.loadPreSaleModule(), app.context.loadCustomerService()]);
    assert.equal(app.calls.length, 2);
    assert.equal(app.context.mercadoStoreTokensLoaded, true);
    assert.equal(app.context.preSaleLoaded, true);
    assert.equal(app.context.customerServiceLoaded, true);
    assert.equal(app.context.mercadoStoreTokensLoadError, '');
});

test('authorization loads without a store table and returns a shared success result', async () => {
    const response = deferred();
    const app = setup(() => response.promise, {withTokenTable: false});
    const first = app.context.loadMercadoStoreTokens();
    const second = app.context.loadMercadoStoreTokens();
    assert.equal(app.calls.length, 1);
    response.resolve(success());

    assert.deepEqual(await Promise.all([first, second]), [true, true]);
    assert.deepEqual(app.context.mercadoStoreTokenRows, authorizedStores);
    assert.equal(app.context.mercadoStoreTokensLoaded, true);
    assert.equal(await app.context.loadMercadoStoreTokens(), true);
    assert.equal(app.calls.length, 1);
});

test('query and update buttons wait for authorization before choosing stores', async () => {
    const response = deferred();
    const app = setup(() => response.promise);
    const authorization = app.context.loadMercadoStoreTokens();
    const preSale = app.context.loadPreSaleQuestions(1);
    const customerService = app.context.loadCustomerServiceData(1);
    await Promise.resolve();

    assert.equal(app.calls.length, 1);
    assert.deepEqual(app.storeReads, []);
    assert.deepEqual(app.queries, {preSale: [], customerService: []});
    assertNoMissingStoreMessage(app.messages);

    response.resolve(success());
    assert.equal(await authorization, true);
    await Promise.all([preSale, customerService]);
    assert.deepEqual(app.queries, {preSale: [[7]], customerService: [[7]]});
    assert.equal(app.context.preSaleLoad.disabled, false);
    assert.equal(app.context.customerServiceLoad.disabled, false);
    assertNoMissingStoreMessage(app.messages);
});

test('query and update buttons retain authorization errors and retry on the next click', async () => {
    const reason = '授权接口连接超时';
    const app = setup(call => call === 1 ? failure(reason) : success());
    await Promise.all([app.context.loadPreSaleQuestions(1), app.context.loadCustomerServiceData(1)]);

    assert.equal(app.calls.length, 1);
    assert.deepEqual(app.queries, {preSale: [], customerService: []});
    assert.equal(app.context.preSaleLoaded, false);
    assert.equal(app.context.customerServiceLoaded, false);
    for (const moduleMessages of Object.values(app.messages)) {
        assert.ok(moduleMessages.some(({message, error}) => error && message.includes(`授权店铺加载失败：${reason}`)));
    }
    assertNoMissingStoreMessage(app.messages);

    await Promise.all([app.context.loadPreSaleQuestions(1), app.context.loadCustomerServiceData(1)]);
    assert.equal(app.calls.length, 2);
    assert.deepEqual(app.queries, {preSale: [[7]], customerService: [[7]]});
    assert.equal(app.context.mercadoStoreTokensLoadError, '');
    assertNoMissingStoreMessage(app.messages);
});

test('an automatic update and a click waiting for authorization share the data request', async () => {
    const response = deferred();
    const data = deferred();
    const app = setup(() => response.promise);
    app.context.customerServiceHasSnapshot = false;
    app.context.customerServiceLoadPostSale = async stores => {
        app.queries.customerService.push(stores.map(store => store.id));
        return data.promise;
    };
    const automatic = app.context.loadCustomerServiceData(1, {automatic: true});
    const click = app.context.loadCustomerServiceData(1);
    response.resolve(success());
    await new Promise(setImmediate);
    data.resolve([]);
    await Promise.all([automatic, click]);

    assert.equal(app.calls.length, 1);
    assert.deepEqual(app.queries.customerService, [[7]]);
    assertNoMissingStoreMessage(app.messages);
});

test('a pre-sale preload and a click waiting for authorization share the data request', async () => {
    const authorization = deferred();
    const data = deferred();
    const app = setup(() => authorization.promise);
    app.context.preSaleAggregateRequest = async stores => {
        app.queries.preSale.push(stores.map(store => store.id));
        return data.promise;
    };
    const preload = app.context.loadPreSaleModule();
    const click = app.context.loadPreSaleQuestions(1);
    authorization.resolve(success());
    await new Promise(setImmediate);
    data.resolve({stores: [{token_id: 7, questions: [], official_total: 0}]});
    await Promise.all([preload, click]);

    assert.equal(app.calls.length, 1);
    assert.deepEqual(app.queries.preSale, [[7]]);
    assert.equal(app.context.preSaleLoading, false);
    assertNoMissingStoreMessage(app.messages);
});
