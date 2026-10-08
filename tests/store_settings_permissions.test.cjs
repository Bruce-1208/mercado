const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync(require('node:path').join(__dirname, '../bit/templates/index.html'), 'utf8');
const start = template.indexOf('        async function loadMercadoSalespeople()');
const end = template.indexOf('        function mercadoSalespersonOptions', start);
function setup(canReadUsers, users = []) {
    let calls = 0;
    const context = vm.createContext({
        currentWorkbenchUser: {display_name: '周乐'},
        mercadoStoreTokenRows: [{site_settings: [{salesperson: '周乐'}, {salesperson: '历史负责人'}]}],
        hasWorkbenchPermission: permission => canReadUsers && permission === 'access.view',
        requestAccessApi: async () => { calls++; if (!canReadUsers) throw Error('Forbidden'); return users; },
    });
    vm.runInContext('let mercadoSalespeople = [];\n' + template.slice(start, end), context);
    return {context, calls: () => calls};
}
test('store owner can load configuration personnel without account administration permission', async () => {
    const {context, calls} = setup(false);
    const names = Array.from(await context.loadMercadoSalespeople());
    assert.ok(names.includes('周乐'));
    assert.ok(names.includes('历史负责人'));
    assert.equal(calls(), 0);
});
test('authorized administrators still load active personnel from account management', async () => {
    const {context, calls} = setup(true, [{display_name: '周乐'}, {display_name: '停用', is_active: false}]);
    assert.deepEqual(Array.from(await context.loadMercadoSalespeople()), ['周乐']);
    assert.equal(calls(), 1);
});
