const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('bit/templates/index.html', 'utf8');
const start = html.indexOf('        async function readZyingOrderResponse');
const end = html.indexOf('        async function loadOrders', start);
const elements = new Map();
const element = id => { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); };
let request;
const context = vm.createContext({
  document: {getElementById: element}, clearTimeout() {}, setTimeout() {},
  orderQueryParams: () => new URLSearchParams('store_id=7&status=待采'),
  loadOrders() {}, orderPage: 1,
  hasWorkbenchPermission: () => true,
  fetch: async (url, options) => {
    request = {url, options};
    return {ok: false, status: 404, text: async () => '<!doctype html><h1>Not Found</h1>'};
  },
});
vm.runInContext(html.slice(start, end), context);
(async () => {
  await assert.rejects(vm.runInContext('readZyingOrderResponse({status:404,text:async()=>"<!doctype html>"})', context), /HTTP 404/);
  await assert.rejects(vm.runInContext('readZyingOrderResponse({status:200,redirected:true,text:async()=>"<html>登录</html>"})', context), /重新登录工作台/);
  await vm.runInContext('pollZyingOrderSync()', context);
  assert.match(element('order-zying-status').textContent, /HTTP 404/);
  for (const id of ['target', 'agent', 'start', 'confirm']) assert.ok(!html.includes(`id="order-zying-${id}"`));
  context.fetch = async () => ({ok: true, text: async () => JSON.stringify({status:'success', data:{running:true,phase:'waiting_login'}})});
  await vm.runInContext('pollZyingOrderSync()', context);
  assert.equal(element('order-zying-stop').disabled, false);
  context.fetch = async (url, options) => {
    request = {url, options};
    return {ok: true, text: async () => JSON.stringify({status: 'success', data: {running:true,phase:'stopping',stop_requested:true}})};
  };
  await vm.runInContext('stopZyingOrderSync()', context);
  assert.equal(element('order-zying-stop').disabled, true);
  context.fetch = async () => ({ok: true, text: async () => JSON.stringify({status:'success', data:{running:false,phase:'stopped'}})});
  await vm.runInContext('pollZyingOrderSync()', context);
  console.log('智赢同步前端回归检查通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
