const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('browser_extension/zeshun_collector/zying-order-runner.js', 'utf8');

function harness() {
  const storage = {};
  const calls = [];
  const writes = [];
  let state = {phase:'idle', running:false};
  let scriptResult = {rows:[{order_id:'2001',detail:{}}]};
  let beforeResult = () => {};
  let lostResponse = false;
  let loaded = true;
  let pageBusy = false;
  let startPhase = 'waiting_login';
  let startError;
  let opened = 0;
  const chrome = {
    alarms: {create() {}},
    tabs: {
      create:async () => {opened++; return {id:42};}, get:async () => ({id:42}),
      update:async () => ({id:42}), reload:async () => {}
    },
    scripting:{executeScript:async ({args}) => {
      if (!args) return [{result:loaded}];
      if (pageBusy) return [{result:{busy:true}}];
      beforeResult();
      return [{result:structuredClone(scriptResult)}];
    }}
  };
  const context = vm.createContext({chrome, console:{warn(){}}, zeshunReadZyingOrderBatch() {}});
  const create = () => {
    vm.runInContext(source, context);
    const runner = context.ZeshunOrderSync;
    runner.configure({
      storageGet:async () => storage, storageSet:async (_area, value) => Object.assign(storage, value),
      waitTab:async () => {},
      apiRequest:async (path, options) => {
        const action = path.split('/').pop();
        const data = options.body ? JSON.parse(options.body) : {};
        calls.push({action, data});
        if (action === 'start') {
          state = {...data,request_id:'run1',phase:startPhase,running:startPhase !== 'completed',total:1};
          if (startError) throw startError;
        }
        if (action === 'confirm') state.phase = 'syncing';
        if (action === 'stop') state = {...state,phase:'stopped',running:false};
        if (action === 'fail') state = {...state,phase:'error',running:false,message:data.message};
        if (action === 'login_required') state.phase = 'waiting_login';
        if (action === 'batch') return {...state,rows:state.running && state.phase==='syncing' ? [{id:'2001'}] : []};
        if (action === 'results') {
          writes.push(data.rows);
          state = {...state,phase:'completed',running:false,completed:1,updated:1};
          if (lostResponse) throw new Error('response lost after commit');
        }
        return structuredClone(state);
      }
    });
    return runner;
  };
  return {create, calls, writes, storage,
    beforeResult(value) {beforeResult = value;},
    loseResponse() {lostResponse = true;},
    setLoaded(value) {loaded = value;},
    setBusy(value) {pageBusy = value;},
    startPhase(value) {startPhase = value;},
    startError(value) {startError = value;},
    opened() {return opened;},
    setResult(value) {scriptResult = value;},
    stopServer() {state = {...state,phase:'stopped',running:false};},
    state() {return state;}
  };
}

test('runs after manual login, and resumes a lost committed response without replaying', async () => {
  const fixture = harness();
  let runner = fixture.create();
  await runner.start({date_start:'2026-10-01',date_end:'2026-10-08'});
  fixture.loseResponse();
  await runner.confirm();
  await runner.resume();
  assert.equal(fixture.writes.length, 1);
  runner = fixture.create(); // service-worker restart, same persisted browser session
  await runner.resume();
  assert.equal(fixture.writes.length, 1);
  assert.equal(fixture.state().phase, 'completed');
});

test('opens login while server preparation is still loading', async () => {
  const fixture = harness();
  fixture.startPhase('loading');
  const state = await fixture.create().start({date_start:'2026-10-01',date_end:'2026-10-08'});
  assert.equal(state.phase, 'loading');
  assert.equal(fixture.opened(), 1);
  assert.equal(fixture.storage.zyingOrderSyncSession.request_id, 'run1');
});

test('recovers a lost start response by status without repeating start', async () => {
  const fixture = harness();
  fixture.startPhase('loading');
  fixture.startError(new Error('timed out after reservation'));
  const state = await fixture.create().start({date_start:'2026-10-01',date_end:'2026-10-08'});
  assert.equal(state.phase, 'loading');
  assert.equal(fixture.opened(), 1);
  assert.equal(fixture.calls.filter(call => call.action === 'start').length, 1);
});

test('does not recover an authorization failure or open an empty run', async () => {
  const fixture = harness();
  fixture.startError(Object.assign(new Error('no permission'), {status:403}));
  await assert.rejects(fixture.create().start({date_start:'2026-10-01',date_end:'2026-10-08'}), /no permission/);
  assert.equal(fixture.opened(), 0);
  assert.equal(fixture.calls.some(call => call.action === 'status'), false);
  const empty = harness();
  empty.startPhase('completed');
  await empty.create().start({date_start:'2026-10-01',date_end:'2026-10-08'});
  assert.equal(empty.opened(), 0);
});

test('stopping while the page is reading prevents late writeback', async () => {
  const fixture = harness();
  const runner = fixture.create();
  await runner.start({date_start:'2026-10-01',date_end:'2026-10-08'});
  fixture.beforeResult(() => fixture.stopServer());
  await runner.confirm();
  await runner.resume();
  assert.equal(fixture.writes.length, 0);
  assert.equal(fixture.state().phase, 'stopped');
});

test('expired page login restores confirmation and preserves pending work', async () => {
  const fixture = harness();
  const runner = fixture.create();
  await runner.start({date_start:'2026-10-01',date_end:'2026-10-08'});
  fixture.setResult({login_required:true});
  await runner.confirm();
  await runner.resume();
  assert.equal(fixture.writes.length, 0);
  assert.equal(fixture.state().phase, 'waiting_login');
  fixture.setResult({rows:[{order_id:'2001',detail:{}}]});
  await runner.confirm();
  await runner.resume();
  assert.equal(fixture.writes.length, 1);
});

test('a surviving page read defers recovery until the next run', async () => {
  const fixture = harness();
  const runner = fixture.create();
  await runner.start({date_start:'2026-10-01',date_end:'2026-10-08'});
  fixture.setBusy(true);
  await runner.confirm();
  await runner.resume();
  assert.equal(fixture.writes.length, 0);
  assert.equal(fixture.state().phase, 'syncing');
  fixture.setBusy(false);
  await runner.resume();
  assert.equal(fixture.writes.length, 1);
});
