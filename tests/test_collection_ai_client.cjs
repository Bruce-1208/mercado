const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const background = fs.readFileSync('browser_extension/zeshun_collector/background.js', 'utf8');
const claimCode = background.slice(background.indexOf('let collectionAiClaiming = false;'), background.indexOf('async function aiWeightPriceStatus()'));

test('collection polling launches direct search and prevents overlapping claims', async () => {
  let release;
  let calls = 0;
  const launched = [];
  const context = vm.createContext({aiWeightPriceRunPromise: null, authSession: async () => ({}),
    aiWeightPriceAction: async action => { assert.equal(action, 'collection/claim'); calls++; return new Promise(resolve => {release = resolve;}); },
    startAIWeightPriceClientRun: step => launched.push(step)});
  vm.runInContext(claimCode, context);
  const first = context.claimCollectionAiCheck();
  await new Promise(resolve => setImmediate(resolve));
  await context.claimCollectionAiCheck();
  assert.equal(calls, 1);
  release({action: 'search', task: {erp_goods_id: 'collection:1:run'}});
  await first;
  assert.equal(launched[0].action, 'search');
  context.aiWeightPriceRunPromise = Promise.resolve();
  await context.claimCollectionAiCheck();
  assert.equal(calls, 1);
});

test('collection polling does not run without authentication', async () => {
  const context = vm.createContext({aiWeightPriceRunPromise: null, authSession: async () => null,
    aiWeightPriceAction: () => {throw Error('must not claim');}});
  vm.runInContext(claimCode, context);
  await context.claimCollectionAiCheck();
});
