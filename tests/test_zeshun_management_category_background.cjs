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
    }} : {consoleAuthSyncDisabled: true},
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
    chrome, URL, AbortController, AbortSignal, setTimeout, clearTimeout, setInterval, clearInterval,
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

test('management category values normalize and invalid selections fail before collection', async () => {
  const {run, send, requests, stored} = setup();
  for (const value of [null, undefined, '', '  ']) {
    assert.equal(run(`normalizeManagementCategoryId(${JSON.stringify(value)})`), null);
  }
  for (const value of [7, '7', ' 7 ']) {
    assert.equal(run(`normalizeManagementCategoryId(${JSON.stringify(value)})`), 7);
  }
  for (const value of [0, -1, 1.2, true, {}, '1e2', 'NaN', '9007199254740992']) {
    assert.throws(() => run(`productBatchParams({max_items: 2, management_category_id: ${JSON.stringify(value)}})`), /分类/);
    const result = await send({type: 'SUBMIT_PRODUCT', product: product(value)});
    assert.equal(result.ok, false);
    assert.match(result.error, /分类/);
  }
  assert.equal(requests.length, 0);
  assert.equal(stored.local.pendingProducts, undefined);
});

test('category message uses plugin authentication and maps rows for the popup', async () => {
  const {context, send, requests} = setup();
  context.fetch = async (url, options) => {
    requests.push({url, options});
    return {ok: true, json: async () => ({status: 'success', data: {
      total: 1, rows: [{id: '7', name: '夏季产品', listing_ratio_percent: 100}]
    }})};
  };
  const result = await send({type: 'GET_ZESHUN_CATEGORIES'});
  assert.equal(result.ok, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.categories)), [
    {id: 7, name: '夏季产品', listing_ratio_percent: 100}
  ]);
  assert.equal(requests[0].url, 'https://console.test/api/browser-extension/management-categories');
  assert.equal(requests[0].options.headers.Authorization, 'Bearer test-token');
});

test('category loading requires login and gives an explicit legacy upgrade error', async () => {
  for (const authMode of [null, 'legacy']) {
    const {send, requests} = setup({authMode});
    const result = await send({type: 'GET_ZESHUN_CATEGORIES'});
    assert.equal(result.ok, false);
    assert.match(result.error, authMode ? /更新泽顺服务端.*重新登录/ : /登录/);
    assert.equal(result.authRequired, !authMode);
    assert.equal(requests.length, 0);
  }
});

test('single Mercado product carries the chosen category to the collect API', async () => {
  const {send, requests} = setup();
  const result = await send({type: 'SUBMIT_PRODUCT', product: product('7')});
  assert.equal(result.ok, true);
  assert.equal(result.queued, false);
  const payload = JSON.parse(requests[0].options.body);
  assert.equal(requests[0].url, 'https://console.test/api/browser-extension/collect');
  assert.equal(payload.product.management_category_id, 7);
  assert.equal(payload.product.source_item_id, 'MLM-123');
});

test('offline products retain separate category selections across queue retry', async () => {
  const {context, send, stored, requests} = setup();
  const onlineFetch = context.fetch;
  context.fetch = async () => { throw new Error('offline'); };
  for (const categoryId of [' 7 ', 8]) {
    const result = await send({type: 'SUBMIT_PRODUCT', product: product(categoryId)});
    assert.equal(result.ok, true);
    assert.equal(result.queued, true);
  }
  assert.deepEqual(stored.local.pendingProducts.map(entry => entry.product.management_category_id), [7, 8]);
  context.fetch = onlineFetch;
  const result = await send({type: 'RETRY_QUEUE'});
  assert.equal(result.uploaded, 2);
  assert.equal(result.remaining, 0);
  assert.deepEqual(requests.map(request => JSON.parse(request.options.body).product.management_category_id), [7, 8]);
});

test('server rejection of a chosen category is visible and does not create an infinite retry', async () => {
  const {context, send, stored} = setup();
  context.fetch = async () => ({ok: false, status: 400, json: async () => ({message: '目标分类已删除'})});
  const result = await send({type: 'SUBMIT_PRODUCT', product: product(7)});
  assert.equal(result.ok, false);
  assert.match(result.error, /分类已删除/);
  assert.equal(stored.local.pendingProducts, undefined);
});

test('legacy selected collection fails before writing and unselected collection remains compatible', async () => {
  const {send, requests, stored} = setup({authMode: 'legacy'});
  const rejected = await send({type: 'SUBMIT_PRODUCT', product: product(7)});
  assert.equal(rejected.ok, false);
  assert.match(rejected.error, /更新泽顺服务端.*重新登录/);
  assert.equal(requests.length, 0);
  assert.equal(stored.local.pendingProducts, undefined);
  const collected = await send({type: 'SUBMIT_PRODUCT', product: product(null)});
  assert.equal(collected.ok, true);
  assert.equal(collected.queued, false);
  assert.deepEqual(requests.map(request => new URL(request.url).pathname), [
    '/api/db/mercado-collection/tasks', '/api/db/mercado-collection/items', '/api/db/mercado-collection/tasks/5'
  ]);
});

test('Mercado batch snapshots the category and applies it to every eligible upload', async () => {
  const {context, run, send, requests, stored} = setup();
  context.batchMessage = {type: 'START_PRODUCT_BATCH', tab_id: 1,
    params: {max_items: 2, management_category_id: '7'}};
  run(`
    sendTabMessage = async (_tabId, message) => message.type === 'CHECK_ZYING_PLUGIN'
      ? {ok: true, logged_in: true} : {ok: true};
    actualRunProductBatch = runProductBatch;
    runProductBatch = async run => { capturedBatch = run; };
    readProductBatchPage = async (_run, url, type, consume) => consume(
      type === 'READ_PRODUCT_LIST' ? {page: 1, international_selected: true, next_url: '', items: [
        {url: 'https://articulo.mercadolibre.com.mx/MLM-1'},
        {url: 'https://articulo.mercadolibre.com.mx/MLM-2'}
      ]} : {product: {source_item_id: url.split('/').pop(), title: 'Test',
        management_category_id: 99,
        plugin_snapshot: {fulfillment_type: 'self_ship', fulfillment_eligible: true}}}
    );
  `);
  const started = await send(context.batchMessage);
  assert.equal(started.ok, true);
  assert.equal(started.state.params.management_category_id, 7);
  context.batchMessage.params.management_category_id = 8;
  await run('actualRunProductBatch(capturedBatch)');
  assert.equal(stored.local.productBatch.params.management_category_id, 7);
  assert.equal(stored.local.productBatch.completed_count, 2);
  assert.deepEqual(requests.filter(request => request.options.body).map(request => JSON.parse(request.options.body).product.management_category_id), [7, 7]);
});

test('legacy category batch is rejected before opening collection windows', async () => {
  const {send, stored, chrome, requests} = setup({authMode: 'legacy'});
  let windows = 0;
  chrome.windows.create = async () => { windows += 1; return {id: 2}; };
  const result = await send({type: 'START_PRODUCT_BATCH', tab_id: 1,
    params: {max_items: 2, management_category_id: 7}});
  assert.equal(result.ok, false);
  assert.match(result.error, /更新泽顺服务端.*重新登录/);
  assert.equal(windows, 0);
  assert.equal(stored.local.productBatch, undefined);
  assert.equal(requests.length, 0);
});

test('Zying starts preserve source filters and forward the target category', async () => {
  const {send, requests} = setup();
  const result = await send({type: 'START_ZYING_COLLECTION',
    context: {credential: 'zying-test', categories: [{id: 'source-9'}]},
    params: {max_items: 2, category_ids: ['source-9'], management_category_id: '7'}});
  assert.equal(result.ok, true);
  const payload = JSON.parse(requests[0].options.body);
  assert.equal(payload.management_category_id, 7);
  assert.deepEqual(payload.category_ids, ['source-9']);
  assert.equal(payload.credential, 'zying-test');
});

test('1688 page collection uses saved category and freezes it for offline retry', async () => {
  const {send, stored, requests, context, run} = setup();
  stored.local.zeshunCollectionCategoryOptions = {'1688': 7};
  context.fetch = async () => { throw new Error('offline'); };
  const source = {source_platform: '1688', source_item_id: '123456', title: '产品', source_url: 'https://detail.1688.com/offer/123456.html'};
  const queued = await send({type: 'SUBMIT_PRODUCT', product: source});
  assert.equal(queued.queued, true);
  assert.equal(stored.local.pendingProducts[0].product.management_category_id, 7);
  stored.local.zeshunCollectionCategoryOptions['1688'] = 8;
  context.fetch = async (url, options) => {
    requests.push({url, options});
    return {ok: true, status: 200, json: async () => ({status: 'success', data: {product_id: 1}})};
  };
  await run('flushQueueOnce()');
  assert.equal(JSON.parse(requests[0].options.body).product.management_category_id, 7);
  await send({type: 'SUBMIT_PRODUCT', product: {...source, management_category_id: null}});
  assert.equal(JSON.parse(requests.at(-1).options.body).product.management_category_id, null);
});
