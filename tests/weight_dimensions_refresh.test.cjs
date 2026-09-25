const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

test('refresh remains clickable during automatic read without a file', async () => {
    const elements = new Map();
    function element() {
        return { value: '', files: [], disabled: false, listeners: {},
            addEventListener(name, callback) { this.listeners[name] = callback; },
            replaceChildren() {}, appendChild() {},
            querySelector() { return element(); },
        };
    }
    let initialize;
    let changedRequests = 0;
    const pendingPolls = [];
    const context = {
        URL, URLSearchParams, Set, console,
        document: {
            getElementById(id) {
                if (!elements.has(id)) elements.set(id, element());
                return elements.get(id);
            },
            createElement: element,
            addEventListener(name, callback) { initialize = callback; },
        },
        window: { location: { href: 'http://localhost/' }, setTimeout() {} },
        fetch: async (url) => {
            if (url.startsWith('/api/weight-dimensions-records/task')) {
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
    assert.equal(changedRequests, 1);
    assert.equal(refresh.disabled, false);
    assert.equal(elements.get('wdr-file').files.length, 0);
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
    assert.equal(elements.get('wdr-state').textContent, 'done');
    assert.equal(refresh.disabled, false);
});
