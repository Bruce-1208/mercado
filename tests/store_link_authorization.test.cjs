const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync(require('node:path').join(__dirname, '../bit/templates/index.html'), 'utf8');
const source = template.slice(template.indexOf('        async function saveMercadoStoreToken()'), template.indexOf('        async function changeMercadoStoreApplication('));

test('authorization immediately adds shop name and invalidates loaded store links', async () => {
    let stores;
    const context = {
        mercadoTokenShopName: {value: '新授权店铺'}, mercadoTokenCode: {value: 'TG-test'},
        mercadoTokenApplication: {value: ''}, saveMercadoTokenBtn: {},
        storeLinksLoaded: true, storeLinkPendingRefreshPage: null,
        mercadoStoreTokensLoaded: true,
        storeLinkStoreOptions: [{token_id: 1, store_name: '原店铺'}], storeLinkStoreGroups: {},
        showMercadoTokenMessage() {}, loadMercadoStoreTokens: async () => {},
        renderStoreLinkStores: rows => { stores = rows; },
        fetch: async () => ({ok: true, json: async () => ({status: 'success', data: {id: 2, display_name: '新授权店铺'}})}),
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    await context.saveMercadoStoreToken();
    assert.equal(context.storeLinksLoaded, false);
    assert.equal(context.storeLinkPendingRefreshPage, 1);
    assert.equal(stores.length, 2);
    assert.equal(stores[1].store_name, '新授权店铺');
    assert.equal(context.mercadoTokenCode.value, '');
});
