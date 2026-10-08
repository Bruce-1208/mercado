let salespersonStatsSequence = 0;
let salespersonStatsReady = false;
const salespersonStatsResults = new Map();
const salespersonStatsPending = new Map();
const salespersonStatsCacheMs = 30000;

async function fetchSalespersonStats(url, force) {
    if (salespersonStatsPending.has(url)) return salespersonStatsPending.get(url);
    const cached = salespersonStatsResults.get(url);
    if (!force && cached && Date.now() - cached.time < salespersonStatsCacheMs) return cached.data;
    const pending = (async () => {
        const response = await fetch(url, {cache: 'no-store'});
        const result = await response.json();
        if (!response.ok || result.status !== 'success') throw new Error(result.message || '加载失败');
        const data = result.data || {};
        salespersonStatsResults.set(url, {data, time: Date.now()});
        if (salespersonStatsResults.size > 12) salespersonStatsResults.delete(salespersonStatsResults.keys().next().value);
        return data;
    })();
    salespersonStatsPending.set(url, pending);
    try {
        return await pending;
    } finally {
        salespersonStatsPending.delete(url);
    }
}

async function loadSalespersonStats(force = false) {
    const form = document.getElementById('salesperson-stats-form');
    const start = document.getElementById('salesperson-stats-start');
    const end = document.getElementById('salesperson-stats-end');
    const message = document.getElementById('salesperson-stats-message');
    const body = document.getElementById('salesperson-stats-rows');
    if (!form || !start || !end || !message || !body) return;
    if (!salespersonStatsReady) {
        salespersonStatsReady = true;
        form.addEventListener('submit', event => { event.preventDefault(); loadSalespersonStats(true); });
        document.getElementById('salesperson-stats-all').addEventListener('click', () => {
            start.value = ''; end.value = ''; loadSalespersonStats(true);
        });
    }
    const sequence = ++salespersonStatsSequence;
    if (start.value && end.value && start.value > end.value) {
        message.textContent = '开始日期不能晚于结束日期'; return;
    }
    message.textContent = '正在加载业务员量统计…';
    try {
        const params = new URLSearchParams({start_date: start.value, end_date: end.value});
        const baseUrl = `/api/salesperson-stats?${params}`;
        const url = typeof enterpriseScopedUrl === 'function' ? enterpriseScopedUrl(baseUrl) : baseUrl;
        const data = await fetchSalespersonStats(url, force);
        if (sequence !== salespersonStatsSequence) return;
        const rows = Array.isArray(data.rows) ? data.rows : [];
        const fragment = document.createDocumentFragment();
        for (const row of rows) {
            const tr = document.createElement('tr');
            for (const value of [`${row.salesperson}（#${row.operator_id}）`, row.print_count, row.purchase_count]) {
                const td = document.createElement('td'); td.textContent = String(value); tr.appendChild(td);
            }
            fragment.appendChild(tr);
        }
        body.replaceChildren(fragment);
        message.textContent = rows.length
            ? `共 ${rows.length} 位业务员 · 打印面单 ${data.print_count} 单 · 新增采购 ${data.purchase_count} 单`
            : '当前范围暂无操作记录';
    } catch (error) {
        if (sequence === salespersonStatsSequence) message.textContent = `加载失败：${error.message}`;
    }
}
