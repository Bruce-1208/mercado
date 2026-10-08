const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const popupSource = fs.readFileSync('browser_extension/zeshun_collector/popup.js', 'utf8');
const popupHtml = fs.readFileSync('browser_extension/zeshun_collector/popup.html', 'utf8');

function createStartHandler({value = '100', infringement = false} = {}) {
  const prefix = infringement ? 'zyingInfringement' : 'zying';
  const start = popupSource.indexOf(`${prefix}StartButton.addEventListener("click",`);
  const end = popupSource.indexOf(`${prefix}StopButton.addEventListener("click",`, start);
  assert.ok(start >= 0 && end > start, 'The popup collection start handler must exist');
  const messages = [];
  const results = [];
  let onClick;
  const context = vm.createContext({
    authenticated: true,
    zyingContext: {tabId: 51, categories: [], developers: []},
    zyingRunning: false,
    zyingRefreshing: false,
    zyingInfringementRunning: false,
    zeshunCategoriesLoading: false,
    [`${prefix}StartProductId`]: {value: ''},
    [`${prefix}MaxItems`]: {value},
    [`${prefix}StartButton`]: {
      disabled: false,
      addEventListener(event, handler) { assert.equal(event, 'click'); onClick = handler; },
    },
    [`${prefix}Category`]: {value: '', selectedOptions: [{textContent: '全部分类'}]},
    [`${prefix}Developer`]: {value: '', selectedOptions: [{textContent: '全部产品开发'}]},
    showResult(message, kind) { results.push({message, kind}); },
    managementCategorySelection() { return null; },
    async saveZeshunCategoryOptions() {},
    async runtimeMessage(message) { messages.push(message); return {ok: true}; },
    async loadZyingStatus() {},
    async loadZyingInfringementStatus() {},
  });
  vm.runInContext(popupSource.slice(start, end), context);
  return {messages, results, click: () => onClick()};
}

test('Zying collection input accepts counts above 10000 without a fixed HTML maximum', () => {
  const input = popupHtml.match(/<input\b[^>]*\bid="zying-max-items"[^>]*>/)?.[0];
  assert.ok(input);
  assert.match(input, /\bmin="1"/);
  assert.match(input, /\bstep="1"/);
  assert.match(input, /\brequired\b/);
  assert.doesNotMatch(input, /\bmax=/);
  assert.match(popupHtml, /<input\b[^>]*\bid="zying-infringement-max-items"[^>]*\bmax="10000"/);
  assert.match(popupHtml, /<input\b[^>]*\bid="weight-price-limit"[^>]*\bmax="10000"/);
});

test('Zying collection sends the chosen count above 10000 without clipping it', async () => {
  for (const value of ['10001', '25001', String(Number.MAX_SAFE_INTEGER)]) {
    const popup = createStartHandler({value});
    await popup.click();
    assert.equal(popup.messages.length, 1);
    assert.equal(popup.messages[0].type, 'START_ZYING_COLLECTION');
    assert.equal(popup.messages[0].params.max_items, Number(value));
    assert.deepEqual(popup.results, []);
  }
});

test('Zying collection rejects blank, nonpositive, fractional and unsafe counts before starting', async () => {
  for (const value of ['', '0', '-1', '1.5', 'not-a-number', 'Infinity', '9007199254740992']) {
    const popup = createStartHandler({value});
    await popup.click();
    assert.deepEqual(popup.messages, [], value);
    assert.equal(popup.results.length, 1, value);
    assert.equal(popup.results[0].kind, 'error');
    assert.match(popup.results[0].message, /正整数/);
  }
});

test('Zying infringement review keeps its existing maximum', async () => {
  const popup = createStartHandler({value: '25001', infringement: true});
  await popup.click();
  assert.deepEqual(popup.messages, []);
  assert.match(popup.results[0].message, /10000/);
});
