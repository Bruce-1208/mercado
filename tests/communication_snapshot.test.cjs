const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const html = fs.readFileSync(require('node:path').join(__dirname, '../bit/templates/index.html'), 'utf8');
function dateInputValue(date) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}
function context() {
    const memory = new Map();
    const today = new Date();
    const firstDay = new Date(today);
    firstDay.setDate(today.getDate() - 6);
    const controls = new Map();
    for (const kind of ['pre-sale', 'customer-service']) {
        for (const [suffix, value] of [['start', dateInputValue(firstDay)], ['end', dateInputValue(today)]]) {
            const id = `${kind}-${suffix}-date`;
            controls.set(id, {id, value, type: 'date', closest: selector => selector === `#tab-${kind}`});
        }
    }
    const c = {console, controls, currentWorkbenchUser: {id: 7}, window: {localStorage: {getItem: k => memory.get(k), setItem: (k,v) => memory.set(k,v)}}, document: {
        querySelectorAll: selector => selector.startsWith('#tab-') ? [...controls.values()].filter(node => node.closest(selector.split(' ')[0])) : [],
        getElementById: id => controls.get(id) || null,
    }, mercadoStoreTokenRows: [{id: 1, access_token: 'secret'}]};
    for (const name of ['preSaleMatchedStores','preSaleAvailableStores','customerServiceTargetStores']) c[name] = () => c.mercadoStoreTokenRows;
    for (const name of ['renderPreSaleStores','renderCustomerServiceStores','renderPreSaleRows','renderPreSalePagination','setPreSaleMessage','renderCustomerServiceTable','renderCustomerServiceQuickFilters','renderCustomerServicePagination','renderCustomerServiceLastUpdated','setCustomerServiceMessage']) c[name] = () => {};
    vm.createContext(c);
    vm.runInContext(html.slice(html.indexOf('        let preSaleLoaded ='), html.indexOf('        let storeLinksLoaded =')), c);
    vm.runInContext(html.slice(html.indexOf('        function communicationSnapshotKey'), html.indexOf('        async function loadPreSaleModule')), c);
    return c;
}
for (const kind of ['pre-sale', 'customer-service']) test(`${kind} restores previous results and separates users`, () => {
    const c = context();
    const rows = kind === 'pre-sale' ? 'preSaleRows' : 'customerServiceAllRows';
    vm.runInContext(`${rows} = [{id: 42, _token_id: 1, _store: mercadoStoreTokenRows[0]}]; saveCommunicationSnapshot('${kind}'); ${rows} = [];`, c);
    assert.equal(vm.runInContext(`restoreCommunicationSnapshot('${kind}')`, c), true);
    assert.equal(vm.runInContext(`${rows}[0].id`, c), 42);
    assert.ok(!c.window.localStorage.getItem(`mercado-communication-snapshot-v1-7-${kind}`).includes('secret'));
    c.currentWorkbenchUser.id = 8;
    assert.equal(vm.runInContext(`restoreCommunicationSnapshot('${kind}')`, c), false);
    c.currentWorkbenchUser.id = 7;
    c.mercadoStoreTokenRows = [];
    assert.equal(vm.runInContext(`restoreCommunicationSnapshot('${kind}')`, c), false);
});
test('corrupt storage safely falls back', () => {
    const c = context();
    c.console = {warn() {}};
    c.window.localStorage.setItem('mercado-communication-snapshot-v1-7-pre-sale', '{');
    assert.equal(vm.runInContext("restoreCommunicationSnapshot('pre-sale')", c), false);
});
for (const kind of ['pre-sale', 'customer-service']) {
    test(`${kind} rejects snapshots from a different date range`, () => {
        const c = context();
        vm.runInContext(`saveCommunicationSnapshot('${kind}')`, c);
        c.controls.get(`${kind}-start-date`).value = '2020-01-01';
        assert.equal(vm.runInContext(`restoreCommunicationSnapshot('${kind}')`, c), false);
    });

    test(`${kind} rejects legacy snapshots that omit the recent-week dates`, () => {
        const c = context();
        vm.runInContext(`saveCommunicationSnapshot('${kind}')`, c);
        const key = `mercado-communication-snapshot-v1-7-${kind}`;
        const snapshot = JSON.parse(c.window.localStorage.getItem(key));
        delete snapshot.controls[`${kind}-start-date`];
        delete snapshot.controls[`${kind}-end-date`];
        c.window.localStorage.setItem(key, JSON.stringify(snapshot));
        assert.equal(vm.runInContext(`restoreCommunicationSnapshot('${kind}')`, c), false);
    });

    test(`${kind} restores a successful empty result for the same week`, () => {
        const c = context();
        vm.runInContext(`saveCommunicationSnapshot('${kind}')`, c);
        assert.equal(vm.runInContext(`restoreCommunicationSnapshot('${kind}')`, c), true);
    });
}
test('startup keeps the default all-status view instead of restoring an old pre-sale filter', () => {
    const c = context();
    vm.runInContext("preSaleStatus = 'UNANSWERED'; saveCommunicationSnapshot('pre-sale'); preSaleStatus = '';", c);
    assert.equal(vm.runInContext("restoreCommunicationSnapshot('pre-sale')", c), false);
});
test('startup keeps the default claims view instead of restoring post-sale', () => {
    const c = context();
    vm.runInContext("customerServiceMode = 'post-sale'; saveCommunicationSnapshot('customer-service'); customerServiceMode = 'claims';", c);
    assert.equal(vm.runInContext("restoreCommunicationSnapshot('customer-service')", c), false);
});
test('old unread-only after-sales snapshots do not hide read conversations for another hour', () => {
    const c = context();
    vm.runInContext("saveCommunicationSnapshot('customer-service')", c);
    const key = 'mercado-communication-snapshot-v1-7-customer-service';
    const snapshot = JSON.parse(c.window.localStorage.getItem(key));
    delete snapshot.version;
    c.window.localStorage.setItem(key, JSON.stringify(snapshot));
    assert.equal(vm.runInContext("restoreCommunicationSnapshot('customer-service')", c), false);
});
