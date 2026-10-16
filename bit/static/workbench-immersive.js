/* Workspace-only viewing. All controls keep their existing handlers and permissions. */
(() => {
    const body = document.body;
    const orders = document.getElementById('tab-orders');
    const mobile = matchMedia('(max-width: 760px)');
    let preference = null;
    try { preference = localStorage.getItem('zeshun-immersive'); } catch (_) {}
    const exit = document.createElement('button');
    exit.id = 'workbench-immersive-exit';
    exit.className = 'secondary';
    exit.type = 'button';
    exit.textContent = '退出沉浸';
    exit.hidden = true;
    exit.onclick = () => window.setWorkbenchImmersive(false);
    body.append(exit);
    const restore = document.createElement('button');
    restore.id = 'order-rows-restore';
    restore.className = 'secondary';
    restore.type = 'button';
    restore.textContent = '显示工具';
    restore.hidden = true;
    restore.onclick = () => { window.setOrderRowsOnly(false); document.getElementById('order-rows-only-toggle').focus(); };
    body.append(restore);
    function refresh() {
        const activeOrders = orders?.classList.contains('active');
        const immersive = preference === 'true' || (preference === null && mobile.matches && activeOrders);
        const rowsOnly = activeOrders && orders.classList.contains('order-rows-only');
        body.classList.toggle('workbench-immersive', Boolean(immersive));
        body.classList.toggle('workbench-rows-only', Boolean(rowsOnly));
        exit.hidden = !immersive || rowsOnly;
        restore.hidden = !rowsOnly;
        document.querySelectorAll('[data-immersive-toggle]').forEach(button => {
            button.setAttribute('aria-pressed', String(Boolean(immersive)));
        });
        if (immersive && body.classList.contains('zs-mobile-nav-open')) document.querySelector('.zs-nav-backdrop')?.click();
        window.resizeOrderViewport?.();
    }
    window.setWorkbenchImmersive = enabled => {
        preference = String(enabled);
        try { localStorage.setItem('zeshun-immersive', preference); } catch (_) {}
        if (!enabled) window.setOrderRowsOnly(false);
        refresh();
        if (enabled) exit.focus();
        else document.querySelector('.topbar [data-immersive-toggle]')?.focus();
    };
    window.setOrderRowsOnly = enabled => {
        orders.classList.toggle('order-rows-only', enabled);
        document.getElementById('order-rows-only-toggle').setAttribute('aria-pressed', String(enabled));
        if (enabled) {
            window.setOrderListFocus(true);
            window.setOrderDensity('compact', false);
            preference = 'true';
        }
        refresh();
        if (enabled) restore.focus();
    };
    document.querySelectorAll('.tab-page').forEach(tab => {
        new MutationObserver(refresh).observe(tab, {attributes: true, attributeFilter: ['class']});
    });
    mobile.addEventListener('change', refresh);
    document.addEventListener('keydown', event => {
        if (event.defaultPrevented || event.key !== 'Escape' || document.querySelector('dialog[open], .market-multi-picker.open')) return;
        if (orders?.classList.contains('active') && orders.classList.contains('order-rows-only')) {
            window.setOrderRowsOnly(false); document.getElementById('order-rows-only-toggle').focus();
        } else if (body.classList.contains('workbench-immersive')) window.setWorkbenchImmersive(false);
    });
    refresh();
})();
