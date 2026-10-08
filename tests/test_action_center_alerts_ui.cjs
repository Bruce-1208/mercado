const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Run the actual page renderers with offline API responses; no business APIs.
const template = fs.readFileSync(path.join(__dirname, '../bit/templates/mercado_action_center.html'), 'utf8');
const scriptStart = template.indexOf('    const canExecute =');
const scriptEnd = template.indexOf("    for(const key of ['salesperson','group_name'])", scriptStart);
assert.ok(scriptStart >= 0 && scriptEnd > scriptStart, 'today page script is present');
const source = template.slice(scriptStart, scriptEnd).replace('{{ can_execute | tojson }}', 'false');

function alertRows() {
    return [
        {id: 1, topic: 'purchase_tracking_missing_48h', title: '48 小时未填写物流号', status: 'open', priority: 'high', overdue: true},
        {id: 2, topic: 'order_not_in_transit_7d', title: '168 小时仍未在途', status: 'in_progress', priority: 'high', overdue: true},
        {id: 3, topic: 'order_not_delivered_30d', title: '30 天仍未交付', status: 'open', priority: 'urgent', overdue: true},
        {id: 4, topic: 'order_not_delivered_30d', title: '已交付的 30 天提醒', status: 'resolved', priority: 'urgent', overdue: true},
        {id: 5, topic: 'orders_v2', title: '普通运营提醒', status: 'open', priority: 'normal', overdue: false},
    ].map(row => ({store_name: '测试店铺', due_at: '2026-10-07T00:00:00Z', entry_url: '/?tab=orders', ...row}));
}

function setup() {
    const controls = new Map([...template.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, {
        value: '', innerHTML: '', textContent: '', style: {}, disabled: false,
    }]));
    controls.get('filter').value = 'active';
    const requests = [];
    const context = vm.createContext({
        URLSearchParams,
        document: {getElementById: id => {
            assert.ok(controls.has(id), `page contains #${id}`);
            return controls.get(id);
        }},
        fetch: async (url, options) => {
            requests.push({url, options});
            const data = url.startsWith('/api/mercado/today/tasks?')
                ? {rows: alertRows(), events: [], filter_options: {salesperson: ['业务员甲'], group_name: ['店铺组 A']}}
                : {rows: [], errors: []};
            return {ok: true, json: async () => ({status: 'success', data})};
        },
    });
    vm.runInContext(source, context);
    return {
        context, controls, requests,
        render(filter = 'active', rows = alertRows()) {
            controls.get('filter').value = filter;
            context.testRows = rows;
            vm.runInContext('actionData = {rows: testRows, events: []}; renderTasks();', context);
        },
        cards(id) {
            return [...controls.get(id).innerHTML.matchAll(/data-task-id="(\d+)"/g)].map(([, taskId]) => Number(taskId));
        },
        count(id) {return Number(controls.get(id).textContent);},
    };
}

test('48h, 168h and 30d alerts render in procurement cards and contribute to open metrics', () => {
    const app = setup();
    app.render();
    assert.deepEqual(app.cards('purchase-tasks'), [1, 2, 3]);
    assert.deepEqual(app.cards('tasks'), [5]);
    assert.equal(app.count('purchase-count'), 3);
    assert.equal(app.count('count-open'), 4);
    assert.equal(app.count('count-overdue'), 3);
    assert.equal(app.count('count-urgent'), 3);
    const purchaseHtml = app.controls.get('purchase-tasks').innerHTML;
    for (const title of ['48 小时未填写物流号', '168 小时仍未在途', '30 天仍未交付']) {
        assert.ok(purchaseHtml.includes(title));
    }
    assert.match(purchaseHtml, /class="task overdue urgent" data-task-id="3"/);
    assert.ok(purchaseHtml.includes('<span class="tag">30 天未交付</span>'));
    assert.ok(!purchaseHtml.includes('order_not_delivered_30d'));
    assert.match(template, /满 48 小时无物流号、满 168 小时未在途、满 30 天未交付告警/);
});

test('all-status, overdue and urgent filters keep 30d alerts in procurement with correct counts', () => {
    const app = setup();
    app.render('all');
    assert.deepEqual(app.cards('purchase-tasks'), [1, 2, 3, 4]);
    assert.equal(app.count('purchase-count'), 4);
    assert.match(app.controls.get('purchase-tasks').innerHTML, /class="task overdue urgent resolved" data-task-id="4"/);

    app.render('overdue');
    assert.deepEqual(app.cards('purchase-tasks'), [1, 2, 3]);
    assert.deepEqual(app.cards('tasks'), []);
    assert.equal(app.count('purchase-count'), 3);

    app.render('urgent');
    assert.deepEqual(app.cards('purchase-tasks'), [1, 2, 3]);
    assert.equal(app.count('purchase-count'), 3);
    assert.equal(app.count('count-open'), 4);

    app.render('active', alertRows().filter(row => row.topic === 'orders_v2'));
    assert.equal(app.count('purchase-count'), 0);
    assert.match(app.controls.get('purchase-tasks').innerHTML, /当前筛选下没有采购必办/);
});

test('loading scoped all-status results sends filters and renders the 30d procurement alert', async () => {
    const app = setup();
    app.controls.get('filter').value = 'all';
    app.controls.get('salesperson').value = '业务员甲';
    app.controls.get('group_name').value = '店铺组 A';
    await app.context.loadData();
    assert.equal(app.requests.length, 2);
    for (const {url, options} of app.requests) {
        const parsed = new URL(url, 'https://workbench.test');
        assert.equal(parsed.searchParams.get('salesperson'), '业务员甲');
        assert.equal(parsed.searchParams.get('group_name'), '店铺组 A');
        assert.equal(options.credentials, 'same-origin');
        if (parsed.pathname.endsWith('/tasks')) assert.equal(parsed.searchParams.get('include_closed'), '1');
    }
    assert.deepEqual(app.cards('purchase-tasks'), [1, 2, 3, 4]);
    assert.deepEqual(app.cards('tasks'), [5]);
    assert.equal(app.count('purchase-count'), 4);
    assert.equal(app.controls.get('salesperson').value, '业务员甲');
    assert.equal(app.controls.get('group_name').value, '店铺组 A');
    assert.equal(app.controls.get('refresh').disabled, false);

    app.controls.get('filter').value = 'active';
    await app.context.loadData();
    const taskRequests = app.requests.filter(({url}) => url.startsWith('/api/mercado/today/tasks?'));
    assert.equal(new URL(taskRequests[1].url, 'https://workbench.test').searchParams.get('include_closed'), '0');
    assert.deepEqual(app.cards('purchase-tasks'), [1, 2, 3]);
});
