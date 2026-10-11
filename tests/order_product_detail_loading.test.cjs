const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../bit/static/order-management-details.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const unescapeHtml = value => value.replaceAll('&quot;', '"').replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&');

function setup({ignoreAbort = false} = {}) {
    const requests = [], timers = new Map(), dialogs = new Map(), closeEvents = [];
    let timerSequence = 0;
    class Element {
        constructor() { this.children = []; this.hidden = false; this.textContent = ''; }
        append(child) { this.children.push(child); }
        querySelector(selector) { return this.children.find(child => '.' + child.className === selector) || null; }
    }
    class ProductBody extends Element {
        constructor() { super(); this.writes = 0; this.scrollTop = 0; this.elements = {}; this.translatable = []; }
        set innerHTML(value) {
            this.html = value;
            this.writes++;
            this.elements = {};
            for (const className of ['order-product-back', 'order-product-retry', 'order-product-translate-retry', 'order-product-description']) {
                if (value.includes(className)) this.elements['.' + className] = new Element();
            }
            if (value.includes('order-product-translation')) {
                const panel = new Element(), status = new Element();
                panel.querySelector = selector => selector === '[role=status]' ? status : null;
                this.elements['.order-product-translation'] = panel;
            }
            if (value.includes('<button')) this.elements.button = new Element();
            this.translatable = [...value.matchAll(/class="order-product-translatable" data-translation-source="([^"]*)"/g)].map(match => {
                const element = new Element();
                element.dataset = {translationSource: unescapeHtml(match[1])};
                return element;
            });
        }
        get innerHTML() { return this.html; }
        querySelector(selector) { return this.elements[selector] || null; }
        querySelectorAll(selector) { return selector === '.order-product-translatable' ? this.translatable : []; }
    }
    class Dialog extends Element {
        constructor() {
            super(); this.open = false; this.events = {}; this.body = new ProductBody(); this.title = new Element(); this.closeButton = new Element();
        }
        querySelector(selector) {
            if (selector === '.order-extra-body') return this.body;
            if (selector === 'h3') return this.title;
            if (selector === 'button' || selector === '.order-detail-close') return this.closeButton;
            return null;
        }
        addEventListener(name, callback) { (this.events[name] ||= []).push(callback); }
        dispatch(name) { (this.events[name] || []).forEach(callback => callback()); }
        showModal() { this.open = true; }
        close() { this.open = false; closeEvents.push(() => this.dispatch('close')); }
    }
    const context = vm.createContext({
        AbortController, URLSearchParams, escapeHtml,
        orderDetailOrderIds: ['order-1'],
        mercadoSafeUrl: value => /^https:\/\//.test(value) ? value : '',
        document: {
            getElementById: id => dialogs.get(id),
            createElement: type => type === 'dialog' ? new Dialog() : new Element(),
            body: {append: dialog => dialogs.set(dialog.id, dialog)}, head: {append() {}},
        },
        setTimeout(callback, delay) { const id = ++timerSequence; timers.set(id, {callback, delay}); return id; },
        clearTimeout(id) { timers.delete(id); },
        fetch(url, options) {
            return new Promise((resolve, reject) => {
                requests.push({url, options, resolve, reject, params: new URLSearchParams(url.split('?')[1])});
                if (!ignoreAbort) options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name: 'AbortError'})));
            });
        },
    });
    vm.runInContext(source, context);
    const respond = (index, data = {}, response = {}) => requests[index].resolve({ok: true, status: 200, json: async () => ({status: 'success', data}), ...response});
    const timeout = delay => {
        const timer = [...timers.values()].find(candidate => candidate.delay === delay);
        assert.ok(timer, `expected ${delay} ms timeout`);
        timer.callback();
    };
    const dialog = () => dialogs.get('order-product-detail');
    return {context, requests, timers, respond, timeout, dialog, closeEvents};
}

function product(id = 'MLM123', title = 'Zapatos para niños') {
    return {
        item: {id, title, currency_id: 'MXN', price: 100, attributes: [{name: 'Color', value_name: 'Azul'}], pictures: [{id: 'p1', secure_url: 'https://example.com/photo.jpg'}]},
        description: {plain_text: 'Descripción completa'}, warnings: [], translations_zh: {}, translation_error: '',
    };
}

test('shows product before translation and adds Chinese without rebuilding content or resetting scroll', async () => {
    const app = setup();
    const opened = app.context.openOrderProductDetail('MLM123');
    assert.equal(app.requests[0].params.get('translate'), '0');
    app.respond(0, product());
    await opened;
    const body = app.dialog().body;
    assert.match(body.innerHTML, /Zapatos para niños/);
    assert.match(body.innerHTML, /photo\.jpg/);
    assert.equal(app.requests.length, 2);
    assert.equal(app.requests[1].params.get('translate'), '1');
    assert.equal(app.requests[1].params.has('refresh'), false);
    const writes = body.writes, nodes = body.translatable;
    body.scrollTop = 320;
    app.respond(1, {...product(), translations_zh: {'Zapatos para niños': '童鞋', 'Color': '颜色', 'Azul': '蓝色'}});
    await tick();
    assert.equal(body.writes, writes);
    assert.equal(body.translatable, nodes);
    assert.equal(body.scrollTop, 320);
    assert.equal(nodes.find(node => node.dataset.translationSource === 'Zapatos para niños').children[0].textContent, '童鞋');
    assert.equal(body.querySelector('.order-product-translation').hidden, true);
    assert.equal(app.timers.size, 0);
});

test('product timeout aborts and retry preserves the original order authorization context', async () => {
    const app = setup();
    const opened = app.context.openOrderProductDetail('MLM123');
    app.timeout(35000);
    await opened;
    assert.equal(app.requests[0].options.signal.aborted, true);
    assert.match(app.dialog().body.innerHTML, /商品资料读取超时/);
    assert.equal(app.timers.size, 0);
    app.context.orderDetailOrderIds = ['other-order'];
    const retry = app.dialog().body.querySelector('button').onclick();
    assert.deepEqual(app.requests[1].params.getAll('order_id'), ['order-1']);
    assert.equal(app.requests[1].params.get('refresh'), '1');
    app.respond(1, product());
    await retry;
    app.respond(2, product());
    await tick();
    assert.equal(app.timers.size, 0);
});

test('switching products cancels the old request and its late response cannot replace the current product', async () => {
    const app = setup({ignoreAbort: true});
    const previous = app.context.openOrderProductDetail('MLM123');
    const current = app.context.openOrderProductDetail('MLM456');
    assert.equal(app.requests[0].options.signal.aborted, true);
    app.respond(1, product('MLM456', 'Current product'));
    await current;
    app.respond(0, product('MLM123', 'Old product'));
    await previous;
    assert.match(app.dialog().body.innerHTML, /Current product/);
    assert.doesNotMatch(app.dialog().body.innerHTML, /Old product/);
    assert.equal(app.requests.length, 3);
    app.respond(2, product('MLM456'));
    await tick();
    assert.equal(app.timers.size, 0);
});

test('close cancels loading and reopening before the queued close event keeps the new request alive', async () => {
    const app = setup({ignoreAbort: true});
    const previous = app.context.openOrderProductDetail('MLM123');
    app.dialog().closeButton.onclick();
    assert.equal(app.requests[0].options.signal.aborted, true);
    const current = app.context.openOrderProductDetail('MLM456');
    app.closeEvents.shift()();
    assert.equal(app.requests[1].options.signal.aborted, false);
    app.respond(0, product('MLM123', 'Old product'));
    await previous;
    assert.match(app.dialog().body.innerHTML, /正在读取商品资料/);
    app.respond(1, product('MLM456', 'Current product'));
    await current;
    app.respond(2, product('MLM456'));
    await tick();
    assert.match(app.dialog().body.innerHTML, /Current product/);
    assert.equal(app.dialog().events.close.length, 1);
    assert.equal(app.timers.size, 0);
});

test('translation error keeps original content and retry requests only translation with safe text insertion', async () => {
    const app = setup();
    const opened = app.context.openOrderProductDetail('MLM123');
    app.respond(0, product());
    await opened;
    const body = app.dialog().body, writes = body.writes;
    app.respond(1, {...product(), translation_error: '翻译服务暂时不可用'});
    await tick();
    assert.match(body.innerHTML, /Zapatos para niños/);
    const panel = body.querySelector('.order-product-translation'), retry = body.querySelector('.order-product-translate-retry');
    assert.equal(panel.querySelector('[role=status]').textContent, '翻译服务暂时不可用');
    assert.equal(retry.hidden, false);
    const retried = retry.onclick();
    assert.equal(app.requests[2].params.get('translate'), '1');
    assert.equal(app.requests[2].params.has('refresh'), false);
    app.respond(2, {...product(), translations_zh: {'Zapatos para niños': '<img src=x onerror=alert(1)>'}});
    await retried;
    assert.equal(body.writes, writes);
    assert.equal(body.translatable[0].children[0].textContent, '<img src=x onerror=alert(1)>');
    assert.equal(panel.hidden, true);
});

test('translation timeout aborts its request without hiding the product', async () => {
    const app = setup();
    const opened = app.context.openOrderProductDetail('MLM123');
    app.respond(0, product());
    await opened;
    const body = app.dialog().body, writes = body.writes;
    app.timeout(40000);
    await tick();
    assert.equal(app.requests[1].options.signal.aborted, true);
    assert.equal(body.writes, writes);
    assert.match(body.querySelector('.order-product-translation').querySelector('[role=status]').textContent, /中文翻译加载超时/);
    assert.equal(body.querySelector('.order-product-translate-retry').hidden, false);
    assert.equal(app.timers.size, 0);
});

test('Escape cancels an in-progress translation and prevents a late translation changing the product', async () => {
    const app = setup({ignoreAbort: true});
    const opened = app.context.openOrderProductDetail('MLM123');
    app.respond(0, product());
    await opened;
    const body = app.dialog().body;
    app.dialog().dispatch('cancel');
    app.dialog().close();
    assert.equal(app.requests[1].options.signal.aborted, true);
    app.respond(1, {...product(), translations_zh: {'Zapatos para niños': '旧翻译'}});
    await tick();
    assert.equal(body.translatable[0].children.length, 0);
    assert.equal(app.timers.size, 0);
});

test('login redirect HTML gives an actionable message and releases the request timeout', async () => {
    const app = setup();
    const opened = app.context.openOrderProductDetail('MLM123');
    app.respond(0, {}, {redirected: true, json: async () => { throw new SyntaxError('Unexpected token <'); }});
    await opened;
    assert.match(app.dialog().body.innerHTML, /登录已失效，请重新登录后重试/);
    assert.doesNotMatch(app.dialog().body.innerHTML, /Unexpected token/);
    assert.equal(app.requests.length, 1);
    assert.equal(app.timers.size, 0);
});

test('non-JSON error responses retain HTTP diagnostics and never start translation', async () => {
    for (const [status, message] of [
        [403, '商品资料访问权限不足（HTTP 403）'],
        [404, '商品详情接口不可用（HTTP 404）'],
        [500, '商品服务异常（HTTP 500）'],
        [502, '商品服务通信异常（HTTP 502）'],
        [503, '商品服务通信异常（HTTP 503）'],
        [504, '商品服务通信异常（HTTP 504）'],
        [429, '商品资料读取失败（HTTP 429）'],
        [401, '登录已失效，请重新登录后重试'],
        [200, '服务器暂时未返回商品资料，请重试'],
    ]) {
        const app = setup();
        const opened = app.context.openOrderProductDetail('MLM123');
        app.respond(0, {}, {ok: status < 400, status, json: async () => { throw new SyntaxError('Unexpected token <'); }});
        await opened;
        assert.ok(app.dialog().body.innerHTML.includes(message), `HTTP ${status} must show ${message}`);
        assert.doesNotMatch(app.dialog().body.innerHTML, /Unexpected token/);
        assert.equal(app.requests.length, 1);
        assert.equal(app.timers.size, 0);
    }
});

test('structured API errors preserve their specific backend explanation', async () => {
    const app = setup();
    const opened = app.context.openOrderProductDetail('MLM123');
    app.respond(0, {}, {ok: false, status: 502, json: async () => ({status: 'error', message: '平台商品资料访问被拒绝，请重新授权店铺'})});
    await opened;
    assert.match(app.dialog().body.innerHTML, /平台商品资料访问被拒绝，请重新授权店铺/);
    assert.doesNotMatch(app.dialog().body.innerHTML, /商品服务通信异常/);
    assert.equal(app.requests.length, 1);
    assert.equal(app.timers.size, 0);
});

test('fast stage displays SKU content while description loads and patches only description later', async () => {
    const app = setup();
    const opened = app.context.openOrderProductDetail('MLM123');
    assert.equal(app.requests[0].params.get('description'), '0');
    app.respond(0, {...product(), description: {}, description_pending: true});
    await opened;
    const body = app.dialog().body, writes = body.writes;
    assert.match(body.innerHTML, /商品描述加载中/);
    assert.match(body.innerHTML, /全部 SKU/);
    body.scrollTop = 240;
    app.respond(1, {...product(), translation_error: '模型暂不可用'});
    await tick();
    assert.equal(body.writes, writes);
    assert.equal(body.scrollTop, 240);
    assert.match(body.querySelector('.order-product-description').innerHTML, /Descripción completa/);
});

test('cached translated detail does not issue another translation request', async () => {
    const app = setup();
    const opened = app.context.openOrderProductDetail('MLM123');
    app.respond(0, {...product(), translations_ready: true, translations_zh: {'Zapatos para niños':'童鞋'}});
    await opened;
    assert.equal(app.requests.length, 1);
    assert.match(app.dialog().body.innerHTML, /童鞋/);
    assert.equal(app.dialog().body.querySelector('.order-product-translation').hidden, true);
});
