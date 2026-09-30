const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync('bit/templates/ai_weight_price.html', 'utf8');
const functionSource = name => template.split('\n').find(line => line.startsWith(`function ${name}(`) || line.startsWith(`async function ${name}(`));

function controls(status) {
    const elements = new Map();
    const $ = id => {
        if (!elements.has(id)) elements.set(id, {value: id === 'run-limit' ? '10' : '', classList: {toggle() {}}, querySelectorAll: () => []});
        return elements.get(id);
    };
    const context = { $, runStatus: status, loginBusy: false, rangeBusy: false, skipBusy: false,
        categoryBusy: false, terminateBusy: false, canExecute: true, agentLaunchAvailable: true,
        executionTarget: 'agent', selectedAgentId: 'signed-in-pc', selectedAgentOnline: true,
        remoteReadOnly: true, categoryOptions: [], runSelection: () => ({category: '', start_page: 1, end_page: 1, start_item: 1}) };
    vm.createContext(context);
    vm.runInContext(functionSource('syncRunControls'), context);
    return {context, $, sync: () => vm.runInContext('syncRunControls()', context)};
}

test('login confirmation and next task unlock after a completed login job', () => {
    const {context, $, sync} = controls({running: false, login: {confirmed: false}, run: {outcome: 'running'}});
    sync();
    assert.equal($('confirm-login').disabled, false);
    assert.equal($('process-range').disabled, true);
    context.runStatus.login.confirmed = true;
    sync();
    assert.equal($('process-range').disabled, false);
    context.runStatus.running = true;
    sync();
    assert.equal($('confirm-login').disabled, true);
    assert.equal($('process-range').disabled, true);
});

test('refresh keeps the signed-in computer and never switches to another online Agent', async () => {
    const {context, $, sync} = controls({running: false, login: {confirmed: true}});
    let agents = [{agent_id: 'other-pc', online: true}, {agent_id: 'signed-in-pc', online: true}];
    context.fetch = async () => ({ok: true, json: async () => ({data: {agents}})});
    context.esc = value => String(value ?? '');
    vm.runInContext(functionSource('loadAgents'), context);
    await vm.runInContext('loadAgents()', context);
    assert.equal($('execution-agent').value, 'signed-in-pc');
    agents = [agents[0]];
    await vm.runInContext('loadAgents()', context);
    await vm.runInContext('loadAgents()', context);
    assert.equal(context.selectedAgentId, 'signed-in-pc');
    assert.equal($('confirm-login').disabled, true);
    assert.equal($('process-range').disabled, true);
    agents.push({agent_id: 'signed-in-pc', online: true});
    await vm.runInContext('loadAgents()', context);
    sync();
    assert.equal($('confirm-login').disabled, false);
    assert.equal($('process-range').disabled, false);
});

test('opening the page restores the login Agent before loading the computer list', async () => {
    const source = template.split('\n').find(line => line.startsWith('(async()=>')).split(';setInterval')[0];
    const calls = [];
    const context = {agentLaunchAvailable: true, selectedAgentId: '', runStatus: {},
        api: async path => path === 'status' ? {execution_identity: {target: 'agent', agent_id: 'signed-in-pc'}} : {options: [], meta: {}},
        loadAgents: async () => calls.push(context.selectedAgentId), loadConfig: async () => {}, loadOwners: async () => {},
        renderCategories() {}, categoryStatus() {}, refresh: async () => {}, notify(message) {throw Error(message);} };
    await vm.runInNewContext(source, context);
    assert.deepEqual(calls, ['signed-in-pc']);
});

test('terminating a stale batch unlocks browser launch and a new batch', () => {
    const {context, $, sync} = controls({running: true, login: {confirmed: true},
        circuit: {reason: 'old pause'}, run: {run_id: 'old', outcome: 'running'}});
    sync();
    assert.equal($('open-login').disabled, true);
    assert.equal($('process-range').disabled, true);
    context.runStatus = {running: false, login: {confirmed: true}, circuit: null, run: {}};
    sync();
    assert.equal($('open-login').disabled, false);
    assert.equal($('process-range').disabled, false);
});

test('Agent login jobs can be terminated before a batch exists', () => {
    for (const name of ['refresh', 'refreshStatusOnly']) {
        const source = functionSource(name);
        const assignment = source.match(/\$\('terminate'\)\.disabled=([^;]+);/)[1];
        const evaluate = changes => vm.runInNewContext(assignment, {
            canExecute: true, terminateBusy: false, executionTarget: 'agent',
            status: {running: false, run: {}}, ...changes,
        });
        assert.equal(evaluate({}), false);
        assert.equal(evaluate({canExecute: false}), true);
        assert.equal(evaluate({terminateBusy: true}), true);
        assert.equal(evaluate({executionTarget: 'local'}), true);
    }
});

test('owner filter resets pagination and log cursor without changing execution account', () => {
    const calls = [];
    const context = {selectedOwnerId: '', ownerFilterVersion: 0, page: 4, seq: 1,
        logCache: [{id: 99}], lastLogId: 99, selectedAgentId: 'my-agent',
        $: id => id === 'owner-filter' ? {value: '8'} : {},
        renderLiveLogs: entries => calls.push(entries.length),
        syncLogHistory: entries => calls.push(entries.length), refresh: () => calls.push('refresh')};
    vm.createContext(context);
    vm.runInContext(functionSource('changeOwnerFilter'), context);
    vm.runInContext('changeOwnerFilter()', context);
    assert.equal(context.selectedOwnerId, '8');
    assert.equal(context.page, 1);
    assert.equal(context.lastLogId, 0);
    assert.equal(context.logCache.length, 0);
    assert.equal(context.ownerFilterVersion, 1);
    assert.equal(context.selectedAgentId, 'my-agent');
    assert.deepEqual(calls, [0, 0, 'refresh']);
});

test('owner query is added only to record reads, never browser actions', async () => {
    const requests = [];
    const context = {selectedOwnerId: '8', executionTarget: 'agent', selectedAgentId: 'my-agent',
        isAgentAction: (_, method) => method === 'POST', encodeURIComponent,
        fetch: async (url, options) => {requests.push({url, options}); return {ok: true, json: async () => ({})};}};
    vm.createContext(context);
    vm.runInContext(functionSource('api'), context);
    await vm.runInContext("api('logs?after=4')", context);
    await vm.runInContext("api('tasks?page=2')", context);
    await vm.runInContext("api('start','POST',{})", context);
    assert.match(requests[0].url, /after=4&owner_user_id=8$/);
    assert.match(requests[1].url, /page=2&owner_user_id=8$/);
    assert.equal(requests[2].url, '/api/ai-weight-price/start');
    assert.equal(JSON.parse(requests[2].options.body).owner_user_id, undefined);
});
