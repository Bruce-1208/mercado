(() => {
    const root = document.getElementById('video-upload-logs');
    if (!root) return;
    const form = document.getElementById('video-upload-log-filters');
    const body = document.getElementById('video-upload-log-rows');
    const message = document.getElementById('video-upload-log-message');
    let page = 1, pages = 1, loaded = false, controller = null;
    const date = value => {
        if (!value) return '—';
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false});
    };
    const safeUrl = value => {
        try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; }
        catch { return ''; }
    };
    function cell(tr, value) {
        const td = document.createElement('td');
        td.textContent = value || '—'; tr.append(td); return td;
    }
    function small(td, value) {
        if (!value) return;
        const el = document.createElement('small'); el.textContent = value; td.append(el);
    }
    function render(rows) {
        body.replaceChildren();
        if (!rows.length) { const tr = document.createElement('tr'); cell(tr, '暂无符合条件的上传日志').colSpan = 9; body.append(tr); }
        for (const row of rows) {
            const tr = document.createElement('tr');
            const imageCell = cell(tr, '暂无主图');
            const imageUrl = safeUrl(row.thumbnail_url);
            if (imageUrl) {
                const img = document.createElement('img'); img.src = imageUrl; img.alt = row.title || '链接主图'; img.loading = 'lazy';
                img.addEventListener('error', () => { imageCell.textContent = '主图不可用'; });
                imageCell.replaceChildren(img);
            }
            const product = cell(tr, row.title || '历史记录未记录链接名');
            const url = safeUrl(row.permalink);
            if (url) { const a = document.createElement('a'); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.textContent = row.title || row.item_id || '查看链接'; product.replaceChildren(a); }
            small(product, `${row.item_id || '—'}${row.link_id ? ` · 链接编号 ${row.link_id}` : ''}`);
            cell(tr, row.salesperson || '历史记录未记录');
            cell(tr, date(row.uploaded_at || row.published_at || row.started_at));
            const store = cell(tr, row.store_name); small(store, `${row.site_id || '—'} · 店铺编号 ${row.token_id || '—'}`);
            cell(tr, row.group_name || '未分组');
            const video = cell(tr, row.video_name || '已有视频 / 历史视频');
            if (row.job_id) { const a = document.createElement('a'); a.href = `/ai-videos/${encodeURIComponent(row.job_id)}/details`; a.textContent = row.video_name || '查看视频明细'; video.replaceChildren(a); }
            const status = cell(tr, row.status_label || row.status);
            if (row.status === 'failed') status.className = 'upload-failed';
            const rawError = row.error || (row.status === 'failed' ? row.message : '');
            if (rawError) {
                const conciseChineseError = /[\u4e00-\u9fff]/.test(rawError) && rawError.length <= 160 && !/[{}\n]/.test(rawError);
                small(status, conciseChineseError ? rawError : /400|not.found|invalid|inactive/i.test(rawError) ? '平台拒绝上传：请核对商品是否在售、归属店铺与站点。生成成功不代表上传成功。' : '上传或审核失败，请查看详情并核实后重试。');
                const details = document.createElement('details');
                const summary = document.createElement('summary'); summary.textContent = '原始错误详情';
                const pre = document.createElement('pre'); pre.textContent = rawError;
                details.append(summary, pre); status.append(details);
            }
            const clip = cell(tr, row.clip_uuid); small(clip, row.checked_at ? date(row.checked_at) : '尚未同步审核状态');
            body.append(tr);
        }
    }
    function pagination(busy = false) {
        document.getElementById('video-upload-log-prev').disabled = busy || page <= 1;
        document.getElementById('video-upload-log-next').disabled = busy || page >= pages;
        document.getElementById('video-upload-log-page').textContent = `第 ${page} / ${pages} 页`;
    }
    async function load(force = false, requested = page) {
        if (!force && (loaded || controller)) return;
        controller?.abort();
        const current = new AbortController(); controller = current;
        pagination(true); message.textContent = '正在读取上传日志…';
        const values = new FormData(form);
        const params = new URLSearchParams({page: String(requested), limit: '20'});
        for (const key of ['search', 'salesperson']) if (values.get(key)?.trim()) params.set(key, values.get(key).trim());
        if (root.dataset.jobId) params.set('job_id', root.dataset.jobId);
        const boundary = (value, next = false) => {
            const d = new Date(`${value}T00:00:00+08:00`); if (next) d.setTime(d.getTime() + 86400000); return d.toISOString();
        };
        if (values.get('from')) params.set('uploaded_from', boundary(values.get('from')));
        if (values.get('to')) params.set('uploaded_before', boundary(values.get('to'), true));
        try {
            const response = await fetch(`/api/ai-videos/upload-logs?${params}`, {signal: current.signal});
            const payload = await response.json();
            if (!response.ok || payload.status !== 'success') throw new Error(payload.message || '读取上传日志失败');
            if (controller !== current) return;
            const data = payload.data || {};
            page = Number(data.page) || 1; pages = Number(data.total_pages) || 1;
            render(data.rows || []); loaded = true;
            document.getElementById('video-upload-log-summary').textContent = `共 ${Number(data.total) || 0} 条上传日志`;
            message.textContent = data.total ? '生成 → 上传受理 → 平台审核；生成完成不代表已发布。待审核记录每天自动同步，时间为北京时间。' : '暂无符合条件的上传日志';
        } catch (error) {
            if (error.name !== 'AbortError') { loaded = false; body.replaceChildren(); message.textContent = error.message || '读取上传日志失败'; }
        } finally { if (controller === current) { controller = null; pagination(); } }
    }
    form.addEventListener('submit', event => { event.preventDefault(); load(true, 1); });
    form.addEventListener('reset', () => setTimeout(() => load(true, 1), 0));
    window.VideoUploadLogs = {load, go: direction => load(true, Math.max(1, Math.min(pages, page + direction)))};
    if (root.dataset.jobId || document.getElementById('tab-ai-video')?.classList.contains('active')) load();
})();
