const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../bit/templates/index.html'), 'utf8');
const start = html.indexOf('        const orderPrintPendingRequests = new Map();');
const end = html.indexOf('        function closeOrderSplitDialog()', start);
assert.ok(start >= 0 && end > start, 'real order print workflow must be present');
const source = html.slice(start, end);
const tick = () => new Promise(resolve => setImmediate(resolve));

function sessionStore() {
    const values = new Map();
    return {
        getItem: key => values.get(key) || null,
        setItem: (key, value) => values.set(key, String(value)),
    };
}

function setup({storage = sessionStore(), userId = 42, ids = ['1001', '1002'], nextId} = {}) {
    const requests = [], previews = [], blobs = [], timers = [], loads = [];
    let sequence = 0;

    class Element {
        constructor(tag = 'div') { this.tagName = tag; this.children = []; this.events = new Map(); this.value = ''; }
        set textContent(value) { this.value = String(value); this.children = []; }
        get textContent() { return this.value + this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); }
        append(...children) { this.children.push(...children); }
        addEventListener(name, callback, options) { this.events.set(name, {callback, once: options?.once}); }
        click() {
            const event = this.events.get('click');
            if (!event) return;
            if (event.once) this.events.delete('click');
            return event.callback();
        }
    }

    const bulkMessage = new Element(), detailMessage = new Element();
    const context = vm.createContext({
        currentWorkbenchUser: {id: userId}, sessionStorage: storage,
        selectedOrderIds: new Set(ids), orderBulkMessage: bulkMessage,
        orderDetailActionMessage: detailMessage, orderPage: 1, orderRows: [],
        orderDetailDialog: {open: false}, orderDetailOrderId: null,
        loadOrders: async page => loads.push(page),
        document: {createElement: tag => new Element(tag)},
        URL: {
            createObjectURL(blob) { blobs.push(blob); return `blob:print-${blobs.length}`; },
            revokeObjectURL() {},
        },
        window: {
            crypto: {randomUUID: nextId || (() => `wallet-request-${++sequence}`)},
            open() {
                const preview = {
                    closed: false, document: {title: '', body: {innerHTML: ''}},
                    location: {replace(url) { preview.url = url; }},
                    close() { preview.closed = true; },
                };
                previews.push(preview);
                return preview;
            },
            setTimeout(callback, delay) { timers.push({callback, delay}); },
        },
        fetch(url, options) {
            return new Promise((resolve, reject) => requests.push({url, options,
                body: JSON.parse(options.body), resolve, reject}));
        },
    });
    vm.runInContext(source, context);

    function respond(index, {status = 200, payload = {}, headers = {}} = {}) {
        const values = {
            'X-Mercado-Printed-Order-Count': '2', 'X-Mercado-Shipment-Count': '2',
            'X-Print-Wallet-Charge-Cents': '600', ...headers,
        };
        requests[index].resolve({
            ok: status >= 200 && status < 300, status,
            json: async () => payload,
            headers: {get: name => values[name] ?? null},
            blob: async () => ({content: '%PDF-test'}),
        });
    }

    return {context, storage, requests, respond, previews, blobs, timers, loads, bulkMessage, detailMessage,
        pending: () => JSON.parse(storage.getItem(`zeshun-pending-prints-${userId}`) || '{}'),
        buttons: () => [...bulkMessage.children, ...detailMessage.children].filter(child => child.tagName === 'button'),
    };
}

test('concurrent clicks cannot start a second request even after the selection changes', async () => {
    const app = setup();
    const printing = app.context.printOrders();
    const originalId = app.requests[0].body.request_id;
    await app.context.printOrders(['1001', '1002']);
    app.context.selectedOrderIds = new Set(['1101']);
    await app.context.printOrders();
    assert.equal(app.requests.length, 1);
    assert.equal(app.previews.length, 1);
    assert.deepEqual(app.pending(), {'1001,1002': originalId});
    assert.match(app.bulkMessage.textContent, /上一批面单正在处理/);
    app.respond(0);
    await printing;
    assert.deepEqual(app.pending(), {});
    assert.equal(app.previews[0].url, 'blob:print-1');
    assert.equal(app.blobs.length, 1);
    assert.deepEqual(app.loads, [1]);
    assert.match(app.bulkMessage.textContent, /已扣 ¥6\.00/);
});

for (const requestStatus of ['reserved', 'settled']) {
    test(`network failure then ${requestStatus} conflict keeps the same request key through retries and reload`, async () => {
        const storage = sessionStore();
        let sequence = 0;
        const nextId = () => `wallet-request-${++sequence}`;
        const app = setup({storage, nextId});
        const first = app.context.printOrders();
        const requestId = app.requests[0].body.request_id;
        app.requests[0].reject(new Error('network response lost'));
        await first;
        assert.deepEqual(app.pending(), {'1001,1002': requestId});
        assert.equal(app.previews[0].closed, true);

        const retry = app.context.printOrders(['1002', '1001']);
        assert.equal(app.requests[1].body.request_id, requestId);
        app.respond(1, {status: 409, payload: {message: '请求已处理', request_status: requestStatus}});
        await retry;
        assert.deepEqual(app.pending(), {'1001,1002': requestId});
        assert.equal(app.blobs.length, 0);
        assert.equal(app.buttons().length, requestStatus === 'settled' ? 2 : 0);

        const repeated = app.context.printOrders();
        assert.equal(app.requests[2].body.request_id, requestId);
        app.respond(2, {status: 409, payload: {request_status: requestStatus}});
        await repeated;

        const reloaded = setup({storage, nextId});
        const afterReload = reloaded.context.printOrders();
        assert.equal(reloaded.requests[0].body.request_id, requestId);
        reloaded.respond(0, {status: 409, payload: {request_status: requestStatus}});
        await afterReload;
        assert.deepEqual(reloaded.pending(), {'1001,1002': requestId});
        assert.equal(sequence, 1, 'ordinary retries must not generate a new charged request');
    });
}

test('only explicit settled reprint confirmation creates a new key and its two buttons cannot duplicate the print', async () => {
    const app = setup();
    const first = app.context.printOrders();
    const oldId = app.requests[0].body.request_id;
    app.respond(0, {status: 409, payload: {message: '该请求已结算', request_status: 'settled'}});
    await first;
    const buttons = app.buttons();
    assert.equal(buttons.length, 2);
    assert.ok(buttons.every(button => button.textContent === '确认重新打印（再次计费）'));
    assert.deepEqual(app.pending(), {'1001,1002': oldId});

    app.context.selectedOrderIds = new Set(['other-selection']);
    buttons[0].click();
    buttons[1].click();
    buttons[0].click();
    assert.equal(app.requests.length, 2);
    assert.notEqual(app.requests[1].body.request_id, oldId);
    assert.deepEqual(app.requests[1].body.order_ids, ['1001', '1002']);
    app.respond(1);
    await tick();
    assert.deepEqual(app.pending(), {});
    assert.equal(app.blobs.length, 1);
    assert.equal(app.previews[1].closed, false);
    assert.equal(app.previews[1].url, 'blob:print-1');
    assert.match(app.bulkMessage.textContent, /已扣 ¥6\.00/);
});

test('cancelled conflict clears its key so the next print starts a new request', async () => {
    const app = setup();
    const first = app.context.printOrders();
    const oldId = app.requests[0].body.request_id;
    app.respond(0, {status: 409, payload: {message: '管理员已取消', request_status: 'cancelled'}});
    await first;
    assert.deepEqual(app.pending(), {});
    assert.equal(app.buttons().length, 0);
    const next = app.context.printOrders();
    assert.notEqual(app.requests[1].body.request_id, oldId);
    app.respond(1);
    await next;
    assert.equal(app.blobs.length, 1);
});

test('insufficient balance clears the uncharged request before a post-recharge retry', async () => {
    const app = setup();
    const first = app.context.printOrders();
    const oldId = app.requests[0].body.request_id;
    app.respond(0, {status: 402, payload: {message: '余额不足，请先充值'}});
    await first;
    assert.deepEqual(app.pending(), {});
    assert.match(app.bulkMessage.textContent, /余额不足/);
    assert.equal(app.blobs.length, 0);
    assert.equal(app.previews[0].closed, true);
    const retry = app.context.printOrders();
    assert.notEqual(app.requests[1].body.request_id, oldId);
    app.respond(1);
    await retry;
    assert.deepEqual(app.pending(), {});
    assert.equal(app.blobs.length, 1);
});

test('persisted pending print keys belong to the logged-in user', async () => {
    const storage = sessionStore();
    const original = setup({storage, userId: 42, nextId: () => 'owner-print-request'});
    const first = original.context.printOrders();
    original.requests[0].reject(new Error('network response lost'));
    await first;

    const otherUser = setup({storage, userId: 43, nextId: () => 'different-user-request'});
    const printing = otherUser.context.printOrders();
    assert.equal(otherUser.requests[0].body.request_id, 'different-user-request');
    assert.deepEqual(original.pending(), {'1001,1002': 'owner-print-request'});
    otherUser.respond(0);
    await printing;
    assert.deepEqual(original.pending(), {'1001,1002': 'owner-print-request'});
});
