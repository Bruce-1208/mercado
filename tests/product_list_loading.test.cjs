const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('bit/templates/index.html', 'utf8');
const fn = html.slice(html.indexOf('        async function loadMercadoList()'), html.indexOf('        function setMercadoListFeedback'));
function setup(riskStatus = '') {
    let nextTimer = 0;
    const timers = new Map(), requests = [];
    const riskFilter = {value: riskStatus};
    const salespersonFilter = {value: ''};
    const context = vm.createContext({
        AbortController, URLSearchParams, TypeError,
        document: {getElementById: id => id === 'mercado-infringement-filter' ? riskFilter
            : id === 'mercado-salesperson-filter' ? salespersonFilter : null},
        mercadoListLoadSequence: 0, mercadoListLoadController: null,
        mercadoListBody: {}, mercadoListSummary: {}, mercadoSelectAll: {},
        mercadoListColumnCount: () => 21, mercadoListMode: 'products',
        mercadoListPageSize: 500, mercadoListPage: 1, mercadoProductSource: '',
        selectedMercadoCollectionIds: new Set([1]),
        updateMercadoSelection() {}, updateMercadoListPagination() {},
        renderMercadoListRows: rows => { context.mercadoListBody.innerHTML = 'rows:' + rows.length; },
        escapeHtml: String,
        setTimeout: (cb, ms) => { const id = ++nextTimer; timers.set(id, {cb, ms}); return id; },
        clearTimeout: id => timers.delete(id),
        fetch: (url, options) => new Promise((resolve, reject) => {
            requests.push({url, resolve, reject, signal: options.signal});
            options.signal?.addEventListener('abort', () => reject(new Error('aborted')));
        }),
    });
    for (const name of fn.matchAll(/(mercado\w+)\.value/g)) context[name[1]] = {value: ''};
    vm.runInContext(fn, context);
    const fire = ms => {
        const [id, timer] = [...timers].find(([, t]) => t.ms === ms);
        timers.delete(id); timer.cb();
    };
    return {context, timers, requests, fire, riskFilter};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const success = () => ({ok: true, json: async () => ({status: 'success', data: {rows: [{id: 2}], total: 1}})});
test('timeout exits loading without duplicating the slow server query', async () => {
    const {context, requests, timers, fire} = setup();
    const task = context.loadMercadoList();
    fire(30000); await task;
    assert.equal(requests.length, 1);
    assert.equal(requests[0].signal.aborted, true);
    assert.match(context.mercadoListBody.innerHTML, /查询超时/);
    assert.match(context.mercadoListBody.innerHTML, /重新加载/);
    assert.equal(context.mercadoListLoadController, null);
    assert.equal(timers.size, 0);
});
test('transient server error retries and succeeds', async () => {
    const {context, requests, timers, fire} = setup();
    const task = context.loadMercadoList();
    requests[0].resolve({ok: false, status: 503}); await tick();
    fire(1000); await tick(); requests[1].resolve(success()); await task;
    assert.equal(context.mercadoListBody.innerHTML, 'rows:1');
    assert.equal(context.mercadoCollectionLoaded, true);
    assert.equal(timers.size, 0);
});
test('authorization failures do not retry', async () => {
    const {context, requests, timers} = setup();
    const task = context.loadMercadoList();
    requests[0].resolve({ok: false, status: 403}); await task;
    assert.equal(requests.length, 1);
    assert.match(context.mercadoListBody.innerHTML, /403/);
    assert.equal(timers.size, 0);
});
test('new request cancels old response body and prevents stale results', async () => {
    const {context, requests, timers} = setup();
    let finishBody;
    const old = context.loadMercadoList();
    requests[0].resolve({ok: true, json: () => new Promise(resolve => { finishBody = resolve; })});
    await tick();
    const current = context.loadMercadoList();
    requests[1].resolve(success()); await current;
    finishBody({status: 'success', data: {rows: [], total: 999}}); await old;
    assert.equal(requests[0].signal.aborted, true);
    assert.equal(context.mercadoListTotal, 1);
    assert.equal(context.mercadoListBody.innerHTML, 'rows:1');
    assert.equal(timers.size, 0);
});
test('superseding a request cancels its retry delay', async () => {
    const {context, requests, timers} = setup();
    const old = context.loadMercadoList();
    requests[0].reject(new TypeError('network')); await tick();
    const current = context.loadMercadoList(); await old;
    requests[1].resolve(success()); await current;
    assert.equal(requests.length, 2);
    assert.equal(timers.size, 0);
});
test('infringement status adds an independent query filter and clears cleanly', async () => {
    const {context, requests, riskFilter} = setup('high');
    context.mercadoReviewFilter.value = 'approved';
    const first = context.loadMercadoList();
    let query = new URL(requests[0].url, 'http://console.test').searchParams;
    assert.equal(query.get('risk_status'), 'high');
    assert.equal(query.get('review_status'), 'approved');
    requests[0].resolve(success()); await first;
    riskFilter.value = '';
    const second = context.loadMercadoList();
    query = new URL(requests[1].url, 'http://console.test').searchParams;
    assert.equal(query.has('risk_status'), false);
    assert.equal(query.get('review_status'), 'approved');
    requests[1].resolve(success()); await second;
});

function functionSource(name) {
    const declaration = new RegExp(`^        (?:async )?function ${name}\\(`, 'm').exec(html);
    assert.ok(declaration, `Missing function ${name}`);
    const remaining = html.slice(declaration.index + declaration[0].length);
    const nextDeclaration = /^        (?:async )?function /m.exec(remaining);
    return html.slice(declaration.index, declaration.index + declaration[0].length + nextDeclaration.index);
}

function select(html = '<option value="">全部</option>') {
    let markup = '', current = '';
    const control = {
        options: [],
        get innerHTML() { return markup; },
        set innerHTML(value) {
            markup = value;
            this.options = [...value.matchAll(/<option value="([^"]*)"([^>]*)>(.*?)<\/option>/g)]
                .map(([, value, attributes, textContent]) => ({value, textContent, selected: /\bselected\b/.test(attributes)}));
            current = (this.options.find(option => option.selected) || this.options[0])?.value || '';
        },
        get value() { return current; },
        set value(value) { current = this.options.some(option => option.value === String(value)) ? String(value) : ''; },
        get selectedOptions() { return this.options.filter(option => option.value === current); },
    };
    control.innerHTML = html;
    return control;
}

function setupMenus() {
    const app = setup();
    const {context} = app;
    const classList = {toggle() {}, add() {}};
    const rowCategory = select('<option value="7" selected>已保存分类</option>');
    Object.assign(context, {
        console: {error() {}, info() {}},
        mercadoListMode: 'collection',
        mercadoListModule: {classList},
        mercadoProductFilters: {classList, querySelectorAll: () => []},
        mercadoListBody: {querySelectorAll: selector => selector === '.market-category-select' ? [rowCategory] : []},
        mercadoManagementCategories: [], mercadoManagementCategoriesLoaded: false,
        mercadoManagementCategoriesLoadPromise: null, mercadoManagementCategoriesLoadForced: false,
        zyingCollectionCategoriesLoaded: false, zyingCollectionCategoriesLoadPromise: null,
        zyingCollectionCategoriesLoadForced: false, zyingCollectionCategoryNames: new Map(),
        zyingProductDevelopersLoaded: false, zyingProductDevelopersLoadPromise: null,
        mercadoProductDevelopersLoadPromise: null,
        mercadoPublishStoresLoaded: false, mercadoPublishStoresLoadPromise: null,
        mercadoPublishStoresLoadForced: false,
        renderMercadoListHeader() {}, setMercadoListFeedback() {}, renderMercadoCategoryManager() {},
        renderMercadoPublishPicker() {},
        renderMercadoPublishStores() { context.mercadoPublishStoresLoaded = true; },
        zyingCategoryDisplayName: (name, id) => name || id,
    });
    for (const name of [
        'mercadoListKicker', 'mercadoListTitle', 'mercadoListDescription', 'mercadoListBadge',
        'mercadoListModuleLabel', 'mercadoFilterHint', 'mercadoCategoryMessage', 'mercadoPublishStatus',
    ]) context[name] = {};
    for (const name of [
        'mercadoCollectionActions', 'mercadoCollectionOnlyActions', 'mercadoProductActions',
        'mercadoProductReviewActions',
    ]) context[name] = {style: {}};
    for (const name of [
        'mercadoManagementCategoryFilter', 'mercadoManagementCategoryBulk', 'storeLinkProductCategoryFilter',
        'zyingCollectionCategory', 'zyingCollectionDeveloper', 'mercadoZyingCategoryFilter',
        'mercadoProductDeveloperFilter', 'mercadoPublishStore', 'mercadoPublishGroup',
    ]) context[name] = select();
    vm.runInContext([
        'mercadoManagementCategoryOptions', 'syncMercadoManagementCategoryOptions',
        'loadMercadoManagementCategories', 'renderZyingCollectionCategories', 'loadZyingCollectionCategories',
        'renderZyingProductDevelopers', 'loadZyingProductDevelopers', 'loadMercadoPublishStores',
        'renderMercadoProductDevelopers', 'loadMercadoProductDevelopers',
        'switchMercadoList',
    ].map(functionSource).join('\n'), context);
    return {...app, rowCategory};
}

const menuResponse = data => ({ok: true, json: async () => ({status: 'success', data})});
function finishMenus(app) {
    for (const request of app.requests) {
        if (request.url === '/api/mercado-management-categories') request.resolve(menuResponse({rows: [{id: 7, name: '已保存分类'}, {id: 8, name: '新选择分类'}]}));
        if (request.url === '/api/zying-collection/categories') request.resolve(menuResponse([{category_id: '9', category_name: '新选择智赢分类'}]));
        if (request.url === '/api/mercado-products/developers') request.resolve(menuResponse({rows: [{id: 10, name: '新选择产品开发'}]}));
    }
}

test('product rows load before delayed menus and late menus preserve filters without another list render', async () => {
    const app = setupMenus();
    const {context, requests, rowCategory} = app;
    let renderCount = 0;
    context.renderMercadoListRows = rows => { renderCount++; context.mercadoListBody.innerHTML = `rows:${rows.length}`; };
    context.switchMercadoList('products');
    assert.match(requests[0].url, /^\/api\/mercado-products\?/);
    assert.equal(requests.length, 4);
    requests[0].resolve(success());
    await tick();
    assert.equal(context.mercadoListBody.innerHTML, 'rows:1');
    assert.equal(context.mercadoManagementCategoriesLoaded, false);

    context.mercadoManagementCategoryFilter.innerHTML = '<option value="8" selected>新选择分类</option>';
    context.mercadoZyingCategoryFilter.innerHTML = '<option value="9" selected>新选择智赢分类</option>';
    context.mercadoProductDeveloperFilter.innerHTML = '<option value="10" selected>新选择产品开发</option>';
    finishMenus(app);
    await tick();
    assert.equal(context.mercadoManagementCategoryFilter.value, '8');
    assert.equal(context.mercadoZyingCategoryFilter.value, '9');
    assert.equal(context.mercadoProductDeveloperFilter.value, '10');
    assert.equal(rowCategory.value, '7');
    assert.ok(rowCategory.options.some(option => option.value === '8'));
    assert.equal(renderCount, 1);
    assert.equal(requests.filter(request => request.url.startsWith('/api/mercado-products?')).length, 1);
});

test('rapid list switching shares menu requests and late menu completion adds no product query', async () => {
    const app = setupMenus();
    const {context, requests} = app;
    context.switchMercadoList('products');
    context.switchMercadoList('products');
    assert.equal(requests.length, 4);
    context.switchMercadoList('collection');
    context.switchMercadoList('products');
    context.switchMercadoList('products');
    assert.equal(requests.length, 6);
    assert.equal(requests.filter(request => request.url === '/api/mercado-management-categories').length, 1);
    assert.equal(requests.filter(request => request.url === '/api/zying-collection/categories').length, 1);
    assert.equal(requests.filter(request => request.url === '/api/mercado-products/developers').length, 1);
    assert.equal(requests.filter(request => request.url === '/api/zying-collection/developers').length, 0);
    requests.at(-1).resolve(success());
    finishMenus(app);
    await tick();
    assert.equal(requests.length, 6);
    assert.equal(context.mercadoListMode, 'products');
    assert.equal(context.mercadoListBody.innerHTML, 'rows:1');
});

test('saved product category is displayed even before its menu has loaded', () => {
    const {context} = setupMenus();
    const markup = context.mercadoManagementCategoryOptions('7', {selectedLabel: '已保存分类'});
    const control = select(markup);
    assert.equal(control.value, '7');
    assert.equal(control.selectedOptions[0].textContent, '已保存分类');
});

for (const [name, loaded, pending, data] of [
    ['loadMercadoManagementCategories', 'mercadoManagementCategoriesLoaded', 'mercadoManagementCategoriesLoadPromise', {rows: []}],
    ['loadZyingCollectionCategories', 'zyingCollectionCategoriesLoaded', 'zyingCollectionCategoriesLoadPromise', []],
    ['loadMercadoPublishStores', 'mercadoPublishStoresLoaded', 'mercadoPublishStoresLoadPromise', {rows: []}],
]) {
    test(`${name} shares pending reads and queues one fresh forced refresh`, async () => {
        const {context, requests} = setupMenus();
        const initial = context[name]();
        const shared = context[name]();
        const forceA = context[name](true);
        const forceB = context[name](true);
        assert.equal(requests.length, 1);
        requests[0].resolve(menuResponse(data));
        await tick();
        assert.equal(requests.length, 2);
        const forceC = context[name](true);
        const sharedRefresh = context[name]();
        assert.equal(requests.length, 2);
        requests[1].resolve(menuResponse(data));
        await Promise.all([initial, shared, forceA, forceB, forceC, sharedRefresh]);
        assert.equal(context[loaded], true);
        assert.equal(context[pending], null);
        await context[name]();
        assert.equal(requests.length, 2);
    });
}

test('product developer reads share one request and a failed read can retry', async () => {
    const {context, requests} = setupMenus();
    const first = context.loadZyingProductDevelopers();
    const second = context.loadZyingProductDevelopers();
    assert.equal(requests.length, 1);
    requests[0].resolve({ok: false, status: 503, json: async () => ({status: 'error', message: '暂不可用'})});
    await Promise.all([first, second]);
    assert.equal(context.zyingProductDevelopersLoadPromise, null);
    const retry = context.loadZyingProductDevelopers();
    assert.equal(requests.length, 2);
    requests[1].resolve(menuResponse({rows: [{id: 10, name: '产品开发'}]}));
    await retry;
    assert.equal(context.zyingProductDevelopersLoaded, true);
});

test('saved product developer reads share a bounded request and retry without a ZYing selector', async () => {
    const {context, requests, timers, fire} = setupMenus();
    context.zyingCollectionDeveloper = null;
    const status = {}, retry = {};
    context.document.getElementById = id => id === 'mercado-product-developer-status' ? status : retry;
    const first = context.loadMercadoProductDevelopers();
    const shared = context.loadMercadoProductDevelopers();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/api/mercado-products/developers');
    fire(30000);
    await Promise.all([first, shared]);
    assert.equal(requests[0].signal.aborted, true);
    assert.match(status.textContent, /超时/);
    assert.equal(retry.hidden, false);
    assert.equal(timers.size, 0);
    assert.equal(context.mercadoProductDevelopersLoadPromise, null);
    const retried = context.loadMercadoProductDevelopers();
    requests[1].resolve(menuResponse({rows: [{id: '17', name: '开发甲'}]}));
    await retried;
    assert.equal(context.mercadoProductDeveloperFilter.options[1].textContent, '开发甲');
    assert.equal(retry.hidden, true);
    assert.equal(status.hidden, true);
});

test('refreshing saved product developers preserves an active choice even when the directory changes', () => {
    const {context} = setupMenus();
    context.mercadoProductDeveloperFilter.innerHTML = '<option value="17" selected>已选开发</option>';
    context.renderMercadoProductDevelopers([{id: '18', name: '新增开发'}]);
    assert.equal(context.mercadoProductDeveloperFilter.value, '17');
    assert.equal(context.mercadoProductDeveloperFilter.selectedOptions[0].textContent, '已选开发');
    context.renderZyingProductDevelopers([{id: '99', name: '另一智赢目录'}]);
    assert.equal(context.mercadoProductDeveloperFilter.value, '17');
    assert.equal(context.mercadoProductDeveloperFilter.options.length, 3);
});

test('two connection interruptions recover on the final bounded retry', async () => {
    const {context, requests, timers, fire} = setup();
    const task = context.loadMercadoList();
    requests[0].reject(new TypeError('Failed to fetch')); await tick();
    fire(1000); await tick();
    requests[1].reject(new TypeError('Failed to fetch')); await tick();
    fire(2000); await tick();
    requests[2].resolve(success()); await task;
    assert.equal(requests.length, 3);
    assert.equal(context.mercadoListBody.innerHTML, 'rows:1');
    assert.equal(timers.size, 0);
});

test('persistent network failure stops retrying and exposes a readable reload action', async () => {
    const {context, requests, timers, fire} = setup();
    const task = context.loadMercadoList();
    for (let attempt = 0; attempt < 3; attempt++) {
        requests[attempt].reject(new TypeError('Failed to fetch')); await tick();
        if (attempt < 2) {fire((attempt + 1) * 1000); await tick();}
    }
    await task;
    assert.equal(requests.length, 3);
    assert.match(context.mercadoListBody.innerHTML, /产品列表连接中断/);
    assert.match(context.mercadoListBody.innerHTML, /重新加载/);
    assert.doesNotMatch(context.mercadoListBody.innerHTML, /Failed to fetch/);
    assert.equal(timers.size, 0);
});

test('database timeout is explained without repeating an expensive query', async () => {
    const {context, requests, timers} = setup();
    const task = context.loadMercadoList();
    requests[0].resolve({ok: false, status: 500, json: async () => ({
        status: 'error', message: '读取产品列表失败：(3024, maximum statement execution time exceeded)',
    })});
    await task;
    assert.equal(requests.length, 1);
    assert.match(context.mercadoListBody.innerHTML, /缩小筛选范围/);
    assert.equal(timers.size, 0);
});

test('server non-retryable failures preserve their cause', async () => {
    const {context, requests, timers} = setup();
    const task = context.loadMercadoList();
    requests[0].resolve({ok: false, status: 503, json: async () => ({
        status: 'error', message: '产品服务维护中', retryable: false,
    })});
    await task;
    assert.equal(requests.length, 1);
    assert.match(context.mercadoListBody.innerHTML, /产品服务维护中/);
    assert.equal(timers.size, 0);
});

test('product queries show the full filtered total across pages', async () => {
    const {context, requests} = setup();
    context.mercadoListPageSize = 2;
    const task = context.loadMercadoList();
    assert.equal(new URL(requests[0].url, 'http://console.test').searchParams.get('include_total'), '1');
    requests[0].resolve({ok: true, json: async () => ({status: 'success', data: {
        rows: [{id: 3}, {id: 2}], total: 12345,
    }})});
    await task;
    assert.equal(requests.length, 1);
    assert.equal(context.mercadoListPages, 6173);
    assert.match(context.mercadoListSummary.textContent, /筛选结果共 12,345 件/);
    context.mercadoListPage = 2;
    const next = context.loadMercadoList();
    requests[1].resolve({ok: true, json: async () => ({status: 'success', data: {
        rows: [{id: 1}], total: 12345,
    }})});
    await next;
    assert.equal(context.mercadoListPages, 6173);
    assert.match(context.mercadoListSummary.textContent, /共 12,345 件 · 第 2\/6173 页/);
    assert.equal(requests.length, 2);
});

test('collection and product queries show an exact zero for empty filters', async () => {
    for (const mode of ['products', 'collection']) {
        const {context, requests} = setup();
        context.mercadoListMode = mode;
        context.mercadoListSearch.value = 'no matching products';
        const task = context.loadMercadoList();
        assert.equal(new URL(requests[0].url, 'http://console.test').searchParams.get('include_total'), '1');
        requests[0].resolve({ok: true, json: async () => ({status: 'success', data: {rows: [], total: 0}})});
        await task;
        assert.equal(context.mercadoListTotal, 0);
        assert.equal(context.mercadoListPages, 1);
        assert.match(context.mercadoListSummary.textContent, /筛选结果共 0 件/);
    }
});

test('unknown total keeps next-page navigation and disables misleading last-page jump', () => {
    const pagination = html.slice(html.indexOf('        function updateMercadoListPagination()'), html.indexOf('        function searchMercadoList()'));
    const context = vm.createContext({mercadoListTotalExact: false, mercadoListTotal: 3,
        mercadoListPage: 1, mercadoListPageSize: 2, mercadoListRows: [{id: 3}, {id: 2}]});
    for (const name of ['mercadoPageSummary', 'mercadoPageRange', 'mercadoPageInput', 'mercadoPageFirst',
        'mercadoPagePrev', 'mercadoPageNext', 'mercadoPageLast', 'mercadoPageSize']) context[name] = {};
    vm.runInContext(pagination, context);
    context.updateMercadoListPagination();
    assert.equal(context.mercadoPageNext.disabled, false);
    assert.equal(context.mercadoPageLast.disabled, true);
    assert.equal(context.mercadoPageSummary.textContent, '第 1 页');
    context.mercadoListPage = 2;
    context.mercadoListRows = [{id: 1}];
    context.updateMercadoListPagination();
    assert.equal(context.mercadoPageNext.disabled, true);
    assert.equal(context.mercadoPagePrev.disabled, false);
    assert.match(context.mercadoPageRange.textContent, /显示 3-3 件/);
});
