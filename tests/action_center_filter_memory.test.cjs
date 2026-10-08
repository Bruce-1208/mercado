const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const preload = fs.readFileSync(path.join(__dirname, '../bit/static/action-center-preload.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../bit/templates/mercado_action_center.html'), 'utf8');
const start = html.indexOf('    const canExecute =');
const pageScript = html.slice(start, html.indexOf('  </script>', start)).replace('{{ can_execute | tojson }}', 'false');
const settle = () => new Promise(resolve => setImmediate(resolve));

function browser(account, storage, options = {}) {
  const requests = [];
  const elements = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, {
    value: '', innerHTML: '', style: {}, listeners: {},
    addEventListener(event, fn) { this.listeners[event] = fn; },
  }]));
  const context = vm.createContext({URLSearchParams,
    window: {localStorage: {
      getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
    }},
    location: {pathname: '/mercado/today'},
    document: {currentScript: {dataset: {account}}, hidden: false,
      getElementById: key => elements[key], addEventListener: () => {}},
    setInterval: () => {}, setTimeout: () => 1, clearTimeout: () => {},
    fetch: async url => {
      requests.push(url);
      return {ok: true, json: async () => ({status: 'success', data: {
        rows: [], events: [], errors: [], filter_options: options,
      }})};
    },
  });
  vm.runInContext(preload, context);
  vm.runInContext(pageScript, context);
  return {context, elements, requests};
}

test('member and store group selections survive reopening and scope the very first reads', async () => {
  const storage = new Map();
  const first = browser('account-A', storage, {salesperson: ['张三'], group_name: ['一组']});
  await settle();
  first.elements.salesperson.value = '张三';
  first.elements.group_name.value = '一组';
  first.elements.salesperson.listeners.change();
  await settle();
  const reopened = browser('account-A', storage, {salesperson: ['张三'], group_name: ['一组']});
  await settle();
  assert.equal(reopened.elements.salesperson.value, '张三');
  assert.equal(reopened.elements.group_name.value, '一组');
  assert.equal(reopened.requests.length, 2, 'page consumes the matching preloads');
  for (const url of reopened.requests) {
    const params = new URL(url, 'https://example.test').searchParams;
    assert.equal(params.get('salesperson'), '张三');
    assert.equal(params.get('group_name'), '一组');
  }
  const other = browser('account-B', storage);
  await settle();
  assert.equal(other.elements.salesperson.value, '');
  assert.equal(other.elements.group_name.value, '');
  reopened.elements.salesperson.value = '';
  reopened.elements.group_name.value = '';
  reopened.elements.group_name.listeners.change();
  await settle();
  const all = browser('account-A', storage);
  await settle();
  assert.equal(all.elements.salesperson.value, '');
  assert.equal(all.elements.group_name.value, '');
});

test('unavailable remembered values stay selected without widening the next poll', async () => {
  const storage = new Map([['zeshun-today-filters:v1:A', JSON.stringify({salesperson: '已离职', group_name: '旧组'})]]);
  const page = browser('A', storage, {salesperson: ['新成员'], group_name: ['新组']});
  await settle();
  assert.match(page.elements.salesperson.innerHTML, /已离职（当前范围不可用）/);
  assert.equal(page.elements.salesperson.value, '已离职');
  await page.context.loadData(false, true);
  const params = new URL(page.requests.at(-1), 'https://example.test').searchParams;
  assert.equal(params.get('salesperson'), '已离职');
  assert.equal(params.get('group_name'), '旧组');
});

test('bad or blocked browser storage does not break initial loading', async () => {
  for (const storage of [
    new Map([['zeshun-today-filters:v1:A', 'broken json']]),
    {get() {throw new Error('blocked');}, set() {throw new Error('blocked');}},
  ]) {
    const page = browser('A', storage);
    await settle();
    assert.equal(page.requests.length, 2);
    assert.equal(page.elements.salesperson.value, '');
    assert.doesNotThrow(() => page.elements.salesperson.listeners.change());
    await settle();
  }
});
