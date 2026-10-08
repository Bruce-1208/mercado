const test = require('node:test');
const assert = require('node:assert/strict');
const mail = require('../browser_extension/zeshun_collector/appeal-mail.js');

const subject = 'Your orders that you shipped with delay report is ready';
const start = Date.parse('2026-10-07T08:00:00Z');

test('Outlook dates preserve absolute timestamps and Chinese AM/PM', () => {
  assert.equal(mail.parseTimestamp('2026-10-07T08:05:00Z'), start + 300000);
  assert.equal(mail.parseTimestamp('周三 2026-10-07 21:11'), new Date(2026, 9, 7, 21, 11).getTime());
  assert.equal(mail.parseTimestamp('周三 2026/10/7 下午 03:06'), new Date(2026, 9, 7, 15, 6).getTime());
  assert.equal(mail.parseTimestamp('周三 2026/10/7 上午 12:06'), new Date(2026, 9, 7, 0, 6).getTime());
  assert.equal(mail.parseTimestamp('Today 10:05'), 0);
  assert.equal(mail.parseTimestamp('周三 2026-02-30 21:11'), 0);
});

test('only current request, last-hour exact report subjects qualify', () => {
  const rows = [
    {subject, key:'old-request', timestamp:start - 1},
    {subject, key:'current', timestamp:start + 1},
    {subject, key:'older-than-hour', timestamp:start - 4000000},
    {subject:`Re: ${subject}`, key:'reply', timestamp:start + 1},
    {subject, key:'future', timestamp:start + 120000},
    {subject, key:'relative', date:'Today 08:05'},
  ];
  assert.deepEqual(mail.freshMessages(rows, start, start + 60000).map(row => row.key), ['current']);
});

test('minute-only Outlook dates need the official report generation timestamp in the overlapping minute', () => {
  const requested=Date.parse('2026-10-07T08:00:42Z');
  const candidate=mail.freshMessages([{subject,key:'same-minute',date:'2026-10-07T08:00Z'}],requested,start+120000)[0];
  assert.ok(candidate);
  assert.equal(candidate.needsTimeProof,true);
  assert.equal(mail.reportTimestamp(`attachment; filename=Report_of_orders_that_you_shipped_with_delayed_handling_time_MX_2026_${requested+1000}.csv`),requested+1000);
  assert.equal(mail.reportTimestamp('unrelated_1791360043000.csv'),0);
  assert.equal(mail.freshMessages([{subject,key:'old-minute',date:'2026-10-07T07:59Z'}],requested,start+120000).length,0);
});

test('trusted links unwrap Outlook SafeLinks but reject spoofed or credential URLs', () => {
  const url = 'https://global-selling.mercadolibre.com/reports/download?id=123&site_id=MLM';
  assert.equal(mail.trustedReportUrl(url), url);
  assert.equal(mail.trustedReportUrl(`https://nam12.safelinks.protection.outlook.com/?url=${encodeURIComponent(url)}`), url);
  for (const invalid of [
    'http://global-selling.mercadolibre.com/report',
    'https://global-selling.mercadolibre.com.evil.test/report',
    'https://global-selling.mercadolibre.com:9443/report',
    'https://login:secret@global-selling.mercadolibre.com/report',
    'javascript:alert(1)',
  ]) assert.equal(mail.trustedReportUrl(invalid), '');
});

test('report site and explicit seller identity must match current bound store', () => {
  assert.deepEqual(mail.contextEvidence('Report_of_orders_MX_2026.csv seller_id=42', 'MLM', ['42', '43']), {siteConfirmed:true,mismatch:false});
  assert.equal(mail.contextEvidence('Report_of_orders_BR_2026.csv', 'MLM', ['42']).mismatch, true);
  assert.equal(mail.contextEvidence('site_id=MLM&seller_id=99', 'MLM', ['42']).mismatch, true);
  assert.equal(mail.contextEvidence('Mexico userId=43', 'MLM', ['42', '43']).siteConfirmed, true);
  assert.equal(mail.contextEvidence('opaque report', 'MLM', ['42']).siteConfirmed, false);
});

function setup(t, {rows, reportSite = 'MX', stopAt = 0, mismatchAt = 0, junkOnly = false, reportUrl, downloadMode, reportTimestamp} = {}) {
  let time = start + 60000, id = 20, folder = '', opened = false, guards = 0;
  const removed = [], created = [], actions = [], progress = [], protocol = [];
  let networkListener;
  const url = reportUrl || 'https://global-selling.mercadolibre.com/reports/download?report_id=123';
  const tabs = new Map([[7, {id:7,windowId:4,url:'https://global-selling.mercadolibre.com/reputation'}]]);
  const originalChrome = global.chrome;
  const originalFetch = global.fetch;
  global.chrome = {
    tabs: {
      get:async id => tabs.get(id), query:async () => [],
      create:async values => { const tab = {...values,id:++id}; tabs.set(tab.id,tab); created.push(tab); return tab; },
      remove:async id => removed.push(id),
    },
    scripting: {executeScript:async ({target,func,args}) => {
      const [action,arg] = args;
      actions.push([target.tabId,func.name,action,arg]);
      if(func.name === 'reportPage') {
        if(downloadMode === 'network' && networkListener) networkListener({tabId:target.tabId},'Fetch.requestPaused',{
          requestId:'download-1',request:{url:'https://global-selling.mercadolibre.com/download/opaque'},
          responseHeaders:[{name:'Content-Type',value:'text/csv'},
            {name:'Content-Disposition',value:`attachment;filename=Report_delayed_handling_${reportSite}_2026.csv`}],
        });
        return [{result:{csv:downloadMode ? '' : 'Sales number,Date,Dispatch\n123,2026-10-07,2026-10-07',
          filename:downloadMode ? '' : `Report_of_orders_that_you_shipped_with_delayed_handling_time_${reportSite}_2026${reportTimestamp ? '_'+reportTimestamp : ''}.csv`,
          text:'Download',clicked:true,urls:downloadMode === 'direct' ? ['https://global-selling.mercadolibre.com/download/report.csv'] : []}}];
      }
      if(action === 'folder') {folder=arg;return [{result:{clicked:true}}];}
      if(action === 'open') {opened=true;return [{result:{clicked:true}}];}
      if(action === 'body') return [{result:opened ? {links:[url],text:'Go to download report'} : {links:[],text:''}}];
      return [{result:{ready:true,rows:folder === 'junk' || !junkOnly ? rows || [{subject,key:'fresh',timestamp:start+30000,text:'Report ready'}] : []}}];
    }},
  };
  if(downloadMode === 'direct') global.fetch=async url=>({ok:true,url,
    headers:new Headers({'content-type':'text/csv','content-disposition':`attachment;filename=Report_delayed_handling_${reportSite}_2026.csv`}),
    text:async()=>'Sales number,Date,Dispatch\n123,2026-10-07,2026-10-07'});
  if(downloadMode === 'network') global.chrome.debugger={
    attach:async()=>{},detach:async()=>protocol.push('detach'),
    onEvent:{addListener:value=>{networkListener=value;},removeListener:value=>{assert.equal(value,networkListener);networkListener=null;}},
    sendCommand:async(_target,method)=>{protocol.push(method);return method==='Fetch.getResponseBody' ? {body:'Sales number,Date,Dispatch\n123,2026-10-07,2026-10-07'} : {};},
  };
  t.after(() => { global.chrome=originalChrome;global.fetch=originalFetch; });
  const options = {tabId:7,siteId:'MLM',sellerId:'42',allowedSellerIds:['42','43'],requestedAt:start,
    now:()=>time,sleep:async ms=>{time+=ms;},pollIntervalMs:1000,
    identityGuard:async()=>{guards++;return !(mismatchAt && guards>=mismatchAt);},
    isStopped:()=>stopAt && time>=stopAt,onProgress:value=>progress.push(value)};
  return {options, removed,created,actions,progress,protocol,get guards(){return guards;}};
}

test('same-window Outlook Inbox returns current fresh CSV and cleans only owned tabs', async t => {
  const state=setup(t);
  const result=await mail.readReport(state.options);
  assert.match(result.csv,/123,2026/);
  assert.equal(result.report_url,'https://global-selling.mercadolibre.com/reports/download?report_id=123');
  assert.equal(state.created.length,2);
  assert.ok(state.created.every(tab=>tab.windowId===4&&!tab.active));
  assert.deepEqual(state.removed,[22,21]);
  assert.ok(state.guards>=4);
});

test('Junk report is automatically read after checking Inbox', async t => {
  const state=setup(t,{junkOnly:true});
  await mail.readReport(state.options);
  assert.deepEqual(state.actions.filter(action=>action[2]==='folder').map(action=>action[3]),['inbox','junk']);
});

test('previous-request mail does not get opened or reused and polling is capped at three', async t => {
  const state=setup(t,{rows:[{subject,key:'stale',timestamp:start-1000,text:'Mexico'}]});
  await assert.rejects(mail.readReport({...state.options,maxPolls:99}),/未收到本次/);
  assert.equal(state.progress.length,3);
  assert.ok(!state.actions.some(action=>action[2]==='open'));
  assert.deepEqual(state.removed,[21]);
});

test('a report for another site stops before returning its CSV', async t => {
  const state=setup(t,{reportSite:'BR'});
  await assert.rejects(mail.readReport(state.options),/店铺或站点无法匹配/);
  assert.deepEqual(state.removed,[22,21]);
});

test('seller mismatch in email report URL is skipped before navigating it', async t => {
  const state=setup(t,{reportUrl:'https://global-selling.mercadolibre.com/reports/download?site_id=MLM&seller_id=99'});
  await assert.rejects(mail.readReport(state.options),/未收到本次/);
  assert.equal(state.created.length,1);
});

test('identity change stops mail polling and cleans the temporary mailbox', async t => {
  const state=setup(t,{mismatchAt:2});
  await assert.rejects(mail.readReport(state.options),error=>error.status==='login_required');
  assert.deepEqual(state.removed,[21]);
});

test('stop is observed during the minute wait within 250 ms', async t => {
  const state=setup(t,{stopAt:start+60500});
  await assert.rejects(mail.readReport({...state.options,pollIntervalMs:60000}),error=>error.status==='stopped');
  assert.deepEqual(state.removed,[21]);
  assert.ok(!state.actions.some(action=>action[2]==='open'));
});

test('CSV direct link is read with the report filename as site evidence', async t => {
  const state=setup(t,{downloadMode:'direct'});
  assert.match((await mail.readReport(state.options)).csv,/Sales number/);
  assert.deepEqual(state.removed,[22,21]);
});

test('response-stage interception reads CSV then aborts native download and detaches', async t => {
  const state=setup(t,{downloadMode:'network'});
  assert.match((await mail.readReport(state.options)).csv,/Sales number/);
  assert.ok(state.protocol.includes('Fetch.enable'));
  assert.ok(state.protocol.includes('Fetch.getResponseBody'));
  assert.ok(state.protocol.includes('Fetch.failRequest'));
  assert.deepEqual(state.protocol.slice(-2),['Fetch.disable','detach']);
  assert.deepEqual(state.removed,[22,21]);
});

test('same-minute email succeeds only when filename proves this request generated the report', async t => {
  const state=setup(t,{rows:[{subject,key:'same-minute',date:'2026-10-07T08:00Z',text:'Report ready'}],reportTimestamp:start+50000});
  assert.match((await mail.readReport({...state.options,requestedAt:start+42000})).csv,/Sales number/);
});

test('same-minute email without exact report generation proof is rejected', async t => {
  const state=setup(t,{rows:[{subject,key:'same-minute',date:'2026-10-07T08:00Z',text:'Report ready'}]});
  await assert.rejects(mail.readReport({...state.options,requestedAt:start+42000}),/无法确认报表由本次请求生成/);
});

test('missing identity snapshot never opens a mailbox', async t => {
  const state=setup(t);
  await assert.rejects(mail.readReport({...state.options,identityGuard:async()=>undefined}),error=>error.status==='login_required');
  assert.equal(state.created.length,0);
});
