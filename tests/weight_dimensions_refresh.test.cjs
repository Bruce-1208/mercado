const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

test('opening reads saved data and manual full sync replaces stale polls', async () => {
    const elements = new Map();
    function element() {
        return { style: {}, value: '', files: [], disabled: false, listeners: {}, classList: {contains: () => true},
            addEventListener(name, callback) { this.listeners[name] = callback; },
            replaceChildren() {}, appendChild() {},
            querySelector() { return element(); },
        };
    }
    let initialize;
    let changedRequests = 0;
    const pendingPolls = [];
    const context = {
        URL, URLSearchParams, Set, Map, console, MutationObserver: class { observe() {} },
        document: {
            getElementById(id) {
                if (!elements.has(id)) elements.set(id, element());
                return elements.get(id);
            },
            createElement: element,
            addEventListener(name, callback) { if (name === "DOMContentLoaded") initialize = callback; },
        },
        window: { location: { href: 'http://localhost/' }, setTimeout() {} },
        fetch: async (url) => {
            if (url.startsWith('/api/weight-dimensions-records/task?include_records')) {
                return await new Promise(resolve => pendingPolls.push(resolve));
            }
            if (url.includes('/changed?')) changedRequests++;
            return { ok: true, headers: { get: () => 'application/json' },
                json: async () => ({ status: 'success', data: { task_id: 'task', agents: [] } }) };
        },
    };
    vm.runInNewContext(fs.readFileSync('bit/static/weight-dimensions-records.js', 'utf8'), context);
    initialize();
    await new Promise(setImmediate);
    const refresh = elements.get('wdr-refresh-changed');
    assert.equal(changedRequests, 0);
    assert.equal(refresh.disabled, false);
    assert.equal(elements.get('wdr-file').files.length, 0);
    refresh.listeners.click();
    await new Promise(setImmediate);
    const refreshPromise = refresh.listeners.click();
    await new Promise(setImmediate);
    assert.equal(changedRequests, 2);
    assert.equal(refresh.disabled, false);
    // A late failed response from the replaced poll must not overwrite the
    // current task or disable its refresh button.
    pendingPolls[0]({ ok: false, headers: { get: () => 'application/json' },
        json: async () => ({ message: 'stale failure' }) });
    pendingPolls[1]({ ok: true, headers: { get: () => 'application/json' },
        json: async () => ({ data: { status: 'ready', records: [], message: 'done' } }) });
    await refreshPromise;
    assert.match(elements.get('wdr-state').textContent, /已读取/);
    assert.equal(refresh.disabled, false);
});

test('recent week, pagination and selection without periodic refresh', async () => {
    let active = false;
    let activate;
    const elements = new Map();
    function element() {
        return { style: {}, value: '', files: [], children: [], listeners: {}, classList: { add() {}, contains: () => active },
            addEventListener(name, callback) { this.listeners[name] = callback; },
            replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); },
            querySelector(name) { return this[name] ||= element(); },
        };
    }
    const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
    let initialize;
    const timers = [];
    const requests = [];
    let rows = Array.from({length: 101}, (_, i) => ({order_number: String(i), can_execute_zeshun: true}));
    const document = { hidden: false, getElementById: get, createElement: element,
        addEventListener(name, callback) { if (name === "DOMContentLoaded") initialize = callback; } };
    const context = { URL, URLSearchParams, Set, Map, console, MutationObserver: class { constructor(callback) { activate = callback; } observe() {} }, document,
        window: {location: {href: 'http://localhost/'}, setTimeout(callback) {timers.push(callback);} },
        fetch: async url => {
            requests.push(url);
            const data = url.includes('/changed?') ? {task_id: 'task', status: 'ready'} : (url.includes('/task?') || url.includes('/saved?')) ?
                (() => { const params = new URL(url, 'http://localhost').searchParams; const size = Number(params.get('page_size')) || 50; const page = Math.min(Number(params.get('page')) || 1, Math.max(1, Math.ceil(rows.length / size))); return {task_id: 'task', status: 'ready', total: rows.length, record_total: rows.length, page, records: params.get('include_records') === '0' ? [] : rows.slice((page - 1) * size, page * size), message: 'done'}; })() : {agents: []};
            return {ok: true, headers: {get: () => 'application/json'}, json: async () => ({data})};
        },
    };
    vm.runInNewContext(fs.readFileSync('bit/static/weight-dimensions-records.js', 'utf8'), context);
    initialize(); await new Promise(setImmediate);
    assert.equal(requests.length, 0, 'inactive tab must not start any requests');
    active = true; activate(); await new Promise(setImmediate);
    const query = new URL(requests.find(url => url.includes('/saved?')), 'http://localhost').searchParams;
    const start = new Date(); start.setDate(start.getDate() - 6); start.setHours(0, 0, 0, 0);
    assert.equal(new Date(query.get('date_from')).getTime(), start.getTime());
    assert.equal(query.has('date_to'), false, 'new changes remain included on automatic refresh');
    assert.equal(requests.some(url => url.includes('/changed?')), false);
    assert.equal(requests.some(url => url.includes('/task?')), false, 'initial saved request already contains first page');
    assert.equal(get('wdr-table').tbody.children.length, 50);
    let select = get('wdr-table').thead.children[0].children[0].children[0];
    select.checked = true; select.listeners.change();
    assert.equal(get('wdr-selection-summary').textContent, '已选择 50 条');
    get('wdr-page-next').listeners.click(); await new Promise(setImmediate);
    assert.match(get('wdr-page-summary').textContent, /第 2 \/ 3 页/);
    const previousRow = get('wdr-table').tbody.children[0];
    assert.equal(timers.length, 0, 'saved records must not trigger periodic refresh');
    assert.equal(get('wdr-table').tbody.children[0], previousRow);
    assert.match(get('wdr-page-summary').textContent, /第 2 \/ 3 页/);
    assert.equal(get('wdr-selection-summary').textContent, '已选择 50 条');
    get('wdr-page-next').listeners.click(); await new Promise(setImmediate);
    assert.equal(get('wdr-table').tbody.children.length, 1);
    assert.equal(get('wdr-page-next').disabled, true);
    rows = rows.slice(0, 20);
    await get('wdr-refresh-changed').listeners.click();
    assert.match(get('wdr-page-summary').textContent, /共 20 条 · 第 1 \/ 1 页/);
    assert.equal(requests.find(url => url.includes('/changed?')), '/api/weight-dimensions-records/changed?');
    assert.ok(requests.filter(url => url.includes('/saved?')).length >= 2);
    const count = requests.length;
    document.hidden = true;
    assert.equal(timers.length, 0);
    assert.equal(requests.length, count);
    get('wdr-page-size').value = '20'; get('wdr-page-size').listeners.change(); await new Promise(setImmediate);
    assert.equal(get('wdr-table').tbody.children.length, 20);
});

test('history table preserves snapshots, filters results and paginates legacy logs safely', async () => {
    const elements = new Map();
    function element() {
        return {style: {}, value: '', files: [], children: [], listeners: {}, classList: {add() {}, contains: () => true},
            addEventListener(name, callback) { this.listeners[name] = callback; },
            replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); },
            querySelector(name) { return this[name] ||= element(); },
            showModal() { this.open = true; }, close() { this.open = false; this.listeners.close?.(); },
        };
    }
    const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
    const logs = Array.from({length: 52}, (_, i) => ({time: `time-${i}`, stage: '美客多链接', status: '成功', message: `old-${i}`}));
    logs.push({time: 'legacy', stage: '净收益', status: '成功', message: '链接 MLB123：已重新提交当前净收益 USD 4.77'});
    logs.push({time: 'latest', execution_id: 'batch1', marketplace_item_id: 'MLM456', stage: '美客多链接',
        submitted_weight_g: '600', submitted_dimensions_cm: '20x10x5', status: '失败', operator: 'user:7',
        message: '<img src=x onerror=alert(1)>'});
    const row = {order_number: '123', actual_weight_g: '999', can_execute_zeshun: true, execution_logs: logs};
    let initialize;
    const context = {URL, URLSearchParams, Set, Map, console, MutationObserver: class {observe() {}},
        document: {hidden: false, getElementById: get, createElement: element,
            addEventListener(name, callback) { if (name === 'DOMContentLoaded') initialize = callback; }},
        window: {location: {href: 'http://localhost/'}, setTimeout() {}},
        fetch: async url => ({ok: true, headers: {get: () => 'application/json'}, json: async () => ({data:
            url.includes('/saved?') ? {task_id: 'task', status: 'ready', record_total: 1, records: [row]} : {agents: []}})}),
    };
    vm.runInNewContext(fs.readFileSync('bit/static/weight-dimensions-records.js', 'utf8'), context);
    initialize(); await new Promise(setImmediate);
    const button = get('wdr-table').tbody.children[0].children[5].children[0];
    assert.equal(button.textContent, '查看更新历史（54）');
    button.listeners.click();
    assert.equal(get('wdr-history-dialog').open, true);
    assert.equal(get('wdr-history-body').children.length, 50);
    const first = get('wdr-history-body').children[0].children;
    assert.equal(first[4].textContent, '600');
    assert.equal(first[9].textContent, '<img src=x onerror=alert(1)>');
    assert.equal(first[9].children.length, 0, 'log text must never become HTML');
    const legacy = get('wdr-history-body').children[1].children;
    assert.equal(legacy[2].textContent, 'MLB123'); assert.equal(legacy[6].textContent, '4.77');
    assert.equal(legacy[4].textContent, '—', 'do not copy current measurements into old logs');
    get('wdr-history-next').listeners.click();
    assert.equal(get('wdr-history-body').children.length, 4);
    get('wdr-history-status').value = '失败'; get('wdr-history-status').listeners.change();
    assert.equal(get('wdr-history-body').children.length, 1);
    assert.match(get('wdr-history-summary').textContent, /第 1 \/ 1 页/);
    get('wdr-history-search').value = 'missing'; get('wdr-history-search').listeners.input();
    assert.equal(get('wdr-history-body').children[0].children[0].textContent, '没有符合条件的更新记录');
    get('wdr-history-close').listeners.click(); assert.equal(get('wdr-history-dialog').open, false);
});
