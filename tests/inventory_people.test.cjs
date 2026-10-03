const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync(require('node:path').join(__dirname, '../bit/templates/index.html'), 'utf8');
const source = template.slice(template.indexOf('        function inventoryEl('), template.indexOf('        function syncDateTimeControl('));
function setup() {
    const elements = new Map();
    const calls = [];
    const body = {};
    const element = id => {
        if (!elements.has(id)) elements.set(id, {
            value: '', options: [], parentElement: body,
            add(option) { this.options.push(option); },
            showModal() { this.open = true; }, close() { this.open = false; },
            insertAdjacentHTML() {},
        });
        return elements.get(id);
    };
    const context = vm.createContext({
        document: {getElementById: element, body},
        window: {}, Option: function(text, value) {return {text, value};},
        URLSearchParams, currentWorkbenchUser: {id: 17, display_name: '仓库员'},
        inventoryRows: [], inventoryShelves: [], inventoryStockPages: 1, inventoryLogPages: 1,
        inventoryMovementSaving: false, inventoryMovementType: 'inbound',
        hasWorkbenchPermission: () => true, escapeHtml: value => String(value ?? ''),
        fetch: async (url, options) => {
            calls.push({url, options});
            return {ok: true, json: async () => ({status: 'success', data: {before_quantity: 0, after_quantity: 2}})};
        },
    });
    vm.runInContext(source, context);
    context.loadInventoryShelves = async () => [];
    return {context, element, calls};
}
const match = {order_id: '20001', product_id: 'MLM1', salesperson: '张三', product_name: '水杯'};

test('order inbound defaults owner to salesperson and records current user', async () => {
    const {context, element} = setup();
    await context.openInventoryMovementDialog('inbound', 0, [match]);
    assert.equal(element('inventory-movement-owner').value, '张三');
    assert.equal(element('inventory-movement-inbound-person').value, '仓库员');
    assert.equal(element('inventory-movement-dialog').open, true);
    context.clearInventoryMatch();
    assert.equal(element('inventory-movement-owner').value, '');
    context.setInventorySelectedProduct({...match, salesperson: ''});
    assert.equal(element('inventory-movement-owner').value, '');
});

test('multiple order products default owner when a product is chosen', async () => {
    const {context, element} = setup();
    await context.openInventoryMovementDialog('inbound', 0, [match, {...match, product_id: 'MLM2', salesperson: '李四'}]);
    context.selectInventoryMatch(1);
    assert.equal(element('inventory-movement-owner').value, '李四');
});

test('append keeps owner while outbound displays the original inbound person', async () => {
    const {context, element} = setup();
    context.inventoryRows = [{...match, id: 70, shelf_id: 1, inventory_owner: '李四', inbound_person: '原入库员'}];
    await context.openInventoryMovementDialog('inbound', 70);
    assert.equal(element('inventory-movement-owner').value, '李四');
    assert.equal(element('inventory-movement-inbound-person').value, '仓库员');
    await context.openInventoryMovementDialog('outbound', 70);
    assert.equal(element('inventory-movement-owner').value, '李四');
    assert.equal(element('inventory-movement-owner').disabled, true);
    assert.equal(element('inventory-movement-inbound-person').value, '原入库员');
});

test('inbound requires owner and submits a changed owner', async () => {
    const {context, element, calls} = setup();
    context.inventorySelectedMatch = match;
    element('inventory-movement-quantity').value = '2';
    await context.saveInventoryMovement();
    assert.equal(calls.length, 0);
    assert.equal(element('inventory-movement-message').textContent, '请选择库存归属人');
    context.loadInventoryStock = async () => {};
    context.loadInventoryLogs = async () => {};
    context.setInventoryOwner('李四');
    await context.saveInventoryMovement();
    assert.equal(JSON.parse(calls[0].options.body).inventory_owner, '李四');
});

test('stock and log filters are sent to API and reset independently', async () => {
    const {context, element, calls} = setup();
    context.renderInventoryStock = () => {};
    context.renderInventoryLogs = () => {};
    for (const [view, load, reset] of [['stock', 'loadInventoryStock', 'resetInventoryStockFilters'], ['log', 'loadInventoryLogs', 'resetInventoryLogFilters']]) {
        element(`inventory-${view}-inbound-person`).value = '仓库员';
        element(`inventory-${view}-owner`).value = '张三';
        await context[load](1);
        const params = new URL(calls.at(-1).url, 'https://test.local').searchParams;
        assert.equal(params.get('inbound_person'), '仓库员');
        assert.equal(params.get('inventory_owner'), '张三');
        context[reset]();
        assert.equal(element(`inventory-${view}-inbound-person`).value, '');
        assert.equal(element(`inventory-${view}-owner`).value, '');
    }
});
