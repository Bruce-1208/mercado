/* Common calendar shortcuts use the existing range inputs and query handlers. */
(() => {
    const byId = id => document.getElementById(id);
    const groups = [
        ['order-start-date', 'order-end-date', () => loadOrders(1)],
        ['mercado-date-from', 'mercado-date-to', () => applyMercadoProductFilters()],
        ['products-1688-date-from', 'products-1688-date-to', () => load1688Products()],
        ['ai-original-date-from', 'ai-original-date-to', () => renderAiOriginalProducts()],
        ['wdr-filter-date-from', 'wdr-filter-date-to', () => byId('wdr-filter-apply').click()],
        ['publish-record-start-date', 'publish-record-end-date', () => loadMercadoPublishRecords()],
        ['salesperson-stats-start', 'salesperson-stats-end'],
        ['store-analysis-start', 'store-analysis-end'],
        ['after-sale-date-from', 'after-sale-date-to', () => loadHighAfterSaleAlerts()],
        ['profit-date-from', 'profit-date-to', () => loadHighProfitProducts()],
        ['freight-date-from', 'freight-date-to', () => loadFreightChanges()],
        ['window-anomaly-date-from', 'window-anomaly-date-to', () => applyWindowAnomalyFilters()],
        ['ai-video-date-from', 'ai-video-date-to', () => applyAiVideoFilters()],
        ['store-link-audit-from', 'store-link-audit-to'],
        ['ad-analysis-date-from', 'ad-analysis-date-to', () => loadAdAnalysis(true), false],
        ['prohibited-occurred-from', 'prohibited-occurred-to', () => loadProhibitedListings(1)],
        ['pre-sale-start-date', 'pre-sale-end-date', () => { resetPreSaleView(); loadPreSaleQuestions(1); }, false],
        ['customer-service-start-date', 'customer-service-end-date', () => loadCustomerServiceData(1), false],
        ['ai-appeal-date-from', 'ai-appeal-date-to', () => applyAiAppealFilters()],
        ['ai-billing-start-date', 'ai-billing-end-date'],
        ['print-wallet-record-date-from', 'print-wallet-record-date-to'],
    ];
    const dateText = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
    function today() {
        // Business filters use Beijing calendar dates, independent of browser timezone.
        const parts = new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(new Date());
        const part = type => parts.find(p => p.type === type).value;
        return new Date(Number(part('year')), Number(part('month')) - 1, Number(part('day')), 12);
    }
    function bounds(preset) {
        if (!preset) return ['', ''];
        const end = today();
        if (preset === 'yesterday') end.setDate(end.getDate() - 1);
        const start = new Date(end);
        if (preset === '7' || preset === '30') start.setDate(start.getDate() - Number(preset) + 1);
        return [dateText(start), dateText(end)];
    }
    function initialize([fromId, toId, apply, allowAll = true]) {
        const from = byId(fromId), to = byId(toId);
        if (!from || !to || from.dataset.dateFilterInitialized) return;
        const fieldSelector = '.order-filter-field, .market-filter-field, .p1688-field, .field, label';
        const fields = [...new Set([from.closest(fieldSelector), to.closest(fieldSelector)])];
        if (fields.some(field => !field)) return;
        let container = fields[0].parentElement;
        while (!container.contains(fields[1] || fields[0])) container = container.parentElement;
        let details = from.closest('.zs-advanced-filters');
        if (details) container = details.parentElement;
        else details = [...container.children].find(node => node.matches('.zs-advanced-filters'));
        if (!details) {
            details = document.createElement('details');
            details.className = 'zs-advanced-filters';
            const summary = document.createElement('summary');
            summary.textContent = '高级筛选';
            const grid = document.createElement('div');
            grid.className = 'zs-advanced-grid';
            details.append(summary, grid);
            container.append(details);
        }
        const grid = details.querySelector('.zs-advanced-grid');
        details.classList.add('zs-date-advanced');
        grid.classList.add('zs-date-advanced-grid');
        container.classList.add('zs-date-filter-panel');
        fields.forEach(field => grid.append(field));
        // Preserve older period shortcuts inside the same advanced section.
        const oldPeriods = [...container.children].filter(node =>
            node.matches('.after-sale-sort-buttons') && node.querySelector('[onclick*="Period("]'));
        oldPeriods.forEach(node => grid.append(node));
        if (fromId === 'salesperson-stats-start') {
            const all = byId('salesperson-stats-all');
            if (all) grid.append(all);
        }
        from.dataset.dateFilterInitialized = 'true';

        const common = document.createElement('div');
        common.className = 'zs-common-date-filter';
        const label = document.createElement('label');
        const select = document.createElement('select');
        select.id = `${fromId}-preset`;
        label.htmlFor = select.id;
        label.textContent = '日期';
        const presets = [['', '全部日期'], ['today', '今天'], ['yesterday', '昨天'], ['7', '近7天'], ['30', '近30天'], ['day', '指定日期'], ['custom', '自定义范围（高级筛选）']];
        presets.filter(([value]) => value || (allowAll && !from.required && !to.required)).forEach(([value, text]) => select.add(new Option(text, value)));
        const day = document.createElement('input');
        day.type = 'date';
        day.id = `${fromId}-day`;
        day.setAttribute('aria-label', '指定日期');
        day.hidden = true;
        common.append(label, select, day);
        const actions = [...container.children].find(node => node.matches('.market-filter-actions, .order-filter-actions, .filter-actions, .store-link-audit-filter-actions, button'));
        const commonRow = container.querySelector('.zs-common-filter-row');
        if (commonRow) commonRow.insertBefore(common, commonRow.querySelector('.market-filter-actions'));
        else container.insertBefore(common, actions || details);
        let updating = false;
        function sync() {
            if (updating) return;
            const fromDate = from.value.slice(0, 10), toDate = to.value.slice(0, 10);
            const wholeDays = from.type === 'date' || ((!from.value || from.value.endsWith('T00:00')) && (!to.value || to.value.endsWith('T23:59')));
            const preset = wholeDays && presets.slice(0, 5).find(([value]) => {
                const [start, end] = bounds(value);
                return start === fromDate && end === toDate;
            });
            if (select.value === 'day' && wholeDays && fromDate && fromDate === toDate) select.value = 'day';
            else if (preset && [...select.options].some(option => option.value === preset[0])) select.value = preset[0];
            else if (wholeDays && fromDate && fromDate === toDate) select.value = 'day';
            else select.value = 'custom';
            day.hidden = select.value !== 'day';
            if (!day.hidden) day.value = fromDate;
            const count = [...grid.querySelectorAll('input, select')].filter(control => !control.closest('[hidden]') && control.value).length;
            if (container.zsRefreshFilterSummary) container.zsRefreshFilterSummary();
            else details.querySelector('summary').textContent = count ? `高级筛选 · 已设置 ${count} 项` : '高级筛选';
        }
        function setRange(start, end) {
            updating = true;
            from.value = start + (start && from.type === 'datetime-local' ? 'T00:00' : '');
            to.value = end + (end && to.type === 'datetime-local' ? 'T23:59' : '');
            // Reuse date placeholders without firing duplicate application queries.
            [from, to].forEach(input => input.dispatchEvent(new Event('input', {bubbles: true})));
            updating = false;
            sync();
            if (apply) apply();
            else if (from.form) from.form.requestSubmit();
        }
        select.addEventListener('change', () => {
            if (select.value === 'custom') {
                day.hidden = true;
                details.open = true;
                from.focus();
            } else if (select.value === 'day') {
                day.hidden = false;
                day.value = from.value.slice(0, 10) || dateText(today());
                setRange(day.value, day.value);
                // Keep the calendar visible even when the day equals a shortcut.
                select.value = 'day';
                day.hidden = false;
                day.focus();
            } else {
                day.hidden = true;
                setRange(...bounds(select.value));
            }
        });
        day.addEventListener('change', () => {
            if (!day.value) return;
            setRange(day.value, day.value);
            select.value = 'day';
            day.hidden = false;
        });
        [from, to].forEach(input => {
            ['input', 'change', 'zs-date-value-change'].forEach(event => input.addEventListener(event, sync));
        });
        from.form?.addEventListener('reset', () => setTimeout(sync, 0));
        container.addEventListener('click', () => queueMicrotask(sync));
        container.addEventListener('change', () => queueMicrotask(sync));
        details.addEventListener('toggle', sync);
        // Required ranges should reveal the field if native validation fails.
        [from, to].forEach(input => input.addEventListener('invalid', () => { details.open = true; }));
        sync();
    }
    function initializePeriods() {
        const native = byId('days-select');
        const buttons = document.querySelector('#tab-infractions .range-switch');
        const source = native?.closest('.field') || buttons;
        if (!source) return;
        const container = source.parentElement;
        const details = document.createElement('details');
        details.className = 'zs-advanced-filters zs-date-advanced';
        const summary = document.createElement('summary');
        summary.textContent = '高级筛选';
        const grid = document.createElement('div');
        grid.className = 'zs-advanced-grid zs-date-advanced-grid';
        details.append(summary, grid);
        const common = document.createElement('div');
        common.className = 'zs-common-date-filter';
        const select = document.createElement('select');
        select.id = native ? 'days-select-preset' : 'infraction-date-preset';
        const label = document.createElement('label');
        label.htmlFor = select.id;
        label.textContent = '日期';
        common.append(label, select);
        if (native) {
            [...native.options].forEach(option => select.add(new Option(option.textContent, option.value)));
            select.value = native.value;
            select.addEventListener('change', () => {
                native.value = select.value;
                native.dispatchEvent(new Event('change', {bubbles: true}));
            });
            native.addEventListener('change', () => { select.value = native.value; });
        } else {
            const choices = [...buttons.querySelectorAll('[data-days]')];
            choices.forEach(button => select.add(new Option(button.textContent, button.dataset.days)));
            const sync = () => { select.value = buttons.querySelector('.active')?.dataset.days || '0'; };
            select.addEventListener('change', () => choices.find(button => button.dataset.days === select.value)?.click());
            new MutationObserver(sync).observe(buttons, {subtree: true, attributes: true, attributeFilter: ['class']});
            sync();
        }
        container.classList.add('zs-date-filter-panel');
        container.insertBefore(common, source);
        grid.append(source);
        container.append(details);
    }
    const start = () => {
        groups.forEach(initialize);
        initializePeriods();
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
