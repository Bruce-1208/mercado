/* Refresh only visible listing metrics; never trigger a listing or price mutation. */
(() => {
    let controller;
    function note(value) { return `<span class="market-metric-note">${escapeHtml(value)}</span>`; }
    function paint(row) {
        const tr = storeLinkBody.querySelector(`[data-store-link-row="${Number(row.id)}"]`);
        if (!tr) return;
        const values = {
            markers: storeLinkMarkers(row),
            sales: row.metrics_error || row.sales_error ? note(row.metrics_error || row.sales_error) : storeLinkNumber(row.sold_quantity, 0),
            visits: row.total_visits == null ? note(row.metrics_error || row.visits_error || '正在读取…')
                : storeLinkNumber(row.total_visits, 0) + (row.visits_error ? note(row.visits_error + '；显示上次数据') : ''),
            quality: row.metrics_error || row.quality_error ? note(row.metrics_error || row.quality_error) : storeLinkQuality(row),
            available: row.metrics_error || row.promotions_error ? note(row.metrics_error || row.promotions_error) : storeLinkPromotionDetails(row, true),
            enrolled: row.metrics_error || row.promotions_error ? note(row.metrics_error || row.promotions_error) : storeLinkPromotionDetails(row),
        };
        for (const [key, html] of Object.entries(values)) {
            const cell = tr.querySelector(`[data-store-link-metric="${key}"]`);
            if (cell) cell.innerHTML = html;
        }
    }
    async function load(rows, requestId) {
        controller?.abort();
        controller = new AbortController();
        const signal = controller.signal;
        const current = () => !signal.aborted && requestId === storeLinkLoadRequestId && isStoreLinksTabActive();
        for (let offset = 0; offset < rows.length && current(); offset += 8) {
            const batch = rows.slice(offset, offset + 8);
            const params = new URLSearchParams();
            batch.forEach(row => params.append('id', row.id));
            try {
                const response = await fetch(`/api/store-links/metrics?${params}`, {signal, cache: 'no-store'});
                if (!response.ok) throw new Error('指标读取失败，请刷新重试');
                const payload = await response.json();
                if (payload.status !== 'success' || !Array.isArray(payload.data)) throw new Error('指标读取失败，请刷新重试');
                if (!current()) return;
                for (const metric of payload.data) {
                    const row = rows.find(value => Number(value.id) === Number(metric.id));
                    if (!row) continue;
                    // Failed visit reads must not erase previously successful values.
                    const patch = {...metric};
                    if (patch.total_visits == null && row.total_visits != null) delete patch.total_visits;
                    Object.assign(row, patch);
                    paint(row);
                }
            } catch (error) {
                if (!current()) return;
                batch.forEach(row => {row.metrics_error = '指标读取失败，请刷新重试'; paint(row);});
            }
        }
    }
    window.StoreLinkMetrics = {load, paint};
})();
