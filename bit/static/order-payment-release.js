/* Payment release is independent of fulfillment and of estimated profit. */
function orderPaymentRelease(row) {
    const labels = {released: '已释放', unreleased: '未释放', partial: '部分释放', unknown: '待核实'};
    const state = Object.hasOwn(labels, row.payment_release_state) ? row.payment_release_state : 'unknown';
    if (row.data_origin === 'yandex') return {label: '不适用', date: '—', note: '资金释放核对仅适用于美客多订单'};
    const actual = ['released', 'partial'].includes(state) && /^\d{4}-\d{2}-\d{2}$/.test(row.payment_release_date || '');
    return {
        label: labels[state],
        date: actual ? row.payment_release_date : state === 'unreleased' ? '尚未释放' : '日期待核实',
        note: [
            '平台资金释放，不表示银行卡到账；日期沿用平台日期',
            state === 'partial' ? '日期为已释放部分的最近释放日期' : '多笔支付全部释放时显示最近释放日期',
            `子订单 ${row.id || ''}`,
            row.payment_release_checked_at ? `最近核对（北京时间）${row.payment_release_checked_at}` : '等待后台核对',
            row.payment_release_error || '',
        ].filter(Boolean).join(' · '),
    };
}

function orderPaymentReleaseMarkup(row) {
    return orderSourceRows(row).map(source => {
        const release = orderPaymentRelease(source);
        return `<div class="order-cell-stack" title="${escapeHtml(release.note)}">
            ${row.is_merged ? `<small>子订单 ${escapeHtml(source.id)}</small>` : ''}
            <strong>${escapeHtml(release.label)}</strong><span>${escapeHtml(release.date)}</span>
            ${source.payment_release_error ? '<small>本次核对失败，显示上次结果</small>' : ''}
        </div>`;
    }).join('');
}

function orderPaymentReleaseText(row, field) {
    return orderSourceRows(row).map(source => `${row.is_merged ? `${source.id}：` : ''}${orderPaymentRelease(source)[field]}`).join('；');
}
