const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const background = fs.readFileSync(
  'browser_extension/zeshun_collector/background.js', 'utf8'
);

function createBackground() {
  let messageListener;
  const event = () => ({addListener() {}, removeListener() {}});
  const storageArea = () => ({
    get(_keys, callback) { callback({}); },
    set(_values, callback) { callback?.(); },
    remove(_keys, callback) { callback?.(); }
  });
  const chrome = {
    storage: {local: storageArea(), session: storageArea(), sync: storageArea()},
    runtime: {
      onInstalled: event(),
      onStartup: event(),
      onMessage: {addListener(listener) { messageListener = listener; }},
      lastError: null,
      getURL: path => `extension:///${path}`
    },
    alarms: {onAlarm: event(), create() {}},
    contextMenus: {onClicked: event(), removeAll(callback) { callback?.(); }, create() {}},
    tabs: {},
    windows: {onRemoved: event()}
  };
  const context = vm.createContext({chrome, importScripts() {}, URL, setTimeout, clearTimeout});
  vm.runInContext(background, context);
  assert.equal(typeof messageListener, 'function');
  return {context, messageListener};
}

function send(messageListener, type) {
  return new Promise(resolve => {
    messageListener({type, params: {max_items: 2}}, {}, resolve);
  });
}

test('manual start and continue launch the returned browser action', async () => {
  const {context, messageListener} = createBackground();
  vm.runInContext(`
    aiWeightPriceStatus = async () => ({client_config: {}});
    aiWeightPriceReadNextProduct = async () => ({
      product: {erp_goods_id: '1001', title: '商品', main_image_url: 'https://img.test/1.jpg'},
      cursor: {page: 1, index: 1}
    });
    aiWeightPriceAction = async action => ({
      action: action === 'start' ? 'search' : 'detail',
      task: {erp_goods_id: '1001'}
    });
    window = [];
    runAIWeightPriceClient = async step => window.push(step.action);
  `, context);

  const started = await send(messageListener, 'START_AI_WEIGHT_PRICE');
  await new Promise(setImmediate);
  const continued = await send(messageListener, 'CONTINUE_AI_WEIGHT_PRICE');
  await new Promise(setImmediate);

  assert.equal(started.ok, true);
  assert.equal(continued.ok, true);
  assert.deepEqual(Array.from(vm.runInContext('window', context)), ['search', 'detail']);
});

test('double start is rejected before reading or navigating another product', async () => {
  const {context, messageListener} = createBackground();
  vm.runInContext(`
    window = {reads: 0};
    aiWeightPriceStatus = () => new Promise(resolve => window.release = resolve);
    aiWeightPriceReadNextProduct = async () => { window.reads++; return {product: {erp_goods_id: '1'}}; };
    aiWeightPriceAction = async () => ({action: 'done'});
  `, context);
  const first = send(messageListener, 'START_AI_WEIGHT_PRICE');
  const second = await send(messageListener, 'START_AI_WEIGHT_PRICE');
  assert.equal(second.ok, false);
  assert.equal(vm.runInContext('window.reads', context), 0);
  vm.runInContext('window.release({})', context);
  assert.equal((await first).ok, true);
  assert.equal(vm.runInContext('window.reads', context), 1);
});

test('pause mail is due after five minutes and webhook success does not stop retries', async () => {
  const {context} = createBackground();
  vm.runInContext(`
    window = {stored: {}, calls: 0, now: 399000};
    Date.now = () => window.now;
    storageGet = async () => window.stored;
    storageSet = async (_, value) => Object.assign(window.stored, value);
    apiRequest = async () => { window.calls++; return {sent: true, channels: {webhook: {sent: true}, email: {sent: window.calls > 1}}}; };
    window.status = {circuit: {at: 100, reason: '人机验证'}, run: {run_id: 'r'}};
  `, context);
  await vm.runInContext('handleAiWeightPricePauseNotification(window.status)', context);
  assert.equal(vm.runInContext('window.calls', context), 0);
  vm.runInContext('window.now = 400000', context);
  await vm.runInContext('handleAiWeightPricePauseNotification(window.status)', context);
  assert.equal(vm.runInContext('window.stored[AI_WEIGHT_PRICE_PAUSE_KEY].sent', context), false);
  vm.runInContext('window.now = 460000', context);
  await vm.runInContext('handleAiWeightPricePauseNotification(window.status)', context);
  assert.equal(vm.runInContext('window.stored[AI_WEIGHT_PRICE_PAUSE_KEY].sent', context), true);
  vm.runInContext('window.now = 900000', context);
  await vm.runInContext('handleAiWeightPricePauseNotification(window.status)', context);
  assert.equal(vm.runInContext('window.calls', context), 2);
});

test('ordinary item failure consumes the next server step without stopping the browser loop', async () => {
  const {context} = createBackground();
  vm.runInContext(`
    window = [];
    aiWeightPriceCancelled = false;
    aiWeightPriceTab = async () => 1;
    aiWeightPriceWaitTab = async () => {};
    aiWeightPriceImageDataUrl = async () => '';
    aiWeightPriceSearchSupplier = async () => { throw new Error('商品定位失败'); };
    aiWeightPriceAction = async (action, body) => {
      window.push({action, task_id: body.task_id, token: body.step_token});
      return body.task_id === '1'
        ? {action: 'search', step_token: 'second', task: {erp_goods_id: '2'}}
        : {action: 'done'};
    };
  `, context);
  const result = await vm.runInContext(`runAIWeightPriceClient({action:'search',step_token:'first',task:{erp_goods_id:'1'},data:{run:{run_id:'r'}}})`, context);
  assert.equal(result.action, 'done');
  assert.deepEqual(JSON.parse(JSON.stringify(context.window)), [
    {action: 'fail', task_id: '1', token: 'first'},
    {action: 'fail', task_id: '2', token: 'second'}
  ]);
});
