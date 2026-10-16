/* Shared catalog presentation. Move existing controls so permissions, values and
   business handlers stay with their original nodes. */
(() => {
    const byId = id => document.getElementById(id);
    const mark = (selector, name) => document.querySelectorAll(selector).forEach(node => node.classList.add(name));
    function initialize() {
        mark('#tab-1688-products, #tab-mercado-collection, #tab-ai-original-products, #tab-mercado-products', 'zs-catalog-workspace');
        mark('.zs-catalog-workspace > .tab-heading', 'zs-catalog-heading');
        mark('.p1688-toolbar, #mercado-product-filters, .ai-original-list-filters', 'zs-catalog-filters');
        mark('.p1688-batch-bar, #mercado-collection-actions, .ai-original-list-head', 'zs-catalog-selection');
        mark('.p1688-pagination, #mercado-list-pagination, .ai-original-list-panel > nav', 'zs-catalog-pagination');

        // Filtering precedes selection in every workspace, including AI drafts.
        const originalPanel = document.querySelector('.ai-original-list-panel');
        const originalFilters = document.querySelector('.ai-original-list-filters');
        if (originalPanel && originalFilters) originalPanel.prepend(originalFilters);

        const supplierBar = document.querySelector('.p1688-batch-bar');
        if (supplierBar && !byId('products-1688-bulk-menu')) {
            const bulk = document.createElement('details');
            bulk.id = 'products-1688-bulk-menu';
            bulk.className = 'zs-bulk-menu';
            bulk.innerHTML = '<summary>批量编辑</summary><div class="market-selection-tools"></div>';
            const tools = bulk.lastElementChild;
            [...supplierBar.children].filter(node => node.matches('.p1688-batch-review, button.secondary, button.danger')).forEach(node => tools.append(node));
            byId('products-1688-selection').after(bulk);
            const primary = supplierBar.querySelector(':scope > button.primary');
            if (primary) supplierBar.append(primary);
        }
        const marketBulk = document.querySelector('#mercado-collection-actions > .zs-bulk-menu');
        if (marketBulk && byId('mercado-collection-only-actions')) marketBulk.after(byId('mercado-collection-only-actions'));

        // Installation guidance stays available without dominating the list.
        const guide = document.querySelector('#tab-mercado-collection .collector-plugin-panel');
        if (guide && !guide.closest('details')) {
            const disclosure = document.createElement('details');
            disclosure.className = 'zs-catalog-help';
            disclosure.innerHTML = '<summary>采集插件 · 安装与使用说明</summary>';
            guide.before(disclosure);
            disclosure.append(guide);
            const actions = document.querySelector('#tab-mercado-collection .market-flow-indicator');
            const download = guide.querySelector('.collector-plugin-download');
            if (actions && download) {
                // Keep the original download destination and browser behavior.
                download.textContent = '下载泽顺插件';
                download.className = 'primary';
                actions.prepend(download);
            }
        }

        ['products-1688-select-all', 'ai-original-select-all', 'mercado-select-all'].forEach(id => {
            const input = byId(id);
            if (!input) return;
            input.setAttribute('aria-label', '全选本页商品');
            [...input.parentElement.childNodes].filter(node => node.nodeType === Node.TEXT_NODE).forEach(node => { node.textContent = ' 全选本页'; });
        });
        [['products-1688-prev', '上一页'], ['products-1688-next', '下一页']].forEach(([id, label]) => {
            const control = byId(id);
            if (control) { control.textContent = label; control.setAttribute('aria-label', label); }
        });
        mark('#products-1688-selection, #ai-original-selection, #mercado-selection-summary', 'zs-catalog-selection-count');

        document.querySelectorAll('.p1688-filter-actions .primary, #mercado-product-filters .market-filter-actions .primary, .ai-original-list-filters > button').forEach(button => {
            button.textContent = '查询';
            button.classList.remove('secondary');
            button.classList.add('primary');
        });
        // Let the compact-filter layout keep Query next to search on phones;
        // Reset can move independently into advanced filters when space is short.
        document.querySelectorAll('.p1688-filter-actions, #mercado-product-filters .market-filter-actions').forEach(actions => {
            const query = actions.querySelector('button.primary');
            if (!query) return;
            const field = document.createElement('div');
            field.className = 'zs-catalog-query';
            actions.before(field);
            field.append(query);
        });
        // The market list already reveals bulk editing after selecting a product.
        // Apply that same presentation to the supplier and AI lists.
        for (const [countId, menuId, selectId, hasSelected] of [
            ['products-1688-selection', 'products-1688-bulk-menu', 'products-1688-select-all', () => products1688Selected.size > 0],
            ['ai-original-selection', 'ai-original-bulk-menu', 'ai-original-select-all', () => aiOriginalSelected.size > 0],
        ]) {
            const count = byId(countId), menu = byId(menuId);
            if (!count || !menu) continue;
            const sync = () => {
                const hasSelection = hasSelected();
                if (!hasSelection && menu.contains(document.activeElement)) {
                    byId(selectId)?.focus();
                }
                menu.hidden = !hasSelection;
                if (!hasSelection) menu.open = false;
            };
            new MutationObserver(sync).observe(count, {childList: true, characterData: true, subtree: true});
            sync();
        }

        // Native details already support Enter/Space; Escape returns to its trigger.
        document.addEventListener('keydown', event => {
            if (event.key !== 'Escape' || event.defaultPrevented || event.target.closest('dialog, .market-multi-picker')) return;
            const details = event.target.closest('.zs-catalog-workspace details[open]');
            if (!details?.matches('.zs-bulk-menu, .zs-catalog-help')) return;
            event.preventDefault();
            details.open = false;
            details.querySelector(':scope > summary').focus();
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
    else initialize();
})();
