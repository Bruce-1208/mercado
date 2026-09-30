/* Order notes, lifecycle navigation and full product detail panels. */
function orderStatusGroups() {
    return {
        "找货": [
                "找货-没汇总",
                "找货-已采购",
                "找货-待确定"
        ],
        "审核": [
                "审核-待审核",
                "审核-已审核"
        ],
        "转运": [
                "转运-待收货",
                "转运-待出库",
                "转运-已出库",
                "转运-待退回"
        ],
        "待发": [
                "待发"
        ],
        "已发": [
                "已发-在途中",
                "已发-异常"
        ],
        "问题": [
                "问题-验货异常",
                "问题-物流退件",
                "问题-亏损超重"
        ],
        "交付": [
                "交付"
        ],
        "取消": [
                "取消-发货前",
                "取消-发货后"
        ]
};
}
let orderStatusHierarchyCounts = {};
function renderOrderStatusHierarchy(counts = orderStatusHierarchyCounts) {
    orderStatusHierarchyCounts = counts;
    const groups = orderStatusGroups();
    const isAllStatuses = !orderStatus || orderStatus === '全部' || orderStatus === '@全部';
    const activeGroup = isAllStatuses ? '' : Object.keys(groups).find(name => orderStatus === '@' + name || groups[name].includes(orderStatus));
    const button = (value, label, count, active) => `<button type="button" class="order-status-button${active ? ' active' : ''}" data-status="${escapeHtml(value)}" onclick="setOrderStatus(this.dataset.status)"><span>${escapeHtml(label)}</span><strong>${Number(count || 0).toLocaleString('zh-CN')}</strong></button>`;
    const total = Object.values(counts).reduce((sum, value) => sum + Number(value || 0), 0);
    const primary = button('', '全部', total, isAllStatuses) + Object.entries(groups).map(([name, statuses]) => button('@' + name, name, statuses.reduce((sum, status) => sum + Number(counts[status] || 0), 0), activeGroup === name)).join('');
    const children = !isAllStatuses && activeGroup && groups[activeGroup].length > 1 ? groups[activeGroup] : [];
    orderStatusStrip.style.display = 'block';
    const secondary = !isAllStatuses && children.length ? `<div class="order-status-level order-status-secondary">${children.map(status => button(status, status.split('-').slice(1).join('-'), counts[status], orderStatus === status)).join('')}</div>` : '';
    orderStatusStrip.innerHTML = `<div class="order-status-level">${primary}</div>${secondary}`;
}
function orderFreightDifference(row) {
    const actual = row.actual_freight_usd ?? row.freight;
    const quoted = row.quoted_freight_usd;
    if (row.freight_missing || actual == null || quoted == null || actual === '' || quoted === '') return null;
    if (!Number.isFinite(Number(actual)) || !Number.isFinite(Number(quoted))) return null;
    return Math.round((Number(actual) - Number(quoted)) * 100) / 100;
}
function orderExtraDialog(id, title) {
    let dialog = document.getElementById(id);
    if (!dialog) {
        dialog = document.createElement('dialog');
        dialog.id = id;
        dialog.className = 'order-detail-dialog order-extra-dialog';
        dialog.innerHTML = `<div class="order-detail-header"><h3></h3><button class="order-detail-close" type="button" aria-label="返回订单">×</button></div><div class="order-extra-body"></div>`;
        dialog.querySelector('button').onclick = () => dialog.close();
        document.body.append(dialog);
    }
    dialog.querySelector('h3').textContent = title;
    return dialog;
}
function orderEditorTarget(index) {
    if (index === 'detail') return {ids: [...orderDetailOrderIds], row: orderDetailRow};
    if (index !== undefined) return {ids: orderRowIds(orderRows[index]), row: orderRows[index]};
    return {ids: [...selectedOrderIds], row: null};
}
function openOrderRemarkEditor(index) { openOrderFieldEditor('order_remark', index); }
function openOrderStatusEditor(index) { openOrderFieldEditor('workflow_status', index); }
function openOrderFieldEditor(field, index) {
    const target = orderEditorTarget(index);
    if (!target.ids.length) { orderBulkMessage.textContent = '请先勾选订单'; return; }
    const isNote = field === 'order_remark';
    const dialog = orderExtraDialog('order-field-editor', `${isNote ? '订单备注' : '修改状态'} · ${target.ids.length} 单`);
    const body = dialog.querySelector('.order-extra-body');
    body.innerHTML = `<form><label>${isNote ? '备注内容' : '订单状态'}${isNote ? `<textarea name="value" rows="7" maxlength="5000" placeholder="填写备注，清空后保存可删除备注">${escapeHtml(target.row?.order_remark || '')}</textarea><p>${target.ids.length > 1 ? '保存后统一替换所选订单的备注。' : '最多 5000 字。'}</p>` : `<select name="value"><option value="">自动流转</option>${orderStatusFlow.map(status => `<option value="${escapeHtml(status)}">${escapeHtml(status)}</option>`).join('')}</select>`}</label><p class="order-editor-message" role="status"></p><div class="order-editor-actions"><button type="button" class="secondary">取消</button><button type="submit" class="primary">保存</button></div></form>`;
    const form = body.querySelector('form');
    if (!isNote) form.elements.value.value = target.row?.status || '';
    form.querySelector('[type=button]').onclick = () => dialog.close();
    form.onsubmit = async event => {
        event.preventDefault();
        const submit = form.querySelector('[type=submit]');
        if (submit.disabled) return;
        submit.disabled = true;
        const message = form.querySelector('[role=status]');
        message.textContent = '正在保存…';
        try {
            const response = await fetch('/api/orders/bulk-update', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({order_ids:target.ids, [field]:form.elements.value.value})});
            const payload = await response.json();
            if (!response.ok || payload.status !== 'success') throw new Error(payload.message || '保存失败');
            dialog.close();
            if (index === 'detail' && orderDetailDialog.open) orderDetailDialog.close();
            orderBulkMessage.textContent = `已保存 ${Number(payload.data?.matched || 0)} 单`;
            await loadOrders(orderPage);
        } catch (error) { message.textContent = error.message || String(error); }
        finally { submit.disabled = false; }
    };
    if (!dialog.open) dialog.showModal();
    form.elements.value.focus();
}
function orderProductAttributeValue(attribute) {
    if (attribute.value_name != null) return String(attribute.value_name);
    if (Array.isArray(attribute.values)) return attribute.values.map(value => value.name ?? value.id ?? '').join(' / ');
    if (attribute.value_struct) return [attribute.value_struct.number, attribute.value_struct.unit].filter(value => value != null).join(' ');
    return String(attribute.value_id ?? '—');
}
function orderProductTranslatedText(value, translations = {}) {
    const original = String(value || '');
    const chinese = translations[original.trim()];
    return escapeHtml(original) + (chinese && chinese !== original.trim() ? `<span class="order-product-chinese" lang="zh-CN">${escapeHtml(chinese)}</span>` : '');
}
function orderProductAttributes(attributes, translations = {}) {
    if (!Array.isArray(attributes) || !attributes.length) return '<p>平台未提供属性</p>';
    return `<dl class="order-product-attributes">${attributes.map(attribute => `<div><dt>${orderProductTranslatedText(attribute.name || attribute.id || '属性', translations)}</dt><dd>${orderProductTranslatedText(orderProductAttributeValue(attribute), translations)}</dd></div>`).join('')}</dl>`;
}
async function openOrderProductDetail(productId) {
    if (!productId) return;
    const ids = [...orderDetailOrderIds];
    const dialog = orderExtraDialog('order-product-detail', '产品详情');
    const body = dialog.querySelector('.order-extra-body');
    const requestId = (dialog.requestId || 0) + 1;
    dialog.requestId = requestId;
    body.innerHTML = '<p role="status">正在读取完整商品资料…</p>';
    if (!dialog.open) dialog.showModal();
    const load = async () => {
        const params = new URLSearchParams({product_id:productId});
        ids.forEach(id => params.append('order_id', id));
        try {
            const response = await fetch('/api/orders/product-detail?' + params, {cache:'no-store'});
            const payload = await response.json();
            if (!response.ok || payload.status !== 'success') throw new Error(payload.message || '商品详情读取失败');
            if (dialog.requestId !== requestId) return;
            const data = payload.data || {};
            const item = data.item || {};
            const translations = data.translations_zh || {};
            const description = data.description || {};
            const descriptionText = description.plain_text || (description.text ? new DOMParser().parseFromString(description.text, 'text/html').body.textContent : '') || '平台未提供商品描述';
            const pictures = Array.isArray(item.pictures) ? item.pictures : [];
            const variants = Array.isArray(item.variations) && item.variations.length ? item.variations : [{...item, id:item.id, attribute_combinations:[]}];
            const pictureMarkup = picture => {
                const url = mercadoSafeUrl(picture?.secure_url || picture?.url || '');
                return url ? `<img src="${escapeHtml(url)}" loading="lazy" alt="商品图片">` : '';
            };
            const scalar = value => value == null ? '—' : escapeHtml(String(value));
            const permalink = mercadoSafeUrl(item.permalink || '');
            body.innerHTML = `<div class="order-product-toolbar"><button class="secondary order-product-back" type="button">返回订单</button><button class="secondary order-product-retry" type="button">刷新</button>${permalink ? `<a href="${escapeHtml(permalink)}" target="_blank" rel="noopener noreferrer">平台商品页 ↗</a>` : ''}</div>
                ${(data.warnings || []).map(warning => `<p role="status">${escapeHtml(warning)}</p>`).join('')}
                <h2 data-copy-text="${escapeHtml(item.title || '')}">${orderProductTranslatedText(item.title || productId, translations)}</h2>
                <p data-copy-text="${escapeHtml(productId)}">${escapeHtml(productId)} · ${scalar(item.currency_id)} ${scalar(item.price)} · 库存 ${scalar(item.available_quantity)}</p>
                <div class="order-product-pictures">${pictures.map(pictureMarkup).join('')}</div>
                <section><h3>完整商品描述</h3><pre class="order-product-description">${orderProductTranslatedText(descriptionText, translations)}</pre></section>
                <section><h3>商品属性</h3>${orderProductAttributes(item.attributes, translations)}</section>
                <section><h3>全部 SKU（${variants.length}）</h3><div class="order-product-variants">${variants.map(variant => {
                    const sku = variant.seller_sku || variant.seller_custom_field || (variant.attributes || []).find(attribute => attribute.id === 'SELLER_SKU')?.value_name || variant.id;
                    const images = (variant.picture_ids || []).map(id => pictures.find(picture => String(picture.id) === String(id))).filter(Boolean);
                    return `<article><div class="order-product-pictures">${images.map(pictureMarkup).join('')}</div><strong data-copy-text="${escapeHtml(String(sku || ''))}">SKU ${scalar(sku)}</strong><p>变体 ID ${scalar(variant.id)} · ${scalar(item.currency_id)} ${scalar(variant.price ?? item.price)} · 库存 ${scalar(variant.available_quantity)} · 已售 ${scalar(variant.sold_quantity)}</p>${orderProductAttributes([...(variant.attribute_combinations || []), ...(variant.attributes || [])], translations)}</article>`;
                }).join('')}</div></section>`;
            body.querySelector('.order-product-back').onclick = () => dialog.close();
            body.querySelector('.order-product-retry').onclick = () => openOrderProductDetail(productId);
        } catch (error) {
            if (dialog.requestId !== requestId) return;
            body.innerHTML = `<p role="alert">${escapeHtml(error.message || String(error))}</p><button class="secondary" type="button">重试</button>`;
            body.querySelector('button').onclick = load;
        }
    };
    await load();
}
const orderDetailsStyles = document.createElement('style');
orderDetailsStyles.textContent = `
.order-product-chinese{display:block;color:#175cd3;margin-top:6px;font-size:14px;font-weight:400;white-space:pre-wrap}
.order-status-level{display:flex;flex-wrap:wrap;gap:8px;padding:8px 0}.order-status-secondary{border-top:1px solid #e4e7ec}.order-status-secondary .order-status-button{font-size:12px;padding:6px 10px}
.order-extra-dialog{width:min(1050px,94vw);max-height:90vh}.order-extra-body{padding:20px;overflow:auto;max-height:76vh}.order-extra-body h2{font-size:20px;overflow-wrap:anywhere}.order-extra-body section{margin-top:24px}.order-extra-body textarea,.order-extra-body select{display:block;width:100%;box-sizing:border-box;margin-top:10px;padding:10px;border:1px solid #d0d5dd;border-radius:8px;font:inherit}.order-editor-actions,.order-product-toolbar{display:flex;align-items:center;gap:12px}.order-editor-actions{justify-content:flex-end}.order-product-pictures{display:flex;gap:10px;flex-wrap:wrap}.order-product-pictures img{width:110px;height:110px;object-fit:contain;border:1px solid #eaecf0;border-radius:8px}.order-product-description{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.7;background:#f8fafc;padding:16px;border-radius:8px}.order-product-attributes{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:1px;background:#eaecf0;border:1px solid #eaecf0}.order-product-attributes div{padding:10px;background:white;overflow-wrap:anywhere}.order-product-attributes dt{color:#667085;font-size:12px}.order-product-attributes dd{margin:4px 0 0}.order-product-variants article{border:1px solid #e4e7ec;border-radius:10px;padding:16px;margin:12px 0}.order-editor-message{color:#b42318}#order-field-editor{width:min(600px,94vw)}
`;
document.head.append(orderDetailsStyles);

async function advanceOrderOutbound(index, confirm) {
    const fromDetail = index === 'detail';
    const row = fromDetail ? orderDetailRow : orderRows[index];
    const ids = fromDetail ? [...orderDetailOrderIds] : orderRowIds(row);
    if (!row || !ids.length || advanceOrderOutbound.running) return;
    advanceOrderOutbound.running = true;
    const messageTarget = fromDetail ? orderDetailActionMessage : orderBulkMessage;
    const actionButton = fromDetail ? orderDetailOutboundButton : null;
    if (actionButton) actionButton.disabled = true;
    messageTarget.textContent = confirm ? '正在确认出库…' : '正在申请出库…';
    try {
        const response = await fetch(confirm ? '/api/orders/outbound/confirm' : '/api/orders/bulk-update', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({order_ids: ids, workflow_status: confirm ? '转运-已出库' : '转运-待出库'}),
        });
        const payload = await response.json();
        if (!response.ok || payload.status !== 'success') throw new Error(payload.message || '操作失败');
        const successMessage = confirm ? '已确认出库，可以打印面单' : '已申请出库，等待仓库确认';
        messageTarget.textContent = successMessage;
        if (fromDetail) {
            row.status = confirm ? '转运-已出库' : '转运-待出库';
            const currentIndex = orderRows.indexOf(row);
            if (currentIndex >= 0) showOrderDetail(currentIndex);
        }
        await loadOrders(orderPage);
        if (fromDetail && orderDetailDialog.open) {
            const refreshedIndex = orderRows.findIndex(candidate => orderRowIds(candidate).some(id => ids.includes(id)));
            if (refreshedIndex >= 0) showOrderDetail(refreshedIndex);
            messageTarget.textContent = successMessage;
        }
    } catch (error) { messageTarget.textContent = error.message || String(error); }
    finally {
        if (actionButton) actionButton.disabled = false;
        advanceOrderOutbound.running = false;
    }
}
