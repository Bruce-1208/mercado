const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const runnerSource = fs.readFileSync('browser_extension/zeshun_collector/appeal-runner.js', 'utf8');
const pageSource = fs.readFileSync('browser_extension/zeshun_collector/appeal-page.js', 'utf8');

function setup({mode = 'AI客服', uncertain = false, reply = 'Your request was reviewed.', groups = 1, loopCount = 1, changeSellerAfterFill = false, changeAccountAfterFill = false, firstFillFails = false} = {}) {
  let clock = Date.now(), nextTab = 1, seller = '123', conversation = 1, sendCount = 0, replyRead = 0, fillCount = 0;
  const calls = [], requests = [], stored = {local: {browserExtensionAuth: {mode:'token',token:'token',user:{id:7}}}, sync:{consoleUrl:'https://console.test'}};
  const tabs = new Map([[1,{id:1,windowId:4,incognito:false,status:'complete',url:'https://global-selling.mercadolibre.com/reputation'}]]);
  const snapshot = (hasReply = false) => ({epoch:String(conversation),conversation_id:'',messages:hasReply ? [{id:'assistant-1',role:'assistant',text:reply}] : [],busy:false});
  class ClockDate extends Date { static now() { return clock; } }
  const groupList = Array.from({length:groups},(_,i)=>({group_id:`g${i}`,site_id:'MLM',site_name:'墨西哥',form:'侵权',message:`appeal ${i}`,group_index:i+1,total_groups:groups}));
  const plan = {task_id:'task-1',identity:{seller_id:'123',root_seller_id:'123',allowed_seller_ids:['123'],sites:[{site_id:'MLM',seller_id:'123'}]},shop:{name:'Test shop'},sites:[{site_id:'MLM'}],params:{mode,loop_count:loopCount},groups:groupList,browser_collections:[],round_interval_seconds:1};
  const context = vm.createContext({URL,Date:ClockDate,TextEncoder,crypto:{randomUUID:()=> 'run-1',subtle:webcrypto.subtle},
    setTimeout:(fn,ms)=>setImmediate(()=>{clock+=ms;fn();}),clearTimeout:clearImmediate,setInterval:()=>1,clearInterval:()=>{},
    MercadoAppealPage:{action:()=>{}},chrome:{tabs:{
      get:async id=>tabs.get(id),update:async(id,args)=>Object.assign(tabs.get(id),args),
      create:async args=>{const tab={id:++nextTab,windowId:args.windowId ?? 4,incognito:false,status:'complete',url:args.url};tabs.set(tab.id,tab);return tab;}
    },scripting:{executeScript:async options=>{
      const [command,args] = options.args; calls.push({command,args,tab:options.target.tabId});
      let result;
      if(command==='context') result={seller_id:seller,site_id:'MLM',logged_out:false,url:tabs.get(options.target.tabId).url};
      else if(command==='probe') result={ready:true,snapshot:snapshot(),url:tabs.get(options.target.tabId).url};
      else if(command==='fill') { fillCount++; result={filled:!firstFillFails || fillCount > 1,snapshot:snapshot()}; if(changeSellerAfterFill)seller='456';if(changeAccountAfterFill)stored.local.browserExtensionAuth.user.id=8; }
      else if(command==='send') {
        assert.equal(stored.local.zeshunAppealRun.in_flight.phase,'sending','durable marker precedes click');
        sendCount++; replyRead=0;
        if(uncertain) throw new Error('lost response after click');
        result={clicked:true,acknowledged:true,snapshot:snapshot()};
      } else if(command==='snapshot') { replyRead++; result={ready:true,snapshot:snapshot(Boolean(reply))}; }
      else if(command==='new_conversation') { const before=snapshot(true); conversation++; result={clicked:true,before}; }
      else if(command==='select_site') result={already:true};
      else result={ready:true};
      return [{frameId:0,result}];
    }}}});
  vm.runInContext(runnerSource,context);
  const runner = context.ZeshunAppealRunner;
  runner.configure({storageGet:async(area,keys)=>Object.fromEntries(keys.map(k=>[k,stored[area][k]])),
    storageSet:async(area,value)=>Object.assign(stored[area],structuredClone(value)),
    apiRequest:async(path,options)=>{const body=JSON.parse(options.body);requests.push({path,body});
      if(path.endsWith('/plan-async')) return {state:'ready',plan:{...plan,round_number:body.round_number || 1}};
      if(path.endsWith('/context')) return {identity:plan.identity,shop:plan.shop,sites:plan.sites};
      return {record_id:1};}
  });
  return {context,runner,calls,requests,stored,tabs,get sendCount(){return sendCount;},get replyRead(){return replyRead;},setSeller:value=>{seller=value;},plan};
}

async function terminal(env) {
  for(let i=0;i<5000;i++) {const status=await env.runner.getAppealStatus();if(!status.running)return status;await new Promise(setImmediate);}
  throw new Error('runner did not reach terminal state');
}
const params = {expected_seller_id:'123',forms:['侵权'],sites:['MLM'],mode:'AI客服',loop_count:1};

test('start without an open backend tab creates one and executes without preflight',async()=>{
  const env=setup();env.tabs.clear();
  await env.runner.startAppealRun({params});
  assert.equal((await terminal(env)).status,'completed');
  assert.equal(env.tabs.size,1);assert.equal(env.sendCount,1);
  assert.equal(env.requests.some(r=>r.path.endsWith('/context')),false);
});

test('closed selected tab is replaced with a new backend tab',async()=>{
  const env=setup();env.context.chrome.tabs.get=async id=>{
    if(!env.tabs.has(id)) throw new Error('No tab');
    return env.tabs.get(id);
  };
  await env.runner.startAppealRun({tabId:99,params});
  assert.equal((await terminal(env)).status,'completed');
  assert.equal(env.sendCount,1);
});

test('login redirect stops before script injection or planning and persists reminder',async()=>{
  const env=setup();env.tabs.get(1).url='https://global-selling.mercadolibre.com/orders/omni/list';
  env.context.chrome.tabs.update=async(id,args)=>Object.assign(env.tabs.get(id),args,{
    url:'https://login.mercadolibre.com/login',status:'loading'
  });
  await env.runner.startAppealRun({tabId:1,params});
  const status=await terminal(env);
  assert.equal(status.running,false);assert.match(status.message,/未登录.*已停止/);
  assert.equal(env.calls.length,0);assert.equal(env.requests.length,0);assert.equal(env.sendCount,0);
  assert.match(env.stored.local.zeshunAppealRun.message,/请登录后台后重新启动/);
});

test('slow preparation returns a visible running state and polls without recreating a plan',async()=>{
  const env=setup();let starts=0,polls=0;
  env.runner.configure({apiRequest:async(path,options)=>{
    if(path.endsWith('/plan-async')) { starts++; return {state:'preparing'}; }
    if(path.endsWith('/plan-status')) {
      polls++; return polls<50 ? {state:'preparing'} : {state:'ready',plan:env.plan};
    }
    return {};
  },storageGet:async(area,keys)=>Object.fromEntries(keys.map(k=>[k,env.stored[area][k]])),
  storageSet:async(area,value)=>Object.assign(env.stored[area],structuredClone(value))});
  const started=await env.runner.startAppealRun({tabId:1,params});
  assert.equal(started.running,true);assert.equal(started.status,'preparing');assert.equal(env.sendCount,0);
  assert.equal((await terminal(env)).status,'completed');
  assert.equal(starts,1);assert.equal(polls,50);assert.equal(env.sendCount,1);
});

test('lost preparation receipt recovers by request id and never resubmits start',async()=>{
  const env=setup();let starts=0,requestId;
  env.runner.configure({apiRequest:async(path,options)=>{
    const body=JSON.parse(options.body);
    if(path.endsWith('/plan-async')) { starts++;requestId=body.request_id;throw new Error('signal timed out'); }
    if(path.endsWith('/plan-status')) {assert.equal(body.request_id,requestId);return {state:'ready',plan:env.plan};}
    return {};
  },storageGet:async(area,keys)=>Object.fromEntries(keys.map(k=>[k,env.stored[area][k]])),
  storageSet:async(area,value)=>Object.assign(env.stored[area],structuredClone(value))});
  await env.runner.startAppealRun({tabId:1,params});
  assert.equal((await terminal(env)).status,'completed');assert.equal(starts,1);assert.equal(env.sendCount,1);
});

test('stop during reputation preparation prevents planning and sending',async()=>{
  const env=setup();env.tabs.get(1).status='loading';
  await env.runner.startAppealRun({tabId:1,params});
  await env.runner.stopAppealRun();
  assert.equal((await terminal(env)).status,'stopped');
  assert.equal(env.requests.length,0);assert.equal(env.sendCount,0);
});

test('sellers orders page reads shop context and starts through the global-selling help page',async()=>{
  const env=setup();
  env.tabs.get(1).url='https://sellers.mercadolibre.com/orders/omni/list?filters=TAB_TODAY';
  const context=await env.runner.getAppealContext(1);
  assert.equal(context.identity.seller_id,'123');
  assert.equal(context.page.url,env.tabs.get(1).url);
  await env.runner.startAppealRun({tabId:1,params});
  const status=await terminal(env);
  assert.equal(status.status,'completed');assert.equal(env.sendCount,1);
  assert.equal(env.tabs.get(1).url,'https://global-selling.mercadolibre.com/help');
});

test('start opens reputation before reading identity and does not require a preflight seller',async()=>{
  const env=setup();
  env.tabs.get(1).url='https://sellers.mercadolibre.com/orders/omni/list';
  const inject=env.context.chrome.scripting.executeScript;
  env.context.chrome.scripting.executeScript=async options=>{
    if(options.args[0]==='context') assert.notEqual(env.tabs.get(1).url,'https://sellers.mercadolibre.com/orders/omni/list');
    return inject(options);
  };
  const update=env.context.chrome.tabs.update;
  const navigations=[];
  env.context.chrome.tabs.update=async(id,args)=>{navigations.push(args.url);return update(id,args);};
  const {expected_seller_id,...selection}=params;
  await env.runner.startAppealRun({tabId:1,params:selection});
  assert.equal((await terminal(env)).status,'completed');
  assert.equal(navigations[0],'https://global-selling.mercadolibre.com/reputation');
  assert.equal(env.requests[0].body.context.seller_id,'123');
  assert.equal(env.sendCount,1);
});

test('stale popup identity no longer blocks start',async()=>{
  const env=setup();
  await env.runner.startAppealRun({tabId:1,params:{...params,expected_seller_id:'old'}});
  assert.equal((await terminal(env)).status,'completed');assert.equal(env.sendCount,1);
});

test('reputation navigation timeout never creates a plan or sends',async()=>{
  const env=setup();env.tabs.get(1).status='loading';
  env.context.chrome.scripting.executeScript=async()=>[{frameId:0,result:{seller_id:'',url:env.tabs.get(1).url}}];
  await env.runner.startAppealRun({tabId:1,params});
  assert.match((await terminal(env)).error,/声誉页面打开超时/);
  assert.equal(env.requests.length,0);assert.equal(env.sendCount,0);
});

test('logged out reputation page stops before planning',async()=>{
  const env=setup();
  env.context.chrome.scripting.executeScript=async()=>[{frameId:0,result:{logged_out:true}}];
  await env.runner.startAppealRun({tabId:1,params});
  assert.match((await terminal(env)).error,/未登录/);
  assert.equal(env.requests.length,0);assert.equal(env.sendCount,0);
});

test('logout after acknowledged send stops even on the last group and preserves sent result',async()=>{
  const env=setup();const inject=env.context.chrome.scripting.executeScript;
  env.context.chrome.scripting.executeScript=async options=>{
    if(options.args[0]==='context' && env.sendCount) return [{frameId:0,result:{logged_out:true}}];
    return inject(options);
  };
  await env.runner.startAppealRun({tabId:1,params});
  const status=await terminal(env);
  assert.equal(status.status,'failed');assert.match(status.message,/未登录.*已停止/);
  assert.equal(env.sendCount,1);
  assert.equal(env.requests.find(r=>r.path.endsWith('/result')).body.execution.acknowledged,true);
});

test('uncertain send is persisted once and never retried',async()=>{
  const env=setup({uncertain:true});await env.runner.startAppealRun({tabId:1,params});
  const status=await terminal(env);assert.equal(status.status,'failed');assert.equal(env.sendCount,1);
  const record=env.requests.find(r=>r.path.endsWith('/result')).body;
  assert.equal(record.execution.status,'sent_unknown');assert.equal(record.execution.sent,true);assert.equal(record.execution.acknowledged,false);
});

test('retrying a proven pre-send fill failure sends exactly once',async()=>{
  const env=setup({firstFillFails:true});await env.runner.startAppealRun({tabId:1,params});
  const status=await terminal(env);assert.equal(status.status,'completed');assert.equal(env.sendCount,1);
  assert.equal(env.calls.filter(c=>c.command==='fill').length,2);
});

test('all-frame probing excludes third-party widgets before injection',async()=>{
  const env=setup();const targets=[];
  env.context.chrome.webNavigation={getAllFrames:async()=>[
    {frameId:0,url:'https://global-selling.mercadolibre.com/help'},
    {frameId:9,url:'https://global-selling.mercadolibre.com/maxwell/new-chat'},
    {frameId:10,url:'https://widgets.example.com/frame'}]};
  const inject=env.context.chrome.scripting.executeScript;
  env.context.chrome.scripting.executeScript=async options=>{targets.push(options.target);return inject(options);};
  await env.runner.startAppealRun({tabId:1,params});await terminal(env);
  assert(targets.some(target=>target.frameIds?.includes(9)));
  assert(targets.every(target=>!target.frameIds?.includes(10)&&!target.allFrames));
});

test('login seller changed after filling is rejected before clicking Send',async()=>{
  const env=setup({changeSellerAfterFill:true});await env.runner.startAppealRun({tabId:1,params});
  const status=await terminal(env);assert.equal(status.status,'failed');assert.equal(env.sendCount,0);
  const execution=env.requests.find(r=>r.path.endsWith('/result')).body.execution;
  assert.equal(execution.status,'pre_send_failed');assert.equal(execution.source_status,'seller_mismatch');
});

test('console account changed after filling stops without sending or posting another account results',async()=>{
  const env=setup({changeAccountAfterFill:true});await env.runner.startAppealRun({tabId:1,params});
  const status=await terminal(env);assert.equal(status.status,'idle');assert.equal(status.logs.length,0);assert.equal(env.sendCount,0);
  for(let i=0;i<100&&env.stored.local.zeshunAppealRun.running;i++)await new Promise(setImmediate);
  assert.equal(env.requests.filter(r=>r.path.endsWith('/result')).length,0);
  assert(env.stored.local.zeshunAppealRun.pending_result);
});

test('confirmed click remains sent after reply timeout; reset required before next group',async()=>{
  const env=setup({reply:'',groups:2});await env.runner.startAppealRun({tabId:1,params});
  const status=await terminal(env);assert.equal(status.status,'completed');assert.equal(status.sent,2);assert.equal(env.sendCount,2);
  assert.equal(env.calls.filter(c=>c.command==='new_conversation').length,1);
  const records=env.requests.filter(r=>r.path.endsWith('/result')).map(r=>r.body.execution);
  assert(records.every(r=>r.status==='sent' && r.reply_status==='reply_timeout' && r.acknowledged));
});

test('human mode submits once, skips AI reply polling, preserves same-window conversation tab',async()=>{
  const env=setup({mode:'人工客服'});await env.runner.startAppealRun({tabId:1,params:{...params,mode:'人工客服'}});
  const status=await terminal(env);assert.equal(status.sent,1);assert.equal(env.replyRead,0);assert.equal(env.sendCount,1);
  assert.equal(env.tabs.size,2);assert.equal(env.tabs.get(status.tab_id).windowId,4);
  assert(env.requests.find(r=>r.path.endsWith('/result')).body.log_records.some(r=>r.event==='human_chat_preserved'));
});

test('backend permanent loop_count 0 continues rounds until stopped',async()=>{
  const env=setup({loopCount:0});await env.runner.startAppealRun({tabId:1,params});
  for(let i=0;i<100 && !env.requests.some(r=>r.path.endsWith('/plan-async')&&r.body.round_number===2);i++)await new Promise(setImmediate);
  assert(env.requests.some(r=>r.path.endsWith('/plan-async')&&r.body.round_number===2));
  await env.runner.stopAppealRun();assert.equal((await terminal(env)).status,'stopped');
});

test('worker restart exposes interruption and never resumes an in-flight group',async()=>{
  const env=setup();const hash=await webcrypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(['https://console.test','token',7])));
  env.stored.local.zeshunAppealRun={status:'running',running:true,in_flight:{group_id:'g0',phase:'sending'},logs:[],scope_digest:Buffer.from(hash).toString('hex')};
  const status=await env.runner.getAppealStatus();assert.equal(status.status,'interrupted');assert.equal(status.running,false);assert.equal(env.sendCount,0);assert.equal(env.requests.length,0);
});

test('stable message identity distinguishes repeated replies and rejects changed sessions',()=>{
  const {runner}=setup();const baseline={epoch:'1',conversation_id:'c',messages:[{id:'old',role:'assistant',text:'same'}]};
  assert.equal(runner.changedMessages(baseline,{...baseline,messages:[{id:'new',role:'assistant',text:'same'}]},'assistant').length,1);
  assert.throws(()=>runner.changedMessages(baseline,{...baseline,epoch:'2'},'assistant'),/会话已更换/);
  assert.equal(runner.siteQuestion('Mexico (Direct to consumer), Mexico (Fulfillment), Brazil, Chile, Colombia, Argentina, Uruguay'),true);
});

test('delay report keeps exactly fresh dispatched orders and handles agent date format/quoted CSV',()=>{
  const {runner}=setup();const csv='date,order,title,a,b,deadline,dispatch\nWednesday 07 Oct 2026 - 08:30,20000000001,"one, product",a,b,c,Dispatched\nTuesday 06 Oct 2026 - 08:30,20000000002,x,a,b,c,Not yet dispatched\nSunday 20 Sep 2026 - 08:30,20000000003,x,a,b,c,Dispatched\n';
  assert.deepEqual(Array.from(runner.delayIdentifiers(csv,Date.parse('2026-10-07T12:00:00Z'))),['20000000001']);
  assert.throws(()=>runner.delayIdentifiers('h,h,h,h,h,h,h\nbad date,20000000001,x,a,b,c,Dispatched'),/日期/);
});

test('page identity reads only logged-in Nordic fields and detects explicit guest Help',async()=>{
  const make = data => {
    const context=vm.createContext({location:new URL('https://global-selling.mercadolibre.com/help/v2'),
      window:{_n:{ctx:{r:{appProps:{pageProps:data}}}}},document:{querySelectorAll:()=>[],getElementById:()=>null,cookie:'cbtSiteId=MLM-remote',title:'Help',body:{innerText:''}},URL});
    vm.runInContext(pageSource,context);return context.MercadoAppealPage.action('context');
  };
  assert.equal((await make({userId:'123',siteId:'CBT',isGuest:false})).seller_id,'123');
  assert.equal((await make({userId:'-1',siteId:'CBT',isGuest:true})).logged_out,true);
  assert.equal((await make({product:{seller_id:'456'}})).seller_id,'');
});


test('reputation collection continues automatically into appeal sending',async()=>{
  const env=setup();
  env.plan.browser_collections=[{site_id:'MLM',form:'投诉'}];
  const inject=env.context.chrome.scripting.executeScript;
  env.context.chrome.scripting.executeScript=async options=>{
    if(options.args[0]==='metric_link') {
      assert.equal(env.tabs.get(1).url,'https://global-selling.mercadolibre.com/reputation');
      return [{frameId:0,result:{empty:true}}];
    }
    return inject(options);
  };
  await env.runner.startAppealRun({tabId:1,params});
  assert.equal((await terminal(env)).status,'completed');
  assert.deepEqual(env.requests.find(r=>r.body.reputation_identifiers).body.reputation_identifiers,{MLM:{'投诉':[]}});
  assert.equal(env.sendCount,1);
});

test('new starts bind browser site accounts without an ERP token',async()=>{
  const env=setup();
  await env.runner.startAppealRun({tabId:1,params});
  assert.equal((await terminal(env)).status,'completed');
  const context=env.requests.find(r=>r.path.endsWith('/plan-async')).body.context;
  assert.equal(context.binding_mode,'browser');
  assert.deepEqual(context.sites,[{site_id:'MLM',seller_id:'123'}]);
  assert.equal(context.token_id,undefined);
});

test('browser infringement pagination submits collected facts and proceeds to sending',async()=>{
  const env=setup();env.plan.identity.binding_mode='browser';
  env.plan.browser_collections=[{site_id:'MLM',form:'侵权'}];
  const inject=env.context.chrome.scripting.executeScript;
  env.context.chrome.scripting.executeScript=async options=>{
    if(options.args[0]==='infringement_items') {
      const offset=options.args[1].offset;
      assert.match(env.tabs.get(1).url,new RegExp(`tab=detections&offset=${offset}$`));
      return [{frameId:0,result:{rows:[{case_id:`c${offset}`,product_id:`MLM123456${offset}`,title:`Hat ${offset}`,eligible:true}],total:2,limit:1,offset}}];
    }
    return inject(options);
  };
  await env.runner.startAppealRun({tabId:1,params});
  assert.equal((await terminal(env)).status,'completed');
  const collected=env.requests.find(r=>r.body.browser_identifiers).body;
  assert.deepEqual(collected.browser_identifiers,{MLM:{'侵权':['MLM1234560','MLM1234561']}});
  assert.equal(collected.browser_products.MLM1234561.title,'Hat 1');
  assert.equal(env.sendCount,1);
});

test('incomplete or changing product pagination never starts customer chat',async()=>{
  const env=setup();env.plan.identity.binding_mode='browser';env.plan.browser_collections=[{site_id:'MLM',form:'侵权'}];
  const inject=env.context.chrome.scripting.executeScript;
  env.context.chrome.scripting.executeScript=async options=>{
    if(options.args[0]==='infringement_items') return [{frameId:0,result:{rows:[{case_id:'duplicate',product_id:'MLM1234567',eligible:true}],total:2,limit:1}}];
    return inject(options);
  };
  await env.runner.startAppealRun({tabId:1,params});
  assert.match((await terminal(env)).error,/分页重复/);
  assert.equal(env.sendCount,0);
  assert.equal(env.requests.some(r=>r.body.browser_identifiers),false);
});

test('browser prohibited collection opens inactive listings and automatically continues',async()=>{
  const env=setup();env.plan.identity.binding_mode='browser';env.plan.browser_collections=[{site_id:'MLM',form:'禁限售'}];
  const inject=env.context.chrome.scripting.executeScript;let opened=false;
  env.context.chrome.scripting.executeScript=async options=>{
    if(options.args[0]==='open_inactive_listings') {opened=true;return [{frameId:0,result:{selected:true}}];}
    if(options.args[0]==='prohibited_items') {
      assert.equal(opened,true);
      return [{frameId:0,result:{rows:[{product_id:'MLM1234567',title:'Plain hat'}],has_next:false,fingerprint:'one'}}];
    }
    return inject(options);
  };
  await env.runner.startAppealRun({tabId:1,params});
  assert.equal((await terminal(env)).status,'completed');
  assert.deepEqual(env.requests.find(r=>r.body.browser_identifiers).body.browser_identifiers,{MLM:{'禁限售':['MLM1234567']}});
  assert.equal(env.sendCount,1);
});

test('usable reputation and help pages continue while the tab remains loading',async()=>{
  const env=setup();env.tabs.get(1).status='loading';
  const inject=env.context.chrome.scripting.executeScript;
  env.context.chrome.scripting.executeScript=async options=>{
    if(options.args[0]==='context') assert.equal(options.injectImmediately,true);
    return inject(options);
  };
  await env.runner.startAppealRun({tabId:1,params});
  assert.equal((await terminal(env)).status,'completed');
  assert.equal(env.tabs.get(1).status,'loading');
  assert.equal(env.sendCount,1);
});

test('existing reputation is reused without forcing another load',async()=>{
  const env=setup();const update=env.context.chrome.tabs.update;const urls=[];
  env.context.chrome.tabs.update=async(id,args)=>{urls.push(args.url);return update(id,args);};
  await env.runner.startAppealRun({tabId:1,params});
  assert.equal((await terminal(env)).status,'completed');
  assert.equal(urls.some(url=>url.endsWith('/reputation')),false);
});

test('new reputation tab is not immediately navigated a second time',async()=>{
  const env=setup();env.tabs.clear();const update=env.context.chrome.tabs.update;const urls=[];
  env.context.chrome.tabs.update=async(id,args)=>{urls.push(args.url);return update(id,args);};
  await env.runner.startAppealRun({params});
  assert.equal((await terminal(env)).status,'completed');
  assert.equal(urls.some(url=>url.endsWith('/reputation')),false);
});

test('account hydration is retried before planning without requiring complete',async()=>{
  const env=setup();env.tabs.get(1).status='loading';const inject=env.context.chrome.scripting.executeScript;let probes=0;
  env.context.chrome.scripting.executeScript=async options=>{
    if(options.args[0]==='context' && ++probes<4) return [{frameId:0,result:{seller_id:'',url:env.tabs.get(1).url}}];
    return inject(options);
  };
  await env.runner.startAppealRun({tabId:1,params});
  assert.equal((await terminal(env)).status,'completed');
  assert.ok(probes>=4);assert.equal(env.sendCount,1);
});

test('unresponsive navigation fails with a specific error instead of remaining preparing',async()=>{
  const env=setup();env.tabs.get(1).url='https://global-selling.mercadolibre.com/orders/omni/list';
  env.context.chrome.tabs.update=()=>new Promise(()=>{});
  await env.runner.startAppealRun({tabId:1,params});
  const status=await terminal(env);
  assert.equal(status.status,'failed');assert.match(status.error,/打开声誉页面未响应/);
  assert.equal(env.requests.length,0);assert.equal(env.sendCount,0);
});

test('unresponsive tab inspection has a bounded wait',async()=>{
  const env=setup();let reads=0;
  env.context.chrome.tabs.get=async id=>++reads===1 ? env.tabs.get(id) : new Promise(()=>{});
  await env.runner.startAppealRun({tabId:1,params});
  assert.match((await terminal(env)).error,/读取店铺标签页超时/);
  assert.equal(env.requests.length,0);assert.equal(env.sendCount,0);
});

test('stopping an unresponsive context probe prevents late planning',async()=>{
  const env=setup();let release;
  env.context.chrome.scripting.executeScript=()=>new Promise(resolve=>{release=resolve;});
  await env.runner.startAppealRun({tabId:1,params});
  for(let i=0;i<20&&!release;i++) await new Promise(setImmediate);
  assert.ok(release);
  await env.runner.stopAppealRun();
  assert.equal((await terminal(env)).status,'stopped');
  release([{frameId:0,result:{seller_id:'123',site_id:'MLM',url:env.tabs.get(1).url}}]);
  await new Promise(setImmediate);
  assert.equal(env.requests.length,0);assert.equal(env.sendCount,0);
});

test('pending reputation navigation never binds the previous document',async()=>{
  const env=setup();env.tabs.get(1).url='https://global-selling.mercadolibre.com/orders/omni/list';
  const update=env.context.chrome.tabs.update, inject=env.context.chrome.scripting.executeScript;let polls=0, injected=0;
  env.context.chrome.tabs.update=async(id,args)=>args.url.endsWith('/reputation')
    ? Object.assign(env.tabs.get(id),{pendingUrl:args.url,status:'complete'}) : update(id,args);
  env.context.chrome.tabs.get=async id=>{
    const tab=env.tabs.get(id);
    if(tab.pendingUrl && ++polls>=4) {tab.url=tab.pendingUrl;delete tab.pendingUrl;}
    return tab;
  };
  env.context.chrome.scripting.executeScript=async options=>{
    if(options.args[0]==='context' && !injected++) assert.equal(env.tabs.get(1).url,'https://global-selling.mercadolibre.com/reputation');
    return inject(options);
  };
  await env.runner.startAppealRun({tabId:1,params});
  assert.equal((await terminal(env)).status,'completed');
  assert.ok(polls>=4);assert.equal(env.sendCount,1);
});

test('same-url pending reload waits for commit before reading the old document',async()=>{
  const env=setup();env.tabs.get(1).pendingUrl=env.tabs.get(1).url;
  const inject=env.context.chrome.scripting.executeScript;let polls=0;
  env.context.chrome.tabs.get=async id=>{
    const tab=env.tabs.get(id);
    if(tab.pendingUrl && ++polls>=5) delete tab.pendingUrl;
    return tab;
  };
  env.context.chrome.scripting.executeScript=async options=>{
    if(options.args[0]==='context') assert.equal(env.tabs.get(1).pendingUrl,undefined);
    return inject(options);
  };
  await env.runner.startAppealRun({tabId:1,params});
  assert.equal((await terminal(env)).status,'completed');
  assert.ok(polls>=5);assert.equal(env.sendCount,1);
});

test('complaint entry waits for hydration before collecting and sending', async()=>{
  const env=setup();
  env.plan.browser_collections=[{site_id:'MLM',form:'投诉'}];
  const inject=env.context.chrome.scripting.executeScript;
  let attempts=0;
  env.context.chrome.scripting.executeScript=async options=>{
    if(options.args[0]==='metric_link') return [{frameId:0,result:++attempts<3
      ? {error:'投诉指标没有可读取的订单入口'}
      : {url:'https://global-selling.mercadolibre.com/metrics?type=claims'}}];
    if(options.args[0]==='orders') return [{frameId:0,result:{ids:['2000000000001234'],fingerprint:'one'}}];
    if(options.args[0]==='next_orders') return [{frameId:0,result:{advanced:false}}];
    return inject(options);
  };
  await env.runner.startAppealRun({tabId:1,params});
  assert.equal((await terminal(env)).status,'completed');
  assert.equal(attempts,3);
  assert.deepEqual(env.requests.find(r=>r.body.reputation_identifiers).body.reputation_identifiers,
    {MLM:{'投诉':['2000000000001234']}});
  assert.equal(env.sendCount,1);
});

test('missing complaint entry stops after bounded retry without sending', async()=>{
  const env=setup();
  env.plan.browser_collections=[{site_id:'MLM',form:'投诉'}];
  const inject=env.context.chrome.scripting.executeScript;
  let attempts=0;
  env.context.chrome.scripting.executeScript=async options=>{
    if(options.args[0]==='metric_link') { attempts++; return [{frameId:0,result:{error:'投诉指标没有可读取的订单入口'}}]; }
    return inject(options);
  };
  await env.runner.startAppealRun({tabId:1,params});
  assert.equal((await terminal(env)).status,'failed');
  assert.ok(attempts>1 && attempts<=31);
  assert.equal(env.sendCount,0);
  assert.equal(env.requests.some(r=>r.body.reputation_identifiers),false);
});

function screenshotMocks(env, fail = false) {
  const shots = [], detached = [];
  env.context.chrome.windows = {update: async () => ({})};
  env.context.chrome.debugger = {
    attach: async () => {},
    sendCommand: async (target, method, options) => {
      assert.equal(method, 'Page.captureScreenshot');
      assert.equal(options.captureBeyondViewport, false);
      shots.push(target.tabId);
      if (fail) throw new Error('capture unavailable');
      return {data: 'mock-png'};
    },
    detach: async target => detached.push(target.tabId)
  };
  env.context.chrome.downloads = {download: async options => {
    assert.match(options.url, /^data:image\/png;base64,/);
    assert.match(options.filename, /^zeshun-appeals\//);
    return 77;
  }};
  env.plan.params.screenshot_mode = true;
  return {shots, detached};
}

test('screenshot mode captures real bound chat, keeps tabs and never resets history across groups', async () => {
  const env = setup({groups: 4});
  const evidence = screenshotMocks(env);
  await env.runner.startAppealRun({tabId: 1, params: {...params, screenshot_mode: true}});
  const state = await terminal(env);
  assert.equal(state.status, 'completed');
  assert.equal(state.selection.screenshot_mode, true);
  assert.equal(evidence.shots.length, 4);
  assert.equal(evidence.detached.length, 4);
  assert.equal(env.calls.filter(c => c.command === 'new_conversation').length, 0);
  assert.ok(env.tabs.has(state.tab_id));
  assert.notEqual(state.tab_id, 1);
  assert.equal(env.tabs.get(state.tab_id).active, true);
  const results = env.requests.filter(r => r.path.endsWith('/result'));
  assert.ok(results.every(r => r.body.execution.screenshot_download_id === 77));
});

test('failed screenshot preserves confirmed send and detaches without resending', async () => {
  const env = setup();
  const evidence = screenshotMocks(env, true);
  await env.runner.startAppealRun({tabId: 1, params: {...params, screenshot_mode: true}});
  assert.equal((await terminal(env)).status, 'completed');
  assert.equal(env.sendCount, 1);
  assert.equal(evidence.detached.length, 1);
  const execution = env.requests.find(r => r.path.endsWith('/result')).body.execution;
  assert.equal(execution.acknowledged, true);
  assert.equal(execution.screenshot_error, 'capture unavailable');
});

test('every order appeal blocks unverified sale numbers in custom text before click', async () => {
  for (const mode of ['AI客服', '人工客服']) for (const form of ['延误', '取消率', '投诉']) {
    const env = setup({mode});
    Object.assign(env.plan.groups[0], {form, identifiers: ['2000000000000099'], message: '销售单号 2000000000000001 请核查'});
    await env.runner.startAppealRun({tabId: 1, params: {...params, mode, forms: [form]}});
    assert.equal((await terminal(env)).status, 'failed');
    assert.equal(env.sendCount, 0);
  }
});
