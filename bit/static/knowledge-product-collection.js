(function () {
    'use strict';
    const el = name => document.getElementById(`knowledge-collection-${name}`);
    if (!el('body')) return;
    const state = {active: false, timer: null, task: {}, page: 1, pages: 1, busy: false, epoch: 0, rowsLoading: false};
    const canManage = () => typeof window.hasWorkbenchPermission === 'function' && window.hasWorkbenchPermission('infringement_knowledge.manage');
    const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
    const value = data => data === null || data === undefined || data === '' ? '—' : escape(data);
    const url = text => { try { const parsed = new URL(text); return ['https:', 'http:'].includes(parsed.protocol) ? escape(parsed.href) : ''; } catch (_) { return ''; } };
    async function request(path, body) {
        const response = await fetch(`/api/infringement-knowledge/collection/${path}`, body === undefined ? {} : {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        const payload = await response.json();
        if (!response.ok || payload.status !== 'success') throw new Error(payload.message || '读取失败');
        return payload.data;
    }
    function controls() {
        const running = Boolean(state.task.running);
        ['open', 'confirm', 'stop'].forEach(name => { el(name).hidden = !canManage(); });
        el('open').disabled = state.busy || running;
        el('confirm').disabled = state.busy || !running || state.task.phase !== 'waiting_login';
        el('stop').disabled = state.busy || !running || state.task.phase === 'stopping';
        el('source').disabled = running || state.busy;
        el('prev').disabled = state.rowsLoading || state.page <= 1;
        el('next').disabled = state.rowsLoading || state.page >= state.pages;
    }
    function renderTask(task) {
        const wasRunning = state.task.running;
        state.task = task;
        if (task.running && task.source) el('source').value = task.source;
        el('status').textContent = (task.message || '等待打开网页') + (task.total || task.collected ? ` · ${task.collected || 0} / ${task.total || 0} 件` : '');
        controls();
        if (wasRunning && !task.running) loadRows(state.page);
    }
    async function poll() {
        clearTimeout(state.timer);
        if (!state.active || document.hidden) return;
        try { renderTask(await request('status')); }
        catch (error) { el('status').textContent = `状态读取失败：${error.message}`; }
        if (state.active && !document.hidden) state.timer = setTimeout(poll, state.task.running ? 1500 : 10000);
    }
    async function loadRows(page = 1) {
        const epoch = ++state.epoch;
        state.rowsLoading = true; controls();
        const query = new URLSearchParams({source: el('source').value, search: el('search').value.trim(), page});
        try {
            const data = await request(`products?${query}`);
            if (epoch !== state.epoch) return;
            state.page = data.page; state.pages = data.pages;
            el('body').innerHTML = (data.rows || []).map(row => {
                const image = url(row.main_image_url), link = url(row.product_url);
                const title = link ? `<a href="${link}" target="_blank" rel="noopener noreferrer">${escape(row.title)}</a>` : escape(row.title);
                return `<tr><td><div class="knowledge-collected-product">${image ? `<img src="${image}" alt="" loading="lazy" referrerpolicy="no-referrer">` : '<span class="knowledge-image-placeholder">无图片</span>'}<div>${title}<small>${escape(row.product_id)}</small>${row.detail_error ? `<small>${escape(row.detail_error)}</small>` : ''}</div></div></td><td>${value(row.sales)}</td><td>${value(row.price)} ${escape(row.currency)}</td><td>${value(row.brand)}</td><td>${value(row.listed_at)}</td><td title="${escape(row.category_path)}">${value(row.subcategory)}</td><td>${escape(row.source_name)}<small>${escape(row.site)} · ${escape(row.storage_mode)}</small></td></tr>`;
            }).join('') || '<tr><td colspan="7" class="empty-state">暂无已采集产品。打开网页并确认登录后开始读取。</td></tr>';
            el('summary').textContent = `已保存 ${data.total} 件 · 每页 50 件`;
            el('page').textContent = `第 ${data.page} / ${data.pages} 页`;
        } catch (error) {
            if (epoch === state.epoch) el('body').innerHTML = `<tr><td colspan="7" class="empty-state">读取失败：${escape(error.message)}</td></tr>`;
        } finally { if (epoch === state.epoch) { state.rowsLoading = false; controls(); } }
    }
    async function action(name) {
        if (state.busy || !canManage()) return;
        state.busy = true; controls();
        try { renderTask(await request(name, {source: el('source').value})); }
        catch (error) { el('status').textContent = error.message; }
        finally { state.busy = false; controls(); poll(); }
    }
    function activate(active) {
        state.active = Boolean(active); clearTimeout(state.timer); controls();
        if (active) { poll(); loadRows(state.page); }
    }
    el('open').addEventListener('click', () => action('open'));
    el('confirm').addEventListener('click', () => action('confirm-login'));
    el('stop').addEventListener('click', () => action('stop'));
    el('refresh').addEventListener('click', () => { poll(); loadRows(state.page); });
    el('source').addEventListener('change', () => loadRows(1));
    el('filter').addEventListener('submit', event => { event.preventDefault(); loadRows(1); });
    el('prev').addEventListener('click', () => loadRows(Math.max(1, state.page - 1)));
    el('next').addEventListener('click', () => loadRows(Math.min(state.pages, state.page + 1)));
    document.addEventListener('visibilitychange', () => { clearTimeout(state.timer); if (state.active && !document.hidden) poll(); });
    window.KnowledgeProductCollection = {activate};
    controls();
})();
