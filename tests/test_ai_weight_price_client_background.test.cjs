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
