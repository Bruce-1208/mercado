(() => {
    const summary = document.getElementById('service-health-summary');
    const list = document.getElementById('service-health-list');
    const button = document.getElementById('service-health-refresh');
    if (!summary || !list || !button) return;
    let services = [], loading = false, failed = false;
    function render() {
        let allOk = services.length === 3 && !failed;
        list.replaceChildren();
        for (const service of services) {
            const date = new Date(service.checked_at || NaN);
            const valid = Number.isFinite(date.getTime());
            const stale = !valid || Date.now() - date.getTime() > 120000 || date.getTime() - Date.now() > 60000;
            const status = failed ? '检测失败' : stale ? (valid ? '检测已过期' : '未检测') :
                ({ok: '正常', error: '异常', unknown: '未知'}[service.status] || '未知');
            const ok = status === '正常';
            allOk = allOk && ok;
            const row = document.createElement('div');
            row.className = 'status-line';
            const info = document.createElement('div');
            const name = document.createElement('span');
            name.textContent = service.name;
            const time = document.createElement('small');
            time.textContent = valid ? `检测时间：${date.toLocaleString('zh-CN', {hour12: false})}` : '检测时间：暂无';
            const detail = document.createElement('div');
            detail.textContent = failed ? '无法获取最新状态，请重新检测' : service.detail;
            detail.style.cssText = 'font-size:11px;color:#64748b;margin-top:4px';
            info.append(name, time, detail);
            const badge = document.createElement('strong');
            badge.textContent = status;
            badge.style.color = ok ? '#2d8e59' : '#9a6700';
            row.append(info, badge);
            list.append(row);
        }
        if (!services.length) list.textContent = failed ? '状态检测失败，请重试' : '正在检测各服务…';
        summary.textContent = loading ? '服务状态检测中' : failed ? '服务状态检测失败' : allOk ? '所检服务正常' : '服务状态需关注';
    }
    async function refresh() {
        if (loading) return;
        loading = true;
        button.disabled = true;
        render();
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch('/api/services/health', {cache: 'no-store', signal: controller.signal});
            if (!response.ok) throw new Error('health request failed');
            const payload = await response.json();
            if (payload.status !== 'success' || !Array.isArray(payload.data?.services) || payload.data.services.length !== 3) throw new Error('invalid health response');
            services = payload.data.services;
            failed = false;
        } catch (_) { failed = true; }
        finally { clearTimeout(timer); loading = false; button.disabled = false; render(); }
    }
    button.addEventListener('click', refresh);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    setInterval(() => { render(); if (!document.hidden) refresh(); }, 60000);
    refresh();
})();
