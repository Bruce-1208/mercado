(function () {
    'use strict';
    const get = id => document.getElementById(id);
    const el = name => get(`infringement-knowledge-${name}`);
    if (!el('body')) return;
    const API = '/api/infringement-knowledge';
    const labels = {pending: '待复核', blacklist: '风险拦截', whitelist: '人工放行', borderline: '擦边品牌'};
    const sources = {manual: '人工维护', analysis: 'AI 提取', wipo: 'WIPO', wikidata: 'Wikidata'};
    const state = {active: false, panel: 'records', loaded: false, loading: false, rows: [], page: 1, pages: 1, total: 0, epoch: 0, editorEpoch: 0, relatedEpoch: 0, saving: false, deleting: false, analysisRunning: false, starting: false, statusLoading: false, statusEpoch: 0, timer: null, lastFocus: null};
    const canManage = () => typeof window.hasWorkbenchPermission === 'function' && window.hasWorkbenchPermission('infringement_knowledge.manage');
    const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
    const number = value => Number(value || 0).toLocaleString('zh-CN');
    const source = row => String(row.source_detail || '').startsWith('global_ip:') && row.source_type !== 'manual' ? 'wikidata' : row.source_type || 'manual';
    const type = row => source(row) === 'manual' && labels[row.list_type] ? row.list_type : source(row) === 'wipo' && row.list_type === 'blacklist' ? 'blacklist' : 'pending';
    const values = value => String(value || '').split(/[,，\r\n]+/).map(text => text.trim()).filter(Boolean);
    const scopeLabel = row => row.scope_label || (row.scope?.mode === 'scoped' ? ['sites', 'categories', 'shops'].map((key, index) => row.scope[key]?.length ? `${['站点', '类目', '店铺'][index]}：${row.scope[key].join('、')}` : '').filter(Boolean).join(' · ') : '企业内通用规则（历史记录）');
    const expiryLabel = row => {
        const now = new Date(), today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
        return row.valid_until < today ? `已过期 · ${row.valid_until}` : `有效至 ${row.valid_until}`;
    };
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
        for (const key of ['total', 'pending', 'blacklist', 'whitelist', 'borderline']) el(key).textContent = number(summary[key]);
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
                return `<tr><td><span class="knowledge-brand">${escape(row.brand_name)}</span>${row.aliases?.length ? `<span class="knowledge-source-detail">别名：${escape(row.aliases.join('、'))}</span>` : ''}</td>
                    <td><span class="knowledge-list-badge ${status}">${labels[status]}</span><span class="knowledge-scope-label">${status === 'pending' ? '待复核，不形成规则' : escape(scopeLabel(row))}</span>${row.valid_until ? `<span class="knowledge-source-detail">${escape(expiryLabel(row))}</span>` : ''}</td>
                    <td title="${escape(row.source_detail)}">${escape(sources[origin] || '其他来源')}<span class="knowledge-source-detail">${count > 0 ? `${number(count)} 条参考样本` : origin === 'manual' ? '人工决定' : '外部名称线索'}</span></td>
                    <td><span class="knowledge-notes" title="${escape(row.notes)}">${escape(row.notes || '尚未填写处理依据')}</span></td>
                    <td>${escape(row.updated_at || '—')}<span class="knowledge-source-detail">v${number(row.rule_version || 1)}${row.reviewed_by ? ` · ${escape(row.reviewed_by)}` : ''}</span></td>
                    <td><div class="knowledge-row-actions"><button type="button" data-edit="${id}">${canManage() ? status === 'pending' ? '复核' : '编辑' : '详情'}</button><button type="button" data-related="${id}">关联商品</button>${canManage() ? `<button class="knowledge-delete" type="button" data-delete="${id}">删除</button>` : ''}</div></td></tr>`;
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
        get('knowledge-decision-hint').textContent = status === 'pending' ? '待复核记录仍由常规商品检测继续判断。' : status === 'blacklist' ? '品牌及别名命中且满足适用范围时触发风险拦截；请填写可复查的依据。' : status === 'borderline' ? '品牌及别名命中时仅提示，不因该品牌拦截或处理商品；其他独立风险继续检查。' : '只在适用范围与有效期内放行品牌命中；其他独立风险继续检查。单件商品请使用商品复核。';
        editorScopeChanged();
    }
    function editorScopeChanged() {
        const global = el('scope-mode').value === 'global';
        get('knowledge-scope-values').hidden = global;
        get('knowledge-scope-hint').textContent = global ? '作用于本企业所有站点、类目和店铺的同品牌商品，不影响其他企业。' : '至少限定一项；不同维度同时满足才命中。目标站点或店铺缺失时，限定该维度的规则不会命中。';
    }
    async function loadDetails(id, epoch) {
        const history = get('knowledge-review-history');
        history.hidden = false;
        get('knowledge-review-history-content').textContent = '正在读取证据与历史…';
        try {
            const data = await request(`/${id}`);
            if (epoch !== state.editorEpoch || !el('dialog').open) return;
            const record = data.record || {};
            const origin = record.original_source_type || record.source_type;
            if (origin) get('knowledge-editor-evidence').textContent = `原始来源：${sources[origin] || origin} · v${record.rule_version || 1}${record.reviewed_by ? ` · 复核人 ${record.reviewed_by}` : ''}\n${record.source_detail || '暂无外部来源信息'}`;
            const evidence = (data.evidence || []).map(item => `<div class="knowledge-history-entry"><strong>${escape(sources[item.source_type] || item.source_type || '来源证据')}</strong> <span>${escape(item.created_at || '')}</span><p>${escape(item.source_detail || '参考样本')}</p>${item.payload && Object.keys(item.payload).length ? `<details><summary>原始样本</summary><pre>${escape(JSON.stringify(item.payload, null, 2))}</pre></details>` : ''}</div>`).join('');
            const actions = {create: '新增', review: '复核保存', restore: '人工恢复', delete: '删除', candidate: '提交线索'};
            const changes = (data.history || []).map(item => `<div class="knowledge-history-entry"><strong>v${number(item.rule_version)} · ${escape(actions[item.action] || item.action)}</strong> <span>${escape(item.actor || '历史记录未记名')} · ${escape(item.created_at || '')}</span><p>${escape(item.after?.notes || '')}</p><details><summary>版本内容</summary><pre>${escape(JSON.stringify(item.after || {}, null, 2))}</pre></details></div>`).join('');
            get('knowledge-review-history-content').innerHTML = `<h4>原始证据</h4>${evidence || '<p>尚无独立证据记录，历史来源保留在上方。</p>'}<h4>复核历史</h4>${changes || '<p>尚无复核历史；后续变更将记录版本与操作者。</p>'}`;
        } catch (error) { if (epoch === state.editorEpoch) get('knowledge-review-history-content').textContent = `证据与历史读取失败：${error.message}`; }
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
        el('aliases').value = (row?.aliases || []).join('\n');
        el('scope-mode').value = row && type(row) !== 'pending' ? row.scope?.mode || 'global' : row?.scope && !row.legacy_global ? row.scope.mode : 'scoped';
        for (const key of ['sites', 'categories', 'shops']) el(`scope-${key}`).value = (row?.scope?.[key] || []).join(', ');
        el('valid-until').value = row?.valid_until || '';
        el('source-detail').value = '';
        el('rule-version').value = row ? row.rule_version || 1 : '';
        el('brand-hint').textContent = row ? '每次复核一个品牌；保存后转为人工维护，保留原始来源与样本信息。' : '每一行会作为一条独立记录，重复行自动忽略；已存在的品牌请通过编辑修改。';
        get('knowledge-editor-title').textContent = row ? canManage() ? type(row) === 'pending' ? '复核品牌' : '编辑品牌规则' : '品牌详情' : '新增品牌';
        const evidence = get('knowledge-editor-evidence');
        evidence.hidden = !row;
        evidence.textContent = row ? `原始来源：${sources[row.original_source_type || source(row)] || '其他来源'} · ${number(row.evidence_count)} 条参考样本 · v${row.rule_version || 1}\n${row.source_detail || '暂无外部来源信息'}` : '';
        get('knowledge-editor-error').hidden = true;
        ['brand', 'list-type', 'notes', 'aliases', 'scope-mode', 'scope-sites', 'scope-categories', 'scope-shops', 'valid-until', 'source-detail'].forEach(name => { el(name).disabled = !canManage(); });
        get('knowledge-review-history').hidden = !row;
        get('save-infringement-knowledge-btn').hidden = !canManage();
        editorTypeChanged();
        el('form').hidden = false;
        if (!el('dialog').open) el('dialog').showModal();
        if (canManage()) el('brand').focus();
        const epoch = ++state.editorEpoch;
        if (row) loadDetails(row.id, epoch);
    }
    function closeEditor() {
        if (state.saving) return;
        state.editorEpoch++;
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
            if (decision !== 'pending' && !notes) throw new Error('请填写品牌规则的依据。');
            const aliases = values(el('aliases').value);
            if (aliases.length && brand.split(/\r?\n/).filter(line => line.trim()).length > 1) throw new Error('有品牌别名时，每次只能维护一个品牌。');
            const scope = {mode: el('scope-mode').value, sites: [], categories: [], shops: []};
            if (scope.mode === 'scoped') for (const key of ['sites', 'categories', 'shops']) scope[key] = values(el(`scope-${key}`).value);
            const hasScope = ['sites', 'categories', 'shops'].some(key => scope[key].length);
            if (decision !== 'pending' && scope.mode === 'scoped' && !hasScope) throw new Error('请至少填写一个站点、类目或店铺；单件商品放行请使用商品复核。');
            const payload = {[id ? 'brand_name' : 'brands_text']: brand, list_type: decision, notes};
            if (aliases.length || id) payload.aliases = aliases;
            if (decision !== 'pending' || hasScope || (id && scope.mode === 'global')) payload.scope = scope;
            if (el('valid-until').value || id) payload.valid_until = el('valid-until').value || null;
            if (el('source-detail').value.trim()) payload.source_detail = el('source-detail').value.trim();
            if (id) payload.rule_version = Number(el('rule-version').value || 1);
            state.saving = true;
            get('save-infringement-knowledge-btn').disabled = true;
            get('save-infringement-knowledge-btn').textContent = '正在保存…';
            const result = await request(id ? `/${id}` : '/bulk', {method: id ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
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
    function closeRelated() {
        state.relatedEpoch++;
        get('knowledge-products-dialog').close();
        state.lastFocus?.focus();
    }
    async function openRelated(id) {
        const row = state.rows.find(item => Number(item.id) === Number(id));
        if (!row) return;
        if (el('dialog').open) closeEditor();
        state.lastFocus = document.activeElement;
        const epoch = ++state.relatedEpoch;
        get('knowledge-products-title').textContent = `${row.brand_name} · 关联商品`;
        get('knowledge-products-summary').textContent = '正在读取检测关联…';
        get('knowledge-products-body').innerHTML = '<tr><td colspan="4">正在读取…</td></tr>';
        if (!get('knowledge-products-dialog').open) get('knowledge-products-dialog').showModal();
        try {
            const data = await request(`/${id}/products?limit=50`);
            if (epoch !== state.relatedEpoch) return;
            const rows = data.rows || [];
            get('knowledge-products-summary').textContent = `${rows.length} 件${data.has_more ? ' · 仅展示最近 50 件' : ''}。旧结果的名称候选需重新检测确认关联。`;
            const sourceLabels = {collection_list: '采集列表', product_list: '产品列表', pulled: '店铺拉取', zying: '智赢商品'};
            get('knowledge-products-body').innerHTML = rows.length ? rows.map(item => {
                const mode = {collection_list: 'collection', product_list: 'products'}[item.source_type];
                return `<tr><td>${escape(item.title || item.product_id || '商品')}<span>${escape(sourceLabels[item.source_type] || item.source_type)} · ${escape(item.source_row_id)}</span></td><td>${item.association === 'matched_rule' ? '检测命中本规则' : '旧结果名称候选'}</td><td>${escape(({0: '未发现标题风险', 1: '待复核', 2: '高风险'})[item.risk_level] || '未知')}<span>${escape(item.checked_at || '')}</span></td><td>${mode ? `<button type="button" class="secondary" data-open-risk="${mode}" data-row="${Number(item.source_row_id)}">查看结果</button>` : '请在侵权检测页查看'}</td></tr>`;
            }).join('') : '<tr><td colspan="4">暂无已检测的关联商品</td></tr>';
        } catch (error) { if (epoch === state.relatedEpoch) { get('knowledge-products-summary').textContent = `读取失败：${error.message}`; get('knowledge-products-body').innerHTML = '<tr><td colspan="4">请关闭后重试</td></tr>'; } }
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
        if (target.hasAttribute('data-related')) openRelated(Number(target.dataset.related));
        if (target.hasAttribute('data-retry')) load(true);
    });
    document.addEventListener('visibilitychange', () => { stopPoll(); if (visible()) loadStatus(); });
    get('knowledge-products-dialog').addEventListener('cancel', event => { event.preventDefault(); closeRelated(); });
    get('knowledge-products-body').addEventListener('click', event => {
        const button = event.target.closest('[data-open-risk]');
        if (!button) return;
        if (typeof window.openMercadoInfringementDetail !== 'function') { get('knowledge-products-summary').textContent = '商品详情组件尚未加载，请稍后重试。'; return; }
        closeRelated(); window.openMercadoInfringementDetail(button.dataset.openRisk, Number(button.dataset.row));
    });
    window.InfringementKnowledge = {activate, showPanel, filter, search, editorTypeChanged, editorScopeChanged, openRelated, closeRelated, turnPage: delta => { if (!state.loading) return load(true, Math.max(1, Math.min(state.pages, state.page + delta))); }};
    window.loadInfringementKnowledge = load;
    window.openInfringementKnowledgeEditor = openEditor;
    window.closeInfringementKnowledgeEditor = closeEditor;
    window.resetInfringementKnowledgeFilters = reset;
    window.deleteInfringementKnowledge = remove;
    window.loadInfringementKnowledgeAnalysisStatus = loadStatus;
    window.startInfringementKnowledgeAnalysis = start;
    activate(get('tab-infringement-knowledge').classList.contains('active'));
})();
