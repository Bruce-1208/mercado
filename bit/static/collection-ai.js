let collectionAiTimer = null;
let collectionAiBusy = false;
let collectionAiSubmitting = false;
const collectionAiElement = id => document.getElementById('collection-ai-' + id);

function syncCollectionAiTarget() {
    const agent = collectionAiElement('target').value === 'agent';
    collectionAiElement('agent-controls').hidden = !agent;
    collectionAiElement('start').disabled = collectionAiSubmitting || collectionAiBusy ||
        !selectedMercadoCollectionIds.size || (agent && !collectionAiElement('agent').value);
    collectionAiElement('stop').disabled = collectionAiSubmitting || !collectionAiBusy;
}

async function loadCollectionAiAgents() {
    const select = collectionAiElement('agent');
    const previous = select.value;
    select.replaceChildren(new Option('正在读取在线 Agent', ''));
    syncCollectionAiTarget();
    try {
        const response = await fetch('/api/execution-agents?capability=ai_weight_price', {cache: 'no-store'});
        const body = await response.json();
        if (!response.ok) throw Error(body.message || '读取 Agent 失败');
        const agents = (body.data?.agents || []).filter(agent => agent.online);
        select.replaceChildren(...agents.map(agent => new Option(`${agent.name} · ${agent.hostname || '未知主机'}`, agent.agent_id)));
        if (!agents.length) select.add(new Option('没有在线 Agent，请启动或更新 Agent 后刷新', ''));
        if (agents.some(agent => agent.agent_id === previous)) select.value = previous;
    } catch (error) {
        select.replaceChildren(new Option('读取失败，请刷新电脑', ''));
        collectionAiElement('status').textContent = error.message;
    } finally { syncCollectionAiTarget(); }
}

async function refreshCollectionAiStatus() {
    clearTimeout(collectionAiTimer);
    try {
        const response = await fetch('/api/mercado-collection/ai-weight-price', {cache: 'no-store'});
        const body = await response.json();
        if (!response.ok) throw Error(body.message || '读取核查状态失败');
        const data = body.data || {}, run = data.run || {};
        const outcome = {running: '执行中', completed: '已完成', failed: '失败', stopped: '已停止', blocked: '已暂停'}[run.outcome] || '';
        const wasBusy = collectionAiBusy;
        collectionAiBusy = Boolean(data.running || data.pending);
        collectionAiElement('status').textContent = data.pending ? '有旧任务等待插件领取，可停止后重新启动' :
            run.run_id ? `${run.execution_terminal || (run.execution_target === 'server' ? '服务器 Edge' : 'Agent')} · ${outcome} · ${run.message || ''} · 已处理 ${run.processed_items || 0}/${run.max_items || 0}` :
            `已选择 ${selectedMercadoCollectionIds.size} 件商品`;
        if (wasBusy && !collectionAiBusy) await loadMercadoList();
    } catch (error) {
        collectionAiElement('status').textContent = error.message;
    } finally {
        syncCollectionAiTarget();
        if (collectionAiElement('dialog').open) collectionAiTimer = setTimeout(refreshCollectionAiStatus, 3000);
    }
}

async function openMercadoCollectionAiCheck() {
    collectionAiElement('dialog').showModal();
    // Do not permit submission until the previous batch has been checked.
    collectionAiBusy = true;
    syncCollectionAiTarget();
    await Promise.all([loadCollectionAiAgents(), refreshCollectionAiStatus()]);
}

async function startMercadoCollectionAiCheck() {
    if (collectionAiSubmitting || collectionAiBusy) return;
    const ids = Array.from(selectedMercadoCollectionIds);
    if (!ids.length) return;
    collectionAiSubmitting = true;
    syncCollectionAiTarget();
    try {
        const response = await fetch('/api/mercado-collection/ai-weight-price', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({collection_item_ids: ids,
                execution_target: collectionAiElement('target').value,
                agent_id: collectionAiElement('agent').value}),
        });
        const body = await response.json();
        if (!response.ok || body.status !== 'success') throw Error(body.message || '启动失败');
        collectionAiBusy = true;
        setMercadoListFeedback(body.data.message + '；完成后自动刷新重量尺寸和核价记录。', 'success');
        await refreshCollectionAiStatus();
    } catch (error) {
        collectionAiElement('status').textContent = error.message;
        setMercadoListFeedback(error.message, 'error');
    } finally { collectionAiSubmitting = false; syncCollectionAiTarget(); }
}

async function stopMercadoCollectionAiCheck() {
    collectionAiSubmitting = true;
    syncCollectionAiTarget();
    try {
        const response = await fetch('/api/mercado-collection/ai-weight-price', {method: 'DELETE'});
        const body = await response.json();
        if (!response.ok) throw Error(body.message || '停止失败');
        await refreshCollectionAiStatus();
        await loadMercadoList();
    } catch (error) { collectionAiElement('status').textContent = error.message; }
    finally { collectionAiSubmitting = false; syncCollectionAiTarget(); }
}
