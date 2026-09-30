const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('mode selection queries the server and reset reloads all modes', async () => {
  const html = fs.readFileSync('bit/templates/index.html', 'utf8');
  assert.match(html, /id="ai-appeal-copy-mode-filter" onchange="loadAiAppealRecords\(\)"/);
  const start = html.indexOf('        function resetAiAppealFilters()');
  const end = html.indexOf('        async function loadLatestReputation()', start);
  const urls = [];
  const context = {
    aiAppealRecordBody: {}, aiAppealTotal: {}, aiAppealLatestTime: {},
    aiAppealDateFrom: {}, aiAppealDateTo: {}, aiAppealSalespersonFilter: {},
    aiAppealGroupFilter: {}, aiAppealAgentFilter: {}, aiAppealCopyModeFilter: { value: 'AI话术模式' },
    setAiAppealFilterOptions() {}, applyAiAppealFilters() {}, escapeHtml: String,
    fetch: async url => { urls.push(url); return {ok: true, json: async () => ({status: 'success', data: {rows: []}})}; },
  };
  vm.createContext(context);
  vm.runInContext(html.slice(start, end), context);
  await context.loadAiAppealRecords();
  assert.equal(new URL(urls[0], 'http://test').searchParams.get('appeal_copy_mode'), 'AI话术模式');
  context.resetAiAppealFilters();
  assert.equal(new URL(urls[1], 'http://test').searchParams.get('appeal_copy_mode'), '');
});
