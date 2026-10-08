/* Product-level infringement tasks, evidence and human review shared by both lists. */
(() => {
    const api = '/api/mercado/infringement';
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    const safeUrl = value => /^https?:\/\//i.test(String(value || '')) ? String(value) : '';
    const scopeName = scope => scope === 'title_image' ? '标题和主图' : '标题';
    const taskLabels = {queued: '排队中', pending: '排队中', running: '检测中', checking: '检测中', completed: '已完成', partial: '部分完成', interrupted: '已中断', succeeded: '已完成', finished: '已完成', failed: '失败', cancelled: '已取消', canceled: '已取消', stopped: '已停止'};
    const terminal = task => ['completed', 'partial', 'interrupted', 'succeeded', 'finished', 'failed', 'cancelled', 'canceled', 'stopped'].includes(String(task.status || task.state));
    let startContext = null;
    let detailContext = null;
    let detailSequence = 0;
    let detailData = null;
    let submitting = false;
    let activeTask = null;
    let pollTimer = null;
    let polling = false;
    let refreshAt = 0;
    let refreshing = false;
    const byId = id => document.getElementById(id);
    const value = id => byId(id)?.value || '';
    const listMode = () => typeof mercadoListMode !== 'undefined' && mercadoListMode === 'products' ? 'products' : 'collection';
    const selectedIds = () => typeof selectedMercadoCollectionIds !== 'undefined' ? [...selectedMercadoCollectionIds].map(Number).filter(id => id > 0) : [];
    const listRows = () => typeof mercadoListRows !== 'undefined' ? mercadoListRows : [];
    const listVisible = () => !document.hidden && (!window.ZeshunPerformance
        || window.ZeshunPerformance.isActive('mercado-collection')
        || window.ZeshunPerformance.isActive('mercado-products'));
    const taskVisible = () => !document.hidden && (byId('mi-task-dialog')?.open || listVisible());
    const canExecute = () => typeof hasWorkbenchPermission !== 'function' || hasWorkbenchPermission('risk_check.execute');
    const canReview = () => typeof hasWorkbenchPermission !== 'function' || hasWorkbenchPermission('risk_check.review');
    const canManageKnowledge = () => typeof hasWorkbenchPermission !== 'function' || hasWorkbenchPermission('infringement_knowledge.manage');

    function normalize(row = {}) {
        row = row || {};
        const raw = row.infringement && typeof row.infringement === 'object' ? row.infringement : row;
        const level = raw.risk_level ?? row.infringement_risk_level;
        const scope = raw.detection_scope || raw.scope || row.infringement_scope || row.infringement_detection_scope || 'title';
        let status = raw.status || row.infringement_status || row.infringement_check_status || '';
        const stale = Boolean(raw.stale || raw.is_stale || row.infringement_stale || row.infringement_needs_recheck);
        if (stale && !['queued', 'checking', 'running'].includes(status)) status = 'stale';
        if (!status || ['completed', 'checked', 'succeeded', 'finished'].includes(status)) status = ({0: 'low', 1: 'suspected', 2: 'high'})[String(level)] || 'unchecked';
        if (status === 'running') status = 'checking';
        if (status === 'pending') status = 'queued';
        const review = raw.review || raw.manual_review || row.infringement_review || {};
        const decision = review.decision || ({allow: 'released', block: 'blocked'})[raw.manual_decision] || raw.review_decision || row.infringement_review_decision || 'pending';
        const imageMethod = String(raw.image_method || raw.coverage?.image_method || '').toLowerCase();
        const imageVision = /vision|multimodal|visual/.test(imageMethod);
        const imageOcr = /ocr/.test(imageMethod) && !imageVision;
        const lowLabel = scope !== 'title_image' ? '未发现标题风险' : imageOcr ? '未发现标题及图中文字风险' : imageVision ? '未发现图文风险' : '未发现本次已检内容风险';
        const labels = {unchecked: '未检测', queued: '排队中', checking: '检测中', low: lowLabel, suspected: '疑似风险', high: '高风险', stale: '资料或知识库已变更 · 待重检', failed: '检测失败'};
        return {raw, status, level, scope, imageMethod, imageVision, imageOcr, decision, review, label: labels[status] || '未检测', reason: raw.reason || raw.error || raw.check_error || row.infringement_reason || row.infringement_error || '', keywords: raw.keywords || row.infringement_keywords || '', checkedAt: raw.checked_at || row.infringement_checked_at || '', coverage: raw.coverage || {}};
    }
    window.normalizeMercadoInfringement = normalize;
    function reviewSites(raw) {
        const sites = raw.review_site_ids || raw.site_ids || raw.review?.site_ids || [];
        if (Array.isArray(sites)) return sites;
        try { const parsed = JSON.parse(sites); return Array.isArray(parsed) ? parsed : []; } catch (_) { return []; }
    }
    window.mercadoInfringementPublishIssues = row => {
        const risk = normalize(row);
        const sites = typeof mercadoSelectedValues === 'function' && typeof mercadoPublishSite !== 'undefined' ? mercadoSelectedValues(mercadoPublishSite) : [];
        const allowedSites = reviewSites(risk.raw);
        if (risk.decision === 'blocked' && risk.raw.review_current !== false) return ['人工复核已拦截'];
        if (risk.decision === 'released' && risk.raw.review_current === true) {
            if (!(risk.raw.review_evidence_ref || risk.review.evidence_ref) || !(risk.raw.review_expires_at || risk.review.expires_at) || !allowedSites.length) return ['人工放行缺少授权凭证、适用站点或有效期'];
            const missing = sites.filter(site => !allowedSites.includes(site));
            if (missing.length) return [`人工放行未覆盖站点 ${missing.join('、')}`];
            if (sites.length) return [];
        }
        const issues = {queued: '侵权检测排队中', checking: '侵权检测进行中', suspected: '疑似风险，需人工复核', high: '高风险，需人工复核'};
        return issues[risk.status] ? [issues[risk.status]] : [];
    };
    window.mercadoInfringementNeedsCheck = row => ['unchecked', 'stale', 'failed'].includes(normalize(row).status);
    window.renderMercadoInfringement = (row, mode) => {
        const id = Number(row.id || row.row_id || 0);
        if (!id) return '<span class="mi-muted">未检测</span>';
        const risk = normalize(row);
        const action = risk.status === 'unchecked' ? '检测' : ['stale', 'failed'].includes(risk.status) ? '重新检测' : '再检测';
        const manual = risk.decision === 'blocked' ? risk.raw.review_current === false ? '拦截记录已失效' : '人工已拦截' : risk.decision === 'released' ? risk.raw.review_current ? '有站点限定的人工放行' : '放行记录已失效' : '';
        const running = ['queued', 'checking'].includes(risk.status);
        return `<div class="mi-row-risk"><button type="button" class="mi-risk-badge mi-${esc(risk.status)}" onclick="openMercadoInfringementDetail('${mode === 'products' ? 'products' : 'collection'}', ${id})" title="${esc([risk.reason, risk.checkedAt].filter(Boolean).join(' · '))}">${esc(risk.label)}</button><small>${esc(scopeName(risk.scope))}${risk.raw.borderline_brands?.length ? ` · 擦边品牌仅提示：${esc(risk.raw.borderline_brands.join('、'))}` : ''}${manual ? ` · ${esc(manual)}` : ''}</small><div class="mi-row-actions"><button type="button" onclick="openMercadoInfringementDetail('${mode === 'products' ? 'products' : 'collection'}', ${id})">详情 / 复核</button><button type="button" data-permission="risk_check.execute" onclick="openMercadoInfringement(${id}, '${mode === 'products' ? 'products' : 'collection'}')" ${running || !canExecute() ? 'disabled' : ''}>${action}</button></div></div>`;
    };

    function captureFilters() {
        const fields = {search: 'mercado-list-search', review_status: 'mercado-review-filter', publish_status: 'mercado-publish-filter', weight_status: 'mercado-weight-status-filter', management_category_id: 'mercado-management-category-filter', mercado_category: 'mercado-platform-category-filter', zying_category: 'mercado-zying-category-filter', product_developer_id: 'mercado-product-developer-filter', salesperson: 'mercado-salesperson-filter', weight_min: 'mercado-weight-min', weight_max: 'mercado-weight-max', price_min: 'mercado-price-min', price_max: 'mercado-price-max', net_proceeds_min: 'mercado-net-min', net_proceeds_max: 'mercado-net-max', date_from: 'mercado-date-from', date_to: 'mercado-date-to', risk_status: 'mercado-infringement-filter'};
        const filters = {};
        for (const [key, id] of Object.entries(fields)) if (value(id).trim()) filters[key] = value(id).trim();
        if (listMode() !== 'products') ['mercado_category', 'zying_category', 'product_developer_id'].forEach(key => delete filters[key]);
        if (listMode() === 'products' && typeof mercadoProductSource !== 'undefined' && mercadoProductSource) filters.source_type = mercadoProductSource;
        // Backend list queries use their existing parameter names.
        if (typeof mercadoSalespersonFilter !== 'undefined' && mercadoSalespersonFilter.value) filters.salesperson = mercadoSalespersonFilter.value;
        return filters;
    }
    window.captureMercadoInfringementFilters = captureFilters;

    function setupDialogs() {
        const container = document.createElement('div');
        container.innerHTML = `
            <dialog id="mi-start-dialog" class="mi-dialog" aria-labelledby="mi-start-title">
                <form id="mi-start-form"><header class="mi-header"><div><small>INFRINGEMENT CHECK</small><h2 id="mi-start-title">侵权检测</h2><p id="mi-start-subtitle"></p></div><button type="button" class="mi-close" data-mi-close="mi-start-dialog" aria-label="关闭">×</button></header>
                <div class="mi-body"><fieldset class="mi-fieldset"><legend>1 · 检测范围</legend>
                    <label class="mi-choice"><input type="radio" name="mi-range" value="selected"><span><strong id="mi-selected-label">检测所选商品</strong><small>只检测已勾选或当前单件商品</small></span></label>
                    <label class="mi-choice"><input type="radio" name="mi-range" value="filtered"><span><strong id="mi-filtered-label">当前筛选结果全部</strong><small>包含其他页，按打开面板时的筛选条件执行</small></span></label>
                    <label class="mi-choice"><input type="radio" name="mi-range" value="unchecked"><span><strong>仅未检、待重检和失败商品</strong><small>按当前筛选范围，跳过已经完成且仍有效的结果</small></span></label>
                </fieldset><fieldset class="mi-fieldset"><legend>2 · 检测内容</legend>
                    <label class="mi-choice"><input type="radio" name="mi-scope" value="title" checked><span><strong>标题检测</strong><small>识别品牌词、IP 名称和知识库匹配；不覆盖图片</small></span></label>
                    <label class="mi-choice"><input type="radio" name="mi-scope" value="title_image"><span><strong>标题和主图检测</strong><small>增加主图中的 Logo、角色与品牌线索；无法读取图片时标明未完成</small></span></label>
                </fieldset><label class="mi-recheck"><input id="mi-recheck" type="checkbox"><span>重新检测已检商品，保留历史结果</span></label><p id="mi-start-summary" class="mi-notice"></p><p id="mi-start-feedback" class="mi-feedback" role="status"></p></div>
                <footer class="mi-footer"><button type="button" class="secondary" data-mi-close="mi-start-dialog">取消</button><button type="submit" class="primary" id="mi-start-submit">开始检测</button></footer></form>
            </dialog>
            <dialog id="mi-task-dialog" class="mi-dialog mi-wide" aria-labelledby="mi-task-title"><header class="mi-header"><div><small>MY CHECK TASKS</small><h2 id="mi-task-title">侵权检测任务</h2><p>任务在后台继续执行，完成后同步更新对应列表。</p></div><button type="button" class="mi-close" data-mi-close="mi-task-dialog" aria-label="关闭">×</button></header><div class="mi-body"><div id="mi-task-current" aria-live="polite"></div><div class="mi-task-heading"><strong>我的任务</strong><button type="button" class="secondary" id="mi-task-refresh">刷新</button></div><div id="mi-task-list" class="mi-task-list"></div><p id="mi-task-feedback" class="mi-feedback" role="status"></p></div></dialog>
            <dialog id="mi-detail-dialog" class="mi-dialog mi-wide" aria-labelledby="mi-detail-title"><header class="mi-header"><div><small>EVIDENCE & REVIEW</small><h2 id="mi-detail-title">侵权风险详情</h2><p id="mi-detail-subtitle"></p></div><button type="button" class="mi-close" data-mi-close="mi-detail-dialog" aria-label="关闭">×</button></header><div class="mi-body"><div id="mi-detail-content"></div><div id="mi-detail-extra-actions" class="mi-detail-actions"><button type="button" class="secondary" id="mi-detail-recheck">重新检测此商品</button><button type="button" class="secondary" id="mi-detail-candidate">提交知识库线索</button></div><form id="mi-candidate-form" class="mi-review-form" hidden><h3>提交知识库线索</h3><p class="mi-muted">保存为待复核证据，保留当前商品、检测范围与原始原因。人工确认后再决定名单处理方式。</p><label>品牌 / 角色 / 风险词<input id="mi-candidate-term" required maxlength="255" placeholder="每次提交一个可核实的名称"></label><label>线索说明<textarea id="mi-candidate-notes" rows="3" maxlength="2000"></textarea></label><p id="mi-candidate-feedback" class="mi-feedback" role="status"></p><div class="mi-detail-actions"><button type="button" class="secondary" id="mi-candidate-close">收起</button><button type="submit" class="primary" id="mi-candidate-submit">提交为待复核</button></div></form><form id="mi-review-form" class="mi-review-form"><h3>人工复核 · 商品级</h3><p class="mi-muted">复核结论与 AI 风险分别保存；放行仅对当前商品资料、指定站点与期限有效。</p><div class="mi-review-grid"><label>复核结论<select id="mi-review-decision"><option value="pending">待复核</option><option value="blocked">确认拦截</option><option value="released">复核放行</option></select></label><label>有效期（放行必填）<input id="mi-review-expires" type="datetime-local"></label></div><label>适用站点（放行必填）<input id="mi-review-sites" placeholder="例如 MLM, MLB；多个站点用逗号分隔" maxlength="200"></label><label>复核理由<textarea id="mi-review-reason" required rows="3" maxlength="2000" placeholder="填写判断依据或需要补充的资料"></textarea></label><label>授权或证据引用<input id="mi-review-evidence" maxlength="2000" placeholder="授权文件编号、供应商证明或可核实链接；放行必须填写"></label><p id="mi-review-feedback" class="mi-feedback" role="status"></p><div class="mi-detail-actions"><button type="submit" class="primary" id="mi-review-submit" data-permission="risk_check.review">保存复核</button></div></form></div></dialog>`;
        document.body.append(container);
        container.querySelectorAll('[data-mi-close]').forEach(button => button.addEventListener('click', () => byId(button.dataset.miClose).close()));
        byId('mi-start-form').addEventListener('submit', startTask);
        byId('mi-start-form').addEventListener('change', updateStartSummary);
        byId('mi-task-refresh').addEventListener('click', loadTasks);
        byId('mi-task-list').addEventListener('click', taskAction);
        byId('mi-task-current').addEventListener('click', taskAction);
        byId('mi-review-form').addEventListener('submit', saveReview);
        byId('mi-candidate-form').addEventListener('submit', saveCandidate);
        byId('mi-detail-candidate').addEventListener('click', () => {
            if (!detailData || !canManageKnowledge()) return;
            byId('mi-candidate-form').hidden = false;
            byId('mi-candidate-term').focus();
        });
        byId('mi-candidate-close').addEventListener('click', () => { byId('mi-candidate-form').hidden = true; });
        byId('mi-detail-recheck').addEventListener('click', () => {
            if (!detailContext) return;
            const {mode, id} = detailContext;
            byId('mi-detail-dialog').close();
            window.openMercadoInfringement(id, mode);
        });
    }
    const chosen = name => document.querySelector(`input[name="${name}"]:checked`)?.value || '';
    function updateStartSummary() {
        if (!startContext) return;
        const range = chosen('mi-range');
        byId('mi-recheck').disabled = range === 'unchecked';
        if (range === 'unchecked') byId('mi-recheck').checked = false;
        byId('mi-start-summary').textContent = `${range === 'selected' ? `所选 ${startContext.ids.length} 件` : range === 'filtered' ? '当前筛选结果（包含所有分页）' : '当前筛选中的未检、待重检和失败商品'} · ${scopeName(chosen('mi-scope'))} · ${byId('mi-recheck').checked ? '包含已检商品，历史结果保留' : '跳过已检且仍有效的结果'}。`;
    }
    window.syncMercadoInfringementSelection = () => {
        const button = byId('mercado-infringement-selected');
        if (button) {
            button.textContent = selectedIds().length ? `侵权检测（${selectedIds().length}）` : '侵权检测';
            button.hidden = !canExecute();
        }
    };
    window.openMercadoInfringement = (id = null, requestedMode = null) => {
        if (submitting || !canExecute()) return;
        const single = Number(id) > 0;
        startContext = {mode: requestedMode === 'products' ? 'products' : requestedMode === 'collection' ? 'collection' : listMode(), ids: single ? [Number(id)] : selectedIds(), filters: captureFilters(), single};
        byId('mi-start-subtitle').textContent = `${startContext.mode === 'products' ? '产品列表' : '采集列表'} · ${single ? '单件商品' : '后台执行，完成后保持当前分页'}`;
        byId('mi-selected-label').textContent = `检测所选商品（${startContext.ids.length} 件）`;
        byId('mi-filtered-label').textContent = `当前筛选结果全部${typeof mercadoListTotal !== 'undefined' ? `（当前 ${mercadoListTotal} 件）` : ''}`;
        document.querySelectorAll('input[name="mi-range"]').forEach(input => {
            input.disabled = (input.value === 'selected' && !startContext.ids.length) || (single && input.value !== 'selected');
            input.checked = input.value === (startContext.ids.length ? 'selected' : 'unchecked');
        });
        const row = listRows().find(item => Number(item.id) === Number(id)) || (detailContext?.mode === startContext.mode && detailContext.id === Number(id) ? detailData?.item : null);
        const risk = normalize(row);
        if (single) document.querySelectorAll('input[name="mi-scope"]').forEach(input => { input.checked = input.value === risk.scope; });
        byId('mi-recheck').checked = single && risk.status !== 'unchecked';
        byId('mi-start-feedback').textContent = '';
        updateStartSummary();
        byId('mi-start-dialog').showModal();
    };

    async function request(path, options = {}) {
        const response = await fetch(`${api}${path}`, {cache: 'no-store', ...options});
        let payload;
        try { payload = await response.json(); } catch (_) { throw new Error(`接口返回无法读取（HTTP ${response.status}）`); }
        if (!response.ok || payload.status === 'error' || payload.status === 'failed') throw new Error(payload.message || `请求失败（HTTP ${response.status}）`);
        return payload.data ?? payload;
    }
    async function startTask(event) {
        event.preventDefault();
        if (!startContext || submitting || !canExecute()) return;
        const context = {...startContext};
        const selectionMode = chosen('mi-range');
        if (selectionMode === 'selected' && !context.ids.length) return;
        submitting = true;
        byId('mi-start-submit').disabled = true;
        byId('mi-start-feedback').textContent = '正在提交检测任务…';
        try {
            const data = await request('/tasks', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({list_mode: context.mode, selection_mode: selectionMode, item_ids: selectionMode === 'selected' ? context.ids : [], filters: context.filters, detection_scope: chosen('mi-scope') || 'title', recheck: byId('mi-recheck').checked})});
            const task = data.task || data;
            activeTask = task;
            byId('mi-start-dialog').close();
            await window.openMercadoInfringementTasks(task);
            await refreshList();
            schedulePoll();
        } catch (error) { byId('mi-start-feedback').textContent = error.message; }
        finally { submitting = false; byId('mi-start-submit').disabled = false; }
    }
    function taskId(task) { return String(task.id || task.task_id || ''); }
    function taskResultList(task) {
        const rows = (task.results || []).slice(-50);
        if (!rows.length) return '';
        return `<details class="mi-history"><summary>商品处理结果（${(task.results || []).length} 件）</summary>${rows.map(row => {
            const mode = ['collection_list', 'collection'].includes(row.source_type) ? 'collection' : 'products';
            const id = Number(row.row_id || row.source_row_id || row.id);
            return `<article><strong>${esc(row.product_id || row.source_item_id || id)} · ${esc(row.error ? '检测失败' : ({0: '未发现本次已检内容风险', 1: '疑似风险', 2: '高风险'})[String(row.risk_level)] || taskLabels[row.status] || row.status || '已处理')}</strong>${row.error ? `<p>${esc(row.error)}</p>` : ''}${id ? `<button type="button" class="secondary" data-mi-item="${mode}:${id}">查看结果 / 复核</button>` : ''}</article>`;
        }).join('')}</details>`;
    }
    function taskCard(task, current = false) {
        const id = taskId(task);
        const summary = task.summary || task;
        const total = Number(task.total ?? summary.total ?? task.item_count ?? 0);
        const done = Number(task.completed ?? summary.completed ?? summary.checked ?? task.checked ?? 0);
        const failed = Number(task.failed ?? summary.failed ?? 0);
        const percent = total ? Math.min(100, Math.round((done + failed) * 100 / total)) : 0;
        const state = String(task.status || task.state || (task.running ? 'running' : 'queued'));
        return `<article class="mi-task-card${current ? ' mi-current-task' : ''}"><div class="mi-task-row"><div><strong>${esc((task.list_mode || task.params?.list_mode) === 'collection' ? '采集列表' : '产品列表')} · ${esc(scopeName(task.detection_scope || task.scope))}</strong><small>${esc(task.created_at || '')} · ${esc(id)}</small></div><span class="mi-task-state">${esc(taskLabels[state] || state)}</span></div><progress max="100" value="${percent}" aria-label="检测进度 ${percent}%"></progress><p>${total ? `共 ${total} 件 · 已处理 ${done} 件 · 失败 ${failed} 件` : terminal(task) ? '当前范围无需要检测的商品' : '正在确定商品范围'}${summary.skipped ? ` · 跳过 ${Number(summary.skipped)} 件` : ''}</p><p class="mi-muted">${esc(task.message || task.error || '')}</p><div class="mi-task-actions">${!current ? `<button type="button" class="secondary" data-mi-task="${esc(id)}">查看进度</button>` : ''}${terminal(task) && canExecute() && (failed > 0 || done < total) ? `<button type="button" class="secondary" data-mi-retry="${esc(id)}">重试失败 / 未完成商品</button>` : ''}${!terminal(task) && canExecute() ? `<button type="button" class="secondary" data-mi-cancel="${esc(id)}" ${task.cancel_requested ? 'disabled' : ''}>${task.cancel_requested ? '正在取消…' : '取消任务'}</button>` : ''}</div>${current ? taskResultList(task) : ''}${current && task.logs?.length ? `<details class="mi-history"><summary>任务日志</summary><pre class="mi-task-log">${esc(task.logs.slice(-50).map(log => typeof log === 'object' ? JSON.stringify(log) : log).join('\n'))}</pre></details>` : ''}</article>`;
    }
    async function loadTasks() {
        try {
            const data = await request('/tasks');
            const tasks = Array.isArray(data) ? data : data.tasks || data.rows || [];
            byId('mi-task-list').innerHTML = tasks.length ? tasks.map(task => taskCard(task)).join('') : '<p class="mi-muted">暂无列表检测任务。</p>';
            byId('mi-task-feedback').textContent = '';
            if (!activeTask) activeTask = tasks.find(task => !terminal(task)) || null;
            if (activeTask) byId('mi-task-current').innerHTML = taskCard(activeTask, true);
            schedulePoll();
        } catch (error) { byId('mi-task-feedback').textContent = error.message; }
    }
    window.openMercadoInfringementTasks = async task => {
        if (task && typeof task === 'object') activeTask = task;
        if (!byId('mi-task-dialog').open) byId('mi-task-dialog').showModal();
        if (activeTask) byId('mi-task-current').innerHTML = taskCard(activeTask, true);
        await loadTasks();
    };
    async function taskAction(event) {
        const item = event.target.closest('[data-mi-item]');
        if (item) {
            const [mode, id] = item.dataset.miItem.split(':');
            byId('mi-task-dialog').close();
            await window.openMercadoInfringementDetail(mode, Number(id));
            return;
        }
        const cancel = event.target.closest('[data-mi-cancel]');
        const retry = event.target.closest('[data-mi-retry]');
        const inspect = event.target.closest('[data-mi-task]');
        const button = cancel || retry || inspect;
        if (!button) return;
        button.disabled = true;
        try {
            const id = cancel ? cancel.dataset.miCancel : retry ? retry.dataset.miRetry : inspect.dataset.miTask;
            const data = await request(`/tasks/${encodeURIComponent(id)}${cancel ? '/cancel' : retry ? '/retry' : ''}`, cancel || retry ? {method: 'POST'} : {});
            activeTask = data.task || data;
            byId('mi-task-current').innerHTML = taskCard(activeTask, true);
            await loadTasks();
            await refreshList();
            schedulePoll();
        } catch (error) { byId('mi-task-feedback').textContent = error.message; button.disabled = false; }
    }
    function schedulePoll() {
        clearTimeout(pollTimer);
        pollTimer = null;
        if (!polling && taskVisible() && activeTask && !terminal(activeTask) && taskId(activeTask)) pollTimer = setTimeout(pollTask, 2500);
    }
    async function pollTask() {
        pollTimer = null;
        if (polling || !taskVisible() || !activeTask || !taskId(activeTask)) return;
        polling = true;
        try {
            const data = await request(`/tasks/${encodeURIComponent(taskId(activeTask))}`);
            activeTask = data.task || data;
            byId('mi-task-current').innerHTML = taskCard(activeTask, true);
            if (terminal(activeTask) || Date.now() - refreshAt > 8000) await refreshList();
            if (terminal(activeTask) && byId('mi-task-dialog').open) await loadTasks();
        } catch (error) { byId('mi-task-feedback').textContent = `状态读取失败：${error.message}；可点击刷新重试。`; return; }
        finally { polling = false; }
        schedulePoll();
    }
    async function refreshList() {
        if (refreshing || !listVisible() || typeof loadMercadoList !== 'function' || !byId('mercado-list-module')) return;
        const mode = listMode();
        const page = typeof mercadoListPage !== 'undefined' ? mercadoListPage : 1;
        const selection = selectedIds();
        refreshing = true;
        try {
            await loadMercadoList();
            if (listMode() === mode && (typeof mercadoListPage === 'undefined' || mercadoListPage === page) && typeof selectedMercadoCollectionIds !== 'undefined') {
                selection.forEach(id => selectedMercadoCollectionIds.add(id));
                if (typeof updateMercadoSelection === 'function') updateMercadoSelection();
            }
            refreshAt = Date.now();
        } finally { refreshing = false; }
    }

    function renderEvidence(evidence) {
        if (!evidence) return '<p class="mi-muted">本次未返回额外证据。</p>';
        const entries = Array.isArray(evidence) ? evidence : typeof evidence === 'object' ? Object.entries(evidence).map(([key, value]) => ({label: key, value})) : [evidence];
        return entries.map(entry => {
            if (typeof entry !== 'object' || !entry) return `<p>${esc(entry)}</p>`;
            const url = safeUrl(entry.source_url || entry.url || entry.reference_url);
            const text = entry.reason || entry.description || entry.notes || entry.risk_description || entry.matched_text || entry.text || entry.value || '';
            return `<article class="mi-evidence"><strong>${esc(entry.name || entry.brand_name || entry.title || entry.label || entry.type || '命中依据')}</strong><p>${esc(typeof text === 'object' ? JSON.stringify(text) : text)}</p>${entry.risk_type ? `<small>类型：${esc(entry.risk_type)}</small>` : ''}${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>` : ''}${entry.knowledge_id || entry.id ? `<small>知识库记录：${esc(entry.knowledge_id || entry.id)}</small>` : ''}</article>`;
        }).join('');
    }
    function renderDetail(data) {
        detailData = data;
        const item = data.item || data.product || data;
        const result = data.result || data.latest_result || data.infringement || item.infringement || {};
        const risk = normalize({...item, infringement: {...result, ...(data.review ? {review: data.review} : {})}});
        const coverage = result.coverage || data.coverage || {};
        const image = safeUrl(item.main_image_url || data.main_image_url);
        const imageStatus = coverage.image_status || result.image_status || '';
        const imageComplete = risk.scope === 'title_image' && risk.imageVision && result.image_checked !== false && (result.image_checked === true || coverage.image === true || ['checked', 'completed', 'ok'].includes(imageStatus));
        const history = (data.history || data.detection_history || data.results || []).filter(record => !record.manual_decision && !record.reviewed_at);
        const reviews = data.review_history || result.review_history || [];
        byId('mi-detail-subtitle').textContent = `${detailContext.mode === 'products' ? '产品列表' : '采集列表'} · ${item.source_item_id || item.product_id || detailContext.id}`;
        byId('mi-detail-content').innerHTML = `<section class="mi-product-summary">${image ? `<img src="${esc(image)}" alt="商品主图" loading="lazy">` : ''}<div><h3>${esc(item.title || '商品资料')}</h3><span class="mi-risk-badge mi-${esc(risk.status)}">${esc(risk.label)}</span><p>检测内容：${esc(scopeName(risk.scope))} · ${esc(risk.checkedAt || '尚未完成检测')}</p>${risk.scope === 'title' ? '<p class="mi-coverage-note">本次只检查标题，图片尚未覆盖。</p>' : `<p class="mi-coverage-note">图片覆盖：${imageComplete ? '已完成主图视觉检测' : risk.imageOcr ? '仅识别图中文字，Logo 与角色外观尚未覆盖' : esc(imageStatus || '请核对检测证据；未确认图片完整覆盖')}。</p>`}</div></section><section class="mi-detail-section"><h3>检测原因</h3><p>${esc(risk.reason || '暂无检测结论。')}</p>${risk.keywords ? `<p><strong>命中关键词：</strong>${esc(Array.isArray(risk.keywords) ? risk.keywords.join('、') : risk.keywords)}</p>` : ''}<p class="mi-muted">商品版本：${esc(result.content_version || result.product_version || result.content_hash || '未提供')} · 知识库版本：${esc(result.knowledge_version || result.rule_version || '未提供')}</p></section><section class="mi-detail-section"><div class="mi-task-heading"><h3>命中知识与证据</h3><button type="button" class="secondary" onclick="document.getElementById('mi-detail-dialog').close(); switchTab('infringement-knowledge')">查看侵权知识库</button></div>${renderEvidence(result.evidence || data.evidence || result.matches || data.matches || data.rules)}</section>${history.length ? `<details class="mi-history"><summary>检测历史（${history.length}）</summary>${history.map(record => `<article><strong>${esc(record.checked_at || record.created_at || record.updated_at || '')} · ${esc(scopeName(record.detection_scope || record.scope))}</strong><p>${esc(record.reason || record.message || '')}</p><small>风险级别：${esc(record.risk_level ?? '未完成')} · ${esc(record.status || '')}</small></article>`).join('')}</details>` : ''}${reviews.length ? `<details class="mi-history"><summary>人工复核历史（${reviews.length}）</summary>${reviews.map(record => `<article><strong>${esc({pending: '待复核', blocked: '已拦截', released: '已放行'}[(record.decision || ({allow: 'released', block: 'blocked'})[record.manual_decision])] || record.decision || record.manual_decision)} · ${esc(record.created_at || record.reviewed_at || record.at || '')}</strong><p>${esc(record.reason || record.review_reason || '')}</p><small>${esc(record.evidence_ref || record.review_evidence_ref || '')}</small></article>`).join('')}</details>` : ''}`;
        const review = data.review || risk.review || {};
        byId('mi-review-decision').value = review.decision || risk.decision;
        byId('mi-review-reason').value = review.reason || result.review_reason || '';
        byId('mi-review-evidence').value = review.evidence_ref || result.review_evidence_ref || result.evidence_ref || '';
        byId('mi-review-sites').value = reviewSites({...result, review}).join(', ');
        const expiresAt = review.expires_at || result.review_expires_at || result.expires_at;
        byId('mi-review-expires').value = expiresAt && Number.isFinite(new Date(expiresAt).getTime()) ? new Date(new Date(expiresAt).getTime() - new Date(expiresAt).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
        byId('mi-review-form').hidden = !canReview();
        byId('mi-detail-recheck').disabled = ['queued', 'checking'].includes(risk.status);
        byId('mi-detail-recheck').hidden = !canExecute();
        byId('mi-detail-candidate').hidden = !canManageKnowledge();
        byId('mi-detail-extra-actions').hidden = !canExecute() && !canManageKnowledge();
        const keywords = Array.isArray(risk.keywords) ? risk.keywords : String(risk.keywords || '').split(/[,，、;；\n]+/).map(word => word.trim()).filter(Boolean);
        byId('mi-candidate-term').value = keywords[0] || '';
        byId('mi-candidate-notes').value = risk.reason.slice(0, 2000);
    }
    window.openMercadoInfringementDetail = async (mode, id) => {
        if (!Number(id)) return;
        const sequence = ++detailSequence;
        detailContext = {mode: mode === 'products' ? 'products' : 'collection', id: Number(id)};
        detailData = null;
        byId('mi-detail-content').textContent = '正在读取商品风险与证据…';
        byId('mi-review-form').hidden = true;
        byId('mi-candidate-form').hidden = true;
        byId('mi-detail-extra-actions').hidden = true;
        byId('mi-review-feedback').textContent = '';
        if (!byId('mi-detail-dialog').open) byId('mi-detail-dialog').showModal();
        try {
            const data = await request(`/items/${detailContext.mode}/${detailContext.id}`);
            if (sequence === detailSequence) renderDetail(data);
        } catch (error) { if (sequence === detailSequence) byId('mi-detail-content').textContent = `读取失败：${error.message}`; }
    };
    async function saveReview(event) {
        event.preventDefault();
        if (!detailContext || !canReview()) return;
        const context = {...detailContext};
        const decision = value('mi-review-decision');
        const reason = value('mi-review-reason').trim();
        const evidence = value('mi-review-evidence').trim();
        const siteIds = [...new Set(value('mi-review-sites').toUpperCase().split(/[\s,，;；]+/).filter(Boolean))];
        if (!reason) { byId('mi-review-reason').reportValidity(); return; }
        if (decision === 'released' && !evidence) { byId('mi-review-feedback').textContent = '复核放行需要填写授权或证据引用。'; byId('mi-review-evidence').focus(); return; }
        const expires = value('mi-review-expires');
        if (decision === 'released' && (!expires || !siteIds.length)) { byId('mi-review-feedback').textContent = '复核放行需要指定适用站点和有效期。'; return; }
        if (siteIds.some(site => !['MLM', 'MLB', 'MLA', 'MLC', 'MCO', 'MLU', 'MPE', 'MEC'].includes(site))) { byId('mi-review-feedback').textContent = '请输入有效的美客多站点编号，例如 MLM、MLB、MLC、MCO。'; return; }
        if (expires && new Date(expires).getTime() <= Date.now()) { byId('mi-review-feedback').textContent = '有效期应晚于当前时间。'; return; }
        byId('mi-review-submit').disabled = true;
        byId('mi-review-feedback').textContent = '正在保存人工复核…';
        try {
            await request(`/items/${context.mode}/${context.id}/review`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({decision, reason, site_ids: siteIds, expires_at: expires ? new Date(expires).toISOString() : null, evidence_ref: evidence})});
            await refreshList();
            if (detailContext?.mode === context.mode && detailContext.id === context.id) {
                await window.openMercadoInfringementDetail(context.mode, context.id);
                byId('mi-review-feedback').textContent = '人工复核已保存。';
            }
        } catch (error) { byId('mi-review-feedback').textContent = error.message; }
        finally { byId('mi-review-submit').disabled = false; }
    }
    async function saveCandidate(event) {
        event.preventDefault();
        if (!detailContext || !detailData || !canManageKnowledge()) return;
        const context = {...detailContext};
        const data = detailData;
        const item = data.item || data;
        const result = data.infringement || data.result || item.infringement || {};
        const term = value('mi-candidate-term').trim();
        if (!term) { byId('mi-candidate-term').reportValidity(); return; }
        byId('mi-candidate-submit').disabled = true;
        byId('mi-candidate-feedback').textContent = '正在保存待复核线索…';
        try {
            const response = await fetch('/api/infringement-knowledge/candidates', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
                brand_name: term, notes: value('mi-candidate-notes').trim(), source_type: 'detection',
                source_detail: `${context.mode === 'products' ? '产品列表' : '采集列表'}商品 ${item.source_item_id || item.product_id || context.id} · ${scopeName(result.detection_scope)}检测 · ${result.checked_at || ''}`,
                source_row_id: context.id, product_source: context.mode === 'products' ? 'product_list' : 'collection_list',
                product_id: item.source_item_id || item.product_id || '', title: item.title || '',
                keywords: result.keywords || '', reason: result.reason || result.check_error || '',
                content_hash: result.content_hash || '', rule_version: result.rule_version || '',
                detection_scope: result.detection_scope || 'title', image_method: result.image_method || '', image_checked: result.image_checked || false,
                evidence: result.evidence || {keywords: result.keywords || '', reason: result.reason || '', image_ocr_text: result.image_ocr_text || ''},
            })});
            const payload = await response.json();
            if (!response.ok || payload.status === 'error') throw new Error(payload.message || '线索保存失败');
            if (detailContext?.mode === context.mode && detailContext.id === context.id) {
                byId('mi-candidate-feedback').textContent = '已提交待复核线索，人工名单保持原处理方式。';
            }
        } catch (error) { byId('mi-candidate-feedback').textContent = error.message; }
        finally { byId('mi-candidate-submit').disabled = false; }
    }
    setupDialogs();
    byId('mi-task-dialog')?.addEventListener('close', schedulePoll);
    document.addEventListener('zeshun:visibility', schedulePoll);
    document.addEventListener('visibilitychange', schedulePoll);
    window.syncMercadoInfringementSelection();
})();
