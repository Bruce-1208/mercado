const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname, '../bit/static/rights-holder-reply.js'), 'utf8');

async function setup(mode = 'submitted') {
    const elements = {}, calls = [], timers = [];
    function element(id) {
        return elements[id] ||= {value: '', files: [], disabled: false, textContent: '', events: {},
            addEventListener(event, fn) { this.events[event] = fn; },
            replaceChildren() {}, add() {}, showModal() {}, close() {}, focus() {this.focused = true;}};
    }
    const context = vm.createContext({
        document: {getElementById: element}, FormData, Map,
        Option: function(label, value) { this.value = value; },
        setTimeout: fn => timers.push(fn), showToast() {}, renderTasks() {},
        actionData: {rights_holder_replies: [{token_id: 7, infraction_id: '123', item_id: 'MLM100', store_name: 'A', site_id: 'MLM'}]},
        async fetch(url, options = {}) {
            calls.push({url, options});
            let data;
            if (url.startsWith('/api/execution-agents')) data = {agents: [{agent_id: 'office', online: true}]};
            else if (url.endsWith('/open-browser')) data = {task_id: 'job1', agent_name: 'Office'};
            else if (url.includes('/open-store/')) data = {running: false, status: 'success', message: '已打开对应店铺'};
            else if (options.method === 'POST') data = {mode, message: mode === 'unknown' ? '结果未确认' : '平台已接收'};
            else data = {mode: 'api', can_reply: true};
            return {ok: true, json: async () => ({status: 'success', data})};
        },
    });
    vm.runInContext(source, context);
    await new Promise(resolve => setImmediate(resolve));
    await elements['rights-cases'].events.click({target: {closest: () => ({dataset: {rightsIndex: '0'}})}});
    elements['rights-text'].value = 'My reply';
    return {elements, calls, timers, context};
}

test('API success submits text, removes sent case and does not open Agent', async () => {
    const {elements, calls, context} = await setup();
    await elements['rights-form'].events.submit({preventDefault() {}});
    const writes = calls.filter(call => call.options.method === 'POST');
    assert.equal(writes.length, 1);
    assert.equal(writes[0].options.body.get('text'), 'My reply');
    assert.equal(context.actionData.rights_holder_replies.length, 0);
    assert.equal(elements['rights-submit'].disabled, true);
    assert.match(elements['rights-message'].textContent, /已移出今日待办/);
});

test('API rejection falls back to selected Agent and reports opened, not sent', async () => {
    const {elements, calls, timers, context} = await setup('browser');
    await elements['rights-form'].events.submit({preventDefault() {}});
    const open = calls.find(call => call.url.endsWith('/open-browser'));
    assert.deepEqual(JSON.parse(open.options.body), {agent_id: 'office', execution_target: 'agent'});
    assert.equal(elements['rights-submit'].disabled, true);
    await timers.shift()();
    assert.match(elements['rights-message'].textContent, /尚未确认发送/);
    assert.equal(context.actionData.rights_holder_replies.length, 1);
});

test('uncertain submission removes inbox item without opening Agent or warning', async () => {
    const {elements, calls, context} = await setup('unknown');
    await elements['rights-form'].events.submit({preventDefault() {}});
    assert.equal(context.actionData.rights_holder_replies.length, 0);
    assert.equal(calls.some(call => call.url.endsWith('/open-browser')), false);
    assert.match(elements['rights-message'].textContent, /已移出今日待办/);
    await elements['rights-form'].events.submit({preventDefault() {}});
    assert.equal(calls.filter(call => call.options.method === 'POST' && !call.url.endsWith('/open-browser')).length, 1);
    assert.equal(elements['rights-submit'].disabled, true);
});

test('no Agent keeps draft and gives an actionable message', async () => {
    const {elements, calls} = await setup('browser');
    elements['rights-agent'].value = '';
    await elements['rights-form'].events.submit({preventDefault() {}});
    assert.equal(calls.some(call => call.url.endsWith('/open-browser')), false);
    assert.match(elements['rights-message'].textContent, /选择在线回复电脑/);
    assert.equal(elements['rights-agent'].focused, true);
    assert.equal(elements['rights-text'].value, 'My reply');
});

test('manual browser button dispatches after choosing an Agent inside the dialog', async () => {
    const {elements, calls} = await setup();
    elements['rights-agent'].value = '';
    await elements['rights-browser'].events.click();
    assert.equal(calls.some(call => call.url.endsWith('/open-browser')), false);
    elements['rights-agent'].value = 'chosen-office';
    await elements['rights-browser'].events.click();
    const request = calls.find(call => call.url.endsWith('/open-browser'));
    assert.equal(JSON.parse(request.options.body).agent_id, 'chosen-office');
    assert.equal(elements['rights-text'].value, 'My reply');
});
