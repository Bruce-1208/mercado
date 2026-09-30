/* Site sales and selected-level Mercado Libre category mix. */
let storeAnalysisReady = false;
let storeAnalysisScope = [];
let storeAnalysisRequest = 0;
let storeAnalysisScopePromise = null;
let storeAnalysisFiltersLoaded = false;
let storeAnalysisQueryRestored = false;
let storeAnalysisRestoredQuery = null;
let storeAnalysisUseRestoredQuery = false;
const storeAnalysisResults = new Map();
const storeAnalysisPending = new Map();
const storeAnalysisCacheMs = 60000;
const storeAnalysisLastQueryKey = "mercado.store-analysis.last-query.v1";

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

function restoreStoreAnalysisQuery() {
    if (storeAnalysisQueryRestored) return;
    storeAnalysisQueryRestored = true;
    if (typeof localStorage === "undefined") return;
    try {
        const saved = JSON.parse(localStorage.getItem(storeAnalysisLastQueryKey) || "null");
        if (!saved || !/^\d{4}-\d{2}-\d{2}$/.test(saved.start_date || "") ||
            !/^\d{4}-\d{2}-\d{2}$/.test(saved.end_date || "")) return;
        const level = Number(saved.category_level || 2);
        const fields = {
            start_date: "start", end_date: "end", metric: "metric",
            category_level: "category-level", salesperson: "salesperson",
            group_name: "group", token_id: "store",
        };
        storeAnalysisRestoredQuery = {};
        Object.entries(fields).forEach(([key, field]) => {
            storeAnalysisRestoredQuery[key] = String(saved[key] || "");
            const input = document.getElementById(`store-analysis-${field}`);
            if (input) input.value = storeAnalysisRestoredQuery[key];
        });
        storeAnalysisRestoredQuery.metric = saved.metric === "gmv" ? "gmv" : "orders";
        storeAnalysisRestoredQuery.category_level = String(Number.isInteger(level) && level >= 1 && level <= 6 ? level : 2);
        document.getElementById("store-analysis-metric").value = storeAnalysisRestoredQuery.metric;
        document.getElementById("store-analysis-category-level").value = storeAnalysisRestoredQuery.category_level;
        storeAnalysisUseRestoredQuery = true;
        if (storeAnalysisFiltersLoaded) {
            document.getElementById("store-analysis-salesperson").value = storeAnalysisRestoredQuery.salesperson;
            document.getElementById("store-analysis-group").value = storeAnalysisRestoredQuery.group_name;
            document.getElementById("store-analysis-store").value = storeAnalysisRestoredQuery.token_id;
            storeAnalysisUseRestoredQuery = false;
        }
    } catch (_) {
        storeAnalysisRestoredQuery = null;
    }
}

function saveStoreAnalysisQuery(params) {
    if (typeof localStorage === "undefined") return;
    try {
        localStorage.setItem(storeAnalysisLastQueryKey, JSON.stringify(Object.fromEntries(params)));
    } catch (_) {
        // The server cache remains available when browser storage is disabled.
    }
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
    if (storeAnalysisUseRestoredQuery && storeAnalysisRestoredQuery) {
        salesperson.value = storeAnalysisRestoredQuery.salesperson;
        group.value = storeAnalysisRestoredQuery.group_name;
        store.value = storeAnalysisRestoredQuery.token_id;
        storeAnalysisUseRestoredQuery = false;
    }
    storeAnalysisFiltersLoaded = true;
}

function initStoreAnalysis() {
    if (!storeAnalysisReady) {
        const now = new Date(Date.now() + 8 * 3600000);
        document.getElementById("store-analysis-start").value = new Date(now.getTime() - 6 * 86400000).toISOString().slice(0, 10);
        document.getElementById("store-analysis-end").value = now.toISOString().slice(0, 10);
        document.getElementById("store-analysis-category-level").value = "2";
        storeAnalysisReady = true;
    }
    if (storeAnalysisScopePromise) return storeAnalysisScopePromise;
    storeAnalysisScopePromise = (async () => {
        const response = await fetch(enterpriseScopedUrl("/api/mercado-tokens"), {cache: "no-store"});
        const payload = await response.json();
        if (!response.ok || payload.status !== "success") throw new Error(payload.message || "店铺列表读取失败");
        storeAnalysisScope = (payload.data?.rows || []).flatMap(token => {
            const id = String(token.id || "");
            const name = String(token.display_name || token.nickname || `店铺 ${id}`);
            const settings = token.site_settings?.length ? token.site_settings : [{}];
            return settings.map(setting => ({
                id, name, salesperson: String(setting.salesperson || ""), group: String(setting.group_name || ""),
            }));
        });
        refreshStoreAnalysisFilters();
    })().catch(error => {
        storeAnalysisScopePromise = null;
        document.getElementById("store-analysis-filter-message").textContent = `店铺筛选项加载失败：${error.message || error}；全部店铺统计仍可查看，点击查看分析可重试。`;
    });
    document.getElementById("store-analysis-filter-message").textContent = "";
    return storeAnalysisScopePromise;
}

async function fetchStoreAnalysis(url, force) {
    const cached = storeAnalysisResults.get(url);
    if (!force && cached && Date.now() - cached.time < storeAnalysisCacheMs) return cached.data;
    const pendingKey = force ? `${url}&refresh=1` : url;
    if (storeAnalysisPending.has(pendingKey)) return storeAnalysisPending.get(pendingKey);
    const pending = (async () => {
        const requestUrl = pendingKey;
        const response = await fetch(requestUrl, {cache: "no-store"});
        const payload = await response.json();
        if (!response.ok || payload.status !== "success") throw new Error(payload.message || "分析失败");
        const data = payload.data || {};
        storeAnalysisResults.set(url, {time: Date.now(), data});
        if (storeAnalysisResults.size > 20) storeAnalysisResults.delete(storeAnalysisResults.keys().next().value);
        return data;
    })();
    storeAnalysisPending.set(pendingKey, pending);
    try {
        return await pending;
    } finally {
        storeAnalysisPending.delete(pendingKey);
    }
}

function renderStoreOrderTrend(trend) {
    const container = document.getElementById("store-analysis-trend");
    if (!container) return;
    if (!trend) { container.innerHTML = ""; return; }
    const days = trend.days || [];
    const max = Math.max(1, ...days.map(day => Number(day.orders || 0)));
    const x = i => 52 + i * 98;
    const y = value => 170 - Number(value) / max * 130;
    const rate = day => day.orders == null ? "未纳入" : day.previous_orders == null ? "无对比数据"
        : day.previous_orders === 0 ? (day.orders === 0 ? "持平" : "新增（前日0单）")
        : `${day.change_rate > 0 ? "+" : ""}${Number(day.change_rate).toFixed(1)}%`;
    const points = days.map((day, i) => day.orders == null ? "" : `${x(i)},${y(day.orders)}`).filter(Boolean).join(" ");
    const marks = days.map((day, i) => `<text x="${x(i)}" y="198" text-anchor="middle">${storeAnalysisEscape(day.date.slice(5))}</text>
        ${day.orders == null ? "" : `<circle cx="${x(i)}" cy="${y(day.orders)}" r="4" fill="#2563eb"><title>${storeAnalysisEscape(day.date)}：${day.orders} 单，环比 ${rate(day)}</title></circle>
        <text x="${x(i)}" y="${y(day.orders) - 10}" text-anchor="middle">${day.orders}</text>`}`).join("");
    container.innerHTML = `<article class="store-analysis-card store-analysis-trend"><header><h3>单量变化率</h3><strong>范围内全部订单 ${Number(trend.total_orders).toLocaleString("zh-CN")} 单</strong></header>
        <p>截至所选结束日期的最近七天 · 北京时间 · 全部订单状态（含取消及无效订单），按订单号去重。每日环比 =（当日单量 − 前日单量）÷ 前日单量。</p>
        <svg viewBox="0 0 692 218" role="img" aria-label="最近七天每日单量折线图"><text x="12" y="20">单量</text>
        <line x1="40" y1="170" x2="660" y2="170" stroke="#cbd5e1"/><polyline points="${points}" fill="none" stroke="#2563eb" stroke-width="3"/>${marks}</svg>
        <div class="store-analysis-trend-table"><table><thead><tr><th>日期</th>${days.map(day => `<th>${storeAnalysisEscape(day.date)}</th>`).join("")}</tr></thead>
        <tbody><tr><th>单量</th>${days.map(day => `<td>${day.orders == null ? "未纳入" : day.orders}</td>`).join("")}</tr>
        <tr><th>日环比</th>${days.map(day => `<td>${rate(day)}</td>`).join("")}</tr></tbody></table></div></article>`;
}

function renderStoreAnalysis(data) {
    renderStoreOrderTrend(data.order_trend);
    const container = document.getElementById("store-analysis-sites");
    const metric = data.metric === "gmv" ? "gmv_usd" : "orders";
    const categoryLevel = Number(data.category_level || 2);
    const money = value => `$${Number(value || 0).toLocaleString("en-US", {maximumFractionDigits: 2})}`;
    const count = value => Number(value || 0).toLocaleString("zh-CN");
    const colors = ["#2563eb", "#00a6a0", "#f59e0b", "#8b5cf6", "#ef6386", "#14b8a6", "#f97316", "#6366f1", "#84cc16", "#ec4899", "#cbd5e1"];
    const sites = data.sites || [];
    if (!sites.length) {
        container.innerHTML = '<div class="empty-state">当前筛选范围没有有效销售订单</div>';
        return;
    }
    container.innerHTML = sites.map((site, index) => {
        const total = Number(site.category_total || 0);
        const slices = site.top_categories.map((item, i) => ({value: Number(item[metric] || 0), color: colors[i]}));
        if (Number(site.other_value || 0) > 0) slices.push({value: Number(site.other_value), color: colors[10]});
        let angle = 0;
        const gradient = slices.map(slice => {
            const next = angle + (total ? slice.value / total * 360 : 0);
            const part = `${slice.color} ${angle.toFixed(3)}deg ${next.toFixed(3)}deg`;
            angle = next;
            return part;
        }).join(", ");
        const pie = total ? `conic-gradient(${gradient})` : "#e2e8f0";
        const rows = site.top_categories.map((item, i) => {
            const value = Number(item[metric] || 0);
            const share = total ? value / total * 100 : 0;
            const categoryName = item.name_zh || (/[㐀-鿿]/.test(String(item.name || ""))
                ? item.name : "分类名称暂不可用");
            return `<li><span class="store-analysis-dot" style="background:${colors[i]}"></span>
                <span class="store-analysis-category">${storeAnalysisEscape(categoryName)}</span>
                <strong>${metric === "gmv_usd" ? money(value) : count(value) + " 单"}</strong>
                <small>${share.toFixed(1)}%</small></li>`;
        }).join("");
        const other = site.other_value > 0 ? `<li><span class="store-analysis-dot" style="background:${colors[10]}"></span>
            <span class="store-analysis-category">其他及未识别分类</span>
            <strong>${metric === "gmv_usd" ? money(site.other_value) : count(site.other_value) + " 单"}</strong>
            <small>${(total ? site.other_value / total * 100 : 0).toFixed(1)}%</small></li>` : "";
        const unknownOrders = Number(site.unrecognized_category_orders || 0);
        const unknownNote = unknownOrders ? `<p class="store-analysis-unknown">未识别分类 ${count(unknownOrders)} 单：可能是订单和关联刊登都没有类目 ID、官方类目目录中找不到该 ID，或类目路径不足所选层级。</p>` : "";
        return `<article class="store-analysis-card">
            <header><div><span class="store-analysis-rank">#${index + 1}</span><h3>${storeAnalysisEscape(site.site_name)} <small>${storeAnalysisEscape(site.site_id)}</small></h3></div>
                <strong>${metric === "gmv_usd" ? money(site.gmv_usd) : count(site.orders) + " 单"}</strong></header>
            <p>销售单量 ${count(site.orders)} 单 · GMV ${money(site.gmv_usd)}${site.unconverted_orders ? ` · ${count(site.unconverted_orders)} 单缺少汇率` : ""}</p>
            ${unknownNote}
            <div class="store-analysis-chart"><div class="store-analysis-pie" role="img" aria-label="${storeAnalysisEscape(site.site_name)}${categoryLevel}级分类前十占比" style="background:${pie}"><span>TOP 10</span></div>
                <ol class="store-analysis-legend">${rows || `<li>暂无可识别的${categoryLevel}级分类</li>`}${other}</ol></div>
        </article>`;
    }).join("");
}

async function loadStoreAnalysis(force = false) {
    const message = document.getElementById("store-analysis-message");
    const container = document.getElementById("store-analysis-sites");
    const requestId = ++storeAnalysisRequest;
    try {
        void initStoreAnalysis();
        restoreStoreAnalysisQuery();
        const params = !force && storeAnalysisUseRestoredQuery && storeAnalysisRestoredQuery
            ? new URLSearchParams(storeAnalysisRestoredQuery)
            : new URLSearchParams({
            start_date: document.getElementById("store-analysis-start").value,
            end_date: document.getElementById("store-analysis-end").value,
            metric: document.getElementById("store-analysis-metric").value,
            category_level: document.getElementById("store-analysis-category-level").value,
            salesperson: document.getElementById("store-analysis-salesperson").value,
            group_name: document.getElementById("store-analysis-group").value,
            token_id: document.getElementById("store-analysis-store").value,
        });
        if (!params.get("start_date") || !params.get("end_date")) throw new Error("请选择统计时间段");
        message.textContent = force
            ? `正在重新统计订单和${params.get("category_level")}级分类…`
            : `正在读取上次的订单和${params.get("category_level")}级分类分析…`;
        const data = await fetchStoreAnalysis(enterpriseScopedUrl(`/api/store-analysis?${params}`), force);
        if (requestId !== storeAnalysisRequest) return;
        renderStoreAnalysis(data);
        saveStoreAnalysisQuery(params);
        storeAnalysisRestoredQuery = Object.fromEntries(params);
        if (storeAnalysisFiltersLoaded) storeAnalysisUseRestoredQuery = false;
        const computedAt = data.computed_at ? new Date(data.computed_at).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false}) : "时间不可用";
        document.getElementById("store-analysis-data-time").textContent = `上次数据时间（北京时间）：${computedAt}`;
        const warning = data.category_translation_warning;
        message.textContent = `${warning ? `${warning} ` : ""}按北京时间统计订单创建日期，排除取消及无效订单；一单含多个分类时，每个分类各计一单，饼图按所选指标显示前十分类占比。GMV 统一折算为 USD。`;
    } catch (error) {
        if (requestId !== storeAnalysisRequest) return;
        message.textContent = `店铺分析失败：${error.message || error}`;
        renderStoreOrderTrend(null);
        container.innerHTML = '<div class="empty-state">请调整筛选条件后重试</div>';
    }
}

document.getElementById("store-analysis-form")?.addEventListener("submit", event => {
    event.preventDefault();
    storeAnalysisUseRestoredQuery = false;
    loadStoreAnalysis(true);
});
["salesperson", "group", "store"].forEach(field => {
    document.getElementById(`store-analysis-${field}`)?.addEventListener("change", () => {
        storeAnalysisUseRestoredQuery = false;
        refreshStoreAnalysisFilters(field);
    });
});
["start", "end", "category-level", "metric"].forEach(field => {
    document.getElementById(`store-analysis-${field}`)?.addEventListener("change", () => {
        storeAnalysisUseRestoredQuery = false;
    });
});
