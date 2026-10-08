const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const details = fs.readFileSync(path.join(__dirname, '../bit/static/order-management-details.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../bit/templates/index.html'), 'utf8');
const source = details.slice(details.indexOf('function orderActualFreightValue('), details.indexOf('function orderExtraDialog('))
    + html.slice(html.indexOf('        function orderFreight('), html.indexOf('        function orderStateClass('))
    + html.slice(html.indexOf('        function mergeVisibleOrders('), html.indexOf('        let orderImagePreviewElement'));

function setup() {
    const context = vm.createContext({
        orderAmount: value => '$' + Number(value).toFixed(2),
        orderNumberValue: value => Number(value || 0),
        orderRowIds: row => [row.id],
        orderSkuItems: row => [{order_id: row.id}],
    });
    vm.runInContext(source, context);
    return context;
}

test('actual freight stays empty before shipment and never falls back to a quote or legacy freight', () => {
    const app = setup();
    for (const row of [
        {quoted_freight_usd: 7.25, freight: 7.25},
        {quoted_freight_usd: 7.25, freight: 7.25, actual_freight_usd: null},
        {quoted_freight_usd: 7.25, actual_freight_usd: 9.5, freight_pending_shipment: true},
        {quoted_freight_usd: 7.25, actual_freight_usd: 9.5, freight_missing: true},
        {quoted_freight_usd: 7.25, actual_freight_usd: ' '},
    ]) {
        assert.equal(app.orderFreight(row), '—');
        assert.equal(app.orderFreightDifference(row), null);
    }
    assert.equal(app.orderFreight({actual_freight_usd: 9.5, freight: 7.25}), '$9.50');
    assert.equal(app.orderFreightDifference({actual_freight_usd: 9.5, quoted_freight_usd: 7.25}), 2.25);
    assert.equal(app.orderFreight({actual_freight_usd: 0}), '$0.00');
    assert.equal(app.orderFreightDifference({actual_freight_usd: 0, quoted_freight_usd: 0}), 0);
});

test('merged orders add official actual charges without requiring the legacy freight alias', () => {
    const app = setup();
    const [row] = app.mergeVisibleOrders([
        {id: '1', pack_id: 'pack', quoted_freight_usd: 3, actual_freight_usd: 4},
        {id: '2', pack_id: 'pack', quoted_freight_usd: 5, actual_freight_usd: 6},
    ]);
    assert.equal(row.quoted_freight_usd, 8);
    assert.equal(row.actual_freight_usd, 10);
    assert.equal(row.freight, 10);
    assert.equal(app.orderFreightDifference(row), 2);
});

test('one unshipped child keeps merged actual freight and variance empty', () => {
    const app = setup();
    const [row] = app.mergeVisibleOrders([
        {id: '1', pack_id: 'pack', quoted_freight_usd: 3, actual_freight_usd: 4},
        {id: '2', pack_id: 'pack', quoted_freight_usd: 5, actual_freight_usd: 6, freight_pending_shipment: true},
    ]);
    assert.equal(row.quoted_freight_usd, 8);
    assert.equal(row.freight_pending_shipment, true);
    assert.equal(row.actual_freight_usd, null);
    assert.equal(app.orderFreight(row), '—');
    assert.equal(app.orderFreightDifference(row), null);
});

test('balance uses the API quote initially and updates when actual freight returns', () => {
    const app = setup();
    const row = {amount: 20, fee: 3, quoted_freight_usd: 7.25, actual_freight_usd: 9.5,
        freight_pending_shipment: true};
    assert.equal(app.orderBalanceValue(row), 9.75);
    assert.equal(app.orderBalanceUsesQuote(row), true);
    row.freight_pending_shipment = false;
    assert.equal(app.orderBalanceValue(row), 7.5);
    assert.equal(app.orderBalanceUsesQuote(row), false);
    row.actual_freight_usd = 0;
    assert.equal(app.orderBalanceValue(row), 17);
    row.actual_freight_usd = null;
    row.quoted_freight_usd = 0;
    assert.equal(app.orderBalanceValue(row), 17);
    row.quoted_freight_usd = null;
    assert.equal(app.orderBalanceValue(row), null);
    row.quoted_freight_usd = ' ';
    assert.equal(app.orderBalanceValue(row), null);
});

test('merged balances preserve actual and quoted deductions for each child', () => {
    const app = setup();
    const children = [
        {id: '1', pack_id: 'pack', amount: 20, fee: 3, quoted_freight_usd: 3, actual_freight_usd: 4, balance: 13},
        {id: '2', pack_id: 'pack', amount: 30, fee: 5, quoted_freight_usd: 5,
            actual_freight_usd: null, freight_pending_shipment: true, balance: 20},
    ];
    let [row] = app.mergeVisibleOrders(children);
    assert.equal(row.balance, 33);
    assert.equal(app.orderBalanceFreightValue(row), 9);
    assert.equal(app.orderBalanceUsesQuote(row), true);
    children[1].quoted_freight_usd = null;
    [row] = app.mergeVisibleOrders(children);
    assert.equal(row.balance, null);
});
