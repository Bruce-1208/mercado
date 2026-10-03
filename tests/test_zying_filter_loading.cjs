const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const backgroundSource = fs.readFileSync('browser_extension/zeshun_collector/background.js', 'utf8');
const popupSource = fs.readFileSync('browser_extension/zeshun_collector/popup.js', 'utf8');
const pageCategories = [{category_id: 'live-category', category_name: '当前分类'}];
const pageDevelopers = [{id: 'live-developer', name: '当前业务员'}];
const copy = value => JSON.parse(JSON.stringify(value));
const flushPromises = async () => {
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

function createClock() {
  let now = 0;
  let nextId = 0;
  const timers = new Map();
  return {
    setTimeout(callback, delay) {
      const id = ++nextId;
      timers.set(id, {callback, at: now + delay});
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    async advance(milliseconds) {
      now += milliseconds;
      for (const [id, timer] of [...timers]) {
        if (timer.at <= now) {
          timers.delete(id);
          timer.callback();
        }
      }
      await flushPromises();
    },
    pending() { return timers.size; }
  };
}

function createBackground(apiRequest, executeScript = async () => []) {
  const clock = createClock();
  const event = () => ({addListener() {}, removeListener() {}});
  const storageArea = () => ({
    get(_keys, callback) { callback({}); },
    set(_values, callback) { callback?.(); },
    remove(_keys, callback) { callback?.(); }
  });
  const chrome = {
    storage: {local: storageArea(), session: storageArea(), sync: storageArea()},
    runtime: {
      onInstalled: event(), onStartup: event(), onMessage: event(), lastError: null,
      getURL: path => `extension:///${path}`
    },
    alarms: {onAlarm: event(), create() {}},
    contextMenus: {onClicked: event(), removeAll(callback) { callback?.(); }, create() {}},
    tabs: {get: async id => ({id, url: 'https://meli.zying.net/#/product'})},
    windows: {onRemoved: event()},
    cookies: {get: async () => null},
    scripting: {executeScript}
  };
  const context = vm.createContext({
    chrome, importScripts() {}, URL, AbortController,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    __apiRequest: apiRequest,
    zeshunReadZyingPageContext: function zeshunReadZyingPageContext() {},
    zeshunReadZyingProductDevelopers: function zeshunReadZyingProductDevelopers() {}
  });
  vm.runInContext(backgroundSource, context);
  vm.runInContext('apiRequest = __apiRequest; aiWeightPriceWaitTab = async () => {};', context);
  return {context, clock};
}

test('collection start carries the resolved personnel directory to the task worker', async () => {
  const requests = [];
  const {context} = createBackground(async (path, options) => {
    requests.push({path, body: JSON.parse(options.body)});
    return {running: true};
  });
  vm.runInContext(`
    normalizeManagementCategoryId = value => value || '';
    assertManagementCategorySession = () => {};
    authSession = async () => ({user:{id:7}});
  `, context);
  await context.startZyingCollection({
    credential: 'test-page-token', categories: pageCategories, developers: pageDevelopers
  }, {category: 'live-category', product_developer_id: 'live-developer', max_items: 2});
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/api/browser-extension/zying/start');
  assert.deepEqual(requests[0].body.developers, pageDevelopers);
  assert.equal(requests[0].body.product_developer_id, 'live-developer');
});

test('complete page filters return immediately even when optional server persistence hangs', async () => {
  const requests = [];
  const {context, clock} = createBackground((path, options) => {
    requests.push({path, options});
    return new Promise(() => {});
  });
  let result;
  const pending = context.loadZyingOptions({
    credential: 'test-page-token', categories: pageCategories, developers: pageDevelopers
  }).then(value => { result = value; });
  await flushPromises();
  assert.deepEqual(copy(result), {categories: pageCategories, developers: pageDevelopers});
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/api/browser-extension/zying/options');
  assert.equal(requests[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    credential: 'test-page-token', categories: pageCategories, developers: pageDevelopers,
    refresh: true, page_only: true
  });
  await pending;
  await clock.advance(2500);
  assert.equal(requests[0].options.signal.aborted, true);
  assert.equal(clock.pending(), 0);
});

test('missing developers wait at most 2500 ms and preserve page categories on a hung server', async () => {
  let signal;
  const {context, clock} = createBackground((_path, options) => {
    signal = options.signal;
    return new Promise(() => {});
  });
  let result;
  const pending = context.loadZyingOptions({categories: pageCategories, developers: []})
    .then(value => { result = value; });
  await clock.advance(2499);
  assert.equal(result, undefined);
  assert.equal(signal.aborted, false);
  await clock.advance(1);
  assert.deepEqual(copy(result.categories), pageCategories);
  assert.deepEqual(copy(result.developers), []);
  assert.match(result.developer_warning, /后台暂不可用/);
  assert.equal(signal.aborted, true);
  assert.equal(clock.pending(), 0);
  await pending;
});

test('service failure preserves incomplete page filters but cannot fabricate missing categories', async () => {
  const error = Object.assign(new Error('后台服务暂时不可用'), {status: 503});
  const {context, clock} = createBackground(async () => { throw error; });
  const result = await context.loadZyingOptions({categories: pageCategories, developers: []});
  assert.deepEqual(copy(result.categories), pageCategories);
  assert.deepEqual(copy(result.developers), []);
  assert.match(result.developer_warning, /产品开发人员未补全/);
  assert.equal(clock.pending(), 0);
  await assert.rejects(context.loadZyingOptions({categories: [], developers: pageDevelopers}), error);
  assert.equal(clock.pending(), 0);
});

for (const errorProperties of [{status: 401}, {status: 401, authRequired: true}, {status: 403}]) {
  test(`HTTP ${errorProperties.status}${errorProperties.authRequired ? ' with authRequired' : ''} is not disguised as usable fallback filters`, async () => {
    const error = Object.assign(new Error('账号或权限异常'), errorProperties);
    const {context, clock} = createBackground(async () => { throw error; });
    await assert.rejects(context.loadZyingOptions({categories: pageCategories, developers: []}), error);
    assert.equal(clock.pending(), 0);
  });
}

test('live page categories take precedence over historical server categories', async () => {
  const serverDevelopers = [{id: 'server-developer', name: '补全业务员'}];
  const {context, clock} = createBackground(async () => ({
    categories: [{category_id: 'stale', category_name: '历史分类'}],
    developers: serverDevelopers, developer_warning: '补全目录'
  }));
  const result = await context.loadZyingOptions({categories: pageCategories, developers: []});
  assert.deepEqual(copy(result.categories), pageCategories);
  assert.deepEqual(copy(result.developers), serverDevelopers);
  assert.equal(result.developer_warning, '补全目录');
  assert.equal(clock.pending(), 0);
});

test('live page developers take precedence when the server supplements categories', async () => {
  const serverCategories = [{category_id: 'server-category', category_name: '补全分类'}];
  const {context} = createBackground(async () => ({
    categories: serverCategories, developers: [{id: 'stale', name: '历史业务员'}]
  }));
  const result = await context.loadZyingOptions({categories: [], developers: pageDevelopers});
  assert.deepEqual(copy(result.categories), serverCategories);
  assert.deepEqual(copy(result.developers), pageDevelopers);
});

test('reading categories and developers scans page categories only once', async () => {
  const scriptNames = [];
  const {context, clock} = createBackground(async () => ({}), async options => {
    scriptNames.push(options.func.name);
    assert.equal(options.target.tabId, 37);
    assert.equal(options.world, 'MAIN');
    return [{result: options.func.name === 'zeshunReadZyingPageContext'
      ? {credential: 'test-page-token', categories: pageCategories, developers: []}
      : pageDevelopers}];
  });
  const result = await context.aiWeightPriceReadZyingTab({
    tabId: 37, requireCategories: true, includeDevelopers: true
  });
  assert.equal(result.tabId, 37);
  assert.deepEqual(copy(result.context.categories), pageCategories);
  assert.deepEqual(copy(result.context.developers), pageDevelopers);
  assert.deepEqual(scriptNames, ['zeshunReadZyingPageContext', 'zeshunReadZyingProductDevelopers']);
  assert.equal(clock.pending(), 0);
});

test('developer page script timeout retains the categories without rescanning them', async () => {
  const scriptNames = [];
  const {context, clock} = createBackground(async () => ({}), async options => {
    scriptNames.push(options.func.name);
    if (options.func.name === 'zeshunReadZyingProductDevelopers') return new Promise(() => {});
    return [{result: {credential: 'test-page-token', categories: pageCategories, developers: []}}];
  });
  let result;
  const pending = context.aiWeightPriceReadZyingTab({
    tabId: 37, requireCategories: true, includeDevelopers: true
  }).then(value => { result = value; });
  await flushPromises();
  assert.equal(result, undefined);
  await clock.advance(2500);
  assert.deepEqual(copy(result.context.categories), pageCategories);
  assert.deepEqual(copy(result.context.developers), []);
  assert.deepEqual(scriptNames, ['zeshunReadZyingPageContext', 'zeshunReadZyingProductDevelopers']);
  assert.equal(clock.pending(), 0);
  await pending;
});

function popupFunction(from, to) {
  const start = popupSource.indexOf(from);
  const end = popupSource.indexOf(to, start);
  assert(start >= 0 && end > start, `Popup function bounds exist: ${from}`);
  return popupSource.slice(start, end);
}

function createSelect() {
  let html = '';
  let value = '';
  let options = [];
  return {
    disabled: false,
    get innerHTML() { return html; },
    set innerHTML(nextHtml) {
      html = nextHtml;
      options = [...html.matchAll(/<option\b([^>]*)>(.*?)<\/option>/g)].map(match => ({
        value: match[1].match(/\bvalue="([^"]*)"/)?.[1] || '',
        disabled: /\bdisabled\b/.test(match[1]),
        textContent: match[2]
      }));
      // Replacing a select's options resets its selected value in the browser.
      value = options[0]?.value || '';
    },
    get options() { return options; },
    get value() { return value; },
    set value(nextValue) {
      value = options.some(option => option.value === String(nextValue)) ? String(nextValue) : '';
    }
  };
}

function createPopup(runtimeMessage, {realRender = false} = {}) {
  const element = () => ({disabled: false, textContent: '', scrollHeight: 0, scrollTop: 0});
  const rendered = [];
  const state = {
    authenticated: true, zyingContext: null, zyingRefreshing: false,
    zyingRunning: false, zyingInfringementRunning: false, activeTab: null,
    runtimeMessage, findZyingPageTab: async () => ({id: 37}),
    renderZyingOptions: data => rendered.push(copy(data)),
    zyingCategoryDisplayName: row => row.category_name,
    refreshState: async () => {},
    scheduleZyingPoll() {}, scheduleZyingInfringementPoll() {}
  };
  for (const name of [
    'zyingRefreshButton', 'zyingPageStatus', 'zyingStartButton', 'zyingStopButton',
    'zyingStartProductId', 'zyingMaxItems', 'zyingCategory', 'zyingDeveloper',
    'zyingTaskStatus', 'zyingSummary', 'zyingLog', 'zyingInfringementRefreshButton',
    'zyingInfringementPageStatus', 'zyingInfringementStartButton', 'zyingInfringementStopButton',
    'zyingInfringementStartProductId', 'zyingInfringementMaxItems', 'zyingInfringementCategory',
    'zyingInfringementDeveloper', 'zyingInfringementTaskStatus', 'zyingInfringementSummary',
    'zyingInfringementLog'
  ]) state[name] = element();
  for (const name of ['zyingCategory', 'zyingDeveloper', 'zyingInfringementCategory', 'zyingInfringementDeveloper']) {
    state[name] = createSelect();
  }
  state.document = {
    createElement() {
      return {
        textContent: '',
        get innerHTML() {
          return this.textContent.replaceAll('&', '&amp;').replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
        }
      };
    }
  };
  const context = vm.createContext(state);
  if (realRender) {
    vm.runInContext(popupFunction('function zyingCategoryDisplayName(', 'const purchasePlatformLabels ='), context);
    const render = context.renderZyingOptions;
    context.renderZyingOptions = (data, options) => {
      rendered.push(copy(data));
      return render(data, options);
    };
  }
  vm.runInContext([
    popupFunction('async function refreshZyingOptions()', 'function scheduleZyingPoll()'),
    popupFunction('async function loadZyingStatus()', 'function scheduleZyingInfringementPoll()'),
    popupFunction('async function loadZyingInfringementStatus()', 'function renderWeightPriceCategories(')
  ].join('\n'), context);
  return {context, rendered};
}

test('popup immediately renders page filters, preserves them on sync failure and blocks duplicate reads during polling', async () => {
  const page = deferred();
  const server = deferred();
  const messages = [];
  const {context, rendered} = createPopup(message => {
    messages.push(message);
    if (message.type === 'READ_ZYING_CONTEXT') return page.promise;
    if (message.type === 'GET_ZYING_OPTIONS') return server.promise;
    return Promise.resolve({ok: true, running: false});
  });
  const pending = context.refreshZyingOptions();
  for (const name of [
    'zyingRefreshButton', 'zyingStartButton', 'zyingInfringementRefreshButton', 'zyingInfringementStartButton'
  ]) assert.equal(context[name].disabled, true, `${name} disabled before the page read resolves`);
  await flushPromises();
  assert.deepEqual(messages.map(message => message.type), ['READ_ZYING_CONTEXT']);
  assert.equal(rendered.length, 0);
  page.resolve({
    ok: true, credential: 'test-page-token', categories: pageCategories, developers: pageDevelopers
  });
  await flushPromises();
  assert.equal(context.zyingRefreshing, true);
  assert.equal(messages[1].type, 'GET_ZYING_OPTIONS');
  assert.deepEqual(rendered, [{
    credential: 'test-page-token', categories: pageCategories, developers: pageDevelopers
  }]);
  assert.match(context.zyingPageStatus.textContent, /已读取当前智赢网页/);
  await Promise.all([context.loadZyingStatus(), context.loadZyingInfringementStatus()]);
  for (const name of [
    'zyingRefreshButton', 'zyingStartButton', 'zyingInfringementRefreshButton', 'zyingInfringementStartButton'
  ]) assert.equal(context[name].disabled, true, name);
  await context.refreshZyingOptions();
  assert.equal(messages.filter(message => message.type === 'READ_ZYING_CONTEXT').length, 1);
  assert.equal(messages.filter(message => message.type === 'GET_ZYING_OPTIONS').length, 1);
  server.resolve({ok: false, error: '后台服务暂时不可用；请求未自动重试'});
  await pending;
  assert.equal(context.zyingRefreshing, false);
  assert.deepEqual(copy(context.zyingContext), rendered[0]);
  assert.equal(rendered.length, 1);
  assert.match(context.zyingPageStatus.textContent, /已保留当前网页分类与开发/);
  assert.equal(context.zyingInfringementPageStatus.textContent, context.zyingPageStatus.textContent);
  assert.equal(context.zyingRefreshButton.disabled, false);
  assert.equal(context.zyingInfringementRefreshButton.disabled, false);
});

test('empty developer preview updates categories and preserves selected developer 17 through supplementation', async () => {
  const server = deferred();
  const developers = [{id: '17', name: '已选业务员'}, {id: '18', name: '另一业务员'}];
  const {context, rendered} = createPopup(message => {
    if (message.type === 'READ_ZYING_CONTEXT') return Promise.resolve({
      ok: true, credential: 'test-page-token', categories: pageCategories, developers: []
    });
    assert.equal(message.type, 'GET_ZYING_OPTIONS');
    return server.promise;
  }, {realRender: true});
  context.renderZyingOptions({
    categories: [{category_id: 'old-category', category_name: '旧分类'}], developers
  });
  context.zyingDeveloper.value = '17';
  context.zyingInfringementDeveloper.value = '17';
  assert.equal(context.zyingDeveloper.value, '17');
  assert.equal(context.zyingInfringementDeveloper.value, '17');
  const pending = context.refreshZyingOptions();
  await flushPromises();
  assert.equal(rendered.length, 2);
  for (const name of ['zyingCategory', 'zyingInfringementCategory']) {
    assert.deepEqual(context[name].options.map(option => option.value), ['', 'live-category']);
  }
  for (const name of ['zyingDeveloper', 'zyingInfringementDeveloper']) {
    assert.equal(context[name].value, '17', `${name} preserves the selection during empty preview`);
    assert.deepEqual(context[name].options.map(option => option.value), ['', '17', '18']);
  }
  server.resolve({ok: true, categories: pageCategories, developers});
  await pending;
  assert.equal(rendered.length, 3);
  assert.equal(context.zyingDeveloper.value, '17');
  assert.equal(context.zyingInfringementDeveloper.value, '17');
});

test('popup renders optional server supplementation after displaying the page categories', async () => {
  const server = deferred();
  const {context, rendered} = createPopup(message => {
    if (message.type === 'READ_ZYING_CONTEXT') return Promise.resolve({
      ok: true, credential: 'test-page-token', categories: pageCategories, developers: []
    });
    assert.equal(message.type, 'GET_ZYING_OPTIONS');
    return server.promise;
  });
  const pending = context.refreshZyingOptions();
  await flushPromises();
  assert.equal(rendered.length, 1);
  assert.deepEqual(rendered[0].categories, pageCategories);
  server.resolve({ok: true, categories: pageCategories, developers: pageDevelopers, developer_warning: '测试提示'});
  await pending;
  assert.equal(rendered.length, 2);
  assert.deepEqual(rendered[1].developers, pageDevelopers);
  assert.deepEqual(copy(context.zyingContext.developers), pageDevelopers);
  assert.match(context.zyingPageStatus.textContent, /1 个分类，1 位产品开发/);
  assert.match(context.zyingPageStatus.textContent, /测试提示/);
});

test('popup clears an old context when the current page cannot be read', async () => {
  const messages = [];
  const {context, rendered} = createPopup(async message => {
    messages.push(message);
    return {ok: false, error: '智赢页面尚未登录'};
  });
  context.zyingContext = {credential: 'old-token', categories: pageCategories};
  await context.refreshZyingOptions();
  assert.equal(context.zyingContext, null);
  assert.deepEqual(rendered, []);
  assert.deepEqual(messages.map(message => message.type), ['READ_ZYING_CONTEXT']);
  assert.match(context.zyingPageStatus.textContent, /尚未登录/);
  assert.equal(context.zyingRefreshing, false);
});
