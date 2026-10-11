/* Source timestamps only: opening a page never makes its data fresh. */
(() => {
    function parse(value) {
        if (!value) return NaN;
        let text = String(value).trim();
        if (/^\d{4}-\d\d-\d\d(?:[ T]\d\d:\d\d(?::\d\d(?:\.\d+)?)?)?$/.test(text)) {
            text = text.replace(' ', 'T');
            if (text.length === 10) text += 'T00:00:00';
            text += '+08:00';
        }
        return Date.parse(text);
    }
    function time(value) {
        const stamp = parse(value);
        return Number.isFinite(stamp) ? new Date(stamp).toLocaleString('zh-CN', {
            timeZone: 'Asia/Shanghai', hour12: false,
        }) : '时间未知';
    }
    function render(anchor, values, {hours = 24, scope = '当前范围', action = '请刷新数据', now = Date.now()} = {}) {
        if (typeof anchor === 'string') anchor = document.getElementById(anchor);
        if (!anchor) return;
        const id = `${anchor.id}-freshness`;
        let node = document.getElementById(id);
        if (!node) { node = document.createElement('div'); node.id = id; node.setAttribute('role', 'status'); anchor.after(node); }
        const stamps = values.map(parse);
        const known = stamps.filter(value => Number.isFinite(value) && value <= now + 300000);
        const stale = known.filter(value => now - value > hours * 3600000).length;
        const unknown = stamps.length - known.length;
        const fresh = known.length - stale;
        node.dataset.state = stale ? 'stale' : unknown || !known.length ? 'unknown' : 'fresh';
        node.className = 'data-freshness';
        node.style.cssText = `padding:8px 12px;margin:8px 0;border-radius:8px;font-size:13px;line-height:1.6;white-space:normal;background:${node.dataset.state === 'fresh' ? '#edf8f3' : '#fff3d6'};color:#513d15;`;
        const latest = known.length ? time(new Date(Math.max(...known)).toISOString()) : '未知';
        const oldest = known.length ? time(new Date(Math.min(...known)).toISOString()) : '未知';
        node.textContent = `${scope} · 最近更新 ${latest}（北京时间） · ${hours} 小时内 ${fresh}/${stamps.length} 条`;
        if (stale) node.textContent += ` · ${stale} 条数据已过期（最早 ${oldest}）`;
        if (unknown || !known.length) node.textContent += ` · ${unknown || stamps.length} 条时间未知，时效无法确认`;
        if (stale || unknown || !known.length) node.textContent += `。${action}；历史数据仅供参考。`;
        return {fresh, stale, unknown, total: stamps.length};
    }
    window.DataFreshness = {parse, time, render};
})();
