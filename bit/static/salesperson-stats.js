let salespersonStatsSequence = 0;
let salespersonStatsReady = false;
async function loadSalespersonStats() {
    const form = document.getElementById('salesperson-stats-form');
    const start = document.getElementById('salesperson-stats-start');
    const end = document.getElementById('salesperson-stats-end');
    const message = document.getElementById('salesperson-stats-message');
    const body = document.getElementById('salesperson-stats-rows');
    if (!salespersonStatsReady) {
        salespersonStatsReady = true;
        form.addEventListener('submit', event => { event.preventDefault(); loadSalespersonStats(); });
        document.getElementById('salesperson-stats-all').addEventListener('click', () => {
            start.value = ''; end.value = ''; loadSalespersonStats();
        });
    }
    const sequence = ++salespersonStatsSequence;
    body.replaceChildren();
    if (start.value && end.value && start.value > end.value) {
        message.textContent = '开始日期不能晚于结束日期'; return;
    }
    message.textContent = '正在加载业务员量统计…';
    try {
        const params = new URLSearchParams({start_date: start.value, end_date: end.value});
        const response = await fetch(`/api/salesperson-stats?${params}`, {cache: 'no-store'});
        const result = await response.json();
        if (!response.ok || result.status !== 'success') throw new Error(result.message || '加载失败');
        if (sequence !== salespersonStatsSequence) return;
        const data = result.data;
        for (const row of data.rows) {
            const tr = document.createElement('tr');
            for (const value of [`${row.salesperson}（#${row.operator_id}）`, row.print_count, row.purchase_count]) {
                const td = document.createElement('td'); td.textContent = String(value); tr.appendChild(td);
            }
            body.appendChild(tr);
        }
        message.textContent = data.rows.length
            ? `共 ${data.rows.length} 位业务员 · 打印面单 ${data.print_count} 单 · 新增采购 ${data.purchase_count} 单`
            : '当前范围暂无操作记录';
    } catch (error) {
        if (sequence === salespersonStatsSequence) message.textContent = `加载失败：${error.message}`;
    }
}
