/* API-first seller responses; Agent fallback opens the authorized store. */
(() => {
    const byId = id => document.getElementById(id);
    const dialog = byId('rights-dialog');
    if (!dialog) return;
    let selected = null, busy = false, replyAllowed = false, activeTask = '', pollTimer;
    const message = text => { byId('rights-message').textContent = text; };
    function controls(value) {
        busy = value;
        byId('rights-submit').disabled = value || !replyAllowed || Boolean(activeTask);
        byId('rights-browser').disabled = value || Boolean(activeTask);
        byId('rights-close').disabled = value;
    }
    async function request(url, options) {
        const response = await fetch(url, {cache: 'no-store', ...options});
        const payload = await response.json();
        if (!response.ok || payload.status !== 'success') {
            const error = new Error(payload.message || `请求失败（${response.status}）`);
            error.status = response.status;
            throw error;
        }
        return payload.data;
    }
    async function refreshAgents() {
        const select = byId('rights-agent'), previous = select.value;
        try {
            const data = await request('/api/execution-agents?capability=daily_task');
            const agents = (data.agents || []).filter(agent => agent.online);
            select.replaceChildren(new Option(agents.length ? '请选择回复电脑' : '暂无在线 Agent', ''));
            agents.forEach(agent => select.add(new Option(agent.name || agent.agent_id, agent.agent_id)));
            if (agents.some(agent => agent.agent_id === previous)) select.value = previous;
            else if (agents.length === 1) select.value = agents[0].agent_id;
        } catch (error) { showToast(`读取回复电脑失败：${error.message}`); }
    }
    async function pollTask() {
        if (!activeTask) return;
        try {
            const data = await request(`/api/mercado/today/rights-holder/open-store/${encodeURIComponent(activeTask)}`);
            message(data.message || '等待 Agent 打开店铺');
            if (data.running) { pollTimer = setTimeout(pollTask, 2000); return; }
            activeTask = '';
            if (data.status === 'success') {
                message(`${data.message}。请在浏览器中找到案件 ${selected.infraction_id}（${selected.item_id}）完成回复；此处尚未确认发送。`);
            } else message(data.message || 'Agent 打开失败，请检查电脑后重试');
            controls(false);
        } catch (error) {
            message(`任务已提交，状态查询中断：${error.message}。正在重查，请勿重复打开。`);
            pollTimer = setTimeout(pollTask, 5000);
        }
    }
    async function openBrowser(prefix = '') {
        if (activeTask) return;
        const agentId = byId('rights-agent').value;
        if (!agentId) {
            message(`${prefix}\n请在上方选择在线回复电脑，再点击“在对应店铺中处理”。回复内容和附件会保留。`);
            byId('rights-agent').focus();
            return;
        }
        controls(true);
        message(`${prefix}\n正在向所选电脑提交打开店铺任务…`);
        try {
            const data = await request(`/api/mercado/today/rights-holder/${Number(selected.token_id)}/open-browser`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({agent_id: agentId, execution_target: 'agent'}),
            });
            activeTask = data.task_id;
            message(`${prefix}\n已提交到 ${data.agent_name}，等待打开对应店铺。回复文字和附件请在浏览器中填写。`);
            pollTimer = setTimeout(pollTask, 1000);
        } catch (error) { message(`${prefix}\n打开店铺未确认：${error.message}，请检查 Agent 任务状态后再操作。`); }
        finally { controls(false); }
    }
    const drafts = new Map();
    const key = row => `${row.token_id}/${row.infraction_id}`;
    function saveDraft() {
        if (selected) drafts.set(key(selected), {text: byId('rights-text').value});
    }
    byId('rights-cases').addEventListener('click', async event => {
        const button = event.target.closest('[data-rights-index]');
        if (!button || busy) return;
        if (activeTask) { dialog.showModal(); return; }
        saveDraft();
        selected = (actionData.rights_holder_replies || [])[Number(button.dataset.rightsIndex)];
        if (!selected) return;
        byId('rights-text').value = drafts.get(key(selected))?.text || '';
        byId('rights-document').value = '';
        byId('rights-context').textContent = `${selected.store_name} · ${selected.site_id}\n商品：${selected.item_id} · 案件：${selected.infraction_id}\n${selected.infraction_reason || ''}`;
        replyAllowed = false;
        controls(true);
        message('正在读取平台案件状态…');
        dialog.showModal();
        try {
            const data = await request(`/api/mercado/today/rights-holder/${Number(selected.token_id)}/${encodeURIComponent(selected.infraction_id)}`);
            if (data.mode === 'browser') {
                await openBrowser(data.message);
            } else {
                replyAllowed = Boolean(data.can_reply);
                message(replyAllowed
                    ? `${data.comment ? `权利人说明：${data.comment}\n` : ''}${data.image_case ? '图片举报也可先提交文字，附件可选；是否接受由平台判断。需替换图片时请选择在店铺中处理。' : '填写回复，可附上证明后直接提交。'}`
                    : `当前状态：${data.current_status}，案件已不可回复，请刷新待办。`);
            }
        } catch (error) {
            message(error.message);
            if (!error.status || error.status >= 500) await openBrowser('API 无法读取案件，请在平台核实状态后回复。');
        } finally { controls(false); }
    });
    byId('rights-form').addEventListener('submit', async event => {
        event.preventDefault();
        if (busy || !replyAllowed || activeTask) return;
        saveDraft();
        const form = new FormData();
        form.set('text', byId('rights-text').value);
        const file = byId('rights-document').files[0];
        if (file) {
            if (file.size > 5 * 1024 * 1024) { message('附件不能超过 5 MB'); return; }
            form.set('document', file);
        }
        controls(true);
        message('正在提交回复，请等待结果…');
        try {
            const data = await request(`/api/mercado/today/rights-holder/${Number(selected.token_id)}/${encodeURIComponent(selected.infraction_id)}`, {method: 'POST', body: form});
            message(data.message);
            if (['submitted', 'closed', 'unknown'].includes(data.mode)) replyAllowed = false;
            if (['submitted', 'unknown'].includes(data.mode)) {
                drafts.delete(key(selected));
                // User policy: hide recorded reply attempts, independently of approval.
                actionData.rights_holder_replies = (actionData.rights_holder_replies || []).filter(row => key(row) !== key(selected));
                renderTasks();
                const notice = data.mode === 'submitted' ? '回复已提交，已移出今日待办' : '已记录本次回复操作，已移出今日待办';
                message(notice);
                showToast(notice);
                dialog.close();
            }
            if (data.mode === 'browser') await openBrowser(data.message);
        } catch (error) {
            message(error.message);
            if (!error.status || error.status >= 500) {
                replyAllowed = false;
                message('连接暂时中断，正在刷新今日待办。');
                await loadData();
            }
        } finally { controls(false); }
    });
    byId('rights-browser').addEventListener('click', () => { saveDraft(); return openBrowser(); });
    byId('rights-close').addEventListener('click', () => { saveDraft(); dialog.close(); });
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); else saveDraft(); });
    byId('rights-agent-refresh').addEventListener('click', refreshAgents);
    refreshAgents();
})();
