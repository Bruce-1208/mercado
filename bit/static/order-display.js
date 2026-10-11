/* Show the same RMB balance used by order profit accounting. */
let orderDisplayRate = null;
let orderDisplayRatePromise = null;
let orderDisplayRateCheckedAt = 0;

function orderBalanceCnyMarkup(row) {
    if (row.data_origin === 'yandex' || orderBalanceValue(row) === null) return '';
    const balance = orderBalanceValue(row);
    if (Object.prototype.hasOwnProperty.call(row, 'balance_cny')) {
        const value = row.balance_cny;
        return `<small class="order-balance-cny-accounting" title="与利润计算使用相同的结余及汇率">${value == null ? '人民币汇率待同步' : orderMoney(value)}</small>`;
    }
    return `<small class="order-balance-cny" data-balance-usd="${balance}"></small>`;
}

function updateOrderBalanceCny() {
    document.querySelectorAll('.order-balance-cny').forEach(element => {
        element.textContent = orderDisplayRate
            ? `≈ ${orderMoney(Number(element.dataset.balanceUsd) * orderDisplayRate.cny_per_usd)}`
            : '人民币汇率待获取';
        element.title = orderDisplayRate
            ? `${orderDisplayRate.source} · ${orderDisplayRate.rate_date} · 1 USD = ${orderDisplayRate.cny_per_usd.toFixed(4)} CNY`
            : '汇率暂不可用，请刷新重试';
    });
}

async function loadOrderDisplayRate() {
    if (orderDisplayRatePromise) return orderDisplayRatePromise;
    if (orderDisplayRate && Date.now() - orderDisplayRateCheckedAt < 3600000) return;
    orderDisplayRatePromise = (async () => {
        try {
            const response = await fetch('/api/orders/display-exchange-rate', {cache: 'no-store', signal: AbortSignal.timeout(35000)});
            const payload = await response.json();
            const rate = Number(payload.data?.cny_per_usd);
            if (!response.ok || payload.status !== 'success' || !Number.isFinite(rate) || rate <= 0) throw new Error('rate unavailable');
            orderDisplayRate = {...payload.data, cny_per_usd: rate};
            orderDisplayRateCheckedAt = Date.now();
        } catch (_) {
            orderDisplayRate = null;
        } finally {
            updateOrderBalanceCny();
            orderDisplayRatePromise = null;
        }
    })();
    return orderDisplayRatePromise;
}

function openOrderImageViewer(url, alt = '商品大图') {
    const safeUrl = mercadoSafeUrl(url);
    if (!safeUrl) return;
    let viewer = document.getElementById('order-image-viewer');
    if (!viewer) {
        viewer = document.createElement('dialog');
        viewer.id = 'order-image-viewer';
        viewer.setAttribute('aria-label', '商品大图');
        viewer.innerHTML = '<button type="button" aria-label="关闭大图">×</button><img alt="商品大图"><p role="status" hidden>图片加载失败，请关闭后重试</p>';
        viewer.querySelector('button').onclick = () => viewer.close();
        viewer.onclick = event => { if (event.target === viewer) viewer.close(); };
        viewer.querySelector('img').onerror = () => { viewer.querySelector('p').hidden = false; };
        document.body.appendChild(viewer);
    }
    viewer.querySelector('p').hidden = true;
    const image = viewer.querySelector('img');
    image.alt = alt;
    image.src = safeUrl;
    if (!viewer.open) viewer.showModal();
}

document.addEventListener('click', event => {
    const target = event.target.closest?.('.order-picture-zoom, .order-sku-image-link');
    if (!target) return;
    event.preventDefault();
    openOrderImageViewer(target.href, target.querySelector('img')?.alt);
});

// Observe only rendered amounts; avoid rerendering the order or interrupting edits.
for (const id of ['order-table-body', 'order-detail-content']) {
    const element = document.getElementById(id);
    if (element) new MutationObserver(records => {
        if (records.some(record => Array.from(record.addedNodes).some(node =>
            node.nodeType === 1 && (node.matches('.order-balance-cny') || node.querySelector('.order-balance-cny'))))) updateOrderBalanceCny();
    }).observe(element, {childList: true, subtree: true});
}

const orderViewportTab = document.getElementById('tab-orders');
function resizeOrderViewport() {
    if (!orderViewportTab?.classList.contains('active')) return;
    const top = orderViewportTab.getBoundingClientRect().top;
    orderViewportTab.style.height = `${Math.max(0, window.innerHeight - top - 8)}px`;
}
new MutationObserver(resizeOrderViewport).observe(orderViewportTab, {attributes: true, attributeFilter: ['class']});
const orderViewportObserver = new ResizeObserver(resizeOrderViewport);
for (const selector of ['.topbar', '.workspace-tabs', '#model-balance-alert']) {
    const element = document.querySelector(selector);
    if (element) orderViewportObserver.observe(element, {box: 'border-box'});
}
window.addEventListener('resize', resizeOrderViewport);
window.addEventListener('scroll', resizeOrderViewport, {passive: true});
resizeOrderViewport();
