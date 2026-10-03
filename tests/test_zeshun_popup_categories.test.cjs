const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const popupSource = fs.readFileSync('browser_extension/zeshun_collector/popup.js', 'utf8');
const popupHtml = fs.readFileSync('browser_extension/zeshun_collector/popup.html', 'utf8');

class Option {
  constructor(text, value) { this.textContent = text; this.value = String(value); this.disabled = false; }
}

class Element {
  constructor(id) {
    this.id = id;
    this.options = [new Option('', '')];
    this._value = '';
    this.textContent = '';
    this.disabled = false;
    this.hidden = false;
    this.style = {};
    this.listeners = {};
    this.classList = {toggle() {}};
  }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
  get selectedOptions() { return this.options.filter(option => option.value === this.value); }
  replaceChildren(...options) { this.options = options; this.value = options[0]?.value || ''; }
  add(option) { this.options.push(option); }
  addEventListener(event, listener) { this.listeners[event] = listener; }
}

async function createPopup({saved = {}, authenticated = true, platform = 'mercado', categories, categoryResponse: initialCategoryResponse} = {}) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, new Element(id));
    return nodes.get(id);
  };
  for (const [id, value] of Object.entries({
    'product-country': 'MLM', 'product-max-items': '20', 'product-concurrency-mode': 'normal',
    'zying-max-items': '100', 'yandex-count': '200', 'weight-price-limit': '10'
  })) node(id).value = value;
  const storage = {zeshunCollectionCategoryOptions: {...saved}};
  const messages = [];
  let categoryResponse = initialCategoryResponse || {ok: true, categories: categories || [{id: 3, name: '家居'}, {id: 4, name: '玩具'}]};
  const responseOverrides = new Map();
  let extractProductHandler = null;
  const currentTab = {id: 42, url: platform === '1688'
    ? 'https://detail.1688.com/offer/123.html'
    : 'https://www.mercadolibre.com.mx/p/MLM123', active: true, title: '商品'};
  const chrome = {
    storage: {local: {
      async get(key) { return {[key]: storage[key]}; },
      async set(values) { Object.assign(storage, values); }
    }},
    runtime: {
      lastError: null,
      openOptionsPage() {},
      sendMessage(message, callback) {
        messages.push(message);
        if (message.type === 'GET_ZESHUN_CATEGORIES' && typeof categoryResponse === 'function') return categoryResponse(callback);
        const responses = {
          GET_STATE: {authenticated, user: {username: 'tester'}, queueLength: 0},
          GET_ZESHUN_CATEGORIES: categoryResponse,
          GET_MERCADO_DATA_STATUS: {ok: true, ready: true},
          GET_PRODUCT_BATCH_STATUS: {ok: true, state: {}},
          GET_ZYING_STATUS: {ok: true, running: false, summary: {}},
          START_ZYING_COLLECTION: {ok: true}, START_PRODUCT_BATCH: {ok: true, state: {running: true}},
          SUBMIT_PRODUCT: {ok: true, taskId: 'task-1', sourcePlatform: platform, productId: 'product-1'}
        };
        callback(responseOverrides.get(message.type) || responses[message.type] || {ok: true});
      }
    },
    tabs: {
      async query() { return [currentTab]; },
      sendMessage(_tabId, message, callback) {
        if (message.type === 'EXTRACT_PRODUCT' && extractProductHandler) return extractProductHandler(callback);
        callback(message.type === 'PING_PAGE'
          ? {ok: true, detail: true, platform, zying: {logged_in: true}}
          : {ok: true, product: {title: '测试商品', source_platform: platform}});
      }
    }
  };
  const context = vm.createContext({
    chrome, document: {getElementById: node, createElement: () => new Element('span')},
    Option, URL, Date, setTimeout() { return 1; }, clearTimeout() {}
  });
  vm.runInContext(popupSource, context);
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  return {node, storage, messages, context,
    setCategoryResponse(response) { categoryResponse = response; },
    setResponse(type, response) { responseOverrides.set(type, response); },
    setExtractProductHandler(handler) { extractProductHandler = handler; },
    evaluate(code) { return vm.runInContext(code, context); },
    async click(id) { await node(id).listeners.click({currentTarget: node(id)}); }
  };
}

test('the two target selectors restore independent choices and keep source filtering separate', async () => {
  const popup = await createPopup({saved: {product: 3, zying: 4}});
  assert.equal(popup.node('product-management-category').value, '3');
  assert.equal(popup.node('zying-management-category').value, '4');
  assert.equal(popup.node('product-management-category').options[0].textContent, '不指定分类');
  assert.match(popupHtml, /智赢来源分类\s*<select id="zying-category"/);
  popup.node('product-management-category').value = '4';
  await popup.node('product-management-category').listeners.change();
  await new Promise(setImmediate);
  assert.deepEqual(JSON.parse(JSON.stringify(popup.storage.zeshunCollectionCategoryOptions)), {product: 4, zying: 4, '1688': null});
  popup.node('zying-management-category').value = '';
  await popup.node('zying-management-category').listeners.change();
  await new Promise(setImmediate);
  assert.deepEqual(JSON.parse(JSON.stringify(popup.storage.zeshunCollectionCategoryOptions)), {product: 4, zying: null, '1688': null});
});

test('Mercado batch and detail collection send the selected target category', async () => {
  const popup = await createPopup({saved: {product: 3, zying: 4}});
  await popup.click('collect');
  const single = popup.messages.find(message => message.type === 'SUBMIT_PRODUCT');
  assert.equal(single.product.management_category_id, 3);
  await popup.click('product-batch-start');
  const batch = popup.messages.find(message => message.type === 'START_PRODUCT_BATCH');
  assert.equal(batch.params.management_category_id, 3);
  assert.equal(batch.params.max_items, 20);
});

test('single collection uses the category chosen when collection starts', async () => {
  const popup = await createPopup({saved: {product: 3}});
  let extracted;
  popup.setExtractProductHandler(callback => { extracted = callback; });
  const collecting = popup.click('collect');
  await new Promise(setImmediate);
  popup.node('product-management-category').value = '4';
  extracted({ok: true, product: {title: '测试商品'}});
  await collecting;
  assert.equal(popup.messages.find(message => message.type === 'SUBMIT_PRODUCT').product.management_category_id, 3);
});

test('Zying collection sends a target category independently of its source category', async () => {
  const popup = await createPopup({saved: {product: 3, zying: 4}});
  popup.evaluate("zyingContext = {tabId: 51, categories: [], developers: []};");
  popup.node('zying-category').add(new Option('智赢鞋类', 'source-88'));
  popup.node('zying-category').value = 'source-88';
  await popup.click('zying-start');
  const started = popup.messages.find(message => message.type === 'START_ZYING_COLLECTION');
  assert.equal(started.params.management_category_id, 4);
  assert.equal(started.params.category, 'source-88');
  assert.equal(started.params.category_name, '智赢鞋类');
});

test('an unspecified category sends null and 1688 collection keeps its own destination', async () => {
  const mercado = await createPopup();
  await mercado.click('collect');
  assert.equal(mercado.messages.find(message => message.type === 'SUBMIT_PRODUCT').product.management_category_id, null);
  assert.equal(mercado.evaluate('productBatchSelection().params.management_category_id'), null);
  const supplier = await createPopup({saved: {product: 3}, platform: '1688'});
  await supplier.click('collect');
  assert.equal(supplier.messages.find(message => message.type === 'SUBMIT_PRODUCT').product.management_category_id, null);
});

test('logged-out users cannot load or choose Zeshun categories', async () => {
  const popup = await createPopup({authenticated: false, saved: {product: 3}});
  for (const source of ['product', 'zying']) {
    assert.equal(popup.node(`${source}-management-category`).disabled, true);
    assert.equal(popup.node(`${source}-refresh-management-categories`).disabled, true);
    assert.match(popup.node(`${source}-management-category-status`).textContent, /登录泽顺/);
  }
  assert.equal(popup.messages.some(message => message.type === 'GET_ZESHUN_CATEGORIES'), false);
  assert.equal(popup.storage.zeshunCollectionCategoryOptions.product, 3);
});

test('failed refresh preserves the choice and deleted categories require a new selection', async () => {
  const popup = await createPopup({saved: {product: 3, zying: 4}});
  popup.setCategoryResponse({ok: false, error: '网络连接失败'});
  await popup.click('product-refresh-management-categories');
  assert.equal(popup.node('product-management-category').value, '3');
  assert.equal(popup.evaluate('managementCategorySelection("product")'), 3);
  assert.match(popup.node('product-management-category-status').textContent, /网络连接失败/);
  assert.equal(popup.node('product-refresh-management-categories').disabled, false);
  popup.setCategoryResponse({ok: true, categories: [{id: 4, name: '玩具'}]});
  await popup.click('zying-refresh-management-categories');
  assert.equal(popup.node('product-management-category').value, '3');
  assert.throws(() => popup.evaluate('managementCategorySelection("product")'), /分类不可用/);
  assert.match(popup.node('product-management-category-status').textContent, /不可用/);
  assert.equal(popup.node('zying-management-category').value, '4');
  popup.node('product-management-category').value = '';
  assert.equal(popup.evaluate('managementCategorySelection("product")'), null);
});

test('category refresh keeps both destinations and collection starts locked until the list returns', async () => {
  const popup = await createPopup({saved: {product: 3, zying: 4}});
  popup.evaluate("zyingContext = {tabId: 51, categories: [], developers: []};");
  let finishRefresh;
  popup.setCategoryResponse(callback => { finishRefresh = callback; });
  const refreshing = popup.click('product-refresh-management-categories');
  await new Promise(setImmediate);
  await popup.evaluate('loadZyingStatus()');
  for (const source of ['product', 'zying']) {
    assert.equal(popup.node(`${source}-management-category`).disabled, true);
    assert.equal(popup.node(`${source}-refresh-management-categories`).disabled, true);
  }
  assert.equal(popup.node('collect').disabled, true);
  assert.equal(popup.node('product-batch-start').disabled, true);
  assert.equal(popup.node('zying-start').disabled, true);
  finishRefresh({ok: true, categories: [{id: 3, name: '家居'}, {id: 4, name: '玩具'}]});
  await refreshing;
  assert.equal(popup.node('product-management-category').value, '3');
  assert.equal(popup.node('zying-management-category').value, '4');
  assert.equal(popup.node('product-batch-start').disabled, false);
  assert.equal(popup.node('zying-start').disabled, false);
});

test('malformed IDs cannot silently lose a saved classification', async () => {
  const popup = await createPopup({saved: {product: 8}, categories: [{id: -1, name: '非法'}, {id: 'x', name: '非法'}, {id: 4, name: '玩具'}]});
  assert.deepEqual(popup.node('product-management-category').options.map(option => option.value), ['', '4', '8']);
  await popup.click('collect');
  assert.equal(popup.messages.some(message => message.type === 'SUBMIT_PRODUCT'), false);
  assert.match(popup.node('result').textContent, /分类不可用/);
});

test('a failed initial request retains the saved category until the user chooses a valid destination', async () => {
  const popup = await createPopup({saved: {product: 8}, categoryResponse: {ok: false, error: '服务不可用'}});
  assert.equal(popup.node('product-management-category').value, '8');
  assert.match(popup.node('product-management-category-status').textContent, /服务不可用/);
  await popup.click('product-batch-start');
  assert.equal(popup.messages.some(message => message.type === 'START_PRODUCT_BATCH'), false);
  popup.node('product-management-category').value = '';
  await popup.click('product-batch-start');
  assert.equal(popup.messages.find(message => message.type === 'START_PRODUCT_BATCH').params.management_category_id, null);
});

test('Zying can retry after an unavailable category or a rejected start', async () => {
  const popup = await createPopup({saved: {zying: 8}});
  popup.evaluate("zyingContext = {tabId: 51, categories: [], developers: []};");
  await popup.click('zying-start');
  assert.equal(popup.messages.some(message => message.type === 'START_ZYING_COLLECTION'), false);
  assert.equal(popup.node('zying-start').disabled, false);
  popup.node('zying-management-category').value = '';
  popup.setResponse('START_ZYING_COLLECTION', {ok: false, error: '启动失败'});
  await popup.click('zying-start');
  assert.equal(popup.messages.find(message => message.type === 'START_ZYING_COLLECTION').params.management_category_id, null);
  assert.equal(popup.node('zying-start').disabled, false);
  popup.setResponse('START_ZYING_COLLECTION', {ok: true});
  await popup.click('zying-start');
  assert.equal(popup.messages.filter(message => message.type === 'START_ZYING_COLLECTION').length, 2);
});

test('1688 restores its own category, submits it and remembers clearing', async () => {
  const popup = await createPopup({saved: {product: 3, '1688': 4}, platform: '1688'});
  assert.equal(popup.node('1688-management-category').value, '4');
  await popup.click('collect');
  assert.equal(popup.messages.find(message => message.type === 'SUBMIT_PRODUCT').product.management_category_id, 4);
  popup.node('1688-management-category').value = '';
  await popup.node('1688-management-category').listeners.change();
  await new Promise(setImmediate);
  assert.equal(popup.storage.zeshunCollectionCategoryOptions['1688'], null);
  assert.equal(popup.storage.zeshunCollectionCategoryOptions.product, 3);
});
