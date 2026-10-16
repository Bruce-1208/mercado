/* Shared rendering for preview and durable publication records. */
(() => {
    const labels = {ready: '可上架', needs_completion: '待补全', mapping_conflict: '映射冲突'};
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const parse = value => { try { return typeof value === 'string' ? JSON.parse(value) : value; } catch (_) { return null; } };
    window.mercadoAttributeStatusLabel = value => {
        const status = parse(value)?.preflight?.status;
        return status === 'needs_completion' || status === 'mapping_conflict' ? labels[status] : '';
    };
    const rules = {source_schema_to_target_id:'属性对应', target_enum:'目标类目选项',
        exact_unit_conversion:'单位精确换算', unsupported_attribute:'目标类目不支持',
        unmapped_value:'未找到明确对应值', user_confirmed_for_this_product_and_target:'人工确认'};
    const valueText = value => {
        if (!value) return '未提交';
        if (value.evidence) return `${(value.original || []).map(valueText).join('、') || '未采集到'}；依据：${value.evidence}`;
        const content = value.value_name || value.value_id ||
            (value.value_struct ? `${value.value_struct.number} ${value.value_struct.unit}` : '') ||
            (value.values || []).map(v => v.name || v.id).join('、') || '空值';
        return `${value.name || value.id || ''}：${content}`;
    };
    window.renderMercadoAttributeReport = value => {
        const data = parse(value) || {};
        const report = data.preflight || data;
        if (!labels[report.status]) return '';
        const issues = (report.issues || []).map(row => `<li>${esc(row.scope)} · ${esc(row.attribute_id || '')}：${esc(row.message)}${row.severity === 'warning' ? '（提示）' : ''}</li>`).join('');
        const mappings = (report.mappings || []).map(row => `<tr><td>${esc(row.scope)}</td><td>${esc(valueText(row.source))}</td><td>${esc(valueText(row.target))}</td><td>${esc(rules[row.rule] || row.rule)}</td></tr>`).join('');
        return `<details class="publish-record-details"><summary>属性预检：${labels[report.status]}</summary>${issues ? `<ul>${issues}</ul>` : '<p>当前目标类目的属性检查已通过。</p>'}${mappings ? `<div style="overflow:auto;max-height:360px"><table><thead><tr><th>商品 / SKU</th><th>采集原值</th><th>映射结果</th><th>依据</th></tr></thead><tbody>${mappings}</tbody></table></div>` : ''}</details>`;
    };
    const card = document.getElementById('mercado-product-actions');
    if (!card) return;
    const panel = document.createElement('section');
    panel.className = 'market-attribute-preflight';
    const run = document.createElement('button');
    run.id = 'mercado-attribute-preflight-run';
    run.type = 'button'; run.className = 'secondary'; run.textContent = '检查上架属性';
    const reset = document.createElement('button');
    reset.type = 'button'; reset.className = 'secondary'; reset.textContent = '清除预检结果';
    const output = document.createElement('div');
    output.setAttribute('aria-live', 'polite');
    output.textContent = '选择商品和目标账号后可提前检查属性；即时上架复用通过预检的草稿，草稿有效期 15 分钟。';
    panel.append(run, reset, output);
    card.append(panel);
    let revisions = null;
    let signature = '';
    let correctionGroups = {};
    let pendingFields = [];
    const selection = () => ({
        ids: Array.from(selectedMercadoCollectionIds).sort((a,b) => a-b),
        mode: mercadoPublishMode.value,
        tokens: mercadoSelectedValues(mercadoPublishStore),
        groups: mercadoSelectedValues(mercadoPublishGroup),
        sites: mercadoSelectedValues(mercadoPublishSite),
        quantity: Number(mercadoPublishQuantity.value),
        ratio: Number(mercadoPublishRatio.value),
        minimumNet: Number(mercadoPublishMinimumNet?.value || 0),
    });
    window.mercadoAttributeDraftRevisions = () => {
        if (run.disabled) throw new Error('属性预检仍在进行，请等待完成');
        if (revisions === null) return undefined;
        if (signature !== JSON.stringify(selection())) throw new Error('所选商品或上架设置已变化，请重新检查上架属性');
        if (Object.values(revisions).some(value => !value)) throw new Error('存在待补全或映射冲突的商品，请修正后重新预检');
        return {...revisions};
    };
    reset.addEventListener('click', () => { revisions = null; correctionGroups = {}; pendingFields = []; output.textContent = '已清除预检结果，正式上架仍会重新检查全部属性。'; });
    run.addEventListener('click', async () => {
        const state = selection();
        if (!state.ids.length) { output.textContent = '请先选择商品。'; return; }
        if (!Number.isFinite(state.minimumNet) || state.minimumNet < 0 || mercadoPublishMinimumNet?.checkValidity() === false) { output.textContent = '最低净收益须为非负金额（USD），最多保留两位小数。'; mercadoPublishMinimumNet?.reportValidity(); return; }
        const targets = mercadoPublishTargetPreview(state.mode, state.tokens.map(Number), state.groups, state.sites);
        if (!targets.length) { output.textContent = '请选择兼容的目标账号和站点。'; return; }
        if (signature === JSON.stringify(state)) {
            for (const entry of pendingFields) {
                const field = output.querySelector(`[data-correction="${entry.index}"]`);
                const value = field?.querySelector('[data-value]')?.value.trim();
                if (!value) continue;
                const evidence = field.querySelector('[data-evidence]').value.trim();
                if (!evidence) { field.querySelector('[data-evidence]').reportValidity(); return; }
                const group = correctionGroups[entry.target] ||= {};
                const corrections = group[String(entry.productId)] ||= [];
                const correction = {...entry.field, evidence, [entry.field.choices?.length ? 'value_id' : 'value_name']: value};
                const previous = corrections.findIndex(c => c.attribute_id === correction.attribute_id && c.sku_index === correction.sku_index);
                if (previous >= 0) corrections[previous] = correction; else corrections.push(correction);
            }
        } else correctionGroups = {};
        pendingFields = [];
        run.disabled = true; reset.disabled = true; revisions = {}; signature = JSON.stringify(state);
        output.textContent = '正在读取目标类目规则并校验商品和 SKU…';
        try {
            let html = '';
            for (const target of targets) {
                const response = await fetch('/api/mercado-products/attribute-preflight', {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({product_item_ids:state.ids, token_id:Number(target.account.id), site_id:target.siteId, quantity:state.quantity, net_proceeds_ratio:state.ratio, minimum_net_proceeds_usd:state.minimumNet, corrections:correctionGroups[`${target.account.id}:${target.siteId}`] || {}}),
                });
                const body = await readMercadoPublishResponse(response);
                if (!response.ok || body.status !== 'success') throw new Error(body.message || '属性预检失败');
                for (const row of body.data.results) {
                    revisions[`${row.product_id}:${target.account.id}:${target.siteId}`] = row.revision || '';
                    html += `<article><strong>${esc(row.source_item_id || row.product_id)} · ${esc(target.account.display_name || target.account.nickname || target.account.id)} · ${esc(target.siteId)}</strong>${window.renderMercadoAttributeReport(row)}</article>`;
                    for (const field of row.correction_fields || []) {
                        const index = pendingFields.length;
                        pendingFields.push({index, target:`${target.account.id}:${target.siteId}`, productId:row.product_id, field});
                        const control = field.choices?.length
                            ? `<select data-value aria-label="${esc(field.name)}"><option value="">请选择真实值</option>${field.choices.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</select>`
                            : `<input data-value aria-label="${esc(field.name)}" placeholder="填写真实属性值，数值属性请带所需单位">`;
                        html += `<fieldset data-correction="${index}"><legend>${esc(row.source_item_id || row.product_id)} · ${esc(field.scope)} · ${esc(field.name)}</legend>${control}<input data-evidence required aria-label="属性依据" placeholder="依据，例如包装标签、供应商规格"><small>仅用于此商品和目标；填写后再次点击“检查上架属性”保存并校验。</small></fieldset>`;
                    }
                }
                output.innerHTML = html;
            }
            if (signature !== JSON.stringify(selection())) output.insertAdjacentHTML('afterbegin', '<p>选择已变化，这份预检结果已失效，请重新检查。</p>');
        } catch (error) {
            revisions.__incomplete = '';
            output.textContent = error.message;
        } finally { run.disabled = false; reset.disabled = false; }
    });
})();
