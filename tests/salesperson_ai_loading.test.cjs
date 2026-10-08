const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function statsSetup() {
    const nodes = new Map();
    const requests = [];
    let organization = 'first';
    const makeNode = () => ({value: '', textContent: '', children: [], listeners: {},
        addEventListener(name, callback) { this.listeners[name] = callback; },
        appendChild(child) { this.children.push(child); },
        replaceChildren(fragment) { this.children = fragment ? [...fragment.children] : []; },
    });
    const context = vm.createContext({Date, URLSearchParams,
        enterpriseScopedUrl: url => `${url}&enterprise_key=${organization}`,
        document: {
            getElementById(id) { if (!nodes.has(id)) nodes.set(id, makeNode()); return nodes.get(id); },
            createElement: makeNode,
            createDocumentFragment: makeNode,
        },
        fetch(url) { return new Promise(resolve => requests.push({url, resolve})); },
    });
    vm.runInContext(fs.readFileSync('bit/static/salesperson-stats.js', 'utf8'), context);
    const reply = (index, salesperson, ok = true) => requests[index].resolve({ok,
        json: async () => ({status: ok ? 'success' : 'error', message: 'temporary failure',
            data: {rows: [{salesperson, operator_id: 1, print_count: 3, purchase_count: 2}], print_count: 3, purchase_count: 2}}),
    });
    return {context, nodes, requests, reply, changeOrganization(value) { organization = value; }};
}

test('salesperson statistics reuse concurrent activation and recent scoped results; submit refreshes', async () => {
    const app = statsSetup();
    const first = app.context.loadSalespersonStats();
    const second = app.context.loadSalespersonStats();
    assert.equal(app.requests.length, 1);
    app.reply(0, 'Alice');
    await Promise.all([first, second]);
    await app.context.loadSalespersonStats();
    assert.equal(app.requests.length, 1);
    const refresh = app.context.loadSalespersonStats(true);
    assert.equal(app.requests.length, 2);
    assert.equal(app.nodes.get('salesperson-stats-rows').children.length, 1, 'existing rows remain during refresh');
    app.reply(1, 'Bob');
    await refresh;
    assert.match(app.nodes.get('salesperson-stats-rows').children[0].children[0].textContent, /Bob/);
    app.nodes.get('salesperson-stats-form').listeners.submit({preventDefault() {}});
    assert.equal(app.requests.length, 3, 'form submission bypasses the recent cache');
    app.reply(2, 'Bob');
    await new Promise(setImmediate);
    app.changeOrganization('second');
    const other = app.context.loadSalespersonStats();
    assert.equal(app.requests.length, 4, 'organization changes use a separate cache entry');
    app.reply(3, 'Carol');
    await other;
});

test('salesperson statistics ignore older filter responses and retry failed requests', async () => {
    const app = statsSetup();
    const old = app.context.loadSalespersonStats();
    app.nodes.get('salesperson-stats-start').value = '2026-10-01';
    const current = app.context.loadSalespersonStats();
    app.reply(1, 'Current');
    await current;
    app.reply(0, 'Old');
    await old;
    assert.match(app.nodes.get('salesperson-stats-rows').children[0].children[0].textContent, /Current/);
    const failed = app.context.loadSalespersonStats(true);
    app.reply(2, '', false);
    await failed;
    assert.match(app.nodes.get('salesperson-stats-message').textContent, /temporary failure/);
    const retry = app.context.loadSalespersonStats(true);
    assert.equal(app.requests.length, 4);
    app.reply(3, 'Recovered');
    await retry;
    assert.match(app.nodes.get('salesperson-stats-rows').children[0].children[0].textContent, /Recovered/);
});

function originalSetup() {
    const nodes = new Map();
    const requests = [];
    let active = true;
    const context = vm.createContext({
        document: {addEventListener() {}, getElementById(id) {
            if (!nodes.has(id)) nodes.set(id, {value: '', dataset: {}, innerHTML: '', textContent: ''});
            return nodes.get(id);
        }},
        ZeshunPerformance: {isActive: () => active},
        fetch(url) { return new Promise(resolve => requests.push({url, resolve})); },
        clearTimeout() {}, setTimeout() { throw new Error('Hidden modules must not continue polling'); },
    });
    const run = code => vm.runInContext(code, context);
    run(fs.readFileSync('bit/static/ai-original-products.js', 'utf8'));
    run('renderAiOriginalPlatformSettings = () => {}; renderAiOriginalProducts = () => {}; renderAiOriginalStoreFilters = () => {}; renderAiOriginalTaskStatus = () => {};');
    const reply = (index, data) => requests[index].resolve({ok: true, json: async () => ({status: 'success', data})});
    return {run, requests, reply, hide() { active = false; }};
}

test('AI original profiles and store metadata share in-flight requests and completed loads', async () => {
    const app = originalSetup();
    const pending = [app.run('loadAiOriginalProfiles()'), app.run('loadAiOriginalProfiles()'),
        app.run('loadAiOriginalStores()'), app.run('loadAiOriginalStores()')];
    assert.equal(app.requests.length, 2);
    app.reply(0, {platforms: [{id: 'mercadolibre', label: '美客多'}], templates: [{id: 'standard', label: '标准'}]});
    app.reply(1, {rows: []});
    await Promise.all(pending);
    await app.run('loadAiOriginalProfiles()');
    await app.run('loadAiOriginalStores()');
    assert.equal(app.requests.length, 2);
});

test('AI original polling stops when its module becomes inactive', async () => {
    const app = originalSetup();
    const task = app.run('loadAiOriginalTaskStatus()');
    const publish = app.run('loadAiOriginalPublishStatus()');
    app.hide();
    app.reply(0, {running: true});
    app.reply(1, {running: true});
    await Promise.all([task, publish]);
    await app.run('loadAiOriginalTaskStatus()');
    await app.run('loadAiOriginalPublishStatus()');
    assert.equal(app.requests.length, 2);
});

test('AI original workbench processes large lists with one row lookup and status check per product', () => {
    let idReads = 0;
    let statusChecks = 0;
    const rows = Array.from({length: 500}, (_, index) => ({get id() {idReads++; return index + 1;}}));
    const cards = rows.map((_, index) => ({dataset: {decorated: '1'},
        querySelector: () => ({value: String(index + 1)}),
    }));
    const context = vm.createContext({aiOriginalRows: rows,
        aiOriginalProductStatusLabels: {approved: '通过', unreviewed: '待审核', failed: '失败', incomplete: '待补充', suspected: '疑似侵权'},
        aiOriginalProductStatus: () => {statusChecks++; return 'approved';},
        document: {addEventListener() {}, getElementById() {return null;}, querySelectorAll() {return cards;}},
    });
    vm.runInContext(fs.readFileSync('bit/static/ai-original-workbench.js', 'utf8'), context);
    vm.runInContext('syncAiOriginalWorkbenchSelection = () => {}; decorateAiOriginalWorkbench();', context);
    assert.equal(idReads, rows.length);
    assert.equal(statusChecks, rows.length);
});

test('infringement task polling pauses for hidden lists and resumes on module visibility', async () => {
    const nodes = new Map();
    const timers = new Map();
    const listeners = new Map();
    const requests = [];
    let nextTimer = 0;
    let active = true;
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, {open: false, innerHTML: '', textContent: '',
            addEventListener(name, fn) { this[name] = fn; },
            showModal() { this.open = true; }, close() { this.open = false; this.closeEvent?.(); },
            querySelectorAll() { return []; },
        });
        return nodes.get(id);
    };
    const document = {hidden: false, getElementById: node, createElement: () => node('container'),
        body: {append() {}}, querySelectorAll() {return [];},
        addEventListener(name, fn) { listeners.set(name, fn); },
    };
    const context = vm.createContext({document,
        ZeshunPerformance: {isActive: () => active},
        setTimeout(fn) {timers.set(++nextTimer, fn); return nextTimer;},
        clearTimeout(id) {timers.delete(id);},
        fetch(url) {requests.push(url); return Promise.resolve({ok: true,
            json: async () => ({status: 'success', data: url.endsWith('/tasks')
                ? {tasks: [{id: 'task', status: 'running'}]} : {id: 'task', status: 'running'}}),
        });},
    });
    context.window = context;
    vm.runInContext(fs.readFileSync('bit/static/mercado-infringement.js', 'utf8'), context);
    await context.openMercadoInfringementTasks({id: 'task', status: 'running'});
    assert.equal(timers.size, 1);
    node('mi-task-dialog').open = false;
    active = false;
    listeners.get('zeshun:visibility')();
    assert.equal(timers.size, 0, 'leaving both lists stops the timer');
    active = true;
    listeners.get('zeshun:visibility')();
    assert.equal(timers.size, 1, 'returning to a list resumes task status polling');
    const poll = [...timers.values()][0];
    timers.clear();
    await poll();
    assert.match(requests.at(-1), /\/tasks\/task$/);
    assert.equal(timers.size, 1);
    document.hidden = true;
    listeners.get('visibilitychange')();
    assert.equal(timers.size, 0, 'background browser tabs stop the timer');
});
