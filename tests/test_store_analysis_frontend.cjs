const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../bit/static/store-analysis.js'), 'utf8');

function setup() {
    const elements = new Map();
    const calls = [];
    const pending = [];
    const context = vm.createContext({
        Date, URLSearchParams, Map, Set,
        document: {getElementById(id) {
            if (!elements.has(id)) elements.set(id, {value: id.endsWith('-metric') ? 'orders' : '', textContent: '', innerHTML: '', addEventListener() {}});
            return elements.get(id);
        }},
        enterpriseScopedUrl: url => url,
        fetch: url => {
            calls.push(url);
            return new Promise(resolve => pending.push({url, resolve}));
        },
    });
    vm.runInContext(source, context);
    function respond(prefix, data, ok = true) {
        const index = pending.findIndex(request => request.url.startsWith(prefix));
        assert.notEqual(index, -1);
        pending.splice(index, 1)[0].resolve({ok, json: async () => ({status: ok ? 'success' : 'error', data, message: 'failed'})});
    }
    return {context, elements, calls, respond};
}

test('defaults to seven Beijing days and loads analysis before filters; deduplicates and reuses results', async () => {
    const app = setup();
    const first = app.context.loadStoreAnalysis();
    const second = app.context.loadStoreAnalysis();
    assert.equal(app.calls.length, 2);
    const params = new URLSearchParams(app.calls[1].split('?')[1]);
    assert.equal((new Date(params.get('end_date')) - new Date(params.get('start_date'))) / 86400000, 6);
    assert.equal(params.get('category_level'), '2');
    assert.equal(params.get('token_id'), '');
    app.respond('/api/store-analysis', {sites: []});
    await Promise.all([first, second]);
    assert.match(app.elements.get('store-analysis-sites').innerHTML, /没有有效销售订单/);
    await app.context.loadStoreAnalysis();
    assert.equal(app.calls.length, 2);
    const forced = app.context.loadStoreAnalysis(true);
    assert.equal(app.calls.length, 3);
    app.respond('/api/store-analysis', {sites: []});
    await forced;
    app.respond('/api/mercado-tokens', {rows: []});
});

test('older responses cannot overwrite a newer filter selection; failed filters do not block results', async () => {
    const app = setup();
    const first = app.context.loadStoreAnalysis();
    app.elements.get('store-analysis-category-level').value = '3';
    const second = app.context.loadStoreAnalysis();
    app.respond('/api/mercado-tokens', {}, false);
    app.respond('/api/store-analysis', {sites: [{site_id: 'OLD'}]});
    app.respond('/api/store-analysis', {sites: []});
    await Promise.all([first, second]);
    assert.doesNotMatch(app.elements.get('store-analysis-sites').innerHTML, /OLD/);
    assert.match(app.elements.get('store-analysis-filter-message').textContent, /筛选项加载失败/);
    assert.doesNotMatch(app.elements.get('store-analysis-message').textContent, /分析失败/);
});

test('forced refresh failures can be retried', async () => {
    const app = setup();
    const first = app.context.loadStoreAnalysis();
    app.respond('/api/mercado-tokens', {rows: []});
    app.respond('/api/store-analysis', {sites: []});
    await first;
    vm.runInContext('for (const result of storeAnalysisResults.values()) result.time -= 61000;', app.context);
    const expired = app.context.loadStoreAnalysis(true);
    assert.equal(app.calls.length, 3);
    app.respond('/api/store-analysis', {}, false);
    await expired;
    assert.match(app.elements.get('store-analysis-message').textContent, /分析失败/);
    const retry = app.context.loadStoreAnalysis(true);
    assert.equal(app.calls.length, 4);
    app.respond('/api/store-analysis', {sites: []});
    await retry;
    assert.doesNotMatch(app.elements.get('store-analysis-message').textContent, /分析失败/);
});


test('order trend remains visible alongside pie and with no valid sales', () => {
    const app = setup();
    const data = {order_trend: {total_orders: 3, days: [
        {date: '2026-09-29', orders: 3, previous_orders: 0, change_rate: null}
    ]}, sites: [{site_name: '墨西哥', site_id: 'MLM', orders: 2, top_categories: []}]};
    app.context.renderStoreAnalysis(data);
    assert.match(app.elements.get('store-analysis-trend').innerHTML, /polyline/);
    assert.match(app.elements.get('store-analysis-trend').innerHTML, /新增（前日0单）/);
    assert.match(app.elements.get('store-analysis-sites').innerHTML, /store-analysis-pie/);
    app.context.renderStoreAnalysis({...data, sites: []});
    assert.match(app.elements.get('store-analysis-trend').innerHTML, /单量变化率/);
});
