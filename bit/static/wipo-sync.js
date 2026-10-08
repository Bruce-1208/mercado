(function (root) {
    'use strict';
    const API = '/api/infringement-knowledge/wipo';
    const STATUS_LABELS = {idle:'等待同步', running:'正在同步', success:'同步成功', continuing:'等待续传', error:'同步失败', failed:'同步失败', interrupted:'同步已中断，进度已保留', disabled:'自动更新已关闭'};
    const COUNT_LABELS = {fetched:'拉取', unique:'去重后', cached_download_bytes:'已缓存字节', archive_bytes:'文件总字节', processed:'已处理', total:'本批品牌', inserted:'新增黑名单', updated:'更新黑名单', historical_blacklisted:'历史转黑名单', unchanged:'未变化', restored:'恢复', skipped_manual:'保留人工', skipped_deleted:'保留删除'};

    function chinaToday(now = new Date()) {
        const parts = new Intl.DateTimeFormat('en-US', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit'}).formatToParts(now);
        const value = type => parts.find(part => part.type === type).value;
        return `${value('year')}-${value('month')}-${value('day')}`;
    }
    function timeLabel(value) {
        if (!value) return '暂无';
        const raw = String(value);
        if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw)) return raw.replace('T', ' ');
        const date = new Date(raw);
        return Number.isFinite(date.getTime())
            ? date.toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false}) : raw;
    }
    function summaryLabel(summary) {
        if (!summary || !Object.keys(summary).length) return '尚无同步结果';
        return Object.entries(COUNT_LABELS).filter(([key]) => !['cached_download_bytes', 'archive_bytes'].includes(key) || Object.hasOwn(summary, key)).map(([key, label]) => {
            const value = Number(summary[key] || 0);
            return `${label} ${Number.isFinite(value) && value >= 0 ? value.toLocaleString('zh-CN') : '—'}`;
        }).join(' · ');
    }
    function validatedSettings(values, forSync = false, today = chinaToday()) {
        const interval = Number(values.interval_hours);
        if (!Number.isInteger(interval) || interval < 1 || interval > 720) throw new Error('更新间隔须为 1 至 720 小时的整数。');
        if (!['madrid', 'feed'].includes(values.source_mode)) throw new Error('请选择有效的同步来源。');
        if (!['auto', 'json', 'csv'].includes(values.format)) throw new Error('请选择有效的数据格式。');
        const feed = String(values.feed_url || '').trim();
        const fromDate = String(values.madrid_from_date || '').trim();
        if (values.source_mode === 'madrid') {
            if (!/^\d{4}-\d{2}-\d{2}$/.test(fromDate) || Number.isNaN(Date.parse(fromDate)) || new Date(`${fromDate}T00:00:00Z`).toISOString().slice(0, 10) !== fromDate) {
                throw new Error('请选择有效的首次拉取起始日期。');
            }
            if (fromDate > today) throw new Error('首次拉取起始日期不能晚于今天。');
        }
        if (values.source_mode === 'feed' && (feed || values.enabled || forSync)) {
            let url;
            try { url = new URL(feed); } catch (_) { throw new Error('请填写有效的授权数据源地址。'); }
            if (url.protocol !== 'https:' || url.username || url.password || url.hash || url.port && url.port !== '443') throw new Error('数据源地址须使用 HTTPS 默认端口，不带片段；认证请填写 Bearer 令牌。');
        }
        const result = {source_mode:values.source_mode, madrid_from_date:fromDate, enabled:Boolean(values.enabled), feed_url:feed, format:values.format, interval_hours:interval};
        const token = String(values.token || '').trim();
        if (token && values.source_mode === 'feed') result.token = token;
        return result;
    }

    function boot(document, window) {
        const panel = document.getElementById('wipo-sync-panel');
        if (!panel) return;
        const get = name => document.getElementById(`wipo-sync-${name}`);
        const form = get('form');
        const fields = ['source-mode', 'madrid-from-date', 'feed-url', 'format', 'interval', 'enabled', 'token'];
        const state = {active:false, running:false, saving:false, starting:false, loadingSettings:false, loadingStatus:false, settingsLoaded:false, settings:null, timer:null, wasRunning:false, seenStatus:false, lastFinished:null, statusEpoch:0};
        const canManage = () => typeof window.hasWorkbenchPermission === 'function' && window.hasWorkbenchPermission('infringement_knowledge.manage');
        const visible = () => state.active && !document.hidden;
        function error(message = '') { get('error').textContent = message; get('error').hidden = !message; }
        function stopPoll() { if (state.timer !== null) window.clearTimeout(state.timer); state.timer = null; }
        function schedulePoll() {
            stopPoll();
            if (visible() && state.running) state.timer = window.setTimeout(loadStatus, 2500);
        }
        function showSourceFields() {
            const madrid = get('source-mode').value === 'madrid';
            get('madrid-fields').hidden = !madrid;
            get('feed-fields').hidden = madrid;
        }
        function controls() {
            const permitted = canManage();
            form.hidden = !permitted;
            const busy = state.saving || state.starting || state.running || state.loadingSettings;
            fields.forEach(name => { get(name).disabled = !permitted || busy; });
            get('save').disabled = !permitted || busy || !state.settingsLoaded;
            get('start').disabled = !permitted || busy || !state.settingsLoaded;
            get('start').textContent = state.starting ? '正在启动…' : state.running ? '同步进行中…' : '保存并立即同步';
            get('save').textContent = state.saving ? '正在保存…' : '保存配置';
            get('refresh').disabled = state.loadingStatus;
        }
        async function request(path, options = {}) {
            const response = await window.fetch(`${API}/${path}`, {cache:'no-store', ...options});
            const payload = await response.json();
            if (!response.ok || payload.status !== 'success') throw new Error(payload.message || `接口请求失败：${response.status}`);
            return payload.data || {};
        }
        function renderSettings(settings) {
            state.settings = settings;
            const madrid = settings.source_mode !== 'feed';
            get('source-mode').value = madrid ? 'madrid' : 'feed';
            get('madrid-from-date').value = settings.madrid_from_date || '';
            get('madrid-from-date').max = chinaToday();
            get('feed-url').value = settings.feed_url || '';
            get('format').value = ['auto', 'json', 'csv'].includes(settings.format) ? settings.format : 'auto';
            get('interval').value = String(settings.interval_hours || 24);
            get('enabled').checked = Boolean(settings.enabled);
            get('token-hint').textContent = settings.has_token ? '已保存令牌；留空将保留，保存后输入框清空' : '尚未配置令牌';
            get('settings-summary').textContent = `${settings.enabled ? '自动更新已开启' : '自动更新已关闭'} · 每 ${settings.interval_hours || 24} 小时 · ${madrid ? '官方马德里商标每日增量' : '授权数据源'}`;
            showSourceFields();
            controls();
        }
        function refreshKnowledge() {
            if (typeof window.loadInfringementKnowledge === 'function') window.loadInfringementKnowledge(true);
        }
        function renderStatus(data) {
            const finished = data.finished_at || null;
            const running = Boolean(data.running);
            const completed = !running && (state.wasRunning || (state.seenStatus && finished && finished !== state.lastFinished));
            state.running = running;
            if (running) state.wasRunning = true;
            else if (completed) { state.wasRunning = false; refreshKnowledge(); }
            state.seenStatus = true;
            state.lastFinished = finished;
            get('status').textContent = data.message || STATUS_LABELS[data.status] || '等待同步';
            get('summary').textContent = summaryLabel(data.summary);
            get('times').textContent = `本次开始：${timeLabel(data.started_at)} · 本次完成：${timeLabel(data.finished_at)}\n最近成功：${timeLabel(data.last_success_at)} · 下次自动更新：${timeLabel(data.next_run_at)}`;
            get('log').textContent = Array.isArray(data.logs) && data.logs.length ? data.logs.map(log => typeof log === 'string' ? log : log?.message || JSON.stringify(log)).join('\n') : '同步尚未启动';
            get('log').scrollTop = get('log').scrollHeight;
            const history = get('history');
            history.replaceChildren();
            if (!Array.isArray(data.history) || !data.history.length) history.textContent = '暂无同步记录';
            else data.history.slice(0, 10).forEach(record => {
                const item = document.createElement('p');
                item.textContent = `${timeLabel(record.finished_at || record.started_at)} · ${STATUS_LABELS[record.status] || record.status || '同步记录'} · ${summaryLabel(record.summary)}${record.message ? `\n${record.message}` : ''}`;
                history.appendChild(item);
            });
            if (data.source) get('source').textContent = `数据来源：${data.source}`;
            if (data.disclaimer) get('disclaimer').textContent = String(data.disclaimer);
            controls();
            schedulePoll();
        }
        async function loadSettings() {
            if (state.loadingSettings || state.settingsLoaded) return;
            state.loadingSettings = true;
            controls();
            try {
                const settings = await request('settings');
                state.settingsLoaded = true;
                renderSettings(settings);
            } catch (cause) { error(`同步配置读取失败：${cause.message || cause}`); }
            finally { state.loadingSettings = false; controls(); }
        }
        async function loadStatus() {
            stopPoll();
            if (state.loadingStatus) return;
            const epoch = state.statusEpoch;
            state.loadingStatus = true;
            controls();
            try {
                const data = await request('status');
                if (epoch === state.statusEpoch) renderStatus(data);
            } catch (cause) {
                if (epoch === state.statusEpoch) get('status').textContent = `状态读取失败：${cause.message || cause}`;
                schedulePoll();
            } finally { state.loadingStatus = false; controls(); }
        }
        function formValues() {
            return {source_mode:get('source-mode').value, madrid_from_date:get('madrid-from-date').value, enabled:get('enabled').checked, feed_url:get('feed-url').value, format:get('format').value, interval_hours:get('interval').value, token:get('token').value};
        }
        async function writeSettings(forSync = false) {
            const settings = validatedSettings(formValues(), forSync);
            const data = await request('settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(settings)});
            get('token').value = '';
            renderSettings(data);
            return data;
        }
        async function saveSettings(event) {
            if (event) event.preventDefault();
            if (!canManage() || state.saving || state.starting || state.running || !state.settingsLoaded) return;
            state.saving = true; error(); controls();
            try {
                await writeSettings();
                get('status').textContent = '配置已保存';
                await loadStatus();
            } catch (cause) { error(`配置保存失败：${cause.message || cause}`); }
            finally { state.saving = false; controls(); }
        }
        async function start() {
            if (!canManage() || state.saving || state.starting || state.running || !state.settingsLoaded) return;
            state.starting = true; error(); controls();
            try {
                await writeSettings(true);
                state.statusEpoch += 1;
                const data = await request('sync', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
                state.wasRunning = true;
                renderStatus(data);
            } catch (cause) { error(`同步启动失败：${cause.message || cause}`); }
            finally { state.starting = false; controls(); }
        }
        function activate(active) {
            state.active = Boolean(active);
            stopPoll();
            if (!visible()) return;
            controls();
            error();
            loadSettings();
            loadStatus();
        }
        form.addEventListener('submit', saveSettings);
        get('start').addEventListener('click', start);
        get('refresh').addEventListener('click', () => { error(); loadSettings(); loadStatus(); });
        get('source-mode').addEventListener('change', showSourceFields);
        document.addEventListener('visibilitychange', () => { stopPoll(); if (visible()) loadStatus(); });
        controls();
        const tab = document.getElementById('tab-infringement-knowledge');
        activate(Boolean(tab && tab.classList.contains('active') && !document.getElementById('knowledge-sources-panel')?.hidden));
        return {activate, loadSettings, loadStatus, saveSettings, start, state};
    }
    const exports = {boot, validatedSettings, summaryLabel, timeLabel, chinaToday};
    if (typeof module === 'object' && module.exports) module.exports = exports;
    if (root && root.document) root.WipoSync = boot(root.document, root);
})(typeof window !== 'undefined' ? window : null);
