"""Batch scheduling tests with simulated tabs and uploads; no business writes."""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "browser_extension/zeshun_collector/product-batch.js"


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not installed")
def test_batch_limits_pagination_concurrency_stop_and_recovery():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const stored = {};
global.storageGet = async () => structuredClone(stored);
global.storageSet = async (_, value) => { Object.assign(stored, structuredClone(value)); };
global.authSession = async () => ({token:'test'});
global.assertManagementCategorySession = () => {};
global.apiRequest = async path => { assert.equal(path, '/api/browser-extension/mercado-data/status'); return {ready:true}; };
let sequence = 0, active = 0, peak = 0, uploads = [], reads = [], failUpload = false;
const tabCreates = [], activations = [], listAttempts = new Map(), detailAttempts = new Map();
let currentActiveTab = 99;
let nextWindowId = 500, createdWindows = [], closedWindows = [], windowUpdates = [], tabUpdates = [];
const openWindows = new Set();
const origin = 'https://listado.mercadolibre.com.mx/';
const tabs = new Map([[99, {id:99, url:origin+'page1', status:'complete'}]]);
global.chrome = {runtime:{getPlatformInfo:cb => cb({})}, windows:{
  create:async options => {
    const id=++nextWindowId;
    const blank={id:++sequence,windowId:id,url:'about:blank'};
    tabs.set(blank.id,blank); openWindows.add(id);
    const created={id,tabs:[blank]}; createdWindows.push({id,options}); return created;
  },
  update:async (id, changes) => { windowUpdates.push({id, changes}); return {}; },
  remove:async id => { closedWindows.push(id); openWindows.delete(id); }
}, tabs:{
  get:async id => { if(!tabs.has(id)) throw Error('Tab closed'); return tabs.get(id); },
  create:async options => {
    const {url} = options;
    assert(openWindows.has(options.windowId),'cannot create a detail in an already closed window');
    const tab = {id:++sequence,windowId:options.windowId,url:'about:blank',pendingUrl:url,status:'loading'}; tabs.set(tab.id,tab);
    tabCreates.push({id:tab.id,...options});
    if(options.active && !options.windowId) currentActiveTab=tab.id;
    if(/\/(?:MLM|CBT)-/.test(url)) { active++; peak=Math.max(peak,active); }
    return tab;
  },
  update:async (id, changes) => {
    tabUpdates.push({id, changes});
    if(!tabs.has(id)) throw Error('Tab closed');
    if(changes.active) { currentActiveTab=id; activations.push(id); }
    return tabs.get(id);
  },
  remove:async id => {
    const tab=tabs.get(id);
    if(/\/(?:MLM|CBT)-/.test(tab?.pendingUrl || tab?.url || '')) active--;
    tabs.delete(id);
    if(tab?.windowId && ![...tabs.values()].some(other=>other.windowId===tab.windowId)) openWindows.delete(tab.windowId);
  }
}};
const item = (id, profile='china') => ({
  url:origin+(profile==='managed'?'CBT-':'MLM-')+id, key:String(id),
  eligible:['china','managed'].includes(profile), isUsOrigin:profile==='us',
  isChinaOrigin:profile==='china', isManaged:profile==='managed'
});
const lists = {
  page1:{ok:true,page:1,international_selected:true,next_url:origin+'page2',items:[item(1)]},
  page2:{ok:true,page:2,international_selected:false,next_url:origin+'page3',items:[item(1),item(2,'us'),item(3,'unknown'),item(4,'managed'),item(5)]},
  page3:{ok:true,page:3,international_selected:true,next_url:origin+'page4',items:[item(5),item(6),item(7)]},
  page4:{ok:true,page:4,international_selected:false,next_url:'',items:[]},
};
global.sendTabMessage = async (id, message) => {
  const url = tabs.get(id).pendingUrl || tabs.get(id).url;
  assert.notEqual(message.type,'CHECK_ZYING_PLUGIN');
  if(message.type==='READ_PRODUCT_LIST') {
    reads.push(url);
    if(id!==99) {
      const attempt=(listAttempts.get(id)||0)+1; listAttempts.set(id,attempt);
      if(attempt<2) return {ok:false,error:'等待列表页加载'};
    }
    return lists[url.split('/').pop().split(INTERNATIONAL_FILTER)[0]];
  }
  const attempt=(detailAttempts.get(id)||0)+1; detailAttempts.set(id,attempt);
  if(attempt<3) return {ok:false,error:'等待商品详情加载'};
  await pause(5);
  const productId=url.split('/').pop();
  const blocked=productId==='MLM-2' ? ['overseas_warehouse','海外仓'] : productId==='MLM-3' ? ['local_warehouse','本土仓'] : null;
  return {ok:true,product:{source_item_id:productId,title:'Test',plugin_snapshot:{
    sales:Number(productId.match(/\d+/)[0]) * 10,
    fulfillment_type:blocked?.[0] || (productId.startsWith('CBT-') ? 'semi_managed' : 'self_ship'),
    fulfillment_label:blocked?.[1] || (productId.startsWith('CBT-') ? '半托管' : '自发货'),
    fulfillment_eligible:!blocked
  }}};
};
global.uploadProduct = async (product, options) => {
  assert.equal(options.openConsole,false);
  await pause(15);
  if(failUpload) throw Error('Upload failed');
  uploads.push(product.source_item_id);
};
const params = {max_items:4,concurrency_mode:'gentle',success_rest_seconds:0};
async function waitDone() { while(productBatchRun) await pause(5); return getProductBatchStatus(); }
(async () => {
  for(const [country,host] of Object.entries(PRODUCT_SEARCH_HOSTS)) {
    const url=new URL(productSearchUrl(country,'café & niños'));
    assert.equal(url.hostname,host); assert.match(decodeURIComponent(url.pathname),/^\/café-&-niños_NoIndex_True_SHIPPING\*ORIGIN_10215069$/);
  }
  assert.throws(()=>productSearchUrl('XX','toy'));
  assert.throws(()=>productSearchUrl('MLM','  '));
  assert.throws(()=>productBatchUrl('https://mercadolibre.com.mx.evil.test/'));
  assert.match(forceInternationalUrl(origin+'toy'),/SHIPPING\*ORIGIN_10215069/);
  assert.equal(forceInternationalUrl(productSearchUrl('MLM','toy')),productSearchUrl('MLM','toy'));
  assert.equal(productBatchParams({max_items:4}).concurrency,3);
  for (const concurrency of [1,2,3,4,5]) assert.equal(productBatchParams({max_items:4,concurrency}).concurrency,concurrency);
  for (const concurrency of [0,6,1.5,'bad']) assert.throws(()=>productBatchParams({max_items:4,concurrency}));
  for(const change of [{concurrency_mode:'invalid'},{max_items:''},{max_items:501}])
    assert.throws(()=>productBatchParams({...params,...change}));
  assert.throws(()=>productBatchParams({...params,min_sales:20,max_sales:10}));
  assert.equal(productBatchParams({...params,min_sales:20,max_sales:40}).min_sales,20);
  assert.equal(productBatchEligibility({plugin_snapshot:{fulfillment_type:'local_warehouse',fulfillment_label:'本土仓',fulfillment_eligible:false,sales:99}},productBatchParams(params)).eligible,false);
  const salesParams=productBatchParams({...params,min_sales:20,max_sales:40});
  assert.equal(productBatchEligibility({plugin_snapshot:{fulfillment_type:'self_ship',fulfillment_label:'自发货',fulfillment_eligible:true,sales:30}},salesParams).eligible,true);
  assert.equal(productBatchEligibility({plugin_snapshot:{fulfillment_type:'semi_managed',fulfillment_label:'半托管',fulfillment_eligible:true,sales:10}},salesParams).eligible,false);
  assert.equal(productBatchEligibility({plugin_snapshot:{fulfillment_type:'self_ship',fulfillment_label:'自发货',fulfillment_eligible:true,sales:null}},salesParams).eligible,false);
  const starts=await Promise.allSettled([startProductBatch({tab_id:99,params}),startProductBatch({tab_id:99,params})]);
  assert.equal(starts.filter(x=>x.status==='fulfilled').length,1);
  const state=await waitDone();
  assert.equal(state.phase,'completed'); assert.equal(state.candidate_count,6);
  assert.equal(state.execution_window,'new_edge_window'); assert.equal(createdWindows.length,1);
  assert.deepEqual(closedWindows,[createdWindows[0].id]);
  assert.deepEqual(createdWindows[0].options,{url:"about:blank",type:"normal",focused:false,width:640,height:480});
  assert.equal(currentActiveTab,99);
  assert.equal(tabs.get(99).url,origin+"page1");
  assert.equal(windowUpdates.length,0);
  assert.equal(tabUpdates.length,0);
  assert(tabCreates.every(tab=>tab.windowId===createdWindows[0].id));
  assert.equal(state.completed_count,4); assert.equal(state.processed_count,6); assert.equal(state.skipped,2);
  assert(state.last_success_ms > 0); assert(state.total_success_ms >= state.last_success_ms);
  assert.equal(state.over_target_count,0);
  assert.equal(productBatchParams({max_items:1}).success_rest_seconds,0);
  assert.equal(productBatchParams({max_items:1,success_rest_seconds:3}).success_rest_seconds,3);
  assert.deepEqual(uploads.sort(),['CBT-4','MLM-1','MLM-5','MLM-6']);
  assert.equal(peak,1); assert.equal(tabs.size,1);
  assert(!reads.some(url=>url.includes('page4'))); assert(tabs.has(99));
  const firstRunCreates=tabCreates.slice();
  const detailTabs=firstRunCreates.filter(tab=>/\/(?:MLM|CBT)-/.test(tab.url));
  const listTabs=firstRunCreates.filter(tab=>!/\/(?:MLM|CBT)-/.test(tab.url));
  assert(detailTabs.length>0); assert(listTabs.length>0);
  assert(detailTabs.every(tab=>tab.active===true));
  assert(detailTabs.every(tab=>activations.filter(id=>id===tab.id).length===0));
  assert(detailTabs.every(tab=>detailAttempts.get(tab.id)===3));
  assert(listTabs.every(tab=>tab.active===true));
  assert(listTabs.every(tab=>activations.filter(id=>id===tab.id).length===0));
  // Five detail pages must stay open concurrently without focusing their window.
  lists.page1.items=Array.from({length:8},(_,i)=>item(20+i));
  peak=0; uploads=[];
  await startProductBatch({tab_id:99,params:{...params,concurrency_mode:'normal',concurrency:5,max_items:5}});
  const parallel=await waitDone();
  assert.equal(parallel.completed_count,5); assert.equal(peak,5);
  assert.equal(tabs.size,1); assert.equal(currentActiveTab,99);
  assert.equal(tabs.get(99).url,origin+'page1');
  assert.equal(windowUpdates.length,0); assert.equal(tabUpdates.length,0);
  assert.equal(activations.length,0);
  assert.equal(createdWindows.length,closedWindows.length);
  lists.page1.items=[item(1)];
  const originalPage4=lists.page4;
  lists.page4={ok:true,page:4,international_selected:true,next_url:origin+'page5',next_url_synthesized:true,items:[item(1),item(7)]};
  reads=[]; uploads=[];
  await startProductBatch({tab_id:99,params:{...params,max_items:20}});
  const exhausted=await waitDone();
  assert.equal(exhausted.phase,'completed');
  assert.equal(exhausted.completed_count,5);
  assert(reads.some(url=>url.includes('page4')));
  assert(!reads.some(url=>url.includes('page5')));
  assert.equal(new Set(uploads).size,uploads.length);
  lists.page4=originalPage4;
  failUpload=true;
  await startProductBatch({tab_id:99,params:{...params,max_items:1}});
  const failed=await waitDone();
  assert.equal(failed.phase,'partial'); assert.equal(failed.failed_count,5);
  assert.equal(failed.completed_count,0); assert.match(failed.last_error,/Upload failed$/);
  failUpload=false; uploads=[];
  const originalSend = global.sendTabMessage;
  global.sendTabMessage = async (id, message) => message.type === 'EXTRACT_BATCH_PRODUCT'
    ? {ok:false, retryable:false, error:'商品数据服务繁忙'} : originalSend(id, message);
  await startProductBatch({tab_id:99,params:{...params,max_items:1}});
  const dataFailed=await waitDone();
  assert.equal(dataFailed.failed_count,7);
  assert.equal(dataFailed.skipped,0);
  assert.match(dataFailed.last_error,/商品数据服务繁忙$/);
  global.sendTabMessage=originalSend;
  await startProductBatch({tab_id:99,params});
  while(!active) await pause(1);
  await stopProductBatch();
  const stopped=await waitDone();
  assert.equal(stopped.phase,'stopped'); assert.equal(tabs.size,1);
  assert.equal(uploads.length,0);
  stored.productBatch={running:true,completed_count:5};
  const recovered=await getProductBatchStatus();
  assert.equal(recovered.phase,'interrupted'); assert.equal(recovered.completed_count,5);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    subprocess.run([shutil.which("node"), "-e", script, str(SCRIPT)],
                   check=True, capture_output=True, text=True, timeout=60)
