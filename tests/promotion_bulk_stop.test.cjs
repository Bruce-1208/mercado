const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('bit/templates/mercado_promotions.html','utf8');
const code = html.slice(html.indexOf('function stopCampaignReason('), html.indexOf('async function syncSelected('));
function setup(confirm=true){
  const fields={},calls=[],messages=[],timers=[];
  const context={URLSearchParams,AbortSignal,
    state:{campaigns:[{id:1,name:'活动一',promotion_type:'DEAL',active_item_count:120},
      {id:2,name:'活动二',promotion_type:'PRICE_DISCOUNT',active_item_count:1},
      {id:3,name:'清仓',promotion_type:'CLEARANCE',active_item_count:2},
      {id:4,name:'候选',promotion_type:'DEAL',active_item_count:0}],selectedCampaigns:new Set()},
    $:id=>fields[id]||(fields[id]={}),
    window:{confirm:()=>confirm,setTimeout:fn=>timers.push(fn),parent:{postMessage(){}},location:{origin:'http://test'}},
    renderCampaigns(){context.updateCampaignSelection()},loadPromotions:async()=>{},
    feedback:(message,type)=>messages.push({message,type}),
    api:async(url,options)=>{calls.push({url,body:options?.body&&JSON.parse(options.body)});return {results:[{job_id:'a',promotion_id:1},{job_id:'b',promotion_id:2},{job_id:'c',promotion_id:4}]};},
  };
  vm.createContext(context);vm.runInContext(code,context);
  return {context,fields,calls,messages,timers};
}
test('type options show Chinese while retaining official API values',()=>{
  for(const [value,label] of Object.entries({DEAL:'平台大促',MARKETPLACE_CAMPAIGN:'平台共补',PRICE_DISCOUNT:'单品折扣',SELLER_CAMPAIGN:'卖家活动',DOD:'每日特惠',LIGHTNING:'闪购',CLEARANCE:'清仓'}))
    assert.ok(html.includes(`<option value="${value}">${label}</option>`));
});
test('batch stop selects eligible campaigns and sends whole campaign IDs once',async()=>{
  const s=setup();s.context.selectCampaigns(true);
  assert.deepEqual([...s.context.state.selectedCampaigns],[1,2,4]);
  await s.context.stopSelectedCampaigns();
  assert.deepEqual(s.calls[0].body,{promotion_ids:[1,2,4],confirmed:true});
  assert.equal(s.fields.stopCampaignsBtn.disabled,true);
  await s.context.stopSelectedCampaigns();assert.equal(s.calls.length,1);
  s.context.api=async()=>({rows:[{job_id:'a',status:'succeeded',success_count:120},
    {job_id:'b',status:'failed',failed_count:1},{job_id:'c',status:'succeeded'}]});
  await s.timers.shift()();
  assert.equal(s.context.state.stoppingCampaigns,false);
  assert.match(s.messages.at(-1).message,/已取消 120 件，失败 1 件/);
  assert.equal(s.messages.at(-1).type,'bad');
});
test('cancelled confirmation does not submit any stop request',async()=>{
  const s=setup(false);s.context.selectCampaigns(true);
  await s.context.stopSelectedCampaigns();assert.equal(s.calls.length,0);
});

test('long running stop shows live counts and continues beyond ten minutes',async()=>{
  const s=setup();s.context.state.stoppingCampaigns=true;
  s.context.api=async url=>{
    assert.match(url,/summary=1/);assert.match(url,/job_id=a/);
    return {rows:[{job_id:'a',status:'running',total_count:3660,success_count:1200,failed_count:0,unknown_count:40}]};
  };
  s.context.watchStoppedCampaigns(['a'],'',300);
  await s.timers.shift()();
  assert.equal(s.context.state.stoppingCampaigns,true);
  assert.match(s.fields.stopProgress.textContent,/已取消 1200 件/);
  assert.match(s.fields.stopProgress.textContent,/共 3660 件/);
  assert.equal(s.context.canStopCampaign(s.context.state.campaigns[1]),true);
  assert.equal(s.timers.length,1);
});
test('temporary status failure keeps polling without sending another cancellation',async()=>{
  const s=setup();s.context.state.stoppingCampaigns=true;
  let reads=0;
  s.context.api=async()=>{if(!reads++)throw new Error('读取超时');return {rows:[{job_id:'a',status:'succeeded',total_count:3,success_count:3}]};};
  s.context.watchStoppedCampaigns(['a']);await s.timers.shift()();
  assert.equal(s.context.state.stoppingCampaigns,true);
  assert.match(s.fields.stopProgress.textContent,/自动重连/);
  await s.timers.shift()();assert.equal(s.context.state.stoppingCampaigns,false);
  assert.equal(s.timers.length,0);
});
test('page reload resumes tracking running stop tasks without resubmission',async()=>{
  const s=setup();
  s.context.api=async url=>{assert.match(url,/status=running/);return {rows:[{job_id:'existing'}]};};
  await s.context.restoreStopProgress();
  assert.equal(s.context.state.stoppingCampaigns,true);
  assert.equal(s.timers.length,1);
});

test('one active batch leaves other campaign types selectable and tracks both batches',async()=>{
  const s=setup();
  s.context.state.campaigns.push({id:5,name:'智能共补',promotion_type:'SMART',active_item_count:9});
  s.context.toggleCampaign(1,true);
  s.context.api=async(url,opts)=>({results:[{job_id:'first',promotion_id:1}]});
  await s.context.stopSelectedCampaigns();
  assert.equal(s.context.canStopCampaign(s.context.state.campaigns[0]),false);
  assert.equal(s.context.canStopCampaign(s.context.state.campaigns[4]),true);
  s.context.toggleCampaign(5,true);
  assert.equal(s.fields.stopCampaignsBtn.disabled,false);
  s.context.api=async()=>({results:[{job_id:'second',promotion_id:5}]});
  await s.context.stopSelectedCampaigns();
  assert.equal(s.context.state.stopJobs.size,2);
  s.context.api=async()=>({rows:[{job_id:'first',promotion_fk:1,status:'succeeded',success_count:1,total_count:1}]});
  await s.timers.shift()();
  assert.equal(s.context.state.stoppingCampaigns,true);
  assert.equal(s.context.canStopCampaign(s.context.state.campaigns[4]),false);
  s.context.api=async()=>({rows:[{job_id:'second',promotion_fk:5,status:'partial',unknown_count:1,total_count:9}]});
  await s.timers.shift()();
  assert.match(s.fields.stopProgress.textContent,/部分活动未全部停止/);
  assert.match(s.fields.stopProgress.textContent,/活动 1\/2 已全部停止/);
  assert.equal(s.context.canStopCampaign(s.context.state.campaigns[4]),true);
});
