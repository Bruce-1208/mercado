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
  const env=setup();
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
