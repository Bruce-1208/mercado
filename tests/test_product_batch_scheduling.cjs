'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('browser_extension/zeshun_collector/product-batch.js', 'utf8');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
function harness({read, upload, timeout=120, connectTimeout=80, listings} = {}) {
  const tabs = new Map();
  const starts = [], uploads = [];
  let seq = 0;
  const context = vm.createContext({URL, console, setTimeout, clearTimeout, setInterval, clearInterval,
    storageSet: async () => {},
    chrome: {runtime:{getPlatformInfo:cb=>cb({})}, windows:{update:async()=>{},remove:async()=>{}},tabs:{
      create:async options=>{const tab={id:++seq,url:options.url}; tabs.set(seq,tab); return tab;},
      update:async id=>tabs.get(id), get:async id=>tabs.get(id), remove:async id=>tabs.delete(id)
    }},
    sendTabMessage: async(id,{type})=>{
      if(type==='READ_PRODUCT_LIST' && listings) return listings(tabs.get(id).url);
      if(type==='READ_PRODUCT_LIST') return {ok:true,page:1,items:Array.from({length:8},(_,i)=>({url:`https://articulo.mercadolibre.com.mx/MLM-${i+1}`,key:i+1}))};
      const key=Number(tabs.get(id).url.match(/MLM-(\d+)/)[1]); starts.push({key,time:Date.now()});
      if(read) return read(key);
      return product(key);
    },
    uploadProduct:async product=>{if(upload) await upload(product); uploads.push({key:product.source_item_id,time:Date.now()});}
  });
  vm.runInContext(source.replace('const PRODUCT_DETAIL_TIMEOUT_MS = 90000;',`const PRODUCT_DETAIL_TIMEOUT_MS = ${timeout};`)
    .replace('const PRODUCT_CONNECT_TIMEOUT_MS = 15000;', `const PRODUCT_CONNECT_TIMEOUT_MS = ${connectTimeout};`),context);
  const run={tabs:new Set(),stop:false,reserved:0,windowId:1,state:{running:true,params:{max_items:8,concurrency:2,success_rest_seconds:0,min_sales:null,max_sales:null},source_url:'https://listado.mercadolibre.com.mx/toy',completed_count:0,failed_count:0,skipped:0,candidate_count:0}};
  context.run=run;
  return {context,run,tabs,starts,uploads,start:()=>vm.runInContext('runProductBatch(run)',context)};
}
function product(key) {return {ok:true,product:{source_item_id:String(key),plugin_snapshot:{fulfillment_type:'self_ship',fulfillment_eligible:true}}};}

test('default delay and validation',()=>{
  const {context}=harness();
  assert.equal(vm.runInContext('productBatchParams({max_items:20}).success_rest_seconds',context),0);
  for(const value of ['NaN','Infinity','-1','3601']) assert.throws(()=>vm.runInContext(`productBatchParams({max_items:20,success_rest_seconds:${value}})`,context));
  assert.equal(vm.runInContext('productBatchParams({max_items:20,success_rest_seconds:0}).success_rest_seconds',context),0);
});
test('a hung detail does not block later products, and times out',async()=>{
  const h=harness({read:key=>key===1?new Promise(()=>{}):product(key),timeout:200});
  await h.start();
  assert.equal(h.run.state.failed_count,1);
  assert.equal(h.run.state.completed_count,7);
  assert(h.uploads.find(x=>x.key==='3').time-h.starts.find(x=>x.key===1).time<180);
  assert.equal(h.tabs.size,0);
});
test('each lane rests after success, while another lane continues',async()=>{
  const h=harness(); h.run.state.params.success_rest_seconds=.12;
  await h.start();
  const finish=h.uploads.find(x=>x.key==='1').time;
  assert(h.starts.find(x=>x.key===3).time-finish>=110);
  assert(h.starts.find(x=>x.key===2).time-finish<100);
});
test('429 stops all lanes without retry, preserving successes',async()=>{
  const h=harness({read:key=>key===3?{ok:false,status:429,error:'Too many requests'}:product(key)});
  await h.start();
  assert.equal(h.run.state.phase,'stopped');
  assert.match(h.run.state.message,/限频.*自动停止/);
  assert.equal(h.run.state.retry_at,null);
  assert(h.run.state.completed_count>0);
  // The other lane may already have started item 4 before item 3 reports 429.
  assert(!h.starts.some(x=>x.key>4));
  assert.equal(h.tabs.size,0);
});
test('upload rate limit also stops the batch',async()=>{
  const h=harness({upload:async()=>{throw Object.assign(new Error('too many requests'),{status:429});}});
  await h.start();
  assert.equal(h.run.state.phase,'stopped');
  assert.equal(h.run.state.completed_count,0);
  assert(!h.starts.some(x=>x.key>2));
});
test('hung upload times out without blocking other lanes or counting late success',async()=>{
  let finish;
  const h=harness({upload:p=>p.source_item_id==='1'?new Promise(resolve=>{finish=resolve;}):Promise.resolve()});
  await h.start();
  assert.equal(h.run.state.completed_count,7);
  assert.equal(h.run.state.failed_count,1);
  finish(); await pause(5);
  assert.equal(h.run.state.completed_count,7);
});
test('stop interrupts a long cooldown',async()=>{
  const h=harness();h.run.state.params.success_rest_seconds=3600;
  const done=h.start();
  while(!h.uploads.length) await pause(2);
  h.run.stop=true;
  await done;
  assert.equal(h.run.state.phase,'stopped');
  assert(h.starts.length<=2);
});
test('rate limit interrupts another lane with a nonresponsive page',async()=>{
  const h=harness({read:key=>key===1?new Promise(()=>{}):{ok:false,status:429},timeout:5000});
  const start=Date.now();await h.start();
  assert(Date.now()-start<1000);
  assert.equal(h.run.state.phase,'stopped');
  assert.equal(h.starts.length,2);
});

test('one verification page cannot pause other lanes or retry indefinitely',async()=>{
  const h=harness({read:key=>key===1?{ok:false,blocked:true,error:'请先在前端页面完成登录或人机验证'}:product(key)});
  await h.start();
  assert.equal(h.run.state.phase,'partial');
  assert.equal(h.run.state.failed_count,1);
  assert.equal(h.run.state.completed_count,7);
  assert.equal(h.starts.filter(x=>x.key===1).length,1);
  assert.equal(h.tabs.size,0);
});
test('a detail redirect is isolated even in single-lane mode',async()=>{
  const h=harness(); h.run.state.params.concurrency=1;
  const get=h.context.chrome.tabs.get;
  h.context.chrome.tabs.get=async id=>{
    const tab=await get(id);
    return /MLM-1$/.test(tab.url)?{...tab,url:'https://www.mercadolibre.com.mx/login'}:tab;
  };
  await h.start();
  assert.equal(h.run.state.failed_count,1);
  assert.equal(h.run.state.completed_count,7);
  assert.equal(h.tabs.size,0);
});
test('blocked search page stops with an actionable message',async()=>{
  const h=harness();
  h.context.sendTabMessage=async()=>({ok:false,blocked:true,error:'需要买家登录'});
  await h.start();
  assert.equal(h.run.state.phase,'stopped');
  assert.match(h.run.state.message,/列表页.*停止/);
  assert.equal(h.run.state.retry_at,null);
  assert.equal(h.tabs.size,0);
});
test('data source rate limit is not mislabeled as Mercado page blocking',async()=>{
  const h=harness({read:key=>key===1?{ok:false,retryable:false,status:503,errorSource:'data_service',error:'数据源暂时限频'}:product(key)});
  await h.start();
  assert.equal(h.run.state.phase,'partial');
  assert.equal(h.run.state.completed_count,7);
  assert.match(h.run.state.last_error,/数据源暂时限频/);
});
test('true plugin authentication failure stops without failing every product',async()=>{
  const h=harness({read:()=>({ok:false,retryable:false,authRequired:true,status:401,error:'插件登录已失效'})});
  await h.start();
  assert.equal(h.run.state.phase,'error');
  assert.match(h.run.state.message,/登录已失效/);
  assert(h.starts.length<=2);
});
test('a rejected progress write does not poison all subsequent saves',async()=>{
  const h=harness();let writes=0;
  h.context.storageSet=async()=>{if(++writes===1) throw Error('Storage temporarily unavailable');};
  await h.start();
  assert.equal(h.run.state.completed_count,8);
  assert.equal(h.run.state.running,false);
  assert(writes>1);
});

test('a hung last detail on page 1 does not hold up page 2', async()=>{
  let release;
  const h=harness({timeout:2000,
    read:key=>key===1?new Promise(resolve=>{release=resolve;}):product(key),
    listings:url=>({ok:true,page:url.includes('page2')?2:1,
      next_url:url.includes('page2')?'':'https://listado.mercadolibre.com.mx/page2',
      items:(url.includes('page2')?[3,4]:[1,2]).map(key=>({key,url:`https://articulo.mercadolibre.com.mx/MLM-${key}`}))})});
  const done=h.start();
  try {
    const deadline=Date.now()+1000;
    while(!h.uploads.some(x=>x.key==='4') && Date.now()<deadline) await pause(5);
    assert(h.uploads.some(x=>x.key==='4'), 'page 2 finishes before page 1 is released');
    assert(h.run.state.active_items.some(x=>x.url.endsWith('MLM-1') && x.stage==='读取详情'));
  } finally {release(product(1)); await done;}
  assert.equal(h.run.state.completed_count,4);
  assert.equal(h.run.state.active_items.length,0);
});

test('missing receiver is repaired in the committed main frame', async()=>{
  const repaired=new Set(); const injections=[];
  const h=harness({timeout:1000,connectTimeout:800,read:key=>{
    if(!repaired.has(key)) throw Error('Could not establish connection. Receiving end does not exist.');
    return product(key);
  }});
  h.context.chrome.scripting={executeScript:async options=>{
    injections.push(options);
    const key=Number(h.tabs.get(options.target.tabId).url.match(/MLM-(\d+)/)[1]);
    repaired.add(key);
  }};
  await h.start();
  assert.equal(h.run.state.completed_count,8);
  assert.equal(injections.length,8);
  assert(injections.every(x=>x.injectImmediately && x.target.frameIds[0]===0 && x.files.includes('content.js')));
});

test('all disconnected lanes release slots before the detail data deadline', async()=>{
  const h=harness({timeout:2000,read:key=>{
    if(key<=2) throw Error('Could not establish connection. Receiving end does not exist.');
    return product(key);
  }});
  await h.start();
  assert.equal(h.run.state.failed_count,2);
  assert.equal(h.run.state.completed_count,6);
  assert.match(h.run.state.last_error,/连接超时.*释放此通道/);
  assert(h.uploads.find(x=>x.key==='3').time-h.starts[0].time<1000);
});

test('pending navigation never injects collector into about:blank', async()=>{
  const h=harness({timeout:1000,read:key=>{
    if(key===1) throw Error('Receiving end does not exist');
    return product(key);
  }});
  const get=h.context.chrome.tabs.get;
  h.context.chrome.tabs.get=async id=>{
    const tab=await get(id);
    return tab.url.endsWith('MLM-1')?{...tab,url:'about:blank',pendingUrl:tab.url}:tab;
  };
  let injections=0;
  h.context.chrome.scripting={executeScript:async()=>{injections++;}};
  await h.start();
  assert.equal(injections,0);
  assert.equal(h.run.state.completed_count,7);
  assert.equal(h.run.state.failed_count,1);
});

test('failed reserved uploads allow waiting lanes to finish the target', async()=>{
  const h=harness({timeout:1000,upload:async p=>{
    await pause(30);
    if(p.source_item_id==='1') throw Error('upload failed');
  }});
  h.run.state.params.max_items=2;
  await h.start();
  assert.equal(h.run.state.completed_count,2);
  assert.equal(h.run.state.failed_count,1);
});
