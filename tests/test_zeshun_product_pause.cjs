const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('browser_extension/zeshun_collector/product-batch.js', 'utf8');
assert.match(source, /PRODUCT_BLOCK_RETRY_MS = 600000/);
assert.match(source, /PRODUCT_READ_TIMEOUT_MS = 30000/);
vm.runInThisContext(source.replace('= 600000;', '= 30;').replace('= 30000;', '= 20;').replaceAll('setTimeout(resolve, 500)', 'setTimeout(resolve, 1)').replace('setTimeout(resolve, 180)', 'setTimeout(resolve, 1)'));
const states = [], alerts = [], reads = [];
global.storageSet = async (_, data) => states.push(structuredClone(data.productBatch));
global.notifyAttention = async text => alerts.push(text);
let seq = 0;
const tabs = new Map();
global.chrome = {tabs: {
  create: async options => { const tab = {id: ++seq, ...options}; tabs.set(tab.id, tab); return tab; },
  get: async id => tabs.get(id), update: async (id, options) => Object.assign(tabs.get(id), options),
  remove: async id => tabs.delete(id)
}};
const url = 'https://articulo.mercadolibre.com.mx/MLM-123';
const makeRun = () => ({tabs: new Set(), state: {running:true, phase:'running'}});
(async () => {
  for (const blocked of [{blocked:true,error:'需要买家登录'}, {status:429,error:'too many requests'}]) {
    const run = makeRun(); let attempts = 0;
    global.sendTabMessage = async id => {
      reads.push({id,time:Date.now()});
      return ++attempts === 1 ? blocked : {ok:true};
    };
    const first = readProductBatchPage(run,url,'EXTRACT_BATCH_PRODUCT',x=>x);
    const second = readProductBatchPage(run,url,'EXTRACT_BATCH_PRODUCT',x=>x);
    assert.equal((await first).ok,true); assert.equal((await second).ok,true);
    assert.equal(attempts,3);
    assert(reads.at(-2).time - reads.at(-3).time >= 25);
    assert.equal(run.state.phase,'running'); assert.equal(tabs.size,0);
  }
  const slow = makeRun();
  global.sendTabMessage = () => new Promise(()=>{});
  await assert.rejects(readProductBatchPage(slow,url,'EXTRACT_BATCH_PRODUCT',x=>x), e=>e.slowProduct === true);
  assert.equal(tabs.size,0);
  const stopped = makeRun();
  global.sendTabMessage = async () => ({blocked:true});
  const pending = readProductBatchPage(stopped,url,'EXTRACT_BATCH_PRODUCT',x=>x);
  while(stopped.state.phase !== 'paused') await new Promise(r=>setTimeout(r,1));
  stopped.stop = true;
  assert.equal(await pending,null); assert.equal(tabs.size,0);
  assert(states.some(x=>x.phase === 'paused' && x.retry_at));
  assert(alerts.some(x=>x.includes('10 分钟')));
  console.log('Product pause, retry, queue isolation, hung response and stop tests passed');
})().catch(e=>{console.error(e);process.exitCode=1});
