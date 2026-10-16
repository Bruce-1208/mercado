/* One ordinary filter row; overflow reuses the original controls in advanced filters. */
(() => {
    const selector = [
        '.zs-filter-panel', '.zs-date-filter-panel', '.wdr-filters', '.wdr-history-filters',
        '.store-link-audit-filters', '.p1688-toolbar', '.knowledge-filters',
        '.ai-original-list-filters', '.ai-original-publish-filters', '.ai-video-filter-bar',
        '.window-anomaly-filter-bar', '.record-filters',
        'body.zs-standalone .filters', '.pd-filters', '.ca-filters', '.filter-panel',
        '[aria-label="店铺内容筛选"]', '.employee-toolbar:has(#employee-filter)',
    ].join(',');
    function initialize(panel) {
        if (panel.classList.contains('zs-compact-filters') || panel.closest('dialog')) return;
        // Supplier rows are only layout wrappers; keep every control and its listeners.
        panel.querySelectorAll(':scope > .p1688-toolbar-row, :scope > .zs-common-filter-row').forEach(row => row.replaceWith(...row.childNodes));
        // Keep Search available on narrow order pages; Reset can fold separately.
        panel.querySelectorAll(':scope > .order-filter-actions').forEach(actions => {
            const query = actions.querySelector('button.primary');
            if (!query) return;
            const field = document.createElement('div');
            field.className = 'zs-compact-query';
            actions.before(field);
            field.append(query);
        });
        let details = panel.querySelector(':scope > .zs-advanced-filters');
        if (!details) {
            details = document.createElement('details');
            details.className = 'zs-advanced-filters';
            details.innerHTML = '<summary>高级筛选</summary><div class="zs-advanced-grid"></div>';
            panel.append(details);
        }
        const grid = details.querySelector('.zs-advanced-grid');
        if (!grid) return;
        const summary = details.querySelector('summary');
        const fields = [...panel.children].filter(node => node !== details && !node.matches('script, style'));
        fields.forEach(node => { node.classList.add('zs-compact-item'); if (node.dataset.advancedOnly !== undefined) grid.append(node); });
        panel.classList.add('zs-compact-filters');
        const isControl = node => node.matches('input, select, button, label') || node.querySelector('input, select, button');
        const isAction = node => node.matches('button') || (!node.querySelector('input, select') && node.querySelector('button'));
        const isSearch = node => node.matches('input[type="search"]') || node.querySelector(':scope > input[type="search"], :scope > input[id*="search"]');
        function refreshSummary() {
            const count = [...grid.querySelectorAll('input, select')].filter(control => {
                if (control.closest('[hidden]') || control.type === 'search' && control.closest('.market-multi-picker')) return false;
                if (control.type === 'checkbox' || control.type === 'radio') return control.checked;
                return control.multiple ? [...control.selectedOptions].some(option => option.value) : Boolean(control.value);
            }).length;
            summary.textContent = count ? `高级筛选 (${count})` : '高级筛选';
            summary.title = count ? `高级筛选，已设置 ${count} 项` : '展开更多筛选条件';
            summary.setAttribute('aria-label', count ? summary.title : '高级筛选，展开更多筛选条件');
        }
        // Legacy date/workflow refreshers delegate here once the row is compact.
        panel.zsRefreshFilterSummary = refreshSummary;
        ['input', 'change', 'click'].forEach(name => panel.addEventListener(name, () => queueMicrotask(refreshSummary)));
        panel.addEventListener('reset', () => setTimeout(refreshSummary, 0));
        let lastWidth = -1;
        function layout() {
            const width = panel.clientWidth;
            if (!width) { lastWidth = -1; return; }
            if (width === lastWidth) return;
            lastWidth = width;
            const focused = document.activeElement;
            const style = getComputedStyle(panel);
            let available = width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) - 112;
            const mobile = width < 600;
            const actionWidth = node => {
                const buttons = (node.matches('button') ? [node] : [...node.querySelectorAll('button')])
                    .filter(button => !button.hidden && getComputedStyle(button).display !== 'none');
                // Account for each button's padding and minimum width: estimating
                // the combined label alone lets query/reset overlap the next field.
                return buttons.reduce((total, button) => {
                    const css = getComputedStyle(button);
                    return total + Math.max(56, parseFloat(css.minWidth) || 0,
                        button.textContent.trim().length * (parseFloat(css.fontSize) || 13)
                        + parseFloat(css.paddingLeft) + parseFloat(css.paddingRight)
                        + parseFloat(css.borderLeftWidth) + parseFloat(css.borderRightWidth));
                }, 0) + Math.max(0, buttons.length - 1) * 8;
            };
            const widths = new Map(fields.map(node => [node, isAction(node)
                ? actionWidth(node)
                : isSearch(node) ? (mobile ? (width < 340 ? 80 : 100) : 180) : 140]));
            // Keep search and query buttons first, then the original common-field order.
            const priority = node => isSearch(node) ? 4 : isAction(node)
                ? (node.matches('.primary') || node.querySelector('button.primary') ? 3 : 2) : 1;
            const ranked = [...fields].sort((a, b) => priority(b) - priority(a));
            const keep = new Set();
            ranked.forEach(node => {
                if (node.dataset.advancedOnly !== undefined || node.hidden || getComputedStyle(node).display === 'none') return;
                const size = widths.get(node) + 8;
                if (isControl(node) && size <= available) { keep.add(node); available -= size; }
            });
            const columns = [];
            fields.forEach(node => {
                if (keep.has(node)) {
                    if (node.parentElement !== panel) panel.insertBefore(node, details);
                    columns.push(isAction(node) ? `minmax(0, ${widths.get(node)}px)` : `minmax(0, ${widths.get(node)}fr)`);
                } else if (node.parentElement !== grid) grid.append(node);
            });
            // Restore original order without replacing nodes (including multi-selects).
            fields.filter(node => keep.has(node)).forEach(node => panel.insertBefore(node, details));
            panel.style.gridTemplateColumns = [...columns, 'minmax(0, 104px)'].join(' ');
            if (focused && grid.contains(focused)) details.open = true;
            if (focused && focused !== document.body && panel.contains(focused)) focused.focus({preventScroll: true});
            refreshSummary();
        }
        panel.addEventListener('invalid', event => {
            if (grid.contains(event.target)) details.open = true;
        }, true);
        panel.addEventListener('keydown', event => {
            if (!event.defaultPrevented && event.key === 'Escape' && details.open && !panel.querySelector('[aria-expanded="true"]')) {
                event.preventDefault();
                details.open = false;
                summary.focus();
            }
        });
        const visibility = new MutationObserver(() => { lastWidth = -1; layout(); });
        fields.forEach(node => visibility.observe(node, {attributes: true, attributeFilter: ['hidden', 'style', 'class']}));
        new ResizeObserver(layout).observe(panel);
        layout();
    }
    const start = () => document.querySelectorAll(selector).forEach(initialize);
    // Existing date and workflow initializers must finish before measuring their fields.
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => requestAnimationFrame(start));
    else requestAnimationFrame(start);
})();
