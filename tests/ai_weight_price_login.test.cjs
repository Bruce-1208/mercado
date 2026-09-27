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
        loadAgents: async () => calls.push(context.selectedAgentId), loadConfig: async () => {},
        renderCategories() {}, categoryStatus() {}, refresh: async () => {}, notify(message) {throw Error(message);} };
    await vm.runInNewContext(source, context);
    assert.deepEqual(calls, ['signed-in-pc']);
});
