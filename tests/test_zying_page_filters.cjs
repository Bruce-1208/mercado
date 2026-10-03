const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../browser_extension/zeshun_collector/zying-page.js'), 'utf8');
const definitions = vm.createContext({});
vm.runInContext(source, definitions);
// MAIN-world injection serializes the function, so exercise it without any
// other declarations from zying-page.js in scope.
const readSource = `(${definitions.zeshunReadZyingProductDevelopers.toString()})()`;
const plain = value => JSON.parse(JSON.stringify(value));

function setup({nodes = [], indexedDB, window = {}} = {}) {
  let now = 0;
  let sequence = 0;
  const timers = new Map();
  const schedule = (callback, delay = 0) => {
    const id = ++sequence;
    timers.set(id, {at: now + delay, callback});
    return id;
  };
  const context = vm.createContext({
    document: {querySelectorAll: () => nodes}, indexedDB, window,
    Date: {now: () => now}, setTimeout: schedule,
    clearTimeout: id => timers.delete(id),
  });
  const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  const next = () => [...timers].sort((a, b) => a[1].at - b[1].at)[0];
  const advance = async target => {
    await flush();
    for (let task = next(); task && task[1].at <= target; task = next()) {
      now = task[1].at;
      timers.delete(task[0]);
      task[1].callback();
      await flush();
    }
    now = target;
    await flush();
  };
  const run = async (limit = 3000) => {
    let settled = false;
    let result;
    let failure;
    vm.runInContext(readSource, context).then(value => {
      settled = true; result = value;
    }, error => { settled = true; failure = error; });
    await flush();
    while (!settled) {
      const task = next();
      assert.ok(task && task[1].at <= limit, 'developer lookup must settle within its total budget');
      await advance(task[1].at);
    }
    if (failure) throw failure;
    return {rows: plain(result), elapsed: now};
  };
  return {schedule, advance, run, timers};
}

function selector(options, reactOptions) {
  const node = {
    closest: () => ({textContent: '产品开发', querySelector: () => null}),
    getAttribute: () => null,
    querySelectorAll: () => options,
  };
  if (reactOptions) node.__reactFiber$test = {memoizedProps: reactOptions};
  return node;
}

function cacheDatabase(env, value, {hang = false, stores = ['keyvaluepairs']} = {}) {
  const state = {closed: 0, aborted: 0, gets: []};
  const db = {
    objectStoreNames: stores,
    close() { state.closed++; },
    transaction(store, mode) {
      assert.equal(mode, 'readonly');
      return {
        abort() { state.aborted++; },
        objectStore() { return {get(key) {
          state.gets.push({store, key});
          const request = {result: value};
          if (!hang) env.schedule(() => request.onsuccess(), 0);
          return request;
        }}; },
      };
    },
  };
  return {db, state};
}

test('rendered native developer options return immediately without touching IndexedDB', async () => {
  let touches = 0;
  const indexedDB = new Proxy({}, {get() { touches++; throw new Error('unexpected DB lookup'); }});
  const env = setup({indexedDB, nodes: [selector([
    {value: '', textContent: '全部'}, {value: '17', textContent: '小林'},
    {value: '17', textContent: '小林'}, {value: '18', textContent: '小陈'},
  ])]});
  assert.deepEqual(await env.run(), {rows: [{id: '17', name: '小林'}, {id: '18', name: '小陈'}], elapsed: 0});
  assert.equal(touches, 0);
  assert.equal(env.timers.size, 0);
});

test('rendered React options respect custom fields and skip placeholder entries', async () => {
  const env = setup({nodes: [selector([], {
    options: [{staff_id: 42, staff_name: '小张'}, {staff_id: 0, staff_name: '请选择'}],
    fieldNames: {value: 'staff_id', label: 'staff_name'},
  })]});
  assert.deepEqual((await env.run()).rows, [{id: '42', name: '小张'}]);
});

test('enumeration opens only existing databases and prefers localforage', async () => {
  const calls = [];
  let env;
  let cached;
  env = setup({indexedDB: {
    databases: async () => [{name: 'unrelated'}, {name: 'localforage'}, {name: 'localforage'}],
    open(name) {
      calls.push(name);
      const request = {result: cached.db};
      env.schedule(() => request.onsuccess(), 0);
      return request;
    },
  }});
  cached = cacheDatabase(env, {logins: [{loginid: '7', loginname: '小周'}]});
  assert.deepEqual((await env.run()).rows, [{id: '7', name: '小周'}]);
  assert.deepEqual(calls, ['localforage']);
  assert.equal(cached.state.closed, 1);
  assert.equal(env.timers.size, 0);

  const absent = setup({indexedDB: {databases: async () => [], open() { throw new Error('must not create localforage'); }}});
  assert.deepEqual(await absent.run(), {rows: [], elapsed: 0});
});

test('cached login wrappers and identifier aliases remain compatible', async () => {
  for (const value of [
    [{id: 8, name: '小吴'}, {id: 8, name: '重复'}, {name: '缺少编号'}],
    {logins: [{loginid: 8, loginname: '小吴'}]},
    {data: {rows: [{value: 8, label: '小吴'}]}},
    {list: [{id: 8, name: '小吴'}]},
  ]) {
    let env;
    let cached;
    env = setup({indexedDB: {
      databases: async () => [{name: 'existing-cache'}],
      open(name) {
        assert.equal(name, 'existing-cache');
        const request = {result: cached.db};
        env.schedule(() => request.onsuccess(), 0);
        return request;
      },
    }});
    cached = cacheDatabase(env, value);
    assert.deepEqual((await env.run()).rows, [{id: '8', name: '小吴'}]);
    assert.equal(cached.state.closed, 1);
  }
});

test('blocked and stalled databases share one budget and close pending transactions', async () => {
  const calls = [];
  let env;
  let cached;
  env = setup({indexedDB: {
    databases: async () => [{name: 'blocked'}, {name: 'slow'}, {name: 'another-slow'}],
    open(name) {
      calls.push(name);
      const request = {result: cached.db};
      env.schedule(() => name === 'blocked' ? request.onblocked() : request.onsuccess(), 10);
      return request;
    },
  }});
  cached = cacheDatabase(env, null, {hang: true});
  assert.deepEqual(await env.run(), {rows: [], elapsed: 800});
  assert.deepEqual(calls, ['blocked', 'slow']);
  assert.equal(cached.state.closed, 1);
  assert.equal(cached.state.aborted, 1);
  assert.equal(env.timers.size, 0);
});

test('database enumeration also has a deadline', async () => {
  let opens = 0;
  const env = setup({indexedDB: {
    databases: () => new Promise(() => {}),
    open() { opens++; },
  }});
  assert.deepEqual(await env.run(), {rows: [], elapsed: 800});
  assert.equal(opens, 0);
});

test('an open connection arriving after timeout is closed without reading stores', async () => {
  let env;
  let cached;
  env = setup({indexedDB: {
    databases: async () => [{name: 'slow-open'}],
    open() {
      const request = {result: cached.db};
      env.schedule(() => request.onsuccess(), 900);
      return request;
    },
  }});
  cached = cacheDatabase(env, [{id: '9', name: '晚到'}]);
  assert.deepEqual(await env.run(), {rows: [], elapsed: 800});
  await env.advance(900);
  assert.equal(cached.state.closed, 1);
  assert.deepEqual(cached.state.gets, []);
});

test('older browsers abort a missing localforage creation instead of leaving an empty database', async () => {
  let created = false;
  let aborted = 0;
  let closed = 0;
  let env;
  env = setup({indexedDB: {open(name) {
    assert.equal(name, 'localforage');
    const request = {
      transaction: {abort() { aborted++; }},
      result: {close() { closed++; }},
    };
    env.schedule(() => {
      request.onupgradeneeded({oldVersion: 0});
      created = aborted === 0;
      if (aborted) request.onerror();
    }, 0);
    return request;
  }}});
  assert.deepEqual(await env.run(), {rows: [], elapsed: 0});
  assert.equal(created, false);
  assert.equal(aborted, 1);
  assert.equal(closed, 1);
  assert.equal(env.timers.size, 0);
});

test('older browsers can still read an existing localforage cache', async () => {
  let env;
  let cached;
  env = setup({indexedDB: {open() {
    const request = {result: cached.db};
    env.schedule(() => request.onsuccess(), 0);
    return request;
  }}});
  cached = cacheDatabase(env, {data: [{id: '6', name: '小郑'}]});
  assert.deepEqual((await env.run()).rows, [{id: '6', name: '小郑'}]);
  assert.equal(cached.state.closed, 1);
});

test('webpack fallback requests share a bounded deadline', async () => {
  let calls = 0;
  const api = {getFreeStyleApi() { calls++; return new Promise(() => {}); }};
  const require = () => ({api});
  require.c = {one: {exports: api}, two: {exports: {getFreeStyleApi() { calls++; }}}};
  const env = setup({
    indexedDB: {databases: async () => []},
    window: {webpackChunkzying: [], __zeshunZyingRequire: require},
  });
  assert.deepEqual(await env.run(), {rows: [], elapsed: 1200});
  assert.equal(calls, 1);
  assert.equal(env.timers.size, 0);
});

test('webpack fallback still returns valid logins and clears its timer', async () => {
  const require = () => ({getFreeStyleApi: async (method, payload) => {
    assert.equal(method, 'logins.select');
    assert.deepEqual(plain(payload), {});
    return {data: {logins: [{id: '23', name: '小赵'}]}};
  }});
  const env = setup({
    indexedDB: {databases: async () => []},
    window: {webpackChunkzying: [], __zeshunZyingRequire: require},
  });
  assert.deepEqual(await env.run(), {rows: [{id: '23', name: '小赵'}], elapsed: 0});
  assert.equal(env.timers.size, 0);
});
