const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const extension = path.join(__dirname, '../browser_extension/zeshun_collector');
const background = fs.readFileSync(path.join(extension, 'background.js'), 'utf8');
const batch = fs.readFileSync(path.join(extension, 'product-batch.js'), 'utf8');
const product = categoryId => ({
  source_item_id: 'MLM-123', title: 'Test product', source_platform: 'mercado',
  source_url: 'https://articulo.mercadolibre.com.mx/MLM-123', scrape_status: 'ok',
  management_category_id: categoryId
});

function setup({authMode = 'token'} = {}) {
  let listener;
  const stored = {
    local: authMode ? {browserExtensionAuth: {
      mode: authMode, token: 'test-token', user: {username: 'test'}
    }} : {},
    sync: {consoleUrl: 'https://console.test', consoleUrlDefaultMigration: 1}, session: {}
  };
  const event = () => ({addListener() {}, removeListener() {}});
  const storage = area => ({
    get(keys, callback) { callback(Object.fromEntries(keys.map(key => [key, stored[area][key]]))); },
    set(values, callback) { Object.assign(stored[area], structuredClone(values)); callback?.(); },
    remove(keys, callback) { for (const key of keys) delete stored[area][key]; callback?.(); }
  });
  const requests = [];
  const chrome = {
    storage: {local: storage('local'), sync: storage('sync'), session: storage('session')},
    runtime: {onInstalled: event(), onStartup: event(), lastError: null,
      onMessage: {addListener(value) { listener = value; }},
      getURL: value => `extension:///${value}`, getPlatformInfo: callback => callback({})},
    alarms: {onAlarm: event(), create() {}},
    contextMenus: {onClicked: event(), removeAll(callback) { callback?.(); }, create() {}},
    action: {setBadgeBackgroundColor: async () => {}, setBadgeText: async () => {}},
    windows: {onRemoved: event(), create: async () => ({id: 2}), remove: async () => {}},
    tabs: {get: async () => ({id: 1, url: 'https://listado.mercadolibre.com.mx/toy'}),
      create: async () => ({id: 3})}
  };
  const context = vm.createContext({
    chrome, URL, AbortSignal, setTimeout, clearTimeout, setInterval, clearInterval,
    fetch: async (url, options) => {
      requests.push({url, options});
      return {ok: true, status: 200, json: async () => ({status: 'success', data: {
        ready: true, task_id: 5, product_id: 12, scrape_status: 'ok', source_platform: 'mercado'
      }})};
    },
    importScripts(file) {
      if (file === 'product-batch.js') vm.runInContext(batch, context);
    }
  });
  vm.runInContext(background, context);
  return {context, stored, requests, chrome,
    run: code => vm.runInContext(code, context),
    send: message => new Promise(resolve => listener(message, {}, resolve))};
}


function singleSetup(responses) {
  const fixture = setup();
  const created = [], removed = [], submitted = [];
  fixture.context.responses = responses;
  fixture.context.submitted = submitted;
  fixture.chrome.tabs.create = async options => { created.push(options); return {id: 7}; };
  fixture.chrome.tabs.get = async () => ({id: 7, url: 'https://articulo.mercadolibre.com.mx/MLM-123456-test-_JM'});
  fixture.chrome.tabs.remove = async id => { removed.push(id); };
  fixture.run(`sendTabMessage = async () => responses.length > 1 ? responses.shift() : responses[0];
    submitProduct = async product => { submitted.push(product); return {ok:true, queued:false}; };`);
  return {...fixture, created, removed, submitted};
}
const detail = {source_item_id:'MLM123456', plugin_snapshot:{fulfillment_eligible:true}};
const collect = fixture => fixture.send({type:'COLLECT_MERCADO_URL', url:'https://articulo.mercadolibre.com.mx/MLM-123456-test-_JM'});

test('one click waits for detail readiness, submits once and closes its background tab', async () => {
  const f = singleSetup([{ok:false, error:'等待商品详情加载'}, {ok:true, product:detail}]);
  const result = await collect(f);
  assert.equal(result.ok, true);
  assert.equal(f.created.length, 1);
  assert.equal(f.created[0].active, false);
  assert.deepEqual(f.submitted, [detail]);
  assert.deepEqual(f.removed, [7]);
});

for (const response of [
  {ok:false, blocked:true, error:'请完成验证'},
  {ok:false, authRequired:true, error:'登录失效'},
  {ok:false, retryable:false, error:'数据源不可用'},
  {ok:true, product:{source_item_id:"MLM123456", plugin_snapshot:{fulfillment_eligible:false, fulfillment_label:'Full'}}}
]) test('failed or ineligible detail is never uploaded and its tab is closed: '+JSON.stringify(response), async () => {
  const f = singleSetup([response]);
  const result = await collect(f);
  assert.equal(result.ok, false);
  assert.ok(result.error);
  assert.equal(f.submitted.length, 0);
  assert.deepEqual(f.removed, [7]);
});

test('queued upload is reported to the listing', async () => {
  const f = singleSetup([{ok:true, product:detail}]);
  f.run('submitProduct = async () => ({ok:true, queued:true})');
  assert.equal((await collect(f)).queued, true);
  assert.deepEqual(f.removed, [7]);
});

test('invalid destination is rejected before creating a tab', async () => {
  const f = singleSetup([]);
  assert.equal((await f.send({type:'COLLECT_MERCADO_URL', url:'https://example.com'})).ok, false);
  assert.equal(f.created.length, 0);
});

test('closed detail tab surfaces a failure without upload', async () => {
  const f = singleSetup([]);
  f.chrome.tabs.get = async () => { throw new Error('标签页已关闭'); };
  assert.equal((await collect(f)).ok, false);
  assert.equal(f.submitted.length, 0);
  assert.deepEqual(f.removed, [7]);
});


test('redirect to a different seller listing is not uploaded', async () => {
  const f = singleSetup([{ok:true, product:{...detail, source_item_id:'MLM999999'}}]);
  const result = await collect(f);
  assert.equal(result.ok, false);
  assert.match(result.error, /其他商品/);
  assert.equal(f.submitted.length, 0);
  assert.deepEqual(f.removed, [7]);
});
