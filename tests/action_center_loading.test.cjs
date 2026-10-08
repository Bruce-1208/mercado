const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('post-sale refresh shares in-flight requests and retries after completion', async () => {
  const html = fs.readFileSync(path.join(__dirname, '../bit/templates/mercado_action_center.html'), 'utf8');
  const script = html.slice(html.indexOf('    let postSaleGeneration'), html.indexOf('    async function loadData'));
  const elements = Object.fromEntries(['salesperson', 'group_name', 'post-sale-count', 'post-sale-tasks'].map(key => [key, {value: ''}]));
  const pending = [];
  const context = vm.createContext({
    URLSearchParams,
    $: key => elements[key],
    escapeHtml: value => value,
    dateLabel: value => value,
    fetch: () => new Promise(resolve => pending.push(resolve)),
  });
  vm.runInContext(script, context);
  const first = context.loadPostSale();
  const second = context.loadPostSale();
  assert.equal(pending.length, 1);
  elements.salesperson.value = 'B';
  const changed = context.loadPostSale();
  assert.equal(pending.length, 2);
  pending[1]({ok: true, json: async () => ({status: 'success', data: {rows: [], errors: []}})});
  await changed;
  const current = elements['post-sale-tasks'].innerHTML;
  pending[0]({ok: false, json: async () => ({message: 'old request failed'})});
  await Promise.all([first, second]);
  assert.equal(elements['post-sale-tasks'].innerHTML, current);
  elements.salesperson.value = '';
  const retry = context.loadPostSale();
  assert.equal(pending.length, 3);
  pending[2]({ok: true, json: async () => ({status: 'success', data: {rows: [], errors: []}})});
  await retry;
});

test('background post-sale polling preserves snapshots and does not report pending as empty', async () => {
  const html = fs.readFileSync(path.join(__dirname, '../bit/templates/mercado_action_center.html'), 'utf8');
  const script = html.slice(html.indexOf('    let postSaleGeneration'), html.indexOf('    async function loadData'));
  const elements = Object.fromEntries(['salesperson', 'group_name', 'post-sale-count', 'post-sale-tasks'].map(key => [key, {value: ''}]));
  const timers = [];
  let data = {rows: [], errors: [], pending: true, refreshing: true};
  const context = vm.createContext({URLSearchParams,
    $: key => elements[key], escapeHtml: value => value, dateLabel: value => value,
    setTimeout: callback => {timers.push(callback); return timers.length;}, clearTimeout: () => {},
    fetch: async () => ({ok: true, json: async () => ({status: 'success', data})}),
  });
  vm.runInContext(script, context);
  await context.loadPostSale();
  assert.equal(elements['post-sale-count'].textContent, '核对中');
  assert.doesNotMatch(elements['post-sale-tasks'].innerHTML, /没有未回复/);
  assert.equal(timers.length, 1);
  data = {rows: [{token_id: 1, store_name: '店铺', pack_id: 'P', text: '未回复'}], errors: [], refreshing: false};
  await timers[0]();
  assert.match(elements['post-sale-tasks'].innerHTML, /未回复/);
  assert.equal(timers.length, 1);
});

test('preload starts both APIs and each response is consumed once', async () => {
  const requests = [];
  const context = vm.createContext({
    URLSearchParams, window: {}, location: {pathname: '/mercado/today'},
    fetch: async url => {requests.push(url); return {ok: true, status: 200, json: async () => ({status: 'success'})};},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../bit/static/action-center-preload.js'), 'utf8'), context);
  assert.equal(requests.length, 2);
  assert.ok(requests.every(url => url.includes('background=1')));
  const result = await context.window.takeTodayPreload(requests[0]);
  assert.equal(result.payload.status, 'success');
  assert.equal(context.window.takeTodayPreload(requests[0]), undefined);
});

test('a late task response cannot overwrite a newer filter selection', async () => {
  const html = fs.readFileSync(path.join(__dirname, '../bit/templates/mercado_action_center.html'), 'utf8');
  const start = html.indexOf('    const canExecute =');
  const end = html.indexOf("    for(const key of ['salesperson','group_name'])", start);
  const elements = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, {value: '', style: {}}]));
  const pending = [];
  const context = vm.createContext({URLSearchParams, document: {getElementById: id => elements[id]},
    fetch: () => new Promise(resolve => pending.push(resolve)),
  });
  vm.runInContext(html.slice(start, end).replace('{{ can_execute | tojson }}', 'false'), context);
  const first = context.loadData(false, true);
  elements.salesperson.value = 'B';
  const second = context.loadData(false, true);
  const response = name => ({ok: true, json: async () => ({status: 'success', data: {rows: [], events: [], generated_at: name}})});
  pending[1](response('new'));
  await second;
  pending[0](response('old'));
  await first;
  assert.equal(elements['generated-at'].textContent, '更新于 new');
});

test('large lists page without losing rights-holder reply identity or total counts', () => {
  const html = fs.readFileSync(path.join(__dirname, '../bit/templates/mercado_action_center.html'), 'utf8');
  const start = html.indexOf('    const canExecute =');
  const end = html.indexOf("    for(const key of ['salesperson','group_name'])", start);
  const elements = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, {value: '', style: {}}]));
  const context = vm.createContext({URLSearchParams, document: {getElementById: id => elements[id]}});
  vm.runInContext(html.slice(start, end).replace('{{ can_execute | tojson }}', 'true'), context);
  vm.runInContext(`actionData={rights_holder_replies:Array.from({length:45},(_,id)=>({infraction_id:'case'+id})),rows:Array.from({length:45},(_,id)=>({id,topic:'claims',status:'open'}))}; renderTasks();`, context);
  assert.equal((elements.tasks.innerHTML.match(/data-task-id=/g)||[]).length,20);
  assert.match(elements['rights-cases-pagination'].innerHTML,/共 45 条/);
  context.changeContentPage('rights-cases',1);
  assert.match(elements['rights-cases'].innerHTML,/data-rights-index="20"/);
  assert.doesNotMatch(elements['rights-cases'].innerHTML,/data-rights-index="0"/);
  context.changeContentPage('tasks',1);
  context.changeContentPage('tasks',1);
  assert.equal((elements.tasks.innerHTML.match(/data-task-id=/g)||[]).length,5);
  assert.equal(elements['count-open'].textContent,45);
});

test('workbench preloaded snapshots survive navigation, expire, and are account scoped', async () => {
  const source = fs.readFileSync(path.join(__dirname, '../bit/static/action-center-preload.js'), 'utf8');
  const storage = new Map();
  const make = (account, now, pathname='/mercado/today') => {
    const calls=[];
    const context=vm.createContext({URLSearchParams, Date:{now:()=>now},
      window:{sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}},
      document:{currentScript:{dataset:{account}},hidden:false},location:{pathname},
      setTimeout:fn=>{if(pathname!=='/mercado/today') fn();},
      fetch:async url=>{calls.push(url);return {ok:true,json:async()=>({status:'success',data:{rows:[{id:7}],refreshing:false}})};},
    });
    vm.runInContext(source,context);
    return {context,calls};
  };
  const home=make('A',1000,'/');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(home.calls.length,2);
  const today=make('A',2000);
  assert.equal(today.calls.length,0,'navigation renders prepared results without waiting for another API round trip');
  const snapshot=await today.context.window.takeTodayPreload(home.calls[0]);
  assert.equal(snapshot.payload.data.rows[0].id,7);
  assert.equal(snapshot.payload.data.refreshing,true,'cached result must be revalidated');
  assert.equal(make('B',2000).calls.length,2);
  assert.equal(make('A',302000).calls.length,2);
});
