const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('bit/static/console-performance.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const response = (data = {status: 'success', data: {rows: [1]}}) => new Response(JSON.stringify(data), {headers: {'Content-Type': 'application/json'}});
function setup() {
    const calls = [], idle = [], buttons = new Map(), events = new Map();
    const document = {hidden: false, addEventListener: (name, fn) => events.set(name, fn),
        querySelector: selector => buttons.get(selector.match(/data-tab="([^"]+)/)?.[1]),
        querySelectorAll: () => [], dispatchEvent() {}, getElementById: () => null};
    let time = 0, timestamp = 1000;
    const window = {document, location: {href: 'https://erp.test/', origin: 'https://erp.test'},
        fetch: (input, options = {}) => new Promise((resolve, reject) => {
            const call = {url: String(input), options, resolve, reject}; calls.push(call);
            options.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        }), requestIdleCallback: fn => idle.push(fn), setTimeout: () => ++time};
    const context = vm.createContext({window, document, URL, Request, Response, Headers, JSON,
        TextDecoder, Uint8Array, ReadableStream, AbortController, DOMException, Date: {now: () => timestamp}, CustomEvent: class {}, clearTimeout() {}});
    vm.runInContext(source, context);
    const runtime = window.ZeshunPerformance;
    runtime.configure({allowedPaths: ['/api/list', '/api/options'], modules: []});
    return {runtime, window, document, calls, idle, buttons, events, advance: ms => {timestamp += ms;}};
}
test('warm response is reused once; explicit second refresh reaches the server', async () => {
    const app = setup(); const warm = app.runtime.preload('/api/list?b=2&a=1');
    app.calls[0].resolve(response()); await warm;
    assert.equal(app.runtime.snapshot().cached, 1);
    assert.deepEqual(await (await app.window.fetch('/api/list?a=1&b=2')).json(), {status: 'success', data: {rows: [1]}});
    assert.equal(app.calls.length, 1);
    const refresh = app.window.fetch('/api/list?a=1&b=2'); app.calls[1].resolve(response()); await refresh;
    assert.equal(app.runtime.snapshot().cached, 0);
});
test('foreground request joins an in-flight warm request without leaving a stale cache', async () => {
    const app = setup(); const warm = app.runtime.preload('/api/list'); const foreground = app.window.fetch('/api/list');
    assert.equal(app.calls.length, 1);
    app.calls[0].resolve(response());
    const [a, b] = await Promise.all([warm, foreground]);
    assert.deepEqual(await a.json(), await b.json());
    assert.equal(app.runtime.snapshot().cached, 0);
});
test('aborting one subscriber leaves the other request usable', async () => {
    const app = setup(), a = new AbortController(), b = new AbortController();
    const first = app.window.fetch('/api/list', {signal: a.signal});
    const second = app.window.fetch('/api/list', {signal: b.signal});
    a.abort(); await assert.rejects(first, {name: 'AbortError'});
    assert.equal(app.calls[0].options.signal.aborted, false);
    app.calls[0].resolve(response()); assert.equal((await second).ok, true);
});
test('aborting all foreground subscribers cancels their transport', async () => {
    const app = setup(), controller = new AbortController();
    const task = app.window.fetch('/api/list', {signal: controller.signal});
    controller.abort(); await assert.rejects(task, {name: 'AbortError'}); await tick();
    assert.equal(app.calls[0].options.signal.aborted, true);
    assert.equal(app.runtime.snapshot().pending, 0);
});
test('caller abort still cancels body reads after response headers arrive', async () => {
    const app = setup(), controller = new AbortController();
    const task = app.window.fetch('/api/list', {signal: controller.signal});
    app.calls[0].resolve(new Response(new ReadableStream({start(stream) {stream.enqueue(new TextEncoder().encode('{'));}}), {headers: {'Content-Type': 'application/json'}}));
    const result = await task;
    const body = result.json(); controller.abort();
    await assert.rejects(body, {name: 'AbortError'});
    assert.equal(app.calls[0].options.signal.aborted, true);
});
test('a body abort after shared headers cannot cancel another subscriber body', async () => {
    const app = setup(), controller = new AbortController(); let stream;
    const first = app.window.fetch('/api/list', {signal: controller.signal}), second = app.window.fetch('/api/list');
    app.calls[0].resolve(new Response(new ReadableStream({start(value) {stream = value; stream.enqueue(new TextEncoder().encode('{'));}}), {headers: {'Content-Type': 'application/json'}}));
    const [a, b] = await Promise.all([first, second]); const bodyA = a.json(), bodyB = b.json();
    controller.abort(); await assert.rejects(bodyA, {name: 'AbortError'});
    assert.equal(app.calls[0].options.signal.aborted, false);
    stream.enqueue(new TextEncoder().encode('"data":1}')); stream.close(); assert.deepEqual(await bodyB, {data: 1});
});
test('mutations clear completed and pending warm snapshots', async () => {
    const app = setup(); const warm = app.runtime.preload('/api/list');
    const mutation = app.window.fetch('/api/save', {method: 'POST'});
    const fresh = app.window.fetch('/api/list');
    assert.equal(app.calls.length, 3);
    app.calls[0].resolve(response({status: 'success', data: 'old'}));
    app.calls[1].resolve(response()); app.calls[2].resolve(response({status: 'success', data: 'new'}));
    await Promise.all([warm, mutation]);
    assert.equal((await (await fresh).json()).data, 'new'); assert.equal(app.runtime.snapshot().cached, 0);
});
test('scope and filters have independent identities', async () => {
    const app = setup(); const task = app.runtime.preload('/api/list?organization_key=a&search=x');
    app.calls[0].resolve(response()); await task;
    const other = app.window.fetch('/api/list?organization_key=b&search=x');
    const filtered = app.window.fetch('/api/list?organization_key=a&search=y');
    assert.equal(app.calls.length, 3); app.calls[1].resolve(response()); app.calls[2].resolve(response()); await Promise.all([other, filtered]);
});
test('API errors and non-JSON/login replies are never retained', async () => {
    for (const result of [response({status: 'error', message: 'denied'}), new Response('<html>Login</html>', {headers: {'Content-Type': 'text/html'}}), new Response('{}', {status: 403, headers: {'Content-Type': 'application/json'}})]) {
        const app = setup(); const task = app.runtime.preload('/api/list'); app.calls[0].resolve(result); await task;
        assert.equal(app.runtime.snapshot().cached, 0);
    }
});
test('failed warming releases pending state and permits a retry', async () => {
    const app = setup(); const failed = app.runtime.preload('/api/list'); app.calls[0].reject(new TypeError('offline')); await assert.rejects(failed);
    const retry = app.window.fetch('/api/list'); app.calls[1].resolve(response()); await retry;
    assert.equal(app.calls.length, 2);
});
test('forced reads and custom credentials bypass warm response reuse', async () => {
    const app = setup(); const task = app.runtime.preload('/api/list'); app.calls[0].resolve(response()); await task;
    const forced = app.window.fetch('/api/list?force=1'); app.calls[1].resolve(response()); await forced;
    assert.equal(app.runtime.snapshot().cached, 0);
    const privateRead = app.window.fetch('/api/list', {headers: {'Authorization': 'test'}}); app.calls[2].resolve(response()); await privateRead;
    assert.equal(app.calls.length, 3);
});
test('large warm bodies are discarded under the memory limit', async () => {
    const app = setup(); const task = app.runtime.preload('/api/list'); app.calls[0].resolve(response({data: 'x'.repeat(4 * 1024 * 1024)})); await task;
    assert.equal(app.runtime.snapshot().bytes, 0); assert.equal(app.runtime.snapshot().cached, 0);
});
test('queue checks permissions, prioritizes intent, and limits simultaneous reads', async () => {
    const app = setup(); ['a', 'b', 'c'].forEach(id => app.buttons.set(id, {hidden: id === 'c'}));
    app.runtime.configure({allowedPaths: ['/api/list'], modules: ['a', 'b', 'c'].map(id => ({id, requests: () => [`/api/list?id=${id}`]}))});
    assert.equal(app.calls.length, 0); app.runtime.prioritize('b'); app.idle.shift()(); await tick();
    assert.equal(app.calls.length, 2); assert.match(app.calls[0].url, /id=b/);
    assert.equal(app.calls.some(call => call.url.endsWith('id=c')), false);
    app.calls.forEach(call => call.resolve(response())); await tick(); await tick();
    assert.equal(app.runtime.snapshot().running, 0);
});
test('hidden browser pauses scheduled preloading until visible', async () => {
    const app = setup(); app.buttons.set('a', {hidden: false}); app.document.hidden = true;
    app.runtime.configure({allowedPaths: ['/api/list'], modules: [{id: 'a', requests: () => ['/api/list']}]});
    assert.equal(app.idle.length, 0); assert.equal(app.calls.length, 0);
    app.document.hidden = false; app.events.get('visibilitychange')(); app.idle.shift()(); await tick();
    assert.equal(app.calls.length, 1); app.calls[0].resolve(response()); await tick();
});
test('background queue waits until foreground body downloads finish', async () => {
    const app = setup(); app.buttons.set('a', {hidden: false});
    app.runtime.configure({allowedPaths: ['/api/list', '/api/options'], modules: [{id: 'a', requests: () => ['/api/options']}]});
    let stream;
    const task = app.window.fetch('/api/list');
    app.calls[0].resolve(new Response(new ReadableStream({start(value) {stream = value; stream.enqueue(new TextEncoder().encode('{'));}}), {headers: {'Content-Type': 'application/json'}}));
    const result = await task, body = result.json();
    app.idle.shift()(); await tick(); assert.equal(app.calls.length, 1);
    stream.enqueue(new TextEncoder().encode('"data":1}')); stream.close(); await body;
    app.idle.shift()(); await tick(); assert.equal(app.calls.length, 2);
    app.calls[1].resolve(response()); await tick();
});
test('discarding a foreground HTTP error does not starve background reads', async () => {
    const app = setup(); app.buttons.set('a', {hidden: false});
    app.runtime.configure({allowedPaths: ['/api/list', '/api/options'], modules: [{id: 'a', requests: () => ['/api/options']}]});
    const task = app.window.fetch('/api/list'); app.calls[0].resolve(new Response('temporary failure', {status: 503}));
    assert.equal((await task).ok, false); assert.equal(app.runtime.snapshot().foregroundReaders, 0);
    app.idle.shift()(); await tick(); assert.equal(app.calls.length, 2); app.calls[1].resolve(response()); await tick();
});
test('task status snapshots expire much sooner than list data', async () => {
    const app = setup(); app.runtime.configure({modules: [], allowedPaths: ['/api/task/status', '/api/list']});
    const status = app.runtime.preload('/api/task/status'), list = app.runtime.preload('/api/list');
    app.calls[0].resolve(response()); app.calls[1].resolve(response()); await Promise.all([status, list]);
    app.advance(6000);
    const fresh = app.window.fetch('/api/task/status');
    assert.equal(app.calls.length, 3); app.calls[2].resolve(response()); await fresh;
    assert.equal((await app.window.fetch('/api/list')).ok, true); assert.equal(app.calls.length, 3);
});
