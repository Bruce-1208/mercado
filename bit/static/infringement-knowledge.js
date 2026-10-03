(function () {
    'use strict';
    const get = id => document.getElementById(id);
    const el = name => get(`infringement-knowledge-${name}`);
    if (!el('body')) return;
    const API = '/api/infringement-knowledge';
    const labels = {pending: '待复核', blacklist: '风险拦截', whitelist: '人工放行'};
    const sources = {manual: '人工维护', analysis: 'AI 提取', wipo: 'WIPO', wikidata: 'Wikidata'};
    const state = {active: false, panel: 'records', loaded: false, loading: false, rows: [], page: 1, pages: 1, total: 0, epoch: 0, saving: false, deleting: false, analysisRunning: false, starting: false, statusLoading: false, statusEpoch: 0, timer: null, lastFocus: null};
    const canManage = () => typeof window.hasWorkbenchPermission === 'function' && window.hasWorkbenchPermission('infringement_knowledge.manage');
    const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
    const number = value => Number(value || 0).toLocaleString('zh-CN');
    const source = row => String(row.source_detail || '').startsWith('global_ip:') && row.source_type !== 'manual' ? 'wikidata' : row.source_type || 'manual';
    const type = row => source(row) === 'manual' && labels[row.list_type] ? row.list_type : 'pending';
    const visible = () => state.active && state.panel === 'sources' && !document.hidden;
    function feedback(message = '', error = false) {
        const box = get('knowledge-feedback');
        box.textContent = message; box.hidden = !message; box.classList.toggle('error', error);
    }
    async function request(path = '', options = {}) {
        const response = await fetch(`${API}${path}`, {cache: 'no-store', ...options});
        const payload = await response.json();
        if (!response.ok || payload.status !== 'success') {
            const error = new Error(payload.message || `请求失败（${response.status}）`);
            error.status = response.status; error.data = payload.data; throw error;
        }
        return payload.data || {};
    }
    function pageControls() {
        get('knowledge-prev-page').disabled = state.loading || state.page <= 1;
        get('knowledge-next-page').disabled = state.loading || state.page >= state.pages;
        get('knowledge-page-number').textContent = `第 ${state.page} / ${state.pages} 页`;
        get('knowledge-page-summary').textContent = state.total ? `第 ${(state.page - 1) * 50 + 1}–${Math.min(state.page * 50, state.total)} 条，共 ${number(state.total)} 条` : '每页 50 条';
    }
    function render(data) {
        const summary = data.summary || {};
        for (const key of ['total', 'pending', 'blacklist', 'whitelist']) el(key).textContent = number(summary[key]);
        const filter = el('type-filter').value;
        document.querySelectorAll('[data-knowledge-filter]').forEach(button => {
            button.classList.toggle('selected', button.dataset.knowledgeFilter === filter);
            button.setAttribute('aria-pressed', String(button.dataset.knowledgeFilter === filter));
        });
        el('result-count').textContent = `筛选结果 ${number(state.total)} 条 · 全库 ${number(summary.total)} 条`;
        if (!state.rows.length) {
            el('body').innerHTML = '<tr><td class="empty-state" colspan="6"><strong>暂无符合条件的品牌</strong><br>可调整筛选条件，或在「数据来源与同步」中收集线索。</td></tr>';
        } else {
            el('body').innerHTML = state.rows.map(row => {
                const status = type(row), origin = source(row), id = Number(row.id);
                const count = Number(row.evidence_count || 0);
                return `<tr><td><span class="knowledge-brand">${escape(row.brand_name)}</span></td>
                    <td><span class="knowledge-list-badge ${status}">${labels[status]}</span></td>
                    <td title="${escape(row.source_detail)}">${escape(sources[origin] || '其他来源')}<span class="knowledge-source-detail">${count > 0 ? `${number(count)} 条参考样本` : origin === 'manual' ? '人工决定' : '外部名称线索'}</span></td>
                    <td><span class="knowledge-notes" title="${escape(row.notes)}">${escape(row.notes || '尚未填写处理依据')}</span></td>
                    <td>${escape(row.updated_at || '—')}</td>
                    <td><div class="knowledge-row-actions"><button type="button" data-edit="${id}">${canManage() ? status === 'pending' ? '复核' : '编辑' : '详情'}</button>${canManage() ? `<button class="knowledge-delete" type="button" data-delete="${id}">删除</button>` : ''}</div></td></tr>`;
            }).join('');
        }
        pageControls();
    }
    async function load(force = false, page = state.page) {
        if (state.loaded && !force) return;
        const epoch = ++state.epoch;
        state.loading = true;
        el('body').setAttribute('aria-busy', 'true');
        el('body').innerHTML = '<tr><td class="empty-state" colspan="6">正在读取品牌记录…</td></tr>';
        el('result-count').textContent = '正在读取品牌记录…';
        pageControls();
        const params = new URLSearchParams({page: String(page), page_size: '50'});
        for (const [param, id] of [['list_type', 'type-filter'], ['source_type', 'source-filter'], ['search', 'search']]) {
            const value = el(id).value.trim();
            if (value) params.set(param, value);
        }
        try {
            const data = await request(`?${params}`);
            if (epoch !== state.epoch) return;
            state.rows = Array.isArray(data.rows) ? data.rows : [];
            state.page = Number(data.page || page);
            state.total = Number(data.filtered_total || 0);
            state.pages = Math.max(1, Number(data.pages || Math.ceil(state.total / 50)));
            state.loaded = true;
            state.loading = false;
            render(data);
        } catch (error) {
            if (epoch !== state.epoch) return;
            state.loaded = false; state.rows = []; state.loading = false;
            el('result-count').textContent = '列表读取失败，请重试';
            el('body').innerHTML = `<tr><td class="empty-state" colspan="6">${escape(error.message)}<br><button class="secondary" type="button" data-retry>重新加载</button></td></tr>`;
            get('knowledge-prev-page').disabled = true; get('knowledge-next-page').disabled = true;
        } finally {
            if (epoch === state.epoch) el('body').setAttribute('aria-busy', 'false');
        }
    }
    function search() { return load(true, 1); }
    function filter(value) { el('type-filter').value = value; showPanel('records'); return search(); }
    function reset() { el('type-filter').value = ''; el('source-filter').value = ''; el('search').value = ''; return search(); }
    function editorTypeChanged() {
        const status = el('list-type').value;
        el('notes').required = status !== 'pending';
        get('knowledge-decision-hint').textContent = status === 'pending' ? '待复核记录仍由常规商品检测继续判断。' : status === 'blacklist' ? '匹配此品牌时触发品牌风险拦截；请填写可复查的依据。' : '放行适用于所有命中该品牌的商品；其他品牌命中和独立风险检测仍会继续。';
    }
    function openEditor(id = 0) {
        const row = state.rows.find(item => Number(item.id) === Number(id));
        if ((!canManage() && !row) || (id && !row)) return;
        state.lastFocus = document.activeElement;
        el('id').value = row ? row.id : '';
        el('brand').value = row?.brand_name || '';
        el('brand').maxLength = row ? 255 : 10000;
        el('list-type').value = row ? type(row) : 'pending';
        el('notes').value = row?.notes || '';
        el('brand-hint').textContent = row ? '每次复核一个品牌；保存后转为人工维护，保留原始来源与样本信息。' : '每一行会作为一条独立记录，重复行自动忽略；已存在的品牌请通过编辑修改。';
        get('knowledge-editor-title').textContent = row ? canManage() ? type(row) === 'pending' ? '复核品牌' : '编辑品牌规则' : '品牌详情' : '新增品牌';
        const evidence = get('knowledge-editor-evidence');
        evidence.hidden = !row;
        evidence.textContent = row ? `${sources[source(row)] || '其他来源'} · ${number(row.evidence_count)} 条参考样本\n${row.source_detail || '暂无外部来源信息'}` : '';
        get('knowledge-editor-error').hidden = true;
        ['brand', 'list-type', 'notes'].forEach(name => { el(name).disabled = !canManage(); });
        get('save-infringement-knowledge-btn').hidden = !canManage();
        editorTypeChanged();
        el('form').hidden = false;
        if (!el('dialog').open) el('dialog').showModal();
        if (canManage()) el('brand').focus();
    }
    function closeEditor() {
        if (state.saving) return;
        el('dialog').close(); el('form').hidden = true;
        state.lastFocus?.focus();
    }
    async function save(event) {
        event.preventDefault();
        if (state.saving || !canManage()) return;
        const id = Number(el('id').value || 0), brand = el('brand').value.trim();
        const notes = el('notes').value.trim(), decision = el('list-type').value;
        const errorBox = get('knowledge-editor-error');
        errorBox.hidden = true;
        try {
            if (!brand) throw new Error('请填写品牌名称。');
            if (id && brand.split(/\r?\n/).filter(line => line.trim()).length > 1) throw new Error('编辑时一次只能填写一个品牌。');
            if (decision !== 'pending' && !notes) throw new Error('请填写拦截或放行的依据。');
            state.saving = true;
            get('save-infringement-knowledge-btn').disabled = true;
            get('save-infringement-knowledge-btn').textContent = '正在保存…';
            const result = await request(id ? `/${id}` : '/bulk', {method: id ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({[id ? 'brand_name' : 'brands_text']: brand, list_type: decision, notes})});
            state.saving = false; closeEditor();
            feedback(id ? `已保存「${brand}」：${labels[decision]}。` : `处理完成：新增 ${number(result.inserted)} 条，恢复 ${number(result.restored)} 条，已存在跳过 ${number(result.skipped)} 条。`);
            await load(true, id ? state.page : 1);
        } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
        finally { state.saving = false; get('save-infringement-knowledge-btn').disabled = false; get('save-infringement-knowledge-btn').textContent = '保存记录'; }
    }
    async function remove(id) {
        const row = state.rows.find(item => Number(item.id) === Number(id));
        if (!row || !canManage() || state.deleting || !window.confirm(`删除「${row.brand_name}」？删除后不再用于品牌规则，自动收集不会恢复此记录。`)) return;
        state.deleting = true;
        try { await request(`/${id}`, {method: 'DELETE'}); feedback(`已删除「${row.brand_name}」。`); await load(true); }
        catch (error) { feedback(`删除失败：${error.message}`, true); }
        finally { state.deleting = false; }
    }
    function stopPoll() { clearTimeout(state.timer); state.timer = null; }
    function schedulePoll() { stopPoll(); if (visible() && state.analysisRunning) state.timer = setTimeout(loadStatus, 2500); }
    function analysisControls() {
        for (const id of ['start-infringement-analysis-btn', 'fetch-global-ip-btn']) {
            get(id).hidden = !canManage(); get(id).disabled = !canManage() || state.analysisRunning || state.starting;
        }
        get('start-infringement-analysis-btn').textContent = state.starting ? '正在启动…' : state.analysisRunning ? '资料收集中…' : '提取待复核线索';
    }
    function renderStatus(data) {
        const completed = state.analysisRunning && !data.running;
        state.analysisRunning = Boolean(data.running);
        get('infringement-analysis-status').textContent = data.message || '等待启动';
        get('infringement-analysis-log').textContent = data.logs?.length ? data.logs.join('\n') : '尚未启动资料收集';
        analysisControls(); schedulePoll();
        if (completed) { state.loaded = false; if (state.active) load(true); }
    }
    async function loadStatus() {
        if (state.statusLoading) return;
        stopPoll(); state.statusLoading = true;
        const epoch = state.statusEpoch;
        try { const data = await request('/analysis/status'); if (epoch === state.statusEpoch) renderStatus(data); }
        catch (error) { if (epoch === state.statusEpoch) { get('infringement-analysis-status').textContent = `状态读取失败：${error.message}`; schedulePoll(); } }
        finally { state.statusLoading = false; }
    }
    async function start(globalIp = false) {
        if (!canManage() || state.analysisRunning || state.starting) return;
        const infraction = get('infringement-analysis-infraction-limit'), active = get('infringement-analysis-active-limit');
        if (!globalIp && (!infraction.reportValidity() || !active.reportValidity())) return;
        if (!globalIp && !window.confirm('将调用 AI 提取品牌线索并保存为待复核记录，是否开始？')) return;
        state.starting = true; state.statusEpoch++; analysisControls();
        try {
            const data = await request('/analysis/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({global_ip: globalIp, infraction_limit: Number(infraction.value || 10000), active_limit: Number(active.value || 5000), batch_size: 300})});
            renderStatus(data);
        } catch (error) {
            if (error.status === 409 && error.data) renderStatus(error.data);
            else get('infringement-analysis-status').textContent = `启动失败：${error.message}`;
        } finally { state.starting = false; analysisControls(); }
    }
    function showPanel(name) {
        state.panel = name === 'sources' ? 'sources' : 'records';
        for (const panel of ['records', 'sources']) {
            get(`knowledge-${panel}-panel`).hidden = state.panel !== panel;
            get(`knowledge-${panel}-tab`).setAttribute('aria-pressed', String(state.panel === panel));
        }
        window.WipoSync?.activate(visible());
        stopPoll();
        if (visible()) loadStatus();
    }
    function activate(active) {
        state.active = Boolean(active); stopPoll();
        if (state.active) { load(); analysisControls(); }
        window.WipoSync?.activate(visible());
        if (visible()) loadStatus();
    }
    get('knowledge-filter-form').addEventListener('submit', event => { event.preventDefault(); search(); });
    el('form').addEventListener('submit', save);
    el('dialog').addEventListener('cancel', event => { event.preventDefault(); closeEditor(); });
    el('body').addEventListener('click', event => {
        const target = event.target.closest('button');
        if (!target) return;
        if (target.hasAttribute('data-edit')) openEditor(Number(target.dataset.edit));
        if (target.hasAttribute('data-delete')) remove(Number(target.dataset.delete));
        if (target.hasAttribute('data-retry')) load(true);
    });
    document.addEventListener('visibilitychange', () => { stopPoll(); if (visible()) loadStatus(); });
    window.InfringementKnowledge = {activate, showPanel, filter, search, editorTypeChanged, turnPage: delta => { if (!state.loading) return load(true, Math.max(1, Math.min(state.pages, state.page + delta))); }};
    window.loadInfringementKnowledge = load;
    window.openInfringementKnowledgeEditor = openEditor;
    window.closeInfringementKnowledgeEditor = closeEditor;
    window.resetInfringementKnowledgeFilters = reset;
    window.deleteInfringementKnowledge = remove;
    window.loadInfringementKnowledgeAnalysisStatus = loadStatus;
    window.startInfringementKnowledgeAnalysis = start;
    activate(get('tab-infringement-knowledge').classList.contains('active'));
})();
