const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../bit/templates/index.html'), 'utf8');
const source = template.slice(
    template.indexOf('        async function preSaleAggregateRequest('),
    template.indexOf('        function preSaleFilterPayload('),
);

function setup(respond, canChangeTaskWorkers = true) {
    const calls = [];
    const progress = [];
    const context = vm.createContext({
        preSaleStatus: 'UNANSWERED', canChangeTaskWorkers,
        setPreSaleMessage: message => progress.push(message),
        fetch: async (url, options) => {
            const body = JSON.parse(options.body);
            calls.push(body);
            return respond(body, calls.length, context);
        },
    });
    vm.runInContext(source, context);
    return {context, calls, progress};
}

function success(body, failures = []) {
    return {ok: true, json: async () => ({status: 'success', data: {
        stores: body.token_ids.filter(id => !failures.includes(id)).map(token_id => ({
            token_id, questions: [{id: token_id * 100}], official_total: 1,
        })),
        failures: failures.map(token_id => ({token_id, message: '店铺失败'})),
        workers: 8,
    }})};
}

const stores = count => Array.from({length: count}, (_, index) => ({id: index + 1}));
const plain = value => JSON.parse(JSON.stringify(value));

for (const count of [1, 100, 101, 250]) {
    test(`${count} stores are queried in sequential batches of at most 100 and merged`, async () => {
        let active = 0;
        const app = setup(async body => {
            active += 1;
            assert.equal(active, 1);
            await Promise.resolve();
            active -= 1;
            return success(body);
        });
        const result = await app.context.preSaleAggregateRequest(stores(count), {item_id: 'MLM123', user_id: '456'});
        assert.equal(app.calls.length, Math.ceil(count / 100));
        assert.ok(app.calls.every(body => body.token_ids.length <= 100));
        assert.ok(app.calls.every(body => body.item_id === 'MLM123' && body.user_id === '456' && body.status === 'UNANSWERED' && body.workers === Math.min(8, count)));
        assert.deepEqual(plain(result.stores.map(row => row.token_id)), stores(count).map(row => row.id));
        assert.equal(result.success_stores, count);
        assert.equal(result.failed_stores, 0);
        assert.equal(result.total_stores, count);
        assert.equal(app.progress.length, app.calls.length);
        assert.ok(Number.isFinite(result.elapsed_seconds));
    });
}

test('batch HTTP failures preserve successful stores and subsequent batches still run', async () => {
    const app = setup((body, call) => call === 2
        ? {ok: false, status: 502, json: async () => ({message: '上游暂不可用'})}
        : success(body, call === 1 ? [1] : []));
    const result = await app.context.preSaleAggregateRequest(stores(250), {});
    assert.equal(app.calls.length, 3);
    assert.equal(result.success_stores, 149);
    assert.equal(result.failed_stores, 101);
    assert.equal(result.stores.at(-1).token_id, 250);
    assert.equal(result.failures[0].token_id, 1);
    assert.deepEqual(plain(result.failures.slice(1).map(row => row.token_id)), stores(200).slice(100).map(row => row.id));
    assert.equal(result.failures[1].message, '上游暂不可用');
});

test('network failures are reported for every affected store', async () => {
    const app = setup(() => { throw new Error('网络错误'); });
    const result = await app.context.preSaleAggregateRequest(stores(101), {});
    assert.equal(app.calls.length, 2);
    assert.equal(result.success_stores, 0);
    assert.equal(result.failed_stores, 101);
    assert.ok(result.failures.every(row => row.message === '网络错误'));
});

test('all batches keep the original status and omit worker settings for restricted users', async () => {
    const app = setup((body, call, context) => {
        context.preSaleStatus = 'ANSWERED';
        return success(body);
    }, false);
    await app.context.preSaleAggregateRequest(stores(101), {});
    assert.ok(app.calls.every(body => body.status === 'UNANSWERED' && !('workers' in body)));
});
