const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const dir = path.join(__dirname, '../browser_extension/zeshun_collector');
const source = fs.readFileSync(path.join(dir, 'background.js'), 'utf8');
const code = source.slice(source.indexOf('// Share data-card and collection reads'), source.indexOf('async function getZeshunCategories'));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function setup(delay = 0) {
  const requests = [];
  let fail = false, token = 'account-a';
  const cachedSkus = new Set();
  const context = vm.createContext({URL, setTimeout, clearTimeout, setInterval, clearInterval,
    settings: async () => ({consoleUrl: 'https://console.test'}),
    authSession: async () => ({token, user: {username: token}}),
    normalizeConsoleUrl: value => value,
    mercadoDataRequest: async (_, options) => {
      const items = JSON.parse(options.body).items;
      requests.push(items);
      const cold = items.some(item => !cachedSkus.has(item.sku));
      await sleep(cold ? delay : 0);
      if (fail) throw Error('temporarily unavailable');
      items.forEach(item => cachedSkus.add(item.sku));
      return {items: items.map(item => ({sku: item.sku, requested_price: item.price,
        product: {sku: item.sku, price: item.price}, fetched_at: Date.now()})), warnings: []};
    }
  });
  vm.runInContext(code, context);
  return {context, requests, query: items => context.queryMercadoProducts(items),
    setFail: value => {fail = value;}, setToken: value => {token = value;}};
}

test('concurrent tabs share one read and distinct products form one upstream batch', async () => {
  const f = setup(30);
  const [a, b, c] = await Promise.all([
    f.query([{sku: 'MLM123456', price: 100}]),
    f.query([{sku: 'MLM123456', price: 100}]),
    f.query([{sku: 'MLM234567', price: 200}])
  ]);
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].length, 2);
  assert.deepEqual(a, b);
  assert.equal(c.items[0].sku, 'MLM234567');
  await f.query([{sku: 'MLM123456', price: 100}]);
  assert.equal(f.requests.length, 1);
});

test('cached reads expire after sixty seconds', async () => {
  const f = setup();
  await f.query([{sku: 'MLM123456'}]);
  f.context.Date = class extends Date { static now() { return Date.now() + 61000; } };
  await f.query([{sku: 'MLM123456'}]);
  assert.equal(f.requests.length, 2);
});

test('one missing product does not reject other cards in the same query', async () => {
  const f = setup();
  f.context.mercadoDataRequest = async (_, options) => ({items: JSON.parse(options.body).items.map(item => ({
    sku: item.sku, requested_price: item.price,
    product: item.sku === 'MLM123456' ? null : {sku: item.sku}
  }))});
  const result = await f.query([{sku: 'MLM123456'}, {sku: 'MLM234567'}]);
  assert.equal(result.items[0].product, null);
  assert.equal(result.items[1].product.sku, 'MLM234567');
});

test('price and login changes cannot reuse a stale calculated result', async () => {
  const f = setup();
  await f.query([{sku: 'MLM123456', price: 100}]);
  const b = await f.query([{sku: 'MLM123456', price: 200}]);
  assert.equal(b.items[0].requested_price, 200);
  f.setToken('account-b');
  await f.query([{sku: 'MLM123456', price: 200}]);
  assert.equal(f.requests.length, 3);
});

test('failed reads are evicted and retry succeeds', async () => {
  const f = setup();
  f.setFail(true);
  await assert.rejects(f.query([{sku: 'MLM123456'}]), /unavailable/);
  f.setFail(false);
  await f.query([{sku: 'MLM123456'}]);
  assert.equal(f.requests.length, 2);
});

test('combined requests obey the 50 item limit', async () => {
  const f = setup();
  await Promise.all(Array.from({length: 60}, (_, i) => f.query([{sku: `MLM${123456 + i}`}])));
  assert.deepEqual(f.requests.map(items => items.length), [50, 10]);
});

test('prefetch excludes catalog identifiers and preserves explicit seller selections', async () => {
  const f = setup();
  f.context.prefetchMercadoProduct('https://www.mercadolibre.com.mx/test/p/MLM123456');
  f.context.prefetchMercadoProduct('https://www.mercadolibre.com.mx/test/up/MLMU123456');
  await sleep(30);
  assert.equal(f.requests.length, 0);
  f.context.prefetchMercadoProduct('https://www.mercadolibre.com.mx/test/p/MLM123456?wid=MLM234567');
  await sleep(40);
  assert.equal(f.requests[0][0].sku, 'MLM234567');
});

test('invalid input does not schedule partially valid work', async () => {
  const f = setup();
  await assert.rejects(f.query([{sku: 'MLM123456'}, {sku: 'wrong'}]));
  await sleep(30);
  assert.equal(f.requests.length, 0);
});

test('navigation and data read overlap: 4s page + 4s data + 0.5s upload stays below 10s', async () => {
  const f = setup(4000);
  vm.runInContext(fs.readFileSync(path.join(dir, 'product-batch.js'), 'utf8'), f.context);
  let openedAt;
  f.context.chrome = {tabs: {
    create: async () => { openedAt = Date.now(); return {id: 1}; },
    get: async () => ({url: 'https://articulo.mercadolibre.com.mx/MLM-123456-test-_JM'}),
    remove: async () => {}
  }};
  f.context.sendTabMessage = async () => {
    if (Date.now() - openedAt < 4000) return {ok: false, error: 'loading'};
    const result = await f.query([{sku: 'MLM123456', price: 150}]);
    return {ok: true, product: result.items[0].product};
  };
  const start = Date.now();
  let uploaded = false;
  await f.context.readProductBatchPage({tabs: new Set(), stop: false},
    'https://articulo.mercadolibre.com.mx/MLM-123456-test-_JM', 'EXTRACT_BATCH_PRODUCT',
    async () => { await sleep(500); uploaded = true; });
  const elapsed = Date.now() - start;
  assert.equal(uploaded, true);
  assert.equal(f.requests.length, 2); // Price-dependent estimate uses the warm server cache.
  assert.ok(elapsed < 6000, `parallel collection took ${elapsed}ms`);
  console.log(`Simulated end-to-end collection: ${elapsed}ms (4s navigation, 4s data, 0.5s upload)`);
});

test('popup displays measured per-item latency and over-target count', () => {
  const source = fs.readFileSync(path.join(dir, 'popup.js'), 'utf8');
  const elements = new Map();
  const context = vm.createContext({
    document: {getElementById: id => {
      if (!elements.has(id)) elements.set(id, {style: {}});
      return elements.get(id);
    }},
    productBatchFields: {'concurrency': {value: '3'}},
    syncProductBatchControls() {}
  });
  vm.runInContext('let productBatchState;\n' + source.slice(source.indexOf('function renderProductBatch('),
    source.indexOf('async function checkProductDataStatus(')), context);
  context.renderProductBatch({completed_count: 2, last_success_ms: 11000,
    total_success_ms: 15000, over_target_count: 1});
  assert.match(elements.get('product-batch-summary').textContent, /最近 11.0 秒 · 平均 7.5 秒 · ≥10 秒 1 件/);
});
