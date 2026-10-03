const {test} = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../bit/static/wipo-sync.js');

const settings = {source_mode:'madrid', madrid_from_date:'2026-09-23', enabled:true, feed_url:'', format:'auto', interval_hours:24, has_token:false};
const response = data => ({ok:true, status:200, json:async () => ({status:'success', data})});
const tick = () => new Promise(setImmediate);
class Element {
    constructor() { this.children = []; this.listeners = {}; this.value = ''; this.checked = false; this.disabled = false; this.hidden = false; this._text = ''; this.active = false; this.classList = {contains:() => this.active}; }
    set innerHTML(_) { throw new Error('Unsafe innerHTML used'); }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    replaceChildren() { this.children = []; this._text = ''; }
    appendChild(child) { this.children.push(child); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
}
function harness(fetcher, canManage = true, active = false) {
    const elements = new Map();
    const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
    get('tab-infringement-knowledge').active = active;
    const document = {hidden:false, listeners:{}, getElementById:get, createElement:() => new Element(), addEventListener(name, callback) { this.listeners[name] = callback; }};
    let timerId = 0, refreshes = 0;
    const timers = new Map();
    const window = {fetch:fetcher, hasWorkbenchPermission:() => canManage, loadInfringementKnowledge(force) { assert.equal(force, true); refreshes++; }, setTimeout(callback, delay) { const id = ++timerId; timers.set(id, {callback, delay}); return id; }, clearTimeout(id) { timers.delete(id); }};
    const app = ui.boot(document, window);
    return {app, get:name => get(`wipo-sync-${name}`), document, timers, refreshes:() => refreshes};
}

test('settings validate source, URL, calendar date, and update interval', () => {
    assert.equal(ui.validatedSettings(settings, true, '2026-09-30').source_mode, 'madrid');
    assert.throws(() => ui.validatedSettings({...settings, interval_hours:721}), /720/);
    assert.throws(() => ui.validatedSettings({...settings, interval_hours:1.5}), /整数/);
    assert.throws(() => ui.validatedSettings({...settings, madrid_from_date:'2026-02-30'}), /有效/);
    assert.throws(() => ui.validatedSettings({...settings, madrid_from_date:'2026-10-01'}, false, '2026-09-30'), /晚于今天/);
    assert.throws(() => ui.validatedSettings({...settings, source_mode:'feed', feed_url:''}, true), /数据源地址/);
    assert.throws(() => ui.validatedSettings({...settings, source_mode:'feed', feed_url:'javascript:alert(1)'}), /HTTP/);
    assert.throws(() => ui.validatedSettings({...settings, source_mode:'feed', feed_url:'http://example.com/feed'}), /HTTPS/);
    assert.throws(() => ui.validatedSettings({...settings, source_mode:'feed', feed_url:'https://example.com:8443/feed'}), /默认端口/);
    assert.throws(() => ui.validatedSettings({...settings, source_mode:'feed', feed_url:'https://example.com/feed#data'}), /片段/);
    assert.throws(() => ui.validatedSettings({...settings, source_mode:'feed', feed_url:'https://user:secret@example.com/feed'}), /Bearer/);
    assert.equal(Object.hasOwn(ui.validatedSettings({...settings, token:'ignored'}), 'token'), false);
    assert.equal(Object.hasOwn(ui.validatedSettings({...settings, source_mode:'feed', feed_url:'https://example.com/feed', token:'  '}), 'token'), false);
    assert.equal(ui.validatedSettings({...settings, source_mode:'feed', feed_url:'https://example.com/feed', token:' new-token '}).token, 'new-token');
});
test('Beijing dates and timestamps do not use the host timezone', () => {
    assert.equal(ui.chinaToday(new Date('2026-09-29T16:00:00Z')), '2026-09-30');
    assert.match(ui.timeLabel('2026-09-29T16:05:00Z'), /2026\/9\/30.*00:05:00/);
    assert.equal(ui.timeLabel(null), '暂无');
});
test('sync panel loads lazily and view-only accounts cannot mutate', async () => {
    const calls = [];
    const h = harness(async (url, options) => { calls.push({url, options}); return response(url.endsWith('/settings') ? settings : {running:false}); }, false);
    assert.equal(calls.length, 0);
    h.app.activate(true); await tick();
    assert.equal(calls.length, 2);
    assert.equal(h.get('form').hidden, true);
    assert.equal(h.get('start').disabled, true);
    await h.app.start(); await h.app.saveSettings({preventDefault(){}});
    assert.equal(calls.length, 2);
    assert.equal(h.timers.size, 0);
});
test('logs, history, errors, and messages render as text with no HTML execution', async () => {
    const malicious = '<img src=x onerror=alert(1)>';
    const h = harness(async url => response(url.endsWith('/settings') ? settings : {running:false, message:malicious, source:'WIPO Global Brand Database', disclaimer:malicious, logs:[malicious], summary:{fetched:2, unique:1, inserted:1}, history:[{status:'interrupted', finished_at:'2026-09-30T08:00:00Z', message:malicious, summary:{updated:1}}]}), true, true);
    await tick();
    assert.equal(h.get('status').textContent, malicious);
    assert.equal(h.get('log').textContent, malicious);
    assert.match(h.get('history').textContent, /<img src=x onerror=alert\(1\)>/);
    assert.match(h.get('summary').textContent, /拉取 2.*新增 1/);
    assert.match(h.get('history').textContent, /同步已中断/);
    assert.equal(h.get('disclaimer').textContent, malicious);
    assert.equal(h.get('source').textContent, '数据来源：WIPO Global Brand Database');
    assert.equal(h.refreshes(), 0);
});
test('save-and-sync saves feed config once, clears token, and refreshes the knowledge list once on completion', async () => {
    const calls = [];
    let running = false;
    const h = harness(async (url, options) => {
        calls.push({url, options});
        if (url.endsWith('/settings')) return response(options.method === 'PUT' ? {...JSON.parse(options.body), has_token:true} : settings);
        if (url.endsWith('/sync')) { running = true; return response({running:true, status:'running', started_at:'2026-09-30T08:00:00Z'}); }
        return response({running, finished_at:running ? null : calls.length > 3 ? '2026-09-30T08:05:00Z' : null, status:running ? 'running' : 'success'});
    }, true, true);
    await tick();
    h.get('source-mode').value = 'feed';
    h.get('feed-url').value = 'https://example.com/brands.json';
    h.get('token').value = 'new-secret';
    await h.app.start();
    const put = calls.find(call => call.options.method === 'PUT');
    assert.equal(JSON.parse(put.options.body).token, 'new-secret');
    assert.equal(JSON.parse(put.options.body).source_mode, 'feed');
    assert.equal(calls.filter(call => call.options.method === 'PUT').length, 1);
    assert.equal(calls.filter(call => call.options.method === 'POST').length, 1);
    assert.equal(h.get('token').value, '');
    assert.equal(h.get('start').disabled, true);
    assert.equal(h.timers.size, 1);
    running = false;
    await h.app.loadStatus(); await h.app.loadStatus();
    assert.equal(h.refreshes(), 1);
    assert.equal(h.get('start').disabled, false);
    assert.equal(h.timers.size, 0);
});
test('polling stops on hidden tab or document, and completion is detected on return', async () => {
    let running = true;
    const h = harness(async url => response(url.endsWith('/settings') ? settings : {running, finished_at:running ? null : '2026-09-30T08:05:00Z'}), true, true);
    await tick();
    assert.equal(h.timers.size, 1);
    h.app.activate(false);
    assert.equal(h.timers.size, 0);
    h.app.activate(true); await tick();
    assert.equal(h.timers.size, 1);
    h.document.hidden = true; h.document.listeners.visibilitychange();
    assert.equal(h.timers.size, 0);
    running = false; h.document.hidden = false; h.document.listeners.visibilitychange(); await tick();
    assert.equal(h.refreshes(), 1);
    assert.equal(h.timers.size, 0);
});
test('failed configuration save retains entered token and never starts a sync', async () => {
    const calls = [];
    const h = harness(async (url, options) => {
        calls.push({url, options});
        if (options.method === 'PUT') throw new Error('网络不可用');
        return response(url.endsWith('/settings') ? settings : {running:false});
    }, true, true);
    await tick();
    h.get('source-mode').value = 'feed'; h.get('feed-url').value = 'https://example.com/brands.json'; h.get('token').value = 'retry-secret';
    await h.app.start();
    assert.match(h.get('error').textContent, /网络不可用/);
    assert.equal(h.get('token').value, 'retry-secret');
    assert.equal(h.get('start').disabled, false);
    assert.equal(calls.some(call => call.options.method === 'POST'), false);
});
