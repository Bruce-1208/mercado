const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const html = fs.readFileSync(require('node:path').join(__dirname, '../bit/templates/index.html'), 'utf8');
function context() {
    const memory = new Map();
    const c = {console, currentWorkbenchUser: {id: 7}, window: {localStorage: {getItem: k => memory.get(k), setItem: (k,v) => memory.set(k,v)}}, document: {querySelectorAll: () => [], getElementById: () => null}, mercadoStoreTokenRows: [{id: 1, access_token: 'secret'}]};
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
