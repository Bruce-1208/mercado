(async () => {
    if (typeof hasWorkbenchPermission !== 'function' || !hasWorkbenchPermission('infringement_knowledge.view')) return;
    const root = document.getElementById('tab-overview');
    if (!root) return;
    const warning = document.createElement('div'); warning.id = 'knowledge-sync-health'; warning.hidden = true;
    warning.setAttribute('role', 'status'); root.prepend(warning);
    try {
        const response = await fetchAnalysis('/api/infringement-knowledge/wipo/status', {cache: 'no-store', deadlineMs: 30000});
        const payload = await response.json();
        if (!response.ok || payload.status !== 'success') throw new Error(payload.message || '读取失败');
        const data = payload.data || {};
        if (!['error', 'failed', 'action_required', 'interrupted'].includes(data.status)) return;
        warning.hidden = false;
        warning.textContent = `知识库同步需处理：${data.message || '最近同步失败'}。最近成功：${DataFreshness.time(data.last_success_at)}（北京时间）。 `;
        const link = document.createElement('a'); link.href = '/?tab=infringement-knowledge'; link.textContent = '前往知识库调整数据来源与同步配置'; warning.append(link);
        warning.style.cssText = 'padding:12px;margin-bottom:12px;background:#fff3d6;color:#714a06;border-radius:8px';
    } catch (_) {
        warning.hidden = false; warning.textContent = '知识库同步状态暂时无法确认，请在知识库的数据来源与同步页重试。';
    }
})();
