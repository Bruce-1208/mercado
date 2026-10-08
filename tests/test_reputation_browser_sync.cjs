const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const template = fs.readFileSync(require('node:path').join(__dirname, '../bit/templates/index.html'), 'utf8');

function loadFunction(name, nextName, context = {}) {
    const start = template.indexOf(`        function ${name}(`);
    const end = template.indexOf(`        ${nextName}`, start + 1);
    assert.ok(start >= 0 && end > start);
    const runtime = vm.createContext(context);
    vm.runInContext(template.slice(start, end), runtime);
    return runtime[name];
}

const escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;');

test('site warning displays one complete API record without store-wide notices', () => {
    const warning = '2026-10-01T09:00:00Z\nLatest site warning\nComplete explanation.';
    const render = loadFunction('reputationWarningText', 'function renderReputationWarningCell', {
        reputationStoreNotices: {1: [{title: 'Other site warning', description: 'Store notice'}]},
    });
    assert.equal(render({token_id: 1, '系统告警': warning}), warning);
    assert.equal(render({token_id: 1}), '告警 API 暂无法获取');
});

test('warning text escapes HTML and preserves the entire detail', () => {
    const render = loadFunction('renderReputationWarningCell', 'function positionReputationWarningPopover', {escapeHtml});
    const markup = render('2026-10-01\n<script>alert(1)</script>\nFull body');
    assert.ok(markup.includes('data-warning-detail='));
    assert.ok(markup.includes('Full body'));
    assert.ok(markup.includes('&lt;script&gt;'));
    assert.ok(!markup.includes('<script>'));
    assert.ok(!render('暂无 API 告警（仅限接口可获取记录）').includes('data-warning-detail='));
});

test('site state distinguishes confirmed ban, no ban and missing evidence', () => {
    const render = loadFunction('renderReputationSiteStatus', 'async function openReputationBrowser', {escapeHtml});
    assert.match(render('已封禁（站点销售暂停）'), /reputation-site-status risk/);
    assert.match(render('未封禁'), /reputation-site-status normal/);
    assert.match(render('待确认（API 仅返回公开 active）'), /reputation-site-status unconfirmed/);
    assert.match(render('待确认（限制刊登，销售权限未提供）'), /unconfirmed/);
});

test('the reputation update UI has no browser dispatch controls or requests', () => {
    assert.ok(!template.includes('id="reputation-browser-sync-btn"'));
    assert.ok(!template.includes('runReputationBrowserSync'));
    assert.ok(!template.includes('/api/reputation/browser-sync'));
    assert.ok(template.includes('/api/reputation/update-selected'));
});
