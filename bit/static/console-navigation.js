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
})();
