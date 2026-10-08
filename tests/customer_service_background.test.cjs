const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../bit/templates/index.html'), 'utf8');
function setup() {
    const timers = new Map();
    const listeners = {};
    let timerId = 0;
    let now = Date.parse('2026-10-07T04:00:00Z');
    class ClockDate extends Date {
        constructor(...args) { super(...(args.length ? args : [now])); }
        static now() { return now; }
    }
    const c = {
        Date: ClockDate, console: {warn() {}}, calls: 0,
        customerServiceLastUpdatedAt: new ClockDate().toISOString(),
        customerServiceLastAttemptAt: 0, customerServiceAutoUpdateTimer: null,
        customerServiceLoading: false, customerServiceHasSnapshot: true,
        hasWorkbenchPermission: () => true,
        loadCustomerService: async () => { c.calls++; },
        document: {visibilityState: 'hidden', addEventListener: (name, fn) => listeners[name] = fn},
        window: {setTimeout: (fn, delay) => { timers.set(++timerId, {fn, delay}); return timerId; }, clearTimeout: id => timers.delete(id)},
    };
    vm.createContext(c);
    vm.runInContext(html.match(/const customerServiceAutoUpdateInterval = [^;]+;/)[0], c);
    vm.runInContext(html.slice(html.indexOf('        function customerServiceDataIsStale()'), html.indexOf('        function recordCustomerServiceUpdatedAt(')), c);
    return {c, timers, listeners, advance: milliseconds => { now += milliseconds; }};
}
test('refresh is scheduled hourly and runs while the after-sales page is hidden', async () => {
    const {c, timers} = setup();
    c.scheduleCustomerServiceAutoUpdate();
    const timer = [...timers.values()][0];
    assert.ok(timer.delay > 3590000 && timer.delay <= 3600000);
    timer.fn();
    await new Promise(setImmediate);
    assert.equal(c.calls, 1);
});
test('failed preload still schedules a retry', async () => {
    const {c, timers} = setup();
    c.customerServiceLastUpdatedAt = '';
    c.loadCustomerService = async () => { throw new Error('offline'); };
    await c.refreshCustomerServiceInBackground();
    assert.ok([...timers.values()].at(-1).delay > 3590000);
});
test('restoring a recent snapshot keeps the next refresh one hour after its update', async () => {
    const {c, timers} = setup();
    c.customerServiceLastUpdatedAt = '';
    c.customerServiceHasSnapshot = false;
    c.loadCustomerService = async () => {
        c.customerServiceHasSnapshot = true;
        c.customerServiceLastUpdatedAt = new c.Date(c.Date.now() - 45 * 60 * 1000).toISOString();
    };
    await c.refreshCustomerServiceInBackground();
    assert.equal([...timers.values()].at(-1).delay, 15 * 60 * 1000);
});
test('resuming after a cached preload refreshes once its snapshot is an hour old', async () => {
    const {c, listeners, advance} = setup();
    c.customerServiceLastUpdatedAt = new c.Date(c.Date.now() - 45 * 60 * 1000).toISOString();
    c.loadCustomerService = async () => {
        if (!c.customerServiceDataIsStale()) return;
        c.calls++;
        c.customerServiceLastUpdatedAt = new c.Date().toISOString();
    };
    await c.refreshCustomerServiceInBackground();
    advance(20 * 60 * 1000);
    c.document.visibilityState = 'visible';
    listeners.visibilitychange();
    await new Promise(setImmediate);
    assert.equal(c.calls, 1);
});
test('resume catches up stale data without duplicating a running request', async () => {
    const {c, listeners} = setup();
    c.customerServiceLastUpdatedAt = new c.Date(c.Date.now() - 7200000).toISOString();
    c.document.visibilityState = 'visible';
    listeners.visibilitychange();
    listeners.visibilitychange();
    await new Promise(setImmediate);
    assert.equal(c.calls, 1);
    c.customerServiceLoading = true;
    await c.refreshCustomerServiceInBackground();
    assert.equal(c.calls, 1);
});
test('background preload respects view permission', async () => {
    const {c, timers} = setup();
    c.hasWorkbenchPermission = () => false;
    await c.refreshCustomerServiceInBackground();
    assert.equal(c.calls, 0);
    assert.equal(timers.size, 0);
});
