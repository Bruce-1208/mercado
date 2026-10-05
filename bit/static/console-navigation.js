/* Group existing navigation nodes so permissions and click handlers stay intact. */
(() => {
    const nav = document.querySelector('.sidebar-scroll');
    if (!nav) return;
    const buttons = [...nav.querySelectorAll('.tab-button')];
    const groups = [
        ['work', '日常工作', ['overview', '/mercado/today', '/employee-tasks', 'tasks']],
        ['products', '选品与刊登', ['1688-products', 'mercado-collection', 'ai-original-products', 'mercado-products', 'ai-weight-price', 'ai-video', 'mercado-publish-records', 'size-charts', 'mercado-shipping-standards']],
        ['orders', '订单与库存', ['orders', 'inventory', 'weight-dimensions-records']],
        ['shops', '店铺与营销', ['window-anomalies', 'store-tokens', 'store-links', 'promotions', 'clearance-alerts', 'yandex', 'store-operations']],
        ['service', '客服与申诉', ['pre-sale', 'customer-service', 'appeal', 'ai-appeals']],
        ['risk', '风控与合规', ['prohibited-listings', 'ip-rights', 'infractions', 'risk-check', 'infringement-knowledge', 'reputation', 'api-reputation']],
        ['analytics', '数据与资金', ['order-analysis', 'store-analysis', 'salesperson-stats', 'ad-analysis', 'funds']],
        ['system', '系统管理', ['access']],
    ];
    const key = 'zeshun-navigation-groups-v1';
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(key) || '{}') || {}; } catch (_) {}
    const persist = () => { try { localStorage.setItem(key, JSON.stringify(saved)); } catch (_) {} };
    const toolbar = document.createElement('div');
    toolbar.className = 'nav-tools';
    toolbar.innerHTML = '<input type="search" aria-label="搜索功能" placeholder="搜索功能…"><div><button type="button" data-expand="true">全部展开</button><button type="button" data-expand="false">全部收起</button></div>';
    nav.before(toolbar);
    nav.querySelectorAll('.nav-title').forEach(title => title.remove());
    const sections = [];
    const assigned = new Set();
    groups.forEach(([id, label, ids]) => {
        const items = ids.map(item => buttons.find(b => b.dataset.tab === item || (!b.dataset.tab && b.getAttribute('onclick')?.includes(`'${item}'`)))).filter(Boolean);
        if (!items.length) return;
        const section = document.createElement('details');
        section.className = 'nav-section';
        section.dataset.group = id;
        const summary = document.createElement('summary');
        summary.innerHTML = `<span>${label}</span><small></small>`;
        const body = document.createElement('div');
        body.className = 'nav-section-items';
        items.forEach(b => { assigned.add(b); b.title = b.textContent.trim(); body.append(b); });
        section.append(summary, body);
        section.open = typeof saved[id] === 'boolean' ? saved[id] : items.some(b => b.classList.contains('active'));
        section.addEventListener('toggle', () => {
            if (!search.value.trim()) { saved[id] = section.open; persist(); }
        });
        nav.append(section);
        sections.push({section, items, summary});
    });
    // Keep future modules reachable even before they receive a category.
    buttons.filter(b => !assigned.has(b)).forEach(b => nav.append(b));
    const empty = document.createElement('p');
    empty.className = 'nav-empty';
    empty.textContent = '没有匹配的功能';
    nav.append(empty);
    const search = toolbar.querySelector('input');
    let searching = false;
    let beforeSearch = [];
    const refresh = () => {
        const query = search.value.trim().toLocaleLowerCase();
        if (query && !searching) beforeSearch = sections.map(({section}) => section.open);
        buttons.forEach(button => {
            if (button.classList.contains('active')) button.setAttribute('aria-current', 'page');
            else button.removeAttribute('aria-current');
        });
        let total = 0;
        sections.forEach(({section, items, summary}, index) => {
            let count = 0;
            items.forEach(b => {
                const match = !query || `${summary.firstElementChild.textContent} ${b.textContent}`.toLocaleLowerCase().includes(query);
                b.classList.toggle('nav-search-hidden', !match);
                if (!b.hidden && match) count++;
            });
            summary.lastElementChild.textContent = count;
            section.hidden = count === 0;
            summary.classList.toggle('has-active', items.some(b => b.classList.contains('active')));
            if (query) section.open = count > 0;
            else if (searching) section.open = beforeSearch[index];
            total += count;
        });
        searching = Boolean(query);
        empty.hidden = total > 0;
    };
    search.addEventListener('input', refresh);
    toolbar.querySelectorAll('button').forEach(button => button.addEventListener('click', () => {
        search.value = ''; refresh();
        sections.forEach(({section}) => { section.open = button.dataset.expand === 'true'; });
    }));
    buttons.forEach(b => new MutationObserver(records => {
        if (records.some(r => r.attributeName === 'class' && b.classList.contains('active') && !(r.oldValue || '').split(' ').includes('active'))) {
            search.value = ''; refresh();
            const section = b.closest('.nav-section');
            if (section) section.open = true;
        }
        refresh();
    }).observe(b, {attributes: true, attributeFilter: ['hidden', 'class'], attributeOldValue: true}));
    refresh();

    const sidebar = nav.closest('.sidebar');
    const toggle = document.getElementById('sidebar-toggle');
    if (!sidebar || !toggle) return;
    const mobile = window.matchMedia('(max-width: 760px)');
    const drawerClass = 'zs-mobile-nav-open';
    sidebar.id = 'console-navigation';
    sidebar.setAttribute('aria-label', '功能导航');
    toggle.setAttribute('aria-controls', sidebar.id);
    const backdrop = document.createElement('button');
    backdrop.type = 'button';
    backdrop.className = 'zs-nav-backdrop';
    backdrop.setAttribute('aria-label', '关闭功能导航');
    backdrop.tabIndex = -1;
    backdrop.hidden = true;
    document.body.append(backdrop);

    const drawerIsOpen = () => mobile.matches && document.body.classList.contains(drawerClass);
    const syncDrawer = () => {
        const open = drawerIsOpen();
        const expanded = mobile.matches ? open : !document.body.classList.contains('sidebar-collapsed');
        const label = `${expanded ? '收起' : '展开'}功能导航`;
        toggle.setAttribute('aria-expanded', String(expanded));
        toggle.setAttribute('aria-label', label);
        toggle.title = label;
        backdrop.hidden = !open;
        sidebar.inert = mobile.matches && !open;
        if (mobile.matches) sidebar.setAttribute('aria-hidden', String(!open));
        else sidebar.removeAttribute('aria-hidden');
    };
    const setDrawerOpen = (open, returnFocus = false) => {
        document.body.classList.toggle(drawerClass, mobile.matches && open);
        syncDrawer();
        if (open && mobile.matches) {
            window.requestAnimationFrame(() => {
                if (drawerIsOpen()) search.focus({preventScroll: true});
            });
        } else if (returnFocus) toggle.focus({preventScroll: true});
    };

    // Capture the mobile action before the existing desktop collapse listener runs.
    toggle.addEventListener('click', event => {
        if (!mobile.matches) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        const open = !drawerIsOpen();
        setDrawerOpen(open, !open);
    }, true);
    backdrop.addEventListener('click', () => setDrawerOpen(false, true));
    nav.addEventListener('click', event => {
        const selected = event.target.closest('.tab-button, a[href]');
        if (!drawerIsOpen() || !selected || !nav.contains(selected)) return;
        setDrawerOpen(false);
        // Move focus out of the hidden drawer after a workspace selection.
        if (selected.dataset.tab) {
            const active = document.querySelector('.workspace-tab.active:not([hidden])');
            (active || toggle).focus({preventScroll: true});
        }
    });
    const focusableNavigation = () => [toggle, ...sidebar.querySelectorAll(
        'button, a[href], input, select, textarea, summary, [tabindex]'
    )].filter(element => !element.disabled && element.tabIndex >= 0 && element.getClientRects().length);
    document.addEventListener('keydown', event => {
        if (!drawerIsOpen()) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            setDrawerOpen(false, true);
        } else if (event.key === 'Tab') {
            const focusable = focusableNavigation();
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            const current = document.activeElement;
            if (event.shiftKey && (current === first || !focusable.includes(current))) {
                event.preventDefault();
                last?.focus({preventScroll: true});
            } else if (!event.shiftKey && (current === last || !focusable.includes(current))) {
                event.preventDefault();
                first?.focus({preventScroll: true});
            }
        }
    }, true);
    document.addEventListener('focusin', event => {
        if (drawerIsOpen() && event.target !== toggle && !sidebar.contains(event.target)) {
            search.focus({preventScroll: true});
        }
    });
    mobile.addEventListener('change', () => {
        const returnFocus = mobile.matches && sidebar.contains(document.activeElement);
        setDrawerOpen(false, returnFocus);
    });
    new MutationObserver(syncDrawer).observe(document.body, {attributes: true, attributeFilter: ['class']});

    const workspaceTabs = document.querySelector('.workspace-tabs');
    if (workspaceTabs) {
        const syncCurrentWorkspace = () => workspaceTabs.querySelectorAll('.workspace-tab').forEach(button => {
            if (button.classList.contains('active')) button.setAttribute('aria-current', 'page');
            else button.removeAttribute('aria-current');
        });
        new MutationObserver(syncCurrentWorkspace).observe(workspaceTabs, {
            attributes: true, attributeFilter: ['class'], childList: true, subtree: true,
        });
        syncCurrentWorkspace();
    }
    syncDrawer();
})();
