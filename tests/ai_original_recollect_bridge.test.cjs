const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('browser_extension/zeshun_collector/background.js','utf8');
const start = source.indexOf('const originalRecollections =');
const end = source.indexOf('\nfunction normalizeZyingToken',start);
function setup() {
  let requests=0, collects=0, finish;
  const context = {URL, Map, Number, settings: async()=>({consoleUrl:'https://wuhanzeshun.cn'}),
    normalizeConsoleUrl:x=>x, apiRequest:async path=>{requests++;assert.equal(path,'/api/browser-extension/ai-original-products/42/recollect-source');return {data:{source_item_id:'123456',source_url:'https://detail.1688.com/offer/123456.html'}};},
    collect1688ListProduct: data=>{collects++;assert.equal(data.source_item_id,'123456');return new Promise(r=>finish=r);}};
  vm.createContext(context);vm.runInContext(source.slice(start,end),context);
  return {context,done:()=>finish({ok:true}),counts:()=>[requests,collects]};
}
test('reject unrelated origin before fetching or collecting',async()=>{
  const x=setup();await assert.rejects(x.context.recollectOriginalProduct(42,{tab:{},url:'https://unrelated.test/'}),/工作台/);
  assert.deepEqual(x.counts(),[0,0]);
});
test('coalesce duplicate clicks and use authorized server source',async()=>{
  const x=setup(),sender={tab:{},url:'https://wuhanzeshun.cn/'};
  const a=x.context.recollectOriginalProduct(42,sender), b=x.context.recollectOriginalProduct(42,sender);
  await new Promise(r=>setImmediate(r));assert.deepEqual(x.counts(),[1,1]);x.done();
  assert.equal((await a).ok,true);assert.equal((await b).ok,true);
});
