/* Shared presentation and feedback. Business requests remain in their modules. */
(() => {
    'use strict';
    const labels = {loading: '正在加载', empty: '暂无数据', error: '查询失败', partial: '部分成功', denied: '权限不足', success: '操作成功', info: '提示'};
    const hints = {loading: '请稍候，数据读取完成后会自动显示。', empty: '可以调整筛选条件，或刷新数据。', error: '请检查网络后重新加载；提交结果不确定时，先查询任务状态。', partial: '请查看失败明细，仅处理未成功的项目。', denied: '请联系管理员开通权限，或切换有权限的账号。'};
    function feedback(node, kind = 'info') {
        if (!node) return;
        node.classList.add('zs-feedback');
        node.dataset.uiState = labels[kind] ? kind : 'info';
        node.dataset.uiLabel = labels[kind] || labels.info;
        node.dataset.uiHint = ['partial', 'denied'].includes(kind) ? hints[kind] : '';
        node.setAttribute('role', ['error', 'denied'].includes(kind) ? 'alert' : 'status');
        node.setAttribute('aria-live', ['error', 'denied'].includes(kind) ? 'assertive' : 'polite');
    }
    function errorKind(error) { return [401, 403].includes(error?.status) ? 'denied' : 'error'; }
    function tableState(body, kind, message, retry) {
        if (!body) return;
        body.dataset.uiState = kind;
        const cell = body.rows?.[0]?.cells?.[0];
        if (!cell || body.rows.length !== 1 || cell.colSpan < 2) return;
        cell.textContent = message;
        cell.classList.add('empty-state');
        const hint = document.createElement('small');
        hint.className = 'zs-state-hint';
        hint.textContent = hints[kind] || '';
        cell.append(hint);
        if (retry && kind === 'error') {
            const button = document.createElement('button');
            button.type = 'button'; button.className = 'secondary'; button.textContent = '重新加载';
            button.addEventListener('click', retry);
            cell.append(button);
        }
    }
    window.WorkbenchUI = {feedback, errorKind, tableState};

    function boot() {
        // Only observe table bodies, never the entire workbench subtree.
        document.querySelectorAll('tbody').forEach(body => {
            const table = body.closest('table');
            const container = table?.parentElement;
            if (!container || !/(wrap|scroll|table-container)/.test(container.className)) return;
            container.classList.add('zs-table-scroll');
            container.tabIndex = 0;
            container.setAttribute('role', 'region');
            container.setAttribute('aria-label', table.getAttribute('aria-label') || '数据列表，可横向滚动');
            const resize = () => container.style.setProperty('--zs-table-width', `${container.clientWidth}px`);
            new ResizeObserver(resize).observe(container);
            resize();
            const enhance = () => {
                const cell = body.rows.length === 1 && body.rows[0].cells.length === 1 ? body.rows[0].cells[0] : null;
                if (!cell || cell.colSpan < 2 || cell.querySelector('.zs-table-state')) return;
                if (!cell.matches('.empty-state, .empty, .p1688-empty') && !cell.parentElement.matches('.order-loading-row')) return;
                const content = document.createElement('div');
                content.className = 'zs-table-state';
                content.setAttribute('role', ['error', 'denied'].includes(body.dataset.uiState) ? 'alert' : 'status');
                while (cell.firstChild) content.append(cell.firstChild);
                cell.append(content);
            };
            new MutationObserver(enhance).observe(body, {childList: true, subtree: true});
            enhance();
        });
        const orderFilters = document.querySelector('#tab-orders .order-filters');
        const orderBulk = document.getElementById('order-bulk-bar');
        if (orderFilters && orderBulk) orderFilters.after(orderBulk);
        // Operational logs remain available below the list without interrupting filtering.
        ['store-link-sync-log', 'store-link-remote-update-log'].forEach(id => {
            const log = document.getElementById(id)?.closest('details');
            const panel = log?.parentElement;
            if (log && panel?.querySelector('.store-link-table-wrap')) {
                log.open = false;
                panel.append(log);
            }
        });
        const clearers = [
            [orderFilters, () => clearOrderSelection()],
            [document.querySelector('.store-link-filters'), () => clearStoreLinkSelection()],
            [document.getElementById('mercado-product-filters'), () => { selectedMercadoCollectionIds.clear(); updateMercadoSelection(); }],
            [document.querySelector('.p1688-toolbar'), () => { products1688Selected.clear(); products1688UpdateSelection(); }],
        ];
        clearers.forEach(([panel, clear]) => {
            if (!panel) return;
            const changed = event => {
                // Picker search changes only the popup's visible options, not the result set.
                if (event.target.closest('.zs-picker-search') || !event.target.matches('input, select')) return;
                clear();
            };
            panel.addEventListener('input', changed);
            panel.addEventListener('change', changed);
        });
        [['order-select-all','选中本页订单'], ['store-link-select-all','选中本页链接，已选链接可跨页保留'], ['mercado-select-all','选中本页商品']].forEach(([id,label]) => {
            const node = document.getElementById(id);
            if (node) { node.setAttribute('aria-label',label); node.title = label; }
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
    else boot();
})();
