const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('bit/templates/index.html', 'utf8');
const fn = html.slice(html.indexOf('        async function loadMercadoList()'), html.indexOf('        function setMercadoListFeedback'));
function setup() {
    let nextTimer = 0;
    const timers = new Map(), requests = [];
    const context = vm.createContext({
        AbortController, URLSearchParams, TypeError,
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
            requests.push({resolve, reject, signal: options.signal});
            options.signal.addEventListener('abort', () => reject(new Error('aborted')));
        }),
    });
    for (const name of fn.matchAll(/(mercado\w+)\.value/g)) context[name[1]] = {value: ''};
    vm.runInContext(fn, context);
    const fire = ms => {
        const [id, timer] = [...timers].find(([, t]) => t.ms === ms);
        timers.delete(id); timer.cb();
    };
    return {context, timers, requests, fire};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const success = () => ({ok: true, json: async () => ({status: 'success', data: {rows: [{id: 2}], total: 1}})});
test('timeout retries once then exits loading with a manual retry button', async () => {
    const {context, requests, timers, fire} = setup();
    const task = context.loadMercadoList();
    fire(30000); await tick();
    assert.match(context.mercadoListBody.innerHTML, /自动重试/);
    fire(1000); await tick();
    fire(30000); await task;
    assert.equal(requests.length, 2);
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
