/* Read-only, session-local preloading. Business actions never enter this queue. */
(() => {
    'use strict';
    if (window.ZeshunPerformance) return;
    const nativeFetch = window.fetch.bind(window);
    const warm = new Map(), pending = new Map(), modules = new Map(), queue = [], served = new Set();
    const allowedPaths = new Set();
    const stats = {requests: 0, hits: 0, shared: 0, failures: 0};
    const MAX_BYTES = 12 * 1024 * 1024, MAX_ENTRY = 3 * 1024 * 1024;
    let bytes = 0, epoch = 0, running = 0, scheduled = false, frameRunning = false;
    let foregroundReaders = 0;
    let scopeUrl = value => value;
    const now = () => Date.now();
    function isActive(tabId) {
        if (document.hidden) return false;
        if (tabId) return !!document.getElementById(`tab-${tabId}`)?.classList.contains('active');
        try {
            const frame = window.frameElement;
            if (frame) {
                const panel = frame.closest('.tab-page');
                return !window.parent.document.hidden && (!panel || panel.classList.contains('active'));
            }
        } catch (_) { /* A standalone page can have a cross-origin parent. */ }
        return true;
    }
    function notifyVisibility() {
        document.dispatchEvent(new CustomEvent('zeshun:visibility', {detail: {active: isActive()}}));
        document.querySelectorAll('iframe').forEach(frame => {
            try {
                const child = frame.contentWindow;
                child?.document.dispatchEvent(new child.CustomEvent('zeshun:visibility', {
                    detail: {active: !document.hidden && !!frame.closest('.tab-page.active')},
                }));
            } catch (_) {}
        });
    }
    function remove(key) {
        const entry = warm.get(key);
        if (entry) bytes -= entry.size;
        warm.delete(key);
    }
    function invalidate() {
        epoch += 1;
        warm.clear(); served.clear(); bytes = 0;
    }
    function identity(input, init = {}) {
        const request = input instanceof Request ? input : null;
        const url = new URL(scopeUrl(request ? request.url : String(input)), window.location.href);
        const method = String(init.method || request?.method || 'GET').toUpperCase();
        const headers = new Headers(init.headers || request?.headers);
        const forced = ['1', 'true'].includes(url.searchParams.get('force'));
        const eligible = !forced && method === 'GET' && url.origin === window.location.origin
            && allowedPaths.has(url.pathname) && !headers.has('Authorization') && !headers.has('Range')
            && ![...headers.keys()].some(name => !['accept', 'content-type'].includes(name));
        url.searchParams.sort();
        return {eligible, method, forced, key: `${epoch}|${url.href}|${init.credentials || request?.credentials || 'same-origin'}|${headers.get('Accept') || ''}`};
    }
    async function remember(key, response, generation) {
        if (!response.ok || response.redirected || !response.headers.get('Content-Type')?.includes('json')) return;
        if (Number(response.headers.get('Content-Length')) > MAX_ENTRY) return;
        // Bound each retained body as well as total memory, even without Content-Length.
        const reader = response.clone().body?.getReader();
        if (!reader) return;
        const chunks = []; let size = 0;
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            size += value.byteLength;
            if (size > MAX_ENTRY || generation !== epoch) { void reader.cancel().catch(() => {}); return; }
            chunks.push(value);
        }
        if (generation !== epoch) return;
        const body = new Uint8Array(size); let offset = 0;
        chunks.forEach(chunk => {body.set(chunk, offset); offset += chunk.byteLength;});
        // API errors sometimes use HTTP 200; never retain these as successful warm data.
        try { const payload = JSON.parse(new TextDecoder().decode(body)); if (payload.status === 'error' || payload.success === false) return; }
        catch (_) { return; }
        remove(key);
        for (const [oldKey, entry] of warm) if (entry.expires <= now()) remove(oldKey);
        while (warm.size >= 64 || bytes + size > MAX_BYTES) remove(warm.keys().next().value);
        const lifetime = /\/status(?:\?|\|)/.test(key) ? 5000 : 120000;
        warm.set(key, {body, size, status: response.status, headers: [...response.headers], expires: now() + lifetime});
        bytes += size;
    }
    function abortReason(signal) {return signal?.reason || new DOMException('Aborted', 'AbortError');}
    function normalizeAbort(response, signal) {
        // Parse after text consumption completes: Chromium can otherwise report a
        // JSON syntax error as a ReadableStream controller.close() failure.
        const readText = response.text.bind(response);
        Object.defineProperty(response, 'json', {configurable: true, value: async () => {
            if (signal?.aborted) throw abortReason(signal);
            const text = await readText();
            if (signal?.aborted) throw abortReason(signal);
            try {return JSON.parse(text);} catch (cause) {
                const path = response.url ? new URL(response.url, window.location.href).pathname : '接口';
                const html = /^\s*</.test(text);
                const message = response.redirected ? '接口发生跳转，请确认登录状态后重新加载'
                    : html ? '接口返回了 HTML 页面而非 JSON，请检查服务或代理路由'
                    : '接口返回的 JSON 数据无效';
                const error = new SyntaxError(`${message}（${path}，HTTP ${response.status}）`, {cause});
                error.status = response.status;
                error.url = response.url;
                throw error;
            }
        }});
        // Chromium synthetic Response.json() can turn a stream AbortError into TypeError.
        for (const name of ['json', 'text', 'arrayBuffer', 'blob', 'formData', 'bytes']) {
            if (typeof response[name] !== 'function') continue;
            const read = response[name].bind(response);
            Object.defineProperty(response, name, {value: async (...args) => {
                if (signal?.aborted) throw abortReason(signal);
                try {return await read(...args);} catch (error) {if (signal?.aborted) throw abortReason(signal); throw error;}
            }});
        }
        const clone = response.clone.bind(response);
        Object.defineProperty(response, 'clone', {value: () => {
            const copy = clone();
            for (const name of ['url', 'redirected', 'type']) Object.defineProperty(copy, name, {value: response[name]});
            return normalizeAbort(copy, signal);
        }});
        return response;
    }
    function subscribe(entry, signal, foreground) {
        if (foreground) {entry.consumed = true; foregroundReaders += 1; clearTimeout(entry.timeout);}
        entry.subscribers += 1;
        return new Promise((resolve, reject) => {
            let finished = false, delivered = false, reader, bodyController, priorityHeld = foreground;
            const releasePriority = () => {if (priorityHeld) {priorityHeld = false; foregroundReaders -= 1; schedule();}};
            const finish = () => {
                if (finished) return;
                finished = true; entry.subscribers -= 1;
                signal?.removeEventListener('abort', abort);
                releasePriority();
                if (!entry.subscribers && pending.get(entry.key) === entry) pending.delete(entry.key);
            };
            const abort = () => {
                if (finished) return;
                const error = abortReason(signal);
                if (delivered) bodyController?.error(error); else reject(error);
                if (reader) void reader.cancel().catch(() => {});
                finish();
                if (!entry.subscribers && (!entry.warming || entry.consumed)) entry.controller.abort();
            };
            if (signal?.aborted) { abort(); return; }
            signal?.addEventListener('abort', abort, {once: true});
            entry.promise.then(response => {
                if (finished) return;
                const copy = response.clone();
                if (!foreground || !copy.body) {finish(); resolve(foreground ? normalizeAbort(copy, signal) : copy); return;}
                // Error handlers often discard an HTTP error body without reading it.
                if (!copy.ok || copy.redirected) {
                    releasePriority();
                    if (pending.get(entry.key) === entry) pending.delete(entry.key);
                }
                reader = copy.body.getReader();
                const body = new ReadableStream({
                    start(controller) {bodyController = controller;},
                    async pull(controller) {
                        try {
                            const chunk = await reader.read();
                            if (finished) return;
                            if (chunk.done) {controller.close(); finish();} else controller.enqueue(chunk.value);
                        } catch (error) {if (!finished) {controller.error(error); finish();}}
                    },
                    cancel() {
                        finish(); void reader.cancel().catch(() => {});
                        if (!entry.subscribers && (!entry.warming || entry.consumed)) entry.controller.abort();
                    },
                });
                const result = new Response(body, {status: copy.status, statusText: copy.statusText, headers: copy.headers});
                for (const name of ['url', 'redirected', 'type']) Object.defineProperty(result, name, {value: copy[name]});
                delivered = true; resolve(normalizeAbort(result, signal));
            }, error => {if (!finished) {finish(); reject(error);}});
        });
    }
    function request(input, init = {}, background = false) {
        const id = identity(input, init);
        if ((id.method !== 'GET' && id.method !== 'HEAD') || id.forced) invalidate();
        if (!id.eligible) return nativeFetch(input, init);
        const signal = init.signal || (input instanceof Request ? input.signal : null);
        if (signal?.aborted) return Promise.reject(signal.reason || new DOMException('Aborted', 'AbortError'));
        if (!background) {served.add(id.key); if (served.size > 256) served.delete(served.values().next().value);}
        const cached = warm.get(id.key);
        if (cached && cached.expires > now()) {
            stats.hits += 1;
            if (!background) remove(id.key); // Explicit refreshes always read the server again.
            return subscribe({promise: Promise.resolve(new Response(cached.body, {status: cached.status, headers: cached.headers})),
                subscribers: 0, controller: new AbortController(), warming: background, consumed: false}, signal, !background);
        }
        remove(id.key);
        let entry = pending.get(id.key);
        if (entry) { stats.shared += 1; if (background) entry.warming = true; return subscribe(entry, signal, !background); }
        const controller = new AbortController(), generation = epoch;
        entry = {controller, subscribers: 0, warming: background, consumed: false, key: id.key};
        stats.requests += 1;
        const timeout = background ? window.setTimeout(() => controller.abort(), 20000) : null;
        entry.timeout = timeout;
        entry.promise = nativeFetch(input, {...init, signal: controller.signal}).then(async response => {
            if (entry.warming && !entry.consumed) {
                await remember(id.key, response, generation);
                if (entry.consumed) remove(id.key);
            }
            return response;
        }).finally(() => {clearTimeout(timeout); if (!entry.subscribers && pending.get(id.key) === entry) pending.delete(id.key); schedule();});
        pending.set(id.key, entry);
        return subscribe(entry, signal, !background);
    }
    window.fetch = (input, init) => request(input, init);
    function visible(module) {
        const button = module.button?.() || document.querySelector(`.tab-button[data-tab="${module.id}"]`);
        return !!button && !button.hidden && (!module.permission || module.permission());
    }
    function preloadFrame(frame) {
        if (!frame || frame.dataset.loaded === '1') return Promise.resolve();
        return new Promise(resolve => {
            const done = () => {clearTimeout(timer); frame.removeEventListener('load', done); notifyVisibility(); resolve();};
            const timer = window.setTimeout(done, 30000);
            frame.addEventListener('load', done, {once: true});
            frame.dataset.loaded = '1';
            frame.src = scopeUrl(frame.dataset.src);
        });
    }
    function schedule() {
        if (scheduled || document.hidden || !queue.length) return;
        scheduled = true;
        const run = () => {scheduled = false; pump();};
        if (window.requestIdleCallback) window.requestIdleCallback(run, {timeout: 1500});
        else window.setTimeout(run, 100);
    }
    function pump() {
        if (document.hidden) return;
        if (foregroundReaders) return;
        while (running < 2 && queue.length) {
            const index = queue.findIndex(task => !task.frame || !frameRunning);
            if (index < 0) break;
            const task = queue.splice(index, 1)[0];
            if (!visible(task.module)) continue;
            if (task.url && served.has(identity(scopeUrl(task.url)).key)) continue;
            running += 1;
            if (task.frame) frameRunning = true;
            Promise.resolve().then(() => task.frame ? preloadFrame(task.frame) : request(scopeUrl(task.url), {cache: 'no-store'}, true))
                .catch(() => {stats.failures += 1;})
                .finally(() => {running -= 1; if (task.frame) frameRunning = false; schedule();});
        }
    }
    function prioritize(id) {
        const selected = queue.filter(task => task.module.id === id);
        for (let index = queue.length - 1; index >= 0; index -= 1) if (queue[index].module.id === id) queue.splice(index, 1);
        queue.unshift(...selected); schedule();
    }
    function configure(options) {
        invalidate(); queue.length = 0; modules.clear(); allowedPaths.clear();
        scopeUrl = options.scopeUrl || (value => value);
        (options.allowedPaths || []).forEach(path => allowedPaths.add(path));
        (options.modules || []).forEach(module => {
            modules.set(module.id, module);
            if (!visible(module)) return;
            try {
                (module.requests?.() || []).forEach(url => {
                    const parsed = new URL(url, window.location.href);
                    if (parsed.origin === window.location.origin && allowedPaths.has(parsed.pathname)) queue.push({module, url});
                });
                const frame = module.frame?.();
                if (frame) queue.push({module, frame});
            } catch (_) {stats.failures += 1;}
        });
        schedule();
    }
    document.addEventListener('visibilitychange', () => {notifyVisibility(); schedule();});
    document.addEventListener('pointerover', event => {const id = event.target.closest?.('[data-tab]')?.dataset.tab; if (id) prioritize(id);}, {passive: true});
    document.addEventListener('focusin', event => {const id = event.target.closest?.('[data-tab]')?.dataset.tab; if (id) prioritize(id);});
    window.ZeshunPerformance = {configure, prioritize, invalidate, isActive, notifyVisibility,
        preload: url => request(scopeUrl(url), {cache: 'no-store'}, true),
        snapshot: () => ({...stats, cached: warm.size, bytes, pending: pending.size, queued: queue.length, running, foregroundReaders,
            modules: [...modules.keys()]}),
    };
})();
