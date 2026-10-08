'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const read = path => fs.readFileSync(path, 'utf8');
function between(source, first, last) {
  const start = source.indexOf(first), end = source.indexOf(last, start);
  assert.ok(start >= 0 && end > start, `Missing source boundary: ${first}`);
  return source.slice(start, end);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
const clients = [
  {name:'employee tasks', file:'bit/static/employee-tasks.js', first:'  function api(', last:'  function render()',
    setup:'const readRequests = new Map();', get:[''], write:['', 'POST', {}]},
  {name:'billing', file:'bit/static/account-billing.js', first:'  const readRequests = new Map();', last:'  function render()',
    get:['/api/test'], write:['/api/test', {method:'POST', body:'{}'}]},
  {name:'print wallet', file:'bit/static/print-wallet.js', first:'  const readRequests = new Map();', last:'  function selectedAccount()',
    get:['/api/test'], write:['/api/test', {method:'POST', body:'{}'}]},
  {name:'size charts', file:'bit/static/size-charts.js', first:'    const readRequests = new Map();', last:'    function controls()',
    get:['/templates'], write:['/templates', 'POST', {}]},
  {name:'promotions', file:'bit/templates/mercado_promotions.html', first:'const promotionReadRequests=new Map();', last:'function fmtDate(',
    get:['/api/test'], write:['/api/test', {method:'POST', body:'{}'}]},
  {name:'AI weight and price', file:'bit/templates/ai_weight_price.html', first:'const awpReadRequests=new Map();', last:'async function action(',
    get:['status'], write:['status', 'POST', {}]},
];
function clientHarness(client, fetcher) {
  const window = {fetch:fetcher, location:{search:'', pathname:'/module'}, top:{location:{}}};
  const context = {fetch:fetcher, window, document:{hidden:false}, URLSearchParams, Map,
    endpoint:() => '/api/employee-tasks?', selectedOwnerId:'', executionTarget:'local', selectedAgentId:'',
    isAgentAction:() => false,
    apiError:(message, outcomeUnknown) => Object.assign(new Error(message), {outcomeUnknown}),
  };
  vm.createContext(context);
  vm.runInContext((client.setup || '') + between(read(client.file), client.first, client.last), context);
  return context;
}
const response = version => ({ok:true, status:200, json:async () => ({status:'success', data:{version}})});
for (const client of clients) {
  test(`${client.name}: duplicate reads share a request, writes remain separate and invalidate pending reads`, async () => {
    const oldRead = deferred(), newRead = deferred(), calls = [];
    let readCount = 0;
    const context = clientHarness(client, (_, options = {}) => {
      const method = options.method || 'GET'; calls.push(method);
      if (method !== 'GET') return Promise.resolve(response('saved'));
      readCount++;
      return readCount === 1 ? oldRead.promise : newRead.promise;
    });
    const first = context.api(...client.get), duplicate = context.api(...client.get);
    assert.equal(first, duplicate);
    assert.equal(readCount, 1);
    await Promise.all([context.api(...client.write), context.api(...client.write)]);
    assert.equal(calls.filter(method => method === 'POST').length, 2);
    const fresh = context.api(...client.get);
    assert.equal(readCount, 2);
    oldRead.resolve(response('old'));
    await first;
    assert.equal(context.api(...client.get), fresh, 'An old response must not clear the newer pending read');
    newRead.resolve(response('fresh')); await fresh;
  });
  test(`${client.name}: a failed read can be retried`, async () => {
    let count = 0;
    const context = clientHarness(client, () => ++count === 1 ? Promise.reject(new Error('offline')) : Promise.resolve(response('retry')));
    await assert.rejects(context.api(...client.get));
    await context.api(...client.get);
    assert.equal(count, 2);
  });
}

test('AI polling respects the parent module visibility and pause control', () => {
  const source = read('bit/templates/ai_weight_price.html');
  const timers = [], events = {}, context = {active:false, autoRefreshPaused:false, agentLaunchAvailable:true,
    awpLastStatusAt:0, statuses:0, agents:0, Date:{now:() => 20000},
    setInterval:(callback, delay) => timers.push({callback, delay}),
    document:{addEventListener:(name, callback) => { events[name] = callback; }},
    awpPageActive:() => context.active,
    refreshStatusOnly:() => { context.statuses++; context.awpLastStatusAt = 20000; },
    loadAgents:() => { context.agents++; },
  };
  vm.createContext(context);
  vm.runInContext(between(source, 'setInterval(()=>{if(!autoRefreshPaused&&awpPageActive())', '</script>'), context);
  timers.forEach(timer => timer.callback());
  events['zeshun:visibility']();
  assert.equal(context.statuses, 0); assert.equal(context.agents, 0);
  context.active = true; events['zeshun:visibility']();
  assert.equal(context.statuses, 1);
  events['visibilitychange'](); assert.equal(context.statuses, 1, 'A fresh activation should reuse the preloaded status');
  timers.forEach(timer => timer.callback());
  assert.equal(context.statuses, 2); assert.equal(context.agents, 1);
  context.autoRefreshPaused = true; timers.forEach(timer => timer.callback());
  assert.equal(context.statuses, 2); assert.equal(context.agents, 1);
});

test('AI agent discovery shares slow in-flight reads and can be refreshed again', async () => {
  const source = read('bit/templates/ai_weight_price.html'), waiting = deferred();
  let reads = 0;
  const context = {performLoadAgents:() => { reads++; return waiting.promise; }};
  vm.createContext(context);
  vm.runInContext(between(source, 'let awpAgentPromise=null;', 'async function performLoadAgents()'), context);
  const first = context.loadAgents();
  assert.equal(context.loadAgents(), first); assert.equal(reads, 1);
  waiting.resolve(); await first;
  await context.loadAgents(); assert.equal(reads, 2);
});

test('infraction preloading reads once and suppresses all hidden iframe polls', () => {
  const source = read('bit/templates/infraction_dashboard.html');
  const start = source.lastIndexOf('        loadData();\n        loadSyncStatus();');
  const timers = [], events = {}, context = {active:false, lists:0, statuses:0,
    dashboardLastLoadedAt:0, dashboardLastSyncAt:0, state:{wasSyncing:false}, Date:{now:() => 600000},
    dashboardActive:() => context.active,
    loadData:() => { context.lists++; context.dashboardLastLoadedAt = 600000; },
    loadSyncStatus:() => { context.statuses++; context.dashboardLastSyncAt = 600000; },
    setInterval:(callback, delay) => timers.push({callback, delay}),
    document:{addEventListener:(name, callback) => { events[name] = callback; }},
    window:{addEventListener:() => {}},
  };
  vm.createContext(context); vm.runInContext(source.slice(start, source.indexOf('</script>', start)), context);
  assert.equal(context.lists, 1); assert.equal(context.statuses, 1);
  timers.forEach(timer => timer.callback()); events['zeshun:visibility']();
  assert.equal(context.lists, 1); assert.equal(context.statuses, 1);
  context.active = true; events['zeshun:visibility']();
  assert.equal(context.lists, 1); assert.equal(context.statuses, 1, 'Opening a fresh preloaded module should not reread');
  timers.forEach(timer => timer.callback());
  assert.equal(context.lists, 2); assert.equal(context.statuses, 2);
});

test('infraction identical dashboard and sync reads are coalesced while filter changes remain independent', async () => {
  const source = read('bit/templates/infraction_dashboard.html'), first = deferred(), second = deferred(), sync = deferred();
  const calls = [], context = {key:'page=1',
    dashboardParams:() => ({toString:() => context.key}),
    performDashboardLoad:key => { calls.push(key); return key === 'page=1' ? first.promise : second.promise; },
    performLoadSyncStatus:() => { calls.push('sync'); return sync.promise; },
    window:{}, document:{hidden:false},
  };
  vm.createContext(context);
  vm.runInContext(between(source, '        let dashboardLoadSequence = 0;', '        async function performDashboardLoad(')
    + between(source, '        let dashboardSyncPromise = null;', '        async function performLoadSyncStatus('), context);
  const a = context.loadData(); assert.equal(context.loadData(), a);
  context.key = 'page=2'; const b = context.loadData(); assert.notEqual(b, a);
  assert.equal(context.loadData(), b);
  const s = context.loadSyncStatus(); assert.equal(context.loadSyncStatus(), s);
  first.resolve(); await a; assert.equal(context.loadData(), b);
  second.resolve(); sync.resolve(); await Promise.all([b, s]);
  assert.deepEqual(calls, ['page=1', 'page=2', 'sync']);
});
