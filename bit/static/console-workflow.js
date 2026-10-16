/* Progressive layout: preserve existing fields, handlers and publishing checks. */
(() => {
    const byId = id => document.getElementById(id);
    const element = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text) node.textContent = text;
        return node;
    };
    const button = (text, onClick, primary = false) => {
        const node = element('button', primary ? 'primary' : 'secondary', text);
        node.type = 'button';
        node.addEventListener('click', onClick);
        return node;
    };
    const filterRefreshers = [];
    function foldFilters(container, ids, fieldSelector) {
        if (!container) return;
        container.classList.add('zs-simple-filters');
        const details = element('details', 'zs-advanced-filters');
        const summary = element('summary', '', '高级筛选');
        const grid = element('div', 'zs-advanced-grid');
        details.append(summary, grid);
        ids.forEach(id => {
            const field = byId(id)?.closest(fieldSelector);
            if (field && !grid.contains(field)) grid.append(field);
        });
        container.append(details);
        const refresh = () => {
            if (container.zsRefreshFilterSummary) return container.zsRefreshFilterSummary();
            const count = [...grid.querySelectorAll('input, select')].filter(control => {
                if (control.closest('[hidden]')) return false;
                return control.multiple ? [...control.selectedOptions].some(o => o.value) : Boolean(control.value);
            }).length;
            summary.textContent = count ? `高级筛选 · 已设置 ${count} 项` : '高级筛选';
        };
        container.addEventListener('input', refresh);
        container.addEventListener('change', refresh);
        container.addEventListener('click', () => queueMicrotask(refresh));
        filterRefreshers.push(refresh);
        refresh();
    }
    foldFilters(document.querySelector('#tab-orders .order-filters'), [
        'order-salesperson-filter', 'order-group-filter', 'order-start-date', 'order-end-date', 'order-freight-variance-filter', 'order-remark-search', 'order-purchase-platform-filter', 'order-freight-difference-min', 'order-payment-release-filter',
    ], '.order-filter-field');
    const orderRegion = document.querySelector('.order-filter-region');
    if (orderRegion) {
        const extra = element('details', 'order-extra-tools');
        extra.append(element('summary', '', '地区与批量操作'));
        [...orderRegion.children].filter(node => node.matches('.order-market-bar, .order-bulk-bar, .order-sync-note')).forEach(node => extra.append(node));
        orderRegion.append(extra);
    }
    foldFilters(byId('mercado-product-filters'), [
        'mercado-platform-category-filter', 'mercado-zying-category-filter',
        'mercado-product-developer-filter', 'mercado-infringement-filter',
        'mercado-weight-status-filter',
        'mercado-weight-min', 'mercado-price-min', 'mercado-net-min', 'mercado-date-from',
    ], '.market-filter-field');
    foldFilters(document.querySelector('.store-link-filters'), [
        'store-link-product-category-filter', 'store-link-mercado-category-filter',
        'store-link-promotion-filter', 'store-link-advertising-filter', 'store-link-video-filter',
    ], '.order-filter-field');
    const supplierFilters = document.querySelector('.p1688-toolbar');
    foldFilters(supplierFilters, [
        'products-1688-status-filter', 'products-1688-review-filter',
        'products-1688-price-min', 'products-1688-price-max',
        'products-1688-weight-min', 'products-1688-weight-max',
        'products-1688-date-from', 'products-1688-date-to',
    ], '.p1688-field');
    // Keep the feedback after all filters and before the batch toolbar.
    const supplierStatus = byId('products-1688-status');
    if (supplierFilters && supplierStatus) supplierFilters.append(supplierStatus);

    // Keep ordinary filters on one scrollable row, separate from expanded filters.
    const productFilters = byId('mercado-product-filters');
    if (productFilters) {
        const row = element('div', 'zs-common-filter-row');
        [...productFilters.children].filter(node => node.matches('.market-filter-field, .market-filter-actions')).forEach(node => row.append(node));
        productFilters.prepend(row);
    }

    const card = byId('mercado-product-actions');
    const module = byId('mercado-list-module');
    if (!card || !module) return;
    module.classList.add('zs-streamlined-products');
    const tools = module.querySelector('.market-selection-tools');
    const bulk = element('details', 'zs-bulk-menu');
    bulk.append(element('summary', '', '批量编辑'));
    tools.before(bulk);
    bulk.append(tools);
    bulk.before(byId('mercado-collection-only-actions'));

    const entry = button('下一步：配置发布', () => openPublish(), true);
    entry.id = 'mercado-publish-next';
    bulk.after(entry);
    const reviewActions = ['mercado-review-publish-selected', 'mercado-review-publish-page'].map(byId);
    entry.after(...reviewActions);
    const flow = element('ol', 'zs-publish-steps');
    flow.setAttribute('aria-label', '发布步骤');
    ['选择商品', '配置发布', '核对并上架'].forEach(text => flow.append(element('li', '', text)));
    card.before(flow);
    const details = element('details', 'zs-publish-workspace');
    details.id = 'mercado-publish-workspace';
    const summary = element('summary', '', '配置发布');
    const content = element('div', 'zs-publish-content');
    const settings = element('div', 'zs-publish-settings');
    const review = element('div', 'zs-publish-review');
    review.id = 'mercado-publish-review';
    review.tabIndex = -1;
    const heading = card.querySelector('.market-publish-card-header');
    const footer = card.querySelector('.market-publish-footer');
    settings.append(card.querySelector('.market-publish-controls'), byId('mercado-group-publish-mode-field'));
    const settingsActions = element('div', 'zs-step-actions');
    const next = button('下一步：核对上架', showReview, true);
    next.id = 'mercado-publish-review-next';
    settingsActions.append(button('返回选择商品', () => {
        details.open = false;
        module.querySelector('.market-table-heading').scrollIntoView({block: 'start'});
        entry.focus({preventScroll: true});
    }), next);
    settings.append(settingsActions);
    const back = button('返回修改配置', showSettings);
    footer.prepend(back);
    content.append(settings, review, footer);
    details.append(summary, content);
    heading.remove();
    card.append(details);
    let stage = 'settings';
    let selectionKey = '';
    const submit = byId('mercado-publish-selected');
    const selectedLabels = id => [...byId(id).selectedOptions].filter(o => o.value).map(o => o.dataset.label || o.textContent).join('、');
    function updateSteps() {
        const current = !details.open ? 0 : stage === 'settings' ? 1 : 2;
        [...flow.children].forEach((node, index) => {
            node.classList.toggle('is-current', index === current);
            if (index === current) node.setAttribute('aria-current', 'step');
            else node.removeAttribute('aria-current');
        });
    }
    function showSettings() {
        stage = 'settings';
        settings.hidden = false;
        review.hidden = true;
        footer.hidden = true;
        summary.textContent = '第 2 步 · 配置发布';
        updateSteps();
    }
    function openPublish() {
        if (!selectedMercadoCollectionIds.size) {
            byId('mercado-selection-summary').scrollIntoView({block: 'center'});
            entry.focus({preventScroll: true});
            return;
        }
        showSettings();
        details.open = true;
        card.scrollIntoView({block: 'start'});
        summary.focus();
        updateSteps();
    }
    function validSettings() {
        for (const id of ['mercado-publish-quantity', 'mercado-publish-ratio', 'mercado-publish-workers']) {
            const input = byId(id);
            if (input && (!input.value || !input.checkValidity())) {
                showSettings();
                input.focus();
                input.reportValidity();
                return false;
            }
        }
        const minimumNet = byId('mercado-publish-minimum-net');
        if (minimumNet && !minimumNet.checkValidity()) {
            showSettings(); minimumNet.focus(); minimumNet.reportValidity(); return false;
        }
        const schedule = byId('mercado-publish-schedule-at');
        schedule.setCustomValidity(schedule.value && (!Number.isFinite(new Date(schedule.value).getTime()) || new Date(schedule.value) <= new Date()) ? '请选择晚于当前时间的上架时间' : '');
        if (!schedule.checkValidity()) {
            showSettings(); schedule.reportValidity(); return false;
        }
        return !submit.disabled;
    }
    function showReview() {
        updateMercadoSelection();
        if (!validSettings()) return;
        review.replaceChildren(element('h4', '', '核对上架信息'));
        const list = element('dl', 'zs-review-list');
        const mode = byId('mercado-publish-mode').value;
        const targets = mercadoPublishTargetPreview(mode,
            mercadoSelectedValues(byId('mercado-publish-store')).map(Number),
            mercadoSelectedValues(byId('mercado-publish-group')),
            mercadoSelectedValues(byId('mercado-publish-site')));
        if (!targets.length) {
            setMercadoListFeedback('所选账号或分组与目标站点没有可用的上架组合，请重新选择。', 'error');
            return;
        }
        const pairs = [
            ['所选商品', byId('mercado-selection-summary').textContent],
            ['最低净收益（USD）', Number(byId('mercado-publish-minimum-net')?.value || 0) > 0
                ? `低于 ${Number(byId('mercado-publish-minimum-net').value).toFixed(2)} 时按最低值上架`
                : '不设下限'],
            [mode === 'groups' ? '账号分组' : '上架账号', selectedLabels(mode === 'groups' ? 'mercado-publish-group' : 'mercado-publish-store')],
            ['目标站点', selectedLabels('mercado-publish-site')],
            ['可用账号 / 站点组合', String(targets.length)],
            ['每件库存', byId('mercado-publish-quantity').value],
            ['本次上架比例', `${byId('mercado-publish-ratio').value}%`],
            ['上架时间', byId('mercado-publish-schedule-at').value.replace('T', ' ') || '立即上架'],
        ];
        if (mode === 'groups') pairs.push(['分配方式', selectedLabels('mercado-group-publish-mode')]);
        pairs.forEach(([label, value]) => list.append(element('dt', '', label), element('dd', '', value)));
        review.append(list, element('p', 'zs-review-note', '上架净收益按产品分类、店铺站点和本次上架比例计算；低于最低净收益时按最低值上架。提交后逐件复核，未通过的商品保留在产品列表并显示原因，通过后才上架；历史成功的相同账号和站点会跳过。'));
        stage = 'review';
        settings.hidden = true;
        review.hidden = false;
        footer.hidden = false;
        summary.textContent = '第 3 步 · 核对并上架';
        updateMercadoPublishActionLabel();
        if (byId('mercado-publish-status').textContent === '选择商品后配置发布账号与目标站点') {
            byId('mercado-publish-status').textContent = '核对无误后，点击右侧按钮提交上架';
        }
        updateSteps();
        review.focus();
    }
    details.addEventListener('toggle', updateSteps);
    settings.addEventListener('input', () => {
        byId('mercado-publish-schedule-at').setCustomValidity('');
        showSettings();
    });
    settings.addEventListener('change', showSettings);
    submit.addEventListener('click', event => {
        if (stage !== 'review' || !validSettings()) {
            event.preventDefault(); event.stopImmediatePropagation();
        }
    }, true);
    window.openMercadoPublishWorkflow = openPublish;
    window.syncConsoleWorkflow = () => {
        filterRefreshers.forEach(refresh => refresh());
        const productMode = mercadoListMode === 'products';
        const hasSelection = selectedMercadoCollectionIds.size > 0;
        module.classList.toggle('has-publish-selection', hasSelection);
        entry.hidden = !productMode;
        reviewActions.forEach(button => { button.hidden = !productMode; });
        entry.disabled = !hasSelection || mercadoPublishRunning;
        flow.hidden = !productMode;
        bulk.hidden = !hasSelection;
        next.disabled = submit.disabled;
        const key = JSON.stringify([mercadoListMode, [...selectedMercadoCollectionIds],
            byId('mercado-selection-summary').textContent, byId('mercado-publish-mode').value,
            selectedLabels('mercado-publish-store'), selectedLabels('mercado-publish-group'),
            selectedLabels('mercado-publish-site')]);
        if (key !== selectionKey) {
            selectionKey = key;
            showSettings();
            if (!hasSelection || !productMode) details.open = false;
        }
        updateSteps();
    };
    showSettings();
    window.syncConsoleWorkflow();
})();
