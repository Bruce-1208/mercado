const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('bit/static/action-center-preload.js', 'utf8');
const hour = 3600000;
const settle = () => new Promise(resolve => setImmediate(resolve));

function browser({pathname='/', now=1000, storage=new Map(), hidden=false}={}) {
  const calls=[], intervals=[], delayed=[], events={};
  const clock={now};
  const context=vm.createContext({URLSearchParams,Date:{now:()=>clock.now},
    location:{pathname},
    document:{hidden,currentScript:{dataset:{account:'A'}},addEventListener:(name,fn)=>events[name]=fn},
    window:{requestIdleCallback:fn=>fn(),addEventListener:(name,fn)=>events[name]=fn,
      localStorage:{getItem:()=>JSON.stringify({salesperson:['A','B'],group_name:['G1','G2']})},
      sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}},
    setInterval:(fn,ms)=>intervals.push({fn,ms}),setTimeout:fn=>delayed.push(fn),
    fetch:async url=>{calls.push(url);return {ok:true,json:async()=>({status:'success',data:{rows:[],refreshing:false}})};},
  });
  vm.runInContext(source,context);
  return {calls,intervals,delayed,events,clock,context,storage};
}

test('home preloads immediately and force refreshes both APIs hourly even in background', async()=>{
  const b=browser({hidden:true}); await settle();
  assert.equal(b.calls.length,2);
  assert.equal(b.intervals.length,1);
  b.clock.now+=hour-1; b.intervals[0].fn(); assert.equal(b.calls.length,2);
  b.clock.now++; b.intervals[0].fn(); await settle();
  assert.equal(b.calls.length,4);
  for(const url of b.calls.slice(2)) {
    const p=new URL(url,'https://test').searchParams;
    assert.equal(p.get('refresh'),'1');
    assert.deepEqual(p.getAll('salesperson'),['A','B']);
    assert.deepEqual(p.getAll('group_name'),['G1','G2']);
  }
  const canonical=b.calls[2].replace('&refresh=1','');
  assert.equal((await b.context.window.takeTodayPreload(canonical)).ok,true);
  b.intervals[0].fn(); assert.equal(b.calls.length,4);
});

test('navigation and cached reads do not postpone the hourly deadline', async()=>{
  const home=browser(); await settle();
  const page=browser({pathname:'/mercado/today',now:1000+hour-60000,storage:home.storage}); await settle();
  let refreshes=0;
  page.context.window.startTodayAutoRefresh(()=>refreshes++);
  page.clock.now=1000+hour;
  page.intervals[0].fn();
  assert.equal(refreshes,1);
  page.intervals[0].fn(); assert.equal(refreshes,1);
});

test('wake catches up once, early focus does not refresh, and reopening overdue preloads forces refresh', async()=>{
  const b=browser(); await settle();
  b.clock.now+=30001; b.events.visibilitychange(); assert.equal(b.calls.length,2);
  b.clock.now+=3*hour; b.events.visibilitychange(); await settle();
  assert.equal(b.calls.length,4);
  b.events.pageshow(); b.events.online(); assert.equal(b.calls.length,4);
  const next=browser({pathname:'/mercado/today',now:b.clock.now+hour,storage:b.storage}); await settle();
  assert.equal(next.calls.length,2);
  assert.ok(next.calls.every(url=>new URL(url,'https://test').searchParams.get('refresh')==='1'));
});

test('pending warmup continues while the workbench browser tab is hidden',async()=>{
  const b=browser({hidden:true}); await settle();
  b.context.fetch=async url=>{b.calls.push(url);return {ok:true,json:async()=>({status:'success',data:{pending:true,refreshing:true}})};};
  b.clock.now+=hour; b.intervals[0].fn(); await settle();
  assert.equal(b.delayed.length,2);
  await b.delayed.shift()();
  assert.equal(b.delayed.length,2,'pending requests continue polling in the background');
});
