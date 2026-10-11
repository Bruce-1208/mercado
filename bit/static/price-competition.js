(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const escape = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const canManage = document.body.dataset.canManage === '1';
    const state = {rows: [], page: 1, pages: 1, total: 0, kind: 'all', busy: new Set(), preview: null, costRow: null, generation: 0, checking: false};
    const kinds = {ordinary: '普通商品', catalog: 'Catalog 商品', unknown: '类型待核实'};
    const statuses = {winning: '获胜', sharing_first_place: '共享首位', competing: '竞争中', losing: '未获胜', listed: '未参与竞争', no_benchmark_lowest: '低于参考价', no_benchmark_ok: '符合参考价', with_benchmark_high: '高于参考价', with_benchmark_highest: '高于参考及竞品价'};
    const boosts = {fulfillment: 'Full 配送', free_shipping: '免运费', free_installments: '免息分期', same_day_shipping: '当日送达', shipping_collect: '集货配送', shipping_quarantine: '常规配送'};
    const boostStates = {opportunity: '可改善', boosted: '已生效', not_boosted: '具备但未加成', not_apply: '不适用'};
    const format = value => value == null ? '—' : Number(value).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '';
    function notice(message, error = false) { $('message').hidden = !message; $('message').textContent = message; $('message').className = error ? 'error' : ''; }
    function scoped(path) {
        const url = new URL(path, location.origin);
        const organization = new URLSearchParams(location.search).get('organization_key');
        if (organization) url.searchParams.set('organization_key', organization);
        return url.pathname + url.search;
    }
    async function api(path, body) {
        const response = await fetch(scoped(path), body === undefined ? {cache: 'no-store'} : {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), cache: 'no-store'
        });
        const result = await response.json();
        if (!response.ok || result.status !== 'success') throw new Error(result.message || '请求失败，请重试');
        return result.data;
    }
    function price(value, currency, className = '') {
        return `<div class="price ${className}">${format(value)}</div>${value == null ? '' : `<div class="currency">${escape(currency || '')}</div>`}`;
    }
    function render() {
        const rows = state.rows.filter(row => state.kind === 'all' || row.pricing.kind === state.kind);
        $('count').textContent = `本页显示 ${rows.length} / ${state.rows.length} 件 · 筛选结果共 ${state.total} 件`;
        $('page-info').textContent = `第 ${state.page} / ${Math.max(1, state.pages)} 页 · 每页 20 件`;
        $('previous').disabled = state.page <= 1 || state.checking;
        $('next').disabled = state.page >= state.pages || state.checking;
        $('rows').innerHTML = rows.length ? rows.map(row => {
            const p = row.pricing, kind = p.kind || 'unknown', currency = p.currency_id || row.currency_id;
            const busy = state.busy.has(row.id);
            const conditions = (p.boosts || []).map(boost => `<span class="boost ${boost.status === 'opportunity' ? 'opportunity' : ''}">${escape(boosts[boost.id] || boost.id)} · ${escape(boostStates[boost.status] || boost.status)}</span>`).join('');
            const imageUrl = /^https?:\/\//i.test(row.thumbnail_url || '') ? row.thumbnail_url : '';
            const approval = row.approval ? `<div class="detail">${row.approval.matches_current ? '已确认建议' : '历史确认（资料已变化）'} ${format(row.approval.suggestion.suggested_price)} ${escape(row.approval.suggestion.currency_id)}${row.approval.matches_current ? ' · 待人工改价' : ''}<br>${escape(date(row.approval.confirmed_at))}</div>` : '';
            return `<tr data-id="${row.id}"><td><div class="product">${imageUrl ? `<img src="${escape(imageUrl)}" loading="lazy" alt="">` : ''}<div><div class="title">${escape(row.title || row.item_id)}</div><span class="kind ${kind}">${kinds[kind] || kinds.unknown}</span><div class="detail">${escape(row.store_name)} · ${escape(row.item_id)}${row.seller_sku ? ' · ' + escape(row.seller_sku) : ''}</div><div class="detail">${escape(statuses[p.competition_status] || p.competition_status)}</div></div></div><div class="signal">${conditions}</div>${p.reasons.length ? `<div class="detail">竞争原因：${escape(p.reasons.join('、'))}</div>` : ''}<div class="detail">${row.checked_at ? '检测于 ' + escape(date(row.checked_at)) : '尚未读取官方价格'}</div></td>
                <td>${price(p.current_price ?? row.price, currency)}</td>
                <td>${price(p.official_reference_price, currency)}<div class="detail">${kind === 'catalog' ? 'Catalog 使用竞争目标价' : p.official_reference_price == null ? '无可用参考价' : '官方价格参考'}</div></td>
                <td>${price(p.competition_target_price, currency)}<div class="detail">${kind === 'ordinary' ? '普通商品不适用' : kind === 'catalog' ? 'price_to_win' : '待检测'}</div></td>
                <td>${price(p.minimum_acceptable_price, currency)}<div class="detail">${row.costs ? '基于核实成本' : '待核实成本'}</div></td>
                <td>${price(p.estimated_profit, currency, p.estimated_profit == null ? '' : p.estimated_profit < 0 ? 'bad' : 'good')}<div class="detail">${p.estimated_profit == null ? '资料不足，暂不估算' : '按建议价估算'}</div></td>
                <td class="suggestion">${p.suggested_price != null ? `<div>建议价 <strong>${format(p.suggested_price)} ${escape(currency)}</strong></div>` : ''}<p class="${row.error ? 'bad' : ''}">${escape(p.reason)}</p>${canManage ? `<div class="actions"><button data-action="refresh" ${busy ? 'disabled' : ''}>${busy ? '处理中…' : '检测'}</button><button data-action="costs" ${busy || !currency ? 'disabled' : ''}>核实成本</button><button data-action="preview" class="primary" ${busy || !p.ready ? 'disabled' : ''}>预览并确认</button></div>` : ''}${approval}</td></tr>`;
        }).join('') : `<tr><td colspan="7" class="empty">${state.rows.length ? '本页没有此类型商品。可先检测本页，或切换类型查看。' : '没有符合当前条件的商品，请调整或清空筛选条件。<button type="button" data-action="clear-filters">清空筛选</button>'}</td></tr>`;
    }
    function replace(row) { const index = state.rows.findIndex(existing => existing.id === row.id); if (index >= 0) state.rows[index] = {...state.rows[index], ...row}; render(); }
    async function load(page = 1) {
        const generation = ++state.generation;
        const query = new URLSearchParams({page: String(page), token_id: $('store').value, site_id: $('site').value, search: $('search').value.trim()});
        $('rows').innerHTML = '<tr><td colspan="7" class="empty">正在加载商品…</td></tr>';
        try {
            const data = await api('/api/price-competition?' + query);
            if (generation !== state.generation) return;
            Object.assign(state, {rows: data.rows || [], page: data.page || page, pages: data.pages || 1, total: data.total || 0}); render();
        } catch (error) { if (generation === state.generation) { state.rows = []; state.total = 0; render(); notice(error.message, true); } }
    }
    async function refresh(row) {
        state.busy.add(row.id); render();
        try { replace(await api('/api/price-competition/refresh', {link_id: row.id})); }
        finally { state.busy.delete(row.id); render(); }
    }
    $('filters').addEventListener('submit', event => { event.preventDefault(); if (!state.checking) { notice(''); load(); } });
    document.querySelectorAll('[data-kind]').forEach(button => button.addEventListener('click', () => {
        state.kind = button.dataset.kind; document.querySelectorAll('[data-kind]').forEach(item => item.classList.toggle('active', item === button)); render();
    }));
    $('previous').addEventListener('click', () => load(state.page - 1));
    $('next').addEventListener('click', () => load(state.page + 1));
    $('refresh-page')?.addEventListener('click', async () => {
        if (state.checking || state.busy.size) return;
        state.checking = true; $('refresh-page').disabled = true; render();
        const batch = [...state.rows]; let done = 0, failed = 0;
        notice(`正在检测本页 ${batch.length} 件商品…`);
        // Small browser-side batches keep progress visible and isolate item failures.
        for (const row of batch) {
            try { await refresh(row); if (state.rows.find(item => item.id === row.id)?.error) failed++; }
            catch (_) { failed++; }
            notice(`已检测 ${++done} / ${batch.length} 件${failed ? `，读取失败 ${failed} 件` : ''}`);
        }
        state.checking = false; $('refresh-page').disabled = false; render();
    });
    $('rows').addEventListener('click', async event => {
        const button = event.target.closest('[data-action]'); if (!button || button.disabled) return;
        if (button.dataset.action === 'clear-filters') { $('filters').reset(); state.kind = 'all'; document.querySelectorAll('[data-kind]').forEach(item => item.classList.toggle('active', item.dataset.kind === 'all')); return load(1); }
        const row = state.rows.find(item => item.id === Number(button.closest('tr').dataset.id)); if (!row) return;
        try {
            if (button.dataset.action === 'refresh') return await refresh(row);
            if (button.dataset.action === 'costs') {
                state.costRow = row; $('cost-error').hidden = true;
                $('cost-item').textContent = `${row.item_id} · ${row.store_name}`;
                $('cost-currency').value = row.pricing.currency_id || row.currency_id || '';
                $('fixed-cost').value = row.costs?.fixed_cost ?? ''; $('variable-rate').value = row.costs?.variable_cost_percent ?? ''; $('minimum-profit').value = row.costs?.minimum_profit ?? '';
                return $('cost-dialog').showModal();
            }
            state.busy.add(row.id); render();
            state.preview = await api('/api/price-competition/preview', {link_id: row.id});
            const p = state.preview.suggestion;
            $('preview-item').textContent = `${row.item_id} · ${row.store_name}`;
            $('preview-values').innerHTML = [['当前售价', p.current_price], ['建议价', p.suggested_price], ['最低可接受价', p.minimum_acceptable_price], ['调整后预计利润', p.estimated_profit]].map(([label,value]) => `<div><span class="muted">${label}</span><strong>${format(value)} <small>${escape(p.currency_id)}</small></strong></div>`).join('');
            $('acknowledged').checked = false; $('confirm').disabled = true; $('preview-error').hidden = true; $('preview-dialog').showModal();
        } catch (error) { notice(error.message, true); }
        finally { state.busy.delete(row.id); render(); }
    });
    $('cost-form').addEventListener('submit', async event => {
        event.preventDefault(); const submit = event.submitter; submit.disabled = true;
        try {
            const row = await api('/api/price-competition/costs', {link_id: state.costRow.id, currency_id: $('cost-currency').value,
                fixed_cost: $('fixed-cost').value, variable_cost_percent: $('variable-rate').value, minimum_profit: $('minimum-profit').value});
            replace(row); $('cost-dialog').close(); notice('成本资料已保存。预览建议时将重新读取官方价格。');
        } catch (error) { $('cost-error').textContent = error.message; $('cost-error').hidden = false; }
        finally { submit.disabled = false; }
    });
    $('acknowledged').addEventListener('change', () => { $('confirm').disabled = !$('acknowledged').checked; });
    $('confirm').addEventListener('click', async () => {
        if (!state.preview || !$('acknowledged').checked) return;
        $('confirm').disabled = true;
        try {
            const result = await api('/api/price-competition/confirm', {preview_id: state.preview.preview_id, confirmed: true});
            replace(result.row); $('preview-dialog').close(); state.preview = null; notice(result.message);
        } catch (error) { $('preview-error').textContent = error.message; $('preview-error').hidden = false; }
        finally { $('confirm').disabled = !$('acknowledged').checked; }
    });
    document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => $(button.dataset.close).close()));
    async function init() {
        try {
            const stores = await api('/api/price-competition/stores');
            $('store').innerHTML += stores.map(store => `<option value="${store.id}">${escape(store.name)}${store.enabled ? '' : '（已关闭）'}</option>`).join('');
            const sites = [...new Set(stores.flatMap(store => (store.sites || []).map(site => site.site_id)))].sort();
            $('site').innerHTML += sites.map(site => `<option value="${escape(site)}">${escape(site)}</option>`).join('');
        } catch (error) { notice(error.message, true); }
        await load();
    }
    init();
})();
