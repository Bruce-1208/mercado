const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('browser_extension/zeshun_collector/background.js', 'utf8');
const code = source.slice(source.indexOf('// Coalesce exchange reads'), source.indexOf('chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {'));
test('open pages share a fresh rate read; expired and failed reads can be retried', async () => {
  let calls = 0, now = 1000, fail = false;
  const context = vm.createContext({Date: {now: () => now}, mercadoDataRequest: async path => {
    assert.equal(path, '/api/browser-extension/mercado-data/exchange-rates');
    calls++;
    if (fail) throw Error('unavailable');
    return {rates_to_usd:{MXN:.05}, expires_at:now/1000+1800};
  }});
  vm.runInContext(code, context);
  await Promise.all(Array.from({length:5}, () => context.getMercadoExchangeRates()));
  assert.equal(calls, 1);
  await context.getMercadoExchangeRates();
  assert.equal(calls, 1);
  now += 1800001; fail = true;
  await assert.rejects(context.getMercadoExchangeRates(), /unavailable/);
  fail = false;
  await context.getMercadoExchangeRates();
  assert.equal(calls, 3);
});
