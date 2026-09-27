const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const template = fs.readFileSync(require('node:path').join(__dirname, '../bit/templates/index.html'), 'utf8');
const source = template.slice(template.indexOf('        let reputationBrowserTaskId ='), template.indexOf('        function reputationRowCheckboxes()'));

test('browser sync submits selected shops and refreshes results on completion', async () => {
    const calls = [], timers = [];
    const elements = {
        'reputation-browser-sync-btn': {disabled: false},
        'reputation-browser-status': {textContent: ''},
        'reputation-browser-target': {value: 'agent'},
        'reputation-browser-agent': {value: 'office'},
    };
    let refreshed = 0;
    const context = vm.createContext({
        document: {getElementById: id => elements[id]},
        selectedReputationShops: new Set(['店铺甲', '店铺乙']),
        setTimeout: callback => timers.push(callback),
        updateReputationTableSelection() {},
        async loadLatestReputation() { refreshed++; },
        async fetch(url, options) {
            calls.push({url, options});
            return {ok: true, json: async () => ({status: 'success', data: options?.method === 'POST'
                ? {task_id: 'browser-job'} : {running: false, status: 'success', message: '同步完成'}})};
        },
    });
    vm.runInContext(source, context);
    await context.runReputationBrowserSync();
    assert.equal(calls[0].url, '/api/reputation/browser-sync');
    assert.deepEqual(JSON.parse(calls[0].options.body), {
        shops: ['店铺甲', '店铺乙'], execution_target: 'agent', agent_id: 'office',
    });
    await timers.shift()();
    assert.equal(calls[1].url, '/api/reputation/browser-sync/browser-job');
    assert.equal(refreshed, 1);
    assert.equal(elements['reputation-browser-status'].textContent, '同步完成');
});

test('missing Agent does not silently use server resources', async () => {
    const status = {};
    let requests = 0;
    const context = vm.createContext({
        document: {getElementById: id => id === 'reputation-browser-status' ? status :
            id === 'reputation-browser-target' ? {value: 'agent'} : {value: ''}},
        selectedReputationShops: new Set(['店铺']),
        updateReputationTableSelection() {},
        fetch() { requests++; },
    });
    vm.runInContext(source, context);
    await context.runReputationBrowserSync();
    assert.equal(requests, 0);
    assert.match(status.textContent, /请选择|请刷新/);
});
