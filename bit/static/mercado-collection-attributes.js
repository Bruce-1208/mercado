/* Read-only checks start automatically after each collection list refresh. */
(function () {
    'use strict';
    let generation = 0;
    let controller = null;

    function paint(host, result) {
        host.replaceChildren();
        const tag = document.createElement('span');
        tag.className = `market-status-tag ${result.status === 'complete' ? 'ok' : 'partial'}`;
        tag.textContent = result.message || '属性暂未核验';
        host.append(tag);
        const details = document.createElement('div');
        details.className = host.closest('.detail-shell') ? 'detail-muted' : 'market-collection-quality';
        if (result.category_id) {
            const scope = document.createElement('span');
            scope.className = 'market-metric-note';
            scope.textContent = `按采集类目 ${result.category_id} 核验`;
            details.append(scope);
        }
        for (const entry of [...(result.missing || []), ...(result.pending || [])]) {
            const line = document.createElement('div');
            line.textContent = `${entry.scope}${entry.sku ? `（${entry.sku}）` : ''}：${entry.reason || '缺少'} · ${entry.name}${entry.attribute_id ? ` (${entry.attribute_id})` : ''}`;
            details.append(line);
        }
        if (details.childElementCount) host.append(details);
        host.dataset.status = result.status;
        const status = host.parentElement.querySelector('[data-collection-data-status]');
        if (status && status.dataset.collectionDataStatus === 'checking') {
            status.textContent = result.status === 'complete' ? '已采集资料可用' : result.status === 'missing' ? '资料待补充' : '属性待核验';
            status.className = `market-status-tag ${result.status === 'complete' ? 'ok' : 'partial'}`;
        }
    }

    async function start(rows, summary) {
        const current = ++generation;
        controller?.abort();
        controller = new AbortController();
        const signal = controller.signal;
        const ids = [...new Set((rows || []).map(row => Number(row.id)).filter(id => Number.isSafeInteger(id) && id > 0))];
        if (summary) summary.hidden = !ids.length;
        const results = new Map();
        const updateSummary = () => {
            if (!summary) return;
            const count = status => [...results.values()].filter(result => result.status === status).length;
            summary.textContent = `本页必填属性：缺失 ${count('missing')} 件 · 齐全 ${count('complete')} 件 · 待确认/暂未核验 ${count('pending') + count('unavailable')} 件 · 核验中 ${ids.length - results.size} 件`;
        };
        updateSummary();
        const batches = [];
        for (let i = 0; i < ids.length; i += 5) batches.push(ids.slice(i, i + 5));
        async function worker() {
            while (batches.length && current === generation) {
                const batch = batches.shift();
                const requestController = new AbortController();
                const abort = () => requestController.abort();
                signal.addEventListener('abort', abort, {once: true});
                const timer = setTimeout(abort, 60000);
                let incoming;
                try {
                    const response = await fetch(`/api/mercado-collection/attribute-check?ids=${batch.join(',')}`, {
                        signal: requestController.signal, cache: 'no-store',
                    });
                    const payload = await response.json();
                    if (!response.ok || payload.status !== 'success' || !Array.isArray(payload.data?.results)) throw new Error('check unavailable');
                    incoming = payload.data.results;
                } catch (_) {
                    incoming = [];
                } finally {
                    clearTimeout(timer);
                    signal.removeEventListener('abort', abort);
                }
                if (current !== generation || signal.aborted) return;
                for (const id of batch) {
                    const result = incoming.find(item => Number(item.id) === id) || {
                        id, status: 'unavailable', message: '属性暂未核验，请稍后刷新自动重试',
                    };
                    results.set(id, result);
                    document.querySelectorAll(`[data-collection-attribute-check="${id}"]`).forEach(host => paint(host, result));
                }
                updateSummary();
            }
        }
        await Promise.all([worker(), worker()]);
    }

    async function detail(host, signal) {
        if (!host) return;
        const id = Number(host.dataset.collectionAttributeCheck);
        const requestController = new AbortController();
        const abort = () => requestController.abort();
        signal?.addEventListener('abort', abort, {once: true});
        const timer = setTimeout(abort, 60000);
        let result;
        try {
            const response = await fetch(`/api/mercado-collection/attribute-check?ids=${id}`, {
                signal: requestController.signal, cache: 'no-store',
            });
            const payload = await response.json();
            if (!response.ok || payload.status !== 'success') throw new Error('check unavailable');
            result = payload.data?.results?.find(item => Number(item.id) === id);
        } catch (_) {
            result = null;
        } finally {
            clearTimeout(timer);
            signal?.removeEventListener('abort', abort);
        }
        if (!host.isConnected || signal?.aborted) return;
        paint(host, result || {status: 'unavailable', message: '属性暂未核验，请稍后刷新自动重试'});
    }

    window.MercadoCollectionAttributes = {start, detail};
    document.addEventListener('DOMContentLoaded', () => {
        detail(document.querySelector('[data-collection-attribute-detail]'));
    });
})();
