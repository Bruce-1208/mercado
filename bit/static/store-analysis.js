/* Site sales and official second-level Mercado Libre category mix. */
let storeAnalysisReady = false;
let storeAnalysisScope = [];
let storeAnalysisRequest = 0;

function storeAnalysisEscape(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[character]);
}

function storeAnalysisOptions(select, values, label, selected) {
    select.innerHTML = `<option value="">${label}</option>` + values.map(row =>
        `<option value="${storeAnalysisEscape(row.value)}">${storeAnalysisEscape(row.label)}</option>`
    ).join("");
    select.value = values.some(row => row.value === selected) ? selected : "";
}

function refreshStoreAnalysisFilters(changed = "") {
    const salesperson = document.getElementById("store-analysis-salesperson");
    const group = document.getElementById("store-analysis-group");
    const store = document.getElementById("store-analysis-store");
    const owner = salesperson.value;
    const team = group.value;
    const shop = store.value;
    if (changed !== "salesperson") {
        const owners = [...new Set(storeAnalysisScope.filter(row =>
            (!team || row.group === team) && (!shop || row.id === shop)
        ).map(row => row.salesperson).filter(Boolean))].sort();
        storeAnalysisOptions(salesperson, owners.map(value => ({value, label: value})), "全部业务员", owner);
    }
    if (changed !== "group") {
        const groups = [...new Set(storeAnalysisScope.filter(row =>
            (!salesperson.value || row.salesperson === salesperson.value) && (!shop || row.id === shop)
        ).map(row => row.group).filter(Boolean))].sort();
        storeAnalysisOptions(group, groups.map(value => ({value, label: value})), "全部店铺组", team);
    }
    const stores = new Map();
    storeAnalysisScope.filter(row =>
        (!salesperson.value || row.salesperson === salesperson.value) &&
        (!group.value || row.group === group.value)
    ).forEach(row => stores.set(row.id, row.name));
    storeAnalysisOptions(store, [...stores].map(([value, label]) => ({value, label})), "全部店铺", shop);
}

async function initStoreAnalysis() {
    if (storeAnalysisReady) return;
    const now = new Date(Date.now() + 8 * 3600000);
    const end = now.toISOString().slice(0, 10);
    const start = new Date(now.getTime() - 29 * 86400000).toISOString().slice(0, 10);
    document.getElementById("store-analysis-start").value = start;
    document.getElementById("store-analysis-end").value = end;
    const response = await fetch(enterpriseScopedUrl("/api/mercado-tokens"), {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") throw new Error(payload.message || "店铺列表读取失败");
    storeAnalysisScope = (payload.data?.rows || []).flatMap(token => {
        const id = String(token.id || "");
        const name = String(token.display_name || token.nickname || `店铺 ${id}`);
        return (token.site_settings || []).map(setting => ({
            id, name, salesperson: String(setting.salesperson || ""), group: String(setting.group_name || ""),
        }));
    });
    refreshStoreAnalysisFilters();
    storeAnalysisReady = true;
}

function renderStoreAnalysis(data) {
    const container = document.getElementById("store-analysis-sites");
    const metric = data.metric === "gmv" ? "gmv_usd" : "orders";
    const money = value => `$${Number(value || 0).toLocaleString("en-US", {maximumFractionDigits: 2})}`;
    const count = value => Number(value || 0).toLocaleString("zh-CN");
    const colors = ["#2563eb", "#00a6a0", "#f59e0b", "#8b5cf6", "#ef6386", "#cbd5e1"];
    const sites = data.sites || [];
    if (!sites.length) {
        container.innerHTML = '<div class="empty-state">当前筛选范围没有有效销售订单</div>';
        return;
    }
    container.innerHTML = sites.map((site, index) => {
        const total = Number(site.category_total || 0);
        const slices = [...site.top_categories.map(item => Number(item[metric] || 0)), Number(site.other_value || 0)];
        let angle = 0;
        const gradient = slices.map((value, i) => {
            const next = angle + (total ? value / total * 360 : 0);
            const part = `${colors[i]} ${angle.toFixed(3)}deg ${next.toFixed(3)}deg`;
            angle = next;
            return part;
        }).join(", ");
        const pie = total ? `conic-gradient(${gradient})` : "#e2e8f0";
        const rows = site.top_categories.map((item, i) => {
            const value = Number(item[metric] || 0);
            const share = total ? value / total * 100 : 0;
            return `<li><span class="store-analysis-dot" style="background:${colors[i]}"></span>
                <span class="store-analysis-category">${storeAnalysisEscape(item.name)}</span>
                <strong>${metric === "gmv_usd" ? money(value) : count(value) + " 单"}</strong>
                <small>${share.toFixed(1)}%</small></li>`;
        }).join("");
        const other = site.other_value > 0 ? `<li><span class="store-analysis-dot" style="background:${colors[5]}"></span>
            <span class="store-analysis-category">其他及未识别分类</span>
            <strong>${metric === "gmv_usd" ? money(site.other_value) : count(site.other_value) + " 单"}</strong>
            <small>${(total ? site.other_value / total * 100 : 0).toFixed(1)}%</small></li>` : "";
        return `<article class="store-analysis-card">
            <header><div><span class="store-analysis-rank">#${index + 1}</span><h3>${storeAnalysisEscape(site.site_name)} <small>${storeAnalysisEscape(site.site_id)}</small></h3></div>
                <strong>${metric === "gmv_usd" ? money(site.gmv_usd) : count(site.orders) + " 单"}</strong></header>
            <p>销售单量 ${count(site.orders)} 单 · GMV ${money(site.gmv_usd)}${site.unconverted_orders ? ` · ${count(site.unconverted_orders)} 单缺少汇率` : ""}</p>
            <div class="store-analysis-chart"><div class="store-analysis-pie" role="img" aria-label="${storeAnalysisEscape(site.site_name)}二级分类前五占比" style="background:${pie}"><span>TOP 5</span></div>
                <ol class="store-analysis-legend">${rows || '<li>暂无可识别的二级分类</li>'}${other}</ol></div>
        </article>`;
    }).join("");
}

async function loadStoreAnalysis() {
    const message = document.getElementById("store-analysis-message");
    const container = document.getElementById("store-analysis-sites");
    const requestId = ++storeAnalysisRequest;
    try {
        await initStoreAnalysis();
        const params = new URLSearchParams({
            start_date: document.getElementById("store-analysis-start").value,
            end_date: document.getElementById("store-analysis-end").value,
            metric: document.getElementById("store-analysis-metric").value,
            salesperson: document.getElementById("store-analysis-salesperson").value,
            group_name: document.getElementById("store-analysis-group").value,
            token_id: document.getElementById("store-analysis-store").value,
        });
        if (!params.get("start_date") || !params.get("end_date")) throw new Error("请选择统计时间段");
        message.textContent = "正在统计订单和二级分类…";
        const response = await fetch(enterpriseScopedUrl(`/api/store-analysis?${params}`), {cache: "no-store"});
        const payload = await response.json();
        if (!response.ok || payload.status !== "success") throw new Error(payload.message || "分析失败");
        if (requestId !== storeAnalysisRequest) return;
        renderStoreAnalysis(payload.data || {});
        message.textContent = "按北京时间统计订单创建日期，排除取消及无效订单；一单含多个分类时，每个分类各计一单，饼图按分类关联订单数占比。GMV 统一折算为 USD。";
    } catch (error) {
        if (requestId !== storeAnalysisRequest) return;
        message.textContent = `店铺分析失败：${error.message || error}`;
        container.innerHTML = '<div class="empty-state">请调整筛选条件后重试</div>';
    }
}

document.getElementById("store-analysis-form")?.addEventListener("submit", event => {
    event.preventDefault();
    loadStoreAnalysis();
});
["salesperson", "group", "store"].forEach(field => {
    document.getElementById(`store-analysis-${field}`)?.addEventListener("change", () => refreshStoreAnalysisFilters(field));
});
