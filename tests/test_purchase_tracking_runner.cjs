const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('browser_extension/zeshun_collector/background.js', 'utf8');
const code = source.slice(source.indexOf('function purchaseTrackingUrlAllowed'), source.indexOf('async function resumePurchaseTracking'));
const ORDER = '261009-557800569231682';
const OTHER = '261009-673028555121287';
function fixture(responses) {
  const navigations = [], messages = [], api = [], events = [];
  const context = vm.createContext({URL, Set, Promise, encodeURIComponent,
    purchaseTrackingRunPromise: null,
    PURCHASE_TRACKING_PLATFORMS: {'1688': {ordersUrl:'https://trade.1688.com/order/buyer_order_list.htm'}},
    chrome: {tabs: {
      update: async (_, value) => {navigations.push(value.url); events.push('navigate');},
      get: async () => ({url: navigations.at(-1)})
    }, scripting: {executeScript: async () => events.push('inject')}},
    aiWeightPriceWaitTab: async () => events.push('loaded'),
    sendTabMessage: async (_, message) => {messages.push(message); events.push('read'); return responses.shift();},
    apiRequest: async (url, options) => api.push({url, body:JSON.parse(options.body)}),
    notifyAttention: async () => {}, clearPurchaseTrackingSession: async () => {}
  });
  vm.runInContext(code, context);
  return {context, navigations, messages, api, events};
}
test('PDD visits each selected order detail before reading and writes all tracking numbers', async () => {
  const f=fixture([{ok:true,status:'synced',tracking_number:'SF123456789'}, {ok:true,status:'synced',tracking_number:'YT123456789'}]);
  await f.context.runPurchaseTracking({request_id:'job',platform:'pdd',orders:[{order_id:'1',purchase_orders:[ORDER,OTHER]}]}, 3);
  assert.deepEqual(f.navigations,[`https://mobile.yangkeduo.com/order.html?order_sn=${ORDER}`,`https://mobile.yangkeduo.com/order.html?order_sn=${OTHER}`]);
  assert.deepEqual(f.events, ['navigate','loaded','inject','read','navigate','loaded','inject','read']);
  assert.equal(f.api[0].body.tracking_number,'SF123456789;YT123456789');
  assert.equal(f.api[0].body.status,'synced');
});
test('follows bound same-platform logistics link and rejects foreign links', async () => {
  const url=`https://mobile.yangkeduo.com/fixture-logistics.html?order_sn=${ORDER}`;
  const f=fixture([{ok:true,status:'navigate',bound_order:ORDER,url}, {ok:true,status:'synced',tracking_number:'SF123456789'}]);
  const result=await f.context.queryPurchaseTrackingOrder(3,'pdd',ORDER);
  assert.equal(result.status,'synced');
  assert.equal(f.messages[1].boundOrder,ORDER);
  const bad=fixture([{ok:true,status:'navigate',bound_order:ORDER,url:'https://example.com/'}]);
  await assert.rejects(bad.context.queryPurchaseTrackingOrder(3,'pdd',ORDER),/不属于/);
});
test('query failure stops batch without generating false skipped results', async () => {
  const f=fixture([{ok:true,status:'failed',fatal:true,message:'查询未生效'}]);
  await f.context.runPurchaseTracking({request_id:'job',platform:'pdd',orders:[{order_id:'1',purchase_order:ORDER},{order_id:'2',purchase_order:OTHER}]},3);
  assert.equal(f.messages.length,1);
  assert.equal(f.api.length,1);
  assert.ok(f.api[0].url.endsWith('/fail'));
  assert.equal(f.api[0].body.message,'查询未生效');
});
test('partial multi-order results never overwrite row tracking', async () => {
  const f=fixture([{ok:true,status:'synced',tracking_number:'SF123456789'}, {ok:true,status:'pending',message:'未发货'}]);
  const result=await f.context.queryPurchaseTrackingRow(3,'pdd',{purchase_orders:[ORDER,OTHER]});
  assert.equal(result.status,'failed');
  assert.equal(result.tracking_number,undefined);
});
test('old task containing another platform number is not sent to PDD', async () => {
  const f=fixture([]);
  assert.equal((await f.context.queryPurchaseTrackingRow(3,'pdd',{purchase_order:'5127480146121008319'})).status,'failed');
  assert.equal(f.navigations.length,0);
});
test('mixed platform record never partially overwrites logistics', async () => {
  const f=fixture([]);
  const result=await f.context.queryPurchaseTrackingRow(3,'pdd',{purchase_orders:[ORDER],other_purchase_orders:['5127480146121008319']});
  assert.equal(result.status,'failed');
  assert.equal(f.navigations.length,0);
});
test('duplicate starts share a single running loop', async () => {
  const f=fixture([{ok:true,status:'synced',tracking_number:'SF123456789'}]);
  const request={request_id:'job',platform:'pdd',orders:[{order_id:'1',purchase_order:ORDER}]};
  const first=f.context.startPurchaseTrackingRun(request,3);
  const second=f.context.startPurchaseTrackingRun(request,3);
  assert.equal(first,second);
  await first;
  assert.equal(f.api.length,1);
});
