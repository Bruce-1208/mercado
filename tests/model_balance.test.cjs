const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const tick = () => new Promise(setImmediate);
function harness(pathname = '/', data = {}) {
  const nodes = new Map();
  function makeNode(tagName = 'div') {
    return {tagName, dataset:{}, children:[], attributes:{}, _text:'', hidden:false,
      get textContent(){return this._text + this.children.map(child => child.textContent).join('');},
      set textContent(value){this._text=String(value); this.children=[];},
      addEventListener(event, fn){this[event]=fn;},
      setAttribute(name, value){this.attributes[name]=value;},
      append(...children){this.children.push(...children);},
      replaceChildren(...children){this._text=''; this.children=children;},
      scrollIntoView(){}};
  }
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, makeNode());
    return nodes.get(id);
  };
  const events={};
  const state={data, urls:[], interval:null, responder:null};
  const document={hidden:false, getElementById:node, querySelector:()=>node('bell'), createElement:makeNode,
    addEventListener(event, fn){events[event]=fn;}};
  const context={document, location:{pathname}, setInterval(fn){state.interval=fn;}, Date, Number,
    fetch:async url=>{state.urls.push(url); return state.responder ? state.responder(url) : {ok:true, status:200, json:async()=>({status:'success', data:state.data})};}};
  vm.runInNewContext(fs.readFileSync('bit/static/model-balance.js','utf8'),context);
  return {node, state, document, events};
}
test('alerts appear, recharge clears warning, and refresh button works', async () => {
  const {node, state} = harness('/', {status:'overdue', message:'overdue'});
  await tick();
  assert.equal(node('model-balance-alert').hidden,false);
  assert.equal(node('model-balance-title').textContent,'DeepSeek 欠费提醒');
  state.data={status:'ok', message:'ok'}; state.interval(); await tick();
  assert.equal(node('model-balance-alert').hidden,true);
  assert.equal(node('bell').textContent,'♢');
  state.data={status:'low', message:'low'}; node('model-balance-refresh').click(); await tick();
  assert.ok(state.urls.at(-1).endsWith('?refresh=1'));
  assert.equal(node('model-balance-title').textContent,'DeepSeek 低余额预警');
  assert.equal(node('model-balance-refresh').disabled,false);
  state.data={status:'ok', message:'ok'}; node('bell').click(); await tick();
  assert.equal(node('model-balance-alert').hidden,false);
  assert.ok(state.urls.every(url=>url.startsWith('/api/account-integrations/balance') && !url.startsWith('/api/account-integrations/balances')));
});
test('integrations display a configured non-DeepSeek provider without implying a zero or healthy balance', async () => {
  const {node, state} = harness('/settings/integrations', {status:'unsupported', providers:[{
    provider:'openai', name:'OpenAI', status:'unsupported', message:'模型 API Key 无法自动查询余额。',
    balances:[{currency:'USD', amount:'0'}], checked_at:'2026-10-01T01:00:00Z', console_url:'javascript:alert(1)',
  }]});
  await tick();
  const cards = node('model-balance-providers').children;
  assert.equal(cards.length,1);
  assert.equal(cards[0].dataset.provider,'openai');
  assert.match(cards[0].textContent,/需在服务商查看/);
  assert.doesNotMatch(cards[0].textContent,/0 美元|余额正常|查询时间/);
  assert.equal(cards[0].children.at(-1).href,'https://platform.openai.com/settings/organization/billing');
  assert.equal(node('model-balance-recharge').hidden,true);
  assert.equal(node('model-balance-title').textContent,'模型服务余额');
  assert.equal(node('model-balance-alert').hidden,false);
  assert.deepEqual(state.urls,['/api/account-integrations/balances']);
  assert.equal(node('bell').textContent,'');
});
test('all five configured services have individual cards, safe text, balances and official consoles', async () => {
  const names=['deepseek','dashscope','openai','seedance','wan'];
  const {node} = harness('/settings/integrations/', {status:'unsupported', providers:names.map(provider=>({
    provider, name:provider==='deepseek' ? '<img src=x onerror=alert(1)>' : provider,
    status:provider==='deepseek' ? 'ok' : 'unsupported',
    message:'<script>alert(1)</script>', console_url:'https://evil.example/',
    balances:provider==='deepseek' ? [{currency:'CNY',amount:'20.50'},{currency:'USD',amount:'2'}] : [],
    checked_at:provider==='deepseek' ? '2026-10-01T01:00:00Z' : undefined,
  }))});
  await tick();
  const cards=node('model-balance-providers').children;
  assert.deepEqual(cards.map(card=>card.dataset.provider), names);
  assert.match(cards[0].textContent,/20.50 元 \/ 2 美元/);
  assert.match(cards[0].textContent,/查询时间：/);
  assert.equal(cards[0].children[0].children[0].textContent,'<img src=x onerror=alert(1)>');
  assert.ok(cards.every(card=>card.children.every(child=>!['img','script'].includes(child.tagName))));
  assert.deepEqual(cards.map(card=>card.children.at(-1).href),[
    'https://platform.deepseek.com/',
    'https://billing-cost.console.aliyun.com/fortune/billing-account',
    'https://platform.openai.com/settings/organization/billing',
    'https://console.volcengine.com/finance/account-overview/',
    'https://billing-cost.console.aliyun.com/fortune/billing-account',
  ]);
  assert.ok(cards.every(card=>card.children.at(-1).rel==='noopener noreferrer'));
});
test('empty account keeps credentials guidance visible', async () => {
  const {node} = harness('/settings/integrations', {status:'unconfigured', providers:[]});
  await tick();
  assert.equal(node('model-balance-alert').hidden,false);
  assert.equal(node('model-balance-providers').hidden,true);
  assert.equal(node('model-balance-providers').children.length,0);
  assert.match(node('model-balance-message').textContent,/尚未配置模型 API Token.*凭证设置/);
});
test('failed overview refresh clears previous amounts and queried timestamps', async () => {
  const {node, state} = harness('/settings/integrations', {status:'ok', providers:[{
    provider:'deepseek', status:'ok', balances:[{currency:'CNY',amount:'42.00'}], checked_at:'2026-10-01T01:00:00Z',
  }]});
  await tick();
  assert.match(node('model-balance-providers').textContent,/42.00 元/);
  state.responder=async()=>{throw new Error('offline');};
  node('model-balance-refresh').click(); await tick();
  assert.equal(node('model-balance-providers').children.length,0);
  assert.equal(node('model-balance-providers').hidden,true);
  assert.equal(node('model-balance-alert').dataset.status,'unknown');
  assert.match(node('model-balance-message').textContent,/暂时无法查询已配置服务的余额/);
  assert.doesNotMatch(node('model-balance-providers').textContent,/42.00|查询时间/);
  assert.equal(node('model-balance-refresh').disabled,false);
});
test('saving credentials, visibility and refresh query the complete overview', async () => {
  const {node, state, events, document} = harness('/settings/integrations', {providers:[]});
  await tick();
  events['model-credentials-saved'](); await tick();
  assert.equal(state.urls.at(-1),'/api/account-integrations/balances?refresh=1');
  node('model-balance-refresh').click(); await tick();
  assert.equal(state.urls.at(-1),'/api/account-integrations/balances?refresh=1');
  document.hidden=true; state.interval(); await tick();
  const previous=state.urls.length;
  document.hidden=false; events.visibilitychange(); await tick();
  assert.equal(state.urls.length,previous+1);
  assert.equal(state.urls.at(-1),'/api/account-integrations/balances');
});
test('saving while a query is running queues a refresh for the newly saved credentials', async () => {
  const {state, events} = harness('/settings/integrations', {providers:[]});
  await tick();
  let finish;
  state.responder=()=>new Promise(resolve=>{finish=resolve;});
  state.interval();
  events['model-credentials-saved']();
  assert.equal(state.urls.length,2);
  state.responder=null;
  finish({ok:true,status:200,json:async()=>({status:'success',data:{providers:[]}})});
  await tick();
  assert.equal(state.urls.length,3);
  assert.equal(state.urls.at(-1),'/api/account-integrations/balances?refresh=1');
});
