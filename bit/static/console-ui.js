/* Progressive enhancement for the console's existing native-select-backed pickers. */
(() => {
    "use strict";

    function filterOptions(picker) {
        const query = (picker.querySelector('.zs-picker-search input')?.value || '').trim().toLocaleLowerCase();
        const rows = [...picker.querySelectorAll('.market-multi-option')];
        let matches = 0;
        rows.forEach(row => {
            row.hidden = !row.textContent.toLocaleLowerCase().includes(query);
            if (!row.hidden) matches++;
        });
        const empty = picker.querySelector('.zs-picker-empty');
        if (empty) empty.hidden = !rows.length || matches > 0;
    }

    function preparePicker(picker) {
        if (!picker) return;
        const panel = picker.querySelector('.market-multi-panel');
        const trigger = picker.querySelector('.market-multi-trigger');
        const options = picker.querySelector('.market-multi-options');
        if (!panel || !trigger || !options) return;
        const select = document.getElementById(options.dataset.selectId);
        const label = select?.labels?.[0]?.textContent.trim() || picker.dataset.placeholder || '选项';
        panel.id ||= `${picker.id}-panel`;
        panel.setAttribute('role', 'group');
        panel.setAttribute('aria-label', label);
        // The panel contains native checkbox controls, not ARIA listbox options.
        trigger.removeAttribute('aria-haspopup');
        trigger.setAttribute('aria-controls', panel.id);
        trigger.setAttribute('aria-label', `${label}：${picker.querySelector('.market-multi-summary')?.textContent || ''}`);
        if (!panel.querySelector('.zs-picker-search')) {
            const wrapper = document.createElement('div');
            wrapper.className = 'zs-picker-search';
            const search = document.createElement('input');
            search.type = 'search';
            search.placeholder = '搜索选项…';
            search.setAttribute('aria-label', `搜索${label}`);
            search.autocomplete = 'off';
            search.addEventListener('input', () => filterOptions(picker));
            wrapper.append(search);
            panel.insertBefore(wrapper, options);
            const empty = document.createElement('div');
            empty.className = 'zs-picker-empty';
            empty.textContent = '没有匹配的选项';
            empty.setAttribute('role', 'status');
            empty.hidden = true;
            panel.append(empty);
        }
        filterOptions(picker);
    }

    function openPicker(picker) {
        preparePicker(picker);
        const panel = picker.querySelector('.market-multi-panel');
        const trigger = picker.querySelector('.market-multi-trigger');
        if (!panel || !trigger) return;
        const rect = trigger.getBoundingClientRect();
        const margin = 12;
        const width = Math.min(Math.max(rect.width, 300), window.innerWidth - margin * 2);
        const below = window.innerHeight - rect.bottom - margin - 6;
        const above = rect.top - margin - 6;
        const upwards = below < 280 && above > below;
        const height = Math.max(80, upwards ? above : below);
        panel.classList.add('zs-picker-positioned');
        panel.style.setProperty('--zs-picker-width', `${width}px`);
        panel.style.setProperty('--zs-picker-height', `${height}px`);
        panel.style.setProperty('--zs-picker-left', `${Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin))}px`);
        const top = upwards ? Math.max(margin, rect.top - Math.min(panel.scrollHeight, height) - 6) : rect.bottom + 6;
        panel.style.setProperty('--zs-picker-top', `${top}px`);
        panel.querySelector('.zs-picker-search input')?.focus({preventScroll: true});
    }

    document.addEventListener('keydown', event => {
        const picker = event.target.closest('.market-multi-picker');
        if (!picker) return;
        const trigger = picker.querySelector('.market-multi-trigger');
        if (event.key === 'Enter' && event.target.matches('.zs-picker-search input')) {
            // Search belongs to the popup; Enter must not submit the enclosing filter form.
            event.preventDefault();
            picker.querySelector('.market-multi-option:not([hidden]) input:not(:disabled)')?.focus();
        } else if (event.key === 'Escape' && picker.classList.contains('open')) {
            event.preventDefault();
            window.closeMercadoPublishPickers();
            trigger?.focus({preventScroll: true});
        } else if (event.key === 'ArrowDown' && event.target === trigger && !picker.classList.contains('open')) {
            event.preventDefault();
            window.toggleMercadoPublishPicker(picker.id);
        } else if (['ArrowDown', 'ArrowUp'].includes(event.key) && picker.classList.contains('open')) {
            const controls = [...picker.querySelectorAll('.zs-picker-search input, .market-multi-option:not([hidden]) input:not(:disabled)')];
            const current = controls.indexOf(event.target);
            if (current < 0) return;
            event.preventDefault();
            controls[(current + (event.key === 'ArrowDown' ? 1 : -1) + controls.length) % controls.length]?.focus();
        }
    }, true);
    document.addEventListener('focusin', event => {
        document.querySelectorAll('.market-multi-picker.open').forEach(picker => {
            if (!picker.contains(event.target)) window.closeMercadoPublishPickers();
        });
    });
    // Fixed panels must close when their anchor moves, but remain open while scrolling options.
    document.addEventListener('scroll', event => {
        if (event.target instanceof Element && event.target.closest('.market-multi-panel')) return;
        window.closeMercadoPublishPickers?.();
    }, true);
    window.addEventListener('resize', () => window.closeMercadoPublishPickers?.());
    window.ZeshunUI = {preparePicker, openPicker};
})();
