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

function setup({authMode = 'token', synced = {consoleUrl: 'https://console.test', consoleUrlDefaultMigration: 1}} = {}) {
  let listener;
  const stored = {
    local: authMode ? {browserExtensionAuth: {
      mode: authMode, token: 'test-token', user: {username: 'test'}
    }} : {},
    sync: {...synced}, session: {}
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


const response = (status, data) => ({ok: status === 200, status,
  json: async () => status === 200 ? {status:'success', data} : {status:'error', message:'expired'}});
const session = {token:'fresh-token', user:{username:'test',role_key:'super_admin'}, expires_in:3600};

test('request timeout identifies the endpoint and preserves the login',async()=>{
  const {context,run,stored}=setup();
  context.fetch=async()=>{throw Object.assign(new Error('signal timed out'),{name:'TimeoutError'});};
  await assert.rejects(run('apiRequest("/api/browser-extension/appeal/plan-status")'),/请求超时.*appeal\/plan-status/);
  assert.equal(stored.local.browserExtensionAuth.token,'test-token');
});

test('console login restores missing plugin auth and concurrent callers share the request', async () => {
  const {context, run, requests, stored} = setup({authMode:null});
  context.fetch = async (url, options) => { requests.push({url,options}); return response(200,session); };
  const results = await Promise.all([run('authSession()'),run('authSession()')]);
  assert.equal(requests.length,1);
  assert.match(requests[0].url,/\/console-session$/);
  assert.equal(requests[0].options.credentials,'include');
  assert.equal(results[0].token,'fresh-token');
  assert.equal(stored.local.browserExtensionAuth.user.role_key,'super_admin');
  await run('authSession()');
  assert.equal(requests.length,1);
});

test('expired local token is replaced using the console session', async () => {
  const {context, run, stored} = setup();
  stored.local.browserExtensionAuth.expiresAt = 1;
  context.fetch = async () => response(200,session);
  assert.equal((await run('authSession()')).token,'fresh-token');
});

test('401 restores the token and retries the rejected request once', async () => {
  const {context,run,requests} = setup();
  context.fetch = async (url,options) => {
    requests.push({url,options});
    if(url.endsWith('/console-session')) return response(200,session);
    return options.headers.Authorization === 'Bearer fresh-token' ? response(200,{rows:[]}) : response(401);
  };
  await run('apiRequest("/api/browser-extension/management-categories")');
  assert.equal(requests.length,3);
  assert.equal(requests[2].options.headers.Authorization,'Bearer fresh-token');
});

test('a late old-token 401 preserves a newer manual login', async () => {
  const {context,run,stored} = setup();
  let calls = 0;
  context.fetch = async () => {
    if(++calls === 1) {
      stored.local.browserExtensionAuth = {mode:'token',token:'new-login',user:{username:'new'}};
      return response(401);
    }
    return response(200,{rows:[]});
  };
  await run('apiRequest("/api/browser-extension/management-categories")');
  assert.equal(stored.local.browserExtensionAuth.token,'new-login');
  assert.equal(calls,2);
});

test('explicit logout stays logged out even with a console session', async () => {
  const {context,run} = setup();
  let calls = 0;
  context.fetch = async () => { calls++; return response(200,session); };
  await run('logout()');
  assert.equal(await run('authSession()'),null);
  assert.equal(calls,0);
});

test('pending console recovery cannot undo an explicit logout', async () => {
  const {context,run,stored} = setup({authMode:null});
  let finish;
  context.fetch = () => new Promise(resolve => {finish = resolve;});
  const pending = run('authSession()');
  while(!finish) await new Promise(resolve => setImmediate(resolve));
  await run('logout()');
  finish(response(200,session));
  assert.equal(await pending,null);
  assert.equal(stored.local.browserExtensionAuth,undefined);
});

test('unauthenticated, offline and old servers do not produce a false login', async () => {
  for(const status of [401,404,503]) {
    const {context,run,stored} = setup({authMode:null});
    context.fetch = async () => response(status);
    assert.equal(await run('authSession()'),null);
    assert.equal(stored.local.browserExtensionAuth,undefined);
  }
});

test('repeated 401 is bounded and clears the invalid token', async () => {
  const {context,run} = setup();
  let calls = 0;
  context.fetch = async url => {calls++; return url.endsWith('/console-session') ? response(200,session) : response(401);};
  await assert.rejects(run('apiRequest("/api/browser-extension/session")'),/expired/);
  assert.equal(calls,3);
});

test('503 during token validation preserves the valid session without login recovery', async () => {
  const {context,run,stored}=setup();let calls=0;
  context.fetch=async()=>{calls++;return response(503);};
  await assert.rejects(run('apiRequest("/api/browser-extension/session")'),error=>error.status===503&&!error.authRequired);
  assert.equal(stored.local.browserExtensionAuth.token,'test-token');
  assert.equal(calls,1);
});
test('API defaults to a network deadline and rejects unreadable response bodies',async()=>{
  const {context,run,stored}=setup();
  context.fetch=async(_url,options)=>{
    assert(options.signal instanceof AbortSignal);
    return {ok:true,status:200,json:async()=>{throw Error('body stalled');}};
  };
  await assert.rejects(run('apiRequest("/api/browser-extension/session")'),/响应读取失败/);
  assert.equal(stored.local.browserExtensionAuth.token,'test-token');
});

test('read-only product query retries 503 once within the original deadline',async()=>{
  const {context,run,stored}=setup();const signals=[];
  context.fetch=async(_url,options)=>{
    signals.push(options.signal);
    return signals.length===1?response(503):response(200,{items:[]});
  };
  await run('mercadoDataRequest("/api/browser-extension/mercado-data/query", {method:"POST",body:"{}"})');
  assert.equal(signals.length,2);
  assert.equal(signals[0],signals[1]);
  assert.equal(stored.local.browserExtensionAuth.token,'test-token');
});
test('persistent data outage has at most two attempts; upload is never replayed',async()=>{
  const {context,run}=setup();let calls=0;
  context.fetch=async()=>{calls++;return response(503);};
  await assert.rejects(run('mercadoDataRequest("/api/browser-extension/mercado-data/query")'));
  assert.equal(calls,2);
  await assert.rejects(run('apiRequest("/api/browser-extension/collect", {method:"POST",body:"{}"})'));
  assert.equal(calls,3);
});

for (const consoleUrl of ['https://wuhanzeshun.com', 'http://www.wuhanzeshun.com/', 'https://WUHANZESHUN.COM///']) {
  test(`domain migration moves ${consoleUrl} once and clears old credentials`, async () => {
    const {run, stored, requests} = setup({synced: {consoleUrl, consoleUrlDefaultMigration: 1}});
    assert.equal((await run('settings()')).consoleUrl, 'https://wuhanzeshun.cn');
    assert.equal(stored.sync.consoleUrlDefaultMigration, 2);
    assert.equal(stored.local.browserExtensionAuth, undefined);
    assert.equal(requests.length, 0);
    // A deliberate choice of the legacy service after migration remains valid.
    stored.sync.consoleUrl = consoleUrl;
    assert.equal((await run('settings()')).consoleUrl, consoleUrl);
  });
}

for (const consoleUrl of ['http://127.0.0.1:5000', 'https://custom.test', 'https://wuhanzeshun.com/custom', 'https://wuhanzeshun.com.evil.test']) {
  test(`domain migration preserves explicit address ${consoleUrl}`, async () => {
    const {run, stored} = setup({synced: {consoleUrl, consoleUrlDefaultMigration: 1}});
    assert.equal((await run('settings()')).consoleUrl, consoleUrl);
    assert.equal(stored.local.browserExtensionAuth.token, 'test-token');
  });
}

test('fresh install and original local default use the new public domain', async () => {
  for (const synced of [{}, {consoleUrl: 'http://127.0.0.1:5000'}]) {
    const {run, stored} = setup({synced});
    assert.equal((await run('settings()')).consoleUrl, 'https://wuhanzeshun.cn');
    assert.equal(stored.sync.consoleUrlDefaultMigration, 2);
  }
});

test('extension supports console bridge on both current and legacy public domains', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(extension, 'manifest.json'), 'utf8'));
  const bridge = manifest.content_scripts.find(script => script.js.includes('console-recollect.js'));
  for (const domain of ['wuhanzeshun.cn', 'www.wuhanzeshun.cn', 'wuhanzeshun.com', 'www.wuhanzeshun.com']) {
    assert.ok(manifest.host_permissions.includes(`https://${domain}/*`));
    assert.ok(bridge.matches.includes(`https://${domain}/*`));
  }
});
