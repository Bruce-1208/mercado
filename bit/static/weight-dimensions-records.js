(function () {
    "use strict";

    const columns = [
        ["time", "下单时间"], ["freight_changed_at", "运费变更时间"], ["image_url", "图片"], ["order_number", "编号"],
        ["salesperson", "业务员"], ["source", "来源"], ["company_store", "公司店铺"],
        ["product_id", "产品id"], ["category", "产品分类"], ["title", "标题"],
        ["shipment_id", "运单号"], ["tracking_number", "追踪号"], ["carrier", "运输商"],
        ["region", "地区"], ["declared_weight_g", "当前标记重量（克）"],
        ["declared_dimensions_cm", "当前标记尺寸（厘米）"], ["declared_freight", "当前运费"],
        ["actual_weight_g", "实际重量（克）"], ["actual_dimensions_cm", "实际尺寸（厘米）"],
        ["actual_freight", "实际运费"], ["freight_difference", "运费差值（实际-当前）"],
        ["marketplace_item_id", "美客多产品编号"], ["current_net_proceeds_usd", "本次净收益（USD）"],
        ["query_status", "查询状态"], ["query_error", "查询说明"],
        ["execution_status", "执行状态"], ["execution_error", "执行说明"], ["execution_logs", "更新记录"]
    ];
    let taskId = "";
    let currentRecords = [];
    let taskReady = false;
    let busy = false;
    let busyMode = "";
    let refreshPending = false;
    let pollGeneration = 0;
    const selectedOrderNumbers = new Set();
    const $ = (id) => document.getElementById(id);

    function status(message) { $("wdr-state").textContent = message; }
    function errorMessage(error) { return error && error.message ? error.message : String(error); }
    async function api(url, options) {
        const response = await fetch(url, options || { cache: "no-store" });
        const type = response.headers.get("content-type") || "";
        const payload = type.includes("application/json") ? await response.json() : {};
        if (!response.ok) throw new Error(payload.message || `请求失败（${response.status}）`);
        return payload.data;
    }
    function safeImageUrl(value) {
        try {
            const url = new URL(value, window.location.href);
            return ["http:", "https:"].includes(url.protocol) ? url.href : "";
        } catch (_) { return ""; }
    }
    function cellValue(row, key) {
        if (key === "execution_logs") return (row.execution_logs || []).map((entry) => [entry.time, entry.stage, entry.status, entry.message].filter(Boolean).join(" ")).join("\n");
        return row[key] == null ? "" : String(row[key]);
    }
    function canAction(row, action) { return Boolean(row[`can_execute_${action}`]); }
    function selectedRows(action = "") {
        return currentRecords.filter((row) => selectedOrderNumbers.has(String(row.order_number || "").trim()) && (action ? canAction(row, action) : row.can_execute_zeshun || row.can_execute_zying));
    }
    function displayText(row, key) {
        const value = cellValue(row, key);
        if (value) return value;
        if ([
            "product_id", "category", "tracking_number", "marketplace_item_id",
            "declared_weight_g", "declared_dimensions_cm", "declared_freight",
            "actual_weight_g", "actual_dimensions_cm", "actual_freight",
        ].includes(key)) return "待补全";
        return "—";
    }
    function render(records) {
        currentRecords = Array.isArray(records) ? records : [];
        const head = $("wdr-table").querySelector("thead");
        const body = $("wdr-table").querySelector("tbody");
        head.replaceChildren(); body.replaceChildren();
        const header = document.createElement("tr");
        const selectHead = document.createElement("th");
        const selectAll = document.createElement("input");
        selectAll.type = "checkbox"; selectAll.id = "wdr-select-all"; selectAll.title = "全选可执行记录";
        const selectableRecords = currentRecords.filter((row) => row.can_execute_zeshun || row.can_execute_zying);
        selectAll.disabled = selectableRecords.length === 0;
        selectAll.checked = selectableRecords.length > 0 && selectableRecords.every((row) => selectedOrderNumbers.has(String(row.order_number || "").trim()));
        selectAll.indeterminate = !selectAll.checked && selectableRecords.some((row) => selectedOrderNumbers.has(String(row.order_number || "").trim()));
        selectAll.addEventListener("change", () => {
            currentRecords.forEach((row) => {
                const orderNumber = String(row.order_number || "").trim();
                if (selectAll.checked && (row.can_execute_zeshun || row.can_execute_zying)) selectedOrderNumbers.add(orderNumber);
                else selectedOrderNumbers.delete(orderNumber);
            });
            render(currentRecords);
        });
        selectHead.appendChild(selectAll); header.appendChild(selectHead);
        columns.forEach(([, label]) => { const th = document.createElement("th"); th.textContent = label; header.appendChild(th); });
        head.appendChild(header);
        if (!currentRecords.length) {
            const tr = document.createElement("tr"); const td = document.createElement("td");
            td.colSpan = columns.length + 1; td.className = "wdr-empty"; td.textContent = "没有符合条件的运费变更订单";
            tr.appendChild(td); body.appendChild(tr); updateButtons(); return;
        }
        currentRecords.forEach((row) => {
            const tr = document.createElement("tr");
            const selectCell = document.createElement("td"); const checkbox = document.createElement("input");
            checkbox.type = "checkbox"; checkbox.checked = selectedOrderNumbers.has(String(row.order_number || "").trim());
            checkbox.disabled = !row.can_execute_zeshun && !row.can_execute_zying;
            checkbox.title = checkbox.disabled ? "缺少实际重量尺寸或商品关联信息" : "选择此订单";
            checkbox.addEventListener("change", () => {
                const orderNumber = String(row.order_number || "").trim();
                if (checkbox.checked) selectedOrderNumbers.add(orderNumber); else selectedOrderNumbers.delete(orderNumber);
                updateButtons();
            });
            selectCell.appendChild(checkbox); tr.appendChild(selectCell);
            columns.forEach(([key]) => {
                const td = document.createElement("td");
                if (key === "image_url") {
                    const src = safeImageUrl(row[key]);
                    if (src) { const img = document.createElement("img"); img.src = src; img.alt = "产品图片"; img.loading = "lazy"; td.appendChild(img); } else td.textContent = "—";
                } else if (key === "execution_logs") { td.className = "wdr-log-cell"; td.textContent = displayText(row, key); }
                else td.textContent = displayText(row, key);
                if (["product_id", "marketplace_item_id", "declared_weight_g", "declared_dimensions_cm", "actual_weight_g", "actual_dimensions_cm"].includes(key) && !cellValue(row, key)) td.classList.add("wdr-warning");
                if (key === "execution_status" && row[key]) td.className = row[key] === "完成" ? "wdr-success" : "wdr-warning";
                tr.appendChild(td);
            });
            body.appendChild(tr);
        });
        updateButtons();
    }
    function renderSelected() { render(currentRecords); }
    function filterParams() {
        const params = new URLSearchParams();
        const values = {
            date_from: $("wdr-filter-date-from")?.value || "", date_to: $("wdr-filter-date-to")?.value || "",
            source: $("wdr-filter-source")?.value || "", category: $("wdr-filter-category")?.value || "",
            region: $("wdr-filter-region")?.value || "", freight_min: $("wdr-filter-freight-min")?.value || "",
            freight_max: $("wdr-filter-freight-max")?.value || "",
        };
        Object.entries(values).forEach(([key, value]) => { if (value) params.set(key, value); });
        String($("wdr-filter-salesperson")?.value || "").split(/[,，]/).map((value) => value.trim()).filter(Boolean).forEach((value) => params.append("salesperson", value));
        const store = String($("wdr-filter-store")?.value || "").trim();
        if (store) params.set("store", store);
        return params;
    }
    async function poll(mode, generation = ++pollGeneration) {
        while (busy && taskId && generation === pollGeneration) {
            const data = await api(`/api/weight-dimensions-records/${encodeURIComponent(taskId)}`);
            if (generation !== pollGeneration) return;
            currentRecords = Array.isArray(data.records) ? data.records : [];
            taskReady = data.status === "ready"; renderSelected();
            if (mode === "query") {
                status(`${data.message || "正在读取"}（${data.processed || 0}/${data.total || 0}）`);
                if (data.status === "ready") { busy = false; status(data.message); updateButtons(); break; }
                if (data.status === "failed") throw new Error(data.message || "订单读取失败");
            } else {
                status(`${data.execute_message || "正在执行"}（${data.execute_processed || 0}/${data.execute_total || 0}）`);
                if (data.execute_status === "completed") { busy = false; status(data.execute_message); updateButtons(); break; }
            }
            await new Promise((resolve) => window.setTimeout(resolve, 1200));
        }
    }
    function updateButtons() {
        const hasRows = taskReady && currentRecords.length > 0;
        const selected = selectedRows();
        const permission = typeof window.hasWorkbenchPermission !== "function" || window.hasWorkbenchPermission("order_analysis.execute");
        $("wdr-export").disabled = !hasRows || !taskId;
        $("wdr-export").textContent = "导出筛选结果";
        $("wdr-zeshun-execute").disabled = !permission || busy || !hasRows || !selected.some((row) => canAction(row, "zeshun"));
        $("wdr-zying-execute").disabled = !permission || busy || !hasRows || !selected.some((row) => canAction(row, "zying"));
        $("wdr-upload").disabled = busy;
        $("wdr-refresh-changed").disabled = refreshPending || (busy && busyMode !== "query");
        $("wdr-filter-apply").disabled = $("wdr-refresh-changed").disabled;
        $("wdr-selection-summary").textContent = `已选择 ${selected.length} 条`;
    }
    async function refreshChanged() {
        if (refreshPending || (busy && busyMode !== "query")) return;
        const generation = ++pollGeneration;
        refreshPending = true; busyMode = "query";
        busy = true; taskReady = false; taskId = ""; currentRecords = []; selectedOrderNumbers.clear();
        renderSelected(); updateButtons(); status("正在读取运费变更订单…");
        try {
            const data = await api(`/api/weight-dimensions-records/changed?${filterParams().toString()}`);
            taskId = data.task_id; refreshPending = false; updateButtons();
            await poll("query", generation);
        } catch (error) {
            if (generation !== pollGeneration) return;
            refreshPending = false; busy = false; status(`读取失败：${errorMessage(error)}`); updateButtons();
        }
    }
    async function upload() {
        if (busy) return;
        const file = $("wdr-file").files[0]; if (!file) { status("请选择订单文件"); return; }
        busyMode = "upload";
        busy = true; taskId = ""; taskReady = false; currentRecords = []; selectedOrderNumbers.clear();
        renderSelected(); updateButtons(); status("正在读取订单文件…");
        try {
            const form = new FormData(); form.append("file", file);
            const data = await api("/api/weight-dimensions-records/upload", { method: "POST", body: form });
            taskId = data.task_id; $("wdr-file-name").textContent = `${file.name}；正在后台查询包裹数据`; await poll("query");
        } catch (error) { busy = false; status(`上传失败：${errorMessage(error)}`); updateButtons(); }
    }
    async function execute(action) {
        if (!taskId || busy) return;
        const rows = selectedRows(action);
        if (!rows.length) { alert(`请先选择可${action === "zeshun" ? "更新泽顺数据和链接" : "更新智赢产品"}的订单`); return; }
        if (action === "zying" && !$("wdr-agent-id")?.value) { alert("请先选择在线的本机 Agent"); return; }
        const label = action === "zeshun" ? "泽顺数据和链接" : "智赢产品";
        if (!window.confirm(`确认更新已选 ${rows.length} 条订单的${label}吗？`)) return;
        busyMode = "execute"; busy = true; updateButtons(); status(`正在提交${label}更新…`);
        try {
            await api(`/api/weight-dimensions-records/${encodeURIComponent(taskId)}/execute`, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action, order_numbers: rows.map((row) => row.order_number), agent_id: $("wdr-agent-id")?.value || ""}) });
            await poll("execute");
        } catch (error) { busy = false; status(`执行失败：${errorMessage(error)}`); updateButtons(); }
    }
    async function loadAgents() {
        const select = $("wdr-agent-id"); if (!select) return;
        try {
            const response = await fetch("/api/execution-agents?capability=ai_weight_price", {cache: "no-store"}); const payload = await response.json();
            if (!response.ok || payload.status !== "success") throw new Error(payload.message || "读取 Agent 失败");
            const agents = (payload.data?.agents || []).filter((agent) => agent.online);
            select.innerHTML = agents.length ? agents.map((agent) => `<option value="${String(agent.agent_id || "").replaceAll('"', '&quot;')}">${String(agent.name || agent.hostname || agent.agent_id).replaceAll('<', '&lt;')}</option>`).join("") : '<option value="">没有在线的智赢 Agent</option>';
        } catch (error) { select.innerHTML = `<option value="">${errorMessage(error)}</option>`; }
        updateButtons();
    }
    document.addEventListener("DOMContentLoaded", () => {
        if (!($("wdr-upload") && $("wdr-table"))) return;
        $("wdr-upload").addEventListener("click", upload); $("wdr-refresh-changed").addEventListener("click", refreshChanged);
        $("wdr-zeshun-execute").addEventListener("click", () => execute("zeshun")); $("wdr-zying-execute").addEventListener("click", () => execute("zying"));
        $("wdr-agent-refresh").addEventListener("click", loadAgents);
        $("wdr-export").addEventListener("click", () => { if (taskId) window.location.assign(`/api/weight-dimensions-records/${encodeURIComponent(taskId)}/export`); });
        const zone = $("wdr-dropzone");
        ["dragenter", "dragover"].forEach((eventName) => zone.addEventListener(eventName, (event) => { event.preventDefault(); zone.classList.add("wdr-drag-active"); }));
        ["dragleave", "drop"].forEach((eventName) => zone.addEventListener(eventName, (event) => { event.preventDefault(); zone.classList.remove("wdr-drag-active"); }));
        zone.addEventListener("drop", (event) => { const file = event.dataTransfer?.files?.[0]; if (!file) return; const transfer = new DataTransfer(); transfer.items.add(file); $("wdr-file").files = transfer.files; $("wdr-file-name").textContent = file.name; });
        $("wdr-file").addEventListener("change", () => { const file = $("wdr-file").files[0]; if (file) $("wdr-file-name").textContent = file.name; });
        $("wdr-filter-apply").addEventListener("click", refreshChanged);
        ["wdr-filter-date-from", "wdr-filter-date-to", "wdr-filter-source", "wdr-filter-salesperson", "wdr-filter-store", "wdr-filter-category", "wdr-filter-region", "wdr-filter-freight-min", "wdr-filter-freight-max"].forEach((id) => $(id)?.addEventListener("keydown", (event) => { if (event.key === "Enter") refreshChanged(); }));
        const year = new Date().getFullYear();
        $("wdr-filter-date-from").value = `${year}-08-01T00:00`;
        $("wdr-filter-date-to").value = `${year}-09-30T23:59`;
        updateButtons(); loadAgents(); refreshChanged();
    });
})();
