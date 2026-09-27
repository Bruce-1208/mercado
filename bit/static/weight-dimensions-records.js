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
    let recordTotal = 0;
    let pageRequest = 0;
    let renderSignature = "";
    let initialized = false;
    let pageLoading = false;
    const selectedRecords = new Map();
    let allMatchingSelected = false;
    const excludedOrderNumbers = new Set();
    let page = 1;
    let pageSize = 50;
    let appliedFilters = "";
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
    function isSelected(orderNumber) {
        return allMatchingSelected
            ? !excludedOrderNumbers.has(String(orderNumber || "").trim())
            : selectedOrderNumbers.has(String(orderNumber || "").trim());
    }
    function selectedRows(action = "") {
        return Array.from(selectedRecords.values()).filter((row) => selectedOrderNumbers.has(String(row.order_number || "").trim()) && (action ? canAction(row, action) : row.can_execute_zeshun || row.can_execute_zying));
    }
    function displayText(row, key) {
        const value = cellValue(row, key);
        if (value) return value;
        if (row.measurement_notes?.[key]) return row.measurement_notes[key];
        if ([
            "product_id", "category", "tracking_number", "marketplace_item_id",
            "declared_weight_g", "declared_dimensions_cm", "declared_freight",
            "actual_weight_g", "actual_dimensions_cm", "actual_freight",
        ].includes(key)) return "待补全";
        return "—";
    }
    function render(records) {
        currentRecords = Array.isArray(records) ? records : [];
        const pageCount = Math.max(1, Math.ceil(recordTotal / pageSize));
        page = Math.min(page, pageCount);
        const pageRecords = currentRecords;
        $("wdr-page-summary").textContent = `共 ${recordTotal} 条 · 第 ${page} / ${pageCount} 页`;
        $("wdr-page-prev").disabled = page <= 1;
        $("wdr-page-next").disabled = page >= pageCount;
        const head = $("wdr-table").querySelector("thead");
        const body = $("wdr-table").querySelector("tbody");
        head.replaceChildren(); body.replaceChildren();
        const header = document.createElement("tr");
        const selectHead = document.createElement("th");
        const selectAll = document.createElement("input");
        selectAll.type = "checkbox"; selectAll.id = "wdr-select-all"; selectAll.title = "全选本页可执行记录";
        const selectableRecords = pageRecords.filter((row) => row.can_execute_zeshun || row.can_execute_zying);
        selectAll.disabled = selectableRecords.length === 0;
        selectAll.checked = selectableRecords.length > 0 && selectableRecords.every((row) => isSelected(row.order_number));
        selectAll.indeterminate = !selectAll.checked && selectableRecords.some((row) => isSelected(row.order_number));
        selectAll.addEventListener("change", () => {
            pageRecords.forEach((row) => {
                const orderNumber = String(row.order_number || "").trim();
                if (!(row.can_execute_zeshun || row.can_execute_zying)) return;
                if (allMatchingSelected) {
                    if (selectAll.checked) excludedOrderNumbers.delete(orderNumber);
                    else excludedOrderNumbers.add(orderNumber);
                } else if (selectAll.checked) {
                    selectedOrderNumbers.add(orderNumber); selectedRecords.set(orderNumber, row);
                } else {
                    selectedOrderNumbers.delete(orderNumber); selectedRecords.delete(orderNumber);
                }
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
        pageRecords.forEach((row) => {
            const tr = document.createElement("tr");
            const selectCell = document.createElement("td"); const checkbox = document.createElement("input");
            checkbox.type = "checkbox"; checkbox.checked = isSelected(row.order_number);
            checkbox.disabled = !row.can_execute_zeshun && !row.can_execute_zying;
            checkbox.title = checkbox.disabled ? "缺少实际重量或商品关联信息" : (row.measurement_notes?.actual_dimensions_cm ? "官方未提供尺寸；执行时只更新重量并保留原尺寸" : "选择此订单");
            checkbox.addEventListener("change", () => {
                const orderNumber = String(row.order_number || "").trim();
                if (allMatchingSelected) {
                    if (checkbox.checked) excludedOrderNumbers.delete(orderNumber);
                    else excludedOrderNumbers.add(orderNumber);
                    render(currentRecords);
                } else {
                    if (checkbox.checked) { selectedOrderNumbers.add(orderNumber); selectedRecords.set(orderNumber, row); } else { selectedOrderNumbers.delete(orderNumber); selectedRecords.delete(orderNumber); }
                    updateButtons();
                }
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
    function acceptPage(data) {
        recordTotal = data.record_total || 0;
        page = data.page || 1;
        currentRecords = Array.isArray(data.records) ? data.records : [];
        currentRecords.forEach(row => {
            const key = String(row.order_number || "").trim();
            if (selectedOrderNumbers.has(key)) selectedRecords.set(key, row);
        });
        const signature = JSON.stringify([page, pageSize, recordTotal, currentRecords]);
        if (signature !== renderSignature) { renderSignature = signature; renderSelected(); }
        else updateButtons();
    }
    async function loadPage() {
        if (!taskId) return;
        const request = ++pageRequest;
        const generation = pollGeneration;
        const id = taskId;
        pageLoading = true; updateButtons();
        try {
            const data = await api(`/api/weight-dimensions-records/${encodeURIComponent(id)}?page=${page}&page_size=${pageSize}`);
            if (request !== pageRequest || generation !== pollGeneration || id !== taskId) return;
            acceptPage(data);
        } catch (error) {
            if (request === pageRequest && generation === pollGeneration) status(`读取页面失败：${errorMessage(error)}`);
        } finally {
            if (request === pageRequest) { pageLoading = false; updateButtons(); }
        }
    }
    async function poll(mode, generation = ++pollGeneration, background = false) {
        while (busy && taskId && generation === pollGeneration) {
            if (document.hidden || !$("tab-weight-dimensions-records")?.classList.contains("active")) {
                await new Promise((resolve) => window.setTimeout(resolve, 3000));
                continue;
            }
            const data = await api(`/api/weight-dimensions-records/${encodeURIComponent(taskId)}?include_records=0`);
            if (generation !== pollGeneration) return;
            taskReady = data.status === "ready";
            if (data.total > 0 || taskReady) {
                await loadPage();
                if (generation !== pollGeneration) return;
            }
            if (mode === "query") {
                status(`${data.message || "正在读取"}（${data.processed || 0}/${data.total || 0}）`);
                if (data.status === "ready") { busy = false; status(data.message); updateButtons(); break; }
                if (data.status === "failed") throw new Error(data.message || "订单读取失败");
            } else {
                status(`${data.execute_message || "正在执行"}（${data.execute_processed || 0}/${data.execute_total || 0}）`);
                if (data.execute_status === "completed") { busy = false; status(data.execute_message); updateButtons(); break; }
            }
            await new Promise((resolve) => window.setTimeout(resolve, 3000));
        }
    }
    function updateButtons() {
        const hasRows = taskReady && recordTotal > 0;
        $("wdr-page-prev").disabled = pageLoading || page <= 1;
        $("wdr-page-next").disabled = pageLoading || page >= Math.max(1, Math.ceil(recordTotal / pageSize));
        $("wdr-page-size").disabled = pageLoading;
        const selected = selectedRows();
        const permission = typeof window.hasWorkbenchPermission !== "function" || window.hasWorkbenchPermission("order_analysis.execute");
        $("wdr-export").disabled = !hasRows || !taskId;
        $("wdr-export").textContent = "导出筛选结果";
        $("wdr-select-all-matching").disabled = busy || !hasRows;
        $("wdr-select-all-matching").textContent = allMatchingSelected ? "取消全选" : "全选筛选结果";
        $("wdr-zeshun-execute").disabled = !permission || busy || !hasRows || (allMatchingSelected ? !recordTotal : !selected.some((row) => canAction(row, "zeshun")));
        $("wdr-zying-execute").disabled = !permission || busy || !hasRows || (allMatchingSelected ? !recordTotal : !selected.some((row) => canAction(row, "zying")));
        $("wdr-upload").disabled = busy;
        $("wdr-refresh-changed").disabled = refreshPending || (busy && busyMode !== "query");
        $("wdr-filter-apply").disabled = refreshPending || busy;
        $("wdr-selection-summary").textContent = allMatchingSelected
            ? `已全选筛选结果 ${recordTotal} 条${excludedOrderNumbers.size ? `（取消 ${excludedOrderNumbers.size} 条）` : ""}`
            : `已选择 ${selected.length} 条`;
    }
    async function loadSavedChanges() {
        if (refreshPending || busy) return;
        renderSignature = ""; appliedFilters = filterParams().toString(); page = 1;
        selectedOrderNumbers.clear(); selectedRecords.clear(); allMatchingSelected = false; excludedOrderNumbers.clear();
        const generation = ++pollGeneration;
        refreshPending = true; busyMode = "saved"; busy = true;
        taskReady = false; taskId = ""; currentRecords = []; recordTotal = 0;
        renderSelected(); updateButtons(); status("正在读取数据库已保存记录…");
        try {
            const data = await api(`/api/weight-dimensions-records/saved?${appliedFilters}&page_size=${pageSize}`);
            if (generation !== pollGeneration) return;
            taskId = data.task_id; taskReady = data.status === "ready";
            busy = false; refreshPending = false; updateButtons();
            status(data.message || `已读取 ${data.total || 0} 条已保存记录`);
            acceptPage(data);
        } catch (error) {
            if (generation !== pollGeneration) return;
            refreshPending = false; busy = false;
            status(`读取已保存记录失败：${errorMessage(error)}`); updateButtons();
        }
    }
    async function refreshChanged(background = false) {
        if (refreshPending || (busy && busyMode !== "query")) return;
        if (background && busy) return;
        if (!background) { renderSignature = ""; appliedFilters = filterParams().toString(); page = 1; selectedOrderNumbers.clear(); selectedRecords.clear(); allMatchingSelected = false; excludedOrderNumbers.clear(); }
        const generation = ++pollGeneration;
        refreshPending = true; busyMode = "query";
        busy = true; taskReady = false; taskId = "";
        if (!background) { currentRecords = []; recordTotal = 0; }
        if (!background) renderSelected();
        updateButtons(); status("正在全量同步重量尺寸并存入数据库…");
        try {
            const data = await api("/api/weight-dimensions-records/changed?");
            taskId = data.task_id; refreshPending = false; updateButtons();
            await poll("query", generation, background);
            if (generation === pollGeneration && taskReady) await loadSavedChanges();
        } catch (error) {
            if (generation !== pollGeneration) return;
            refreshPending = false; busy = false; status(`读取失败：${errorMessage(error)}`); updateButtons();
        }
    }
    async function upload() {
        if (busy) return;
        const file = $("wdr-file").files[0]; if (!file) { status("请选择订单文件"); return; }
        page = 1; renderSignature = "";
        busyMode = "upload";
        busy = true; taskId = ""; taskReady = false; currentRecords = []; recordTotal = 0; selectedOrderNumbers.clear(); selectedRecords.clear(); allMatchingSelected = false; excludedOrderNumbers.clear();
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
        if (!allMatchingSelected && !rows.length) { alert(`请先选择可${action === "zeshun" ? "更新泽顺数据和链接" : "更新智赢产品"}的订单`); return; }
        if (action === "zying" && !$("wdr-agent-id")?.value) { alert("请先选择在线的本机 Agent"); return; }
        const label = action === "zeshun" ? "泽顺数据和链接" : "智赢产品";
        const selectionDescription = allMatchingSelected
            ? `筛选结果 ${recordTotal} 条（执行时自动跳过缺少商品关联、实际重量或已完成该操作的记录）`
            : `已选 ${rows.length} 条订单`;
        if (!window.confirm(`确认更新${selectionDescription}的${label}吗？`)) return;
        busyMode = "execute"; busy = true; updateButtons(); status(`正在提交${label}更新…`);
        try {
            await api(`/api/weight-dimensions-records/${encodeURIComponent(taskId)}/execute`, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action, select_all_matching: allMatchingSelected, excluded_order_numbers: allMatchingSelected ? Array.from(excludedOrderNumbers) : [], order_numbers: allMatchingSelected ? [] : rows.map((row) => row.order_number), agent_id: $("wdr-agent-id")?.value || ""}) });
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
        const start = new Date();
        start.setDate(start.getDate() - 6);
        start.setHours(0, 0, 0, 0);
        const localDateTime = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}T00:00`;
        if (!$("wdr-filter-date-from").value) $("wdr-filter-date-from").value = localDateTime(start);
        $("wdr-upload").addEventListener("click", upload); $("wdr-refresh-changed").addEventListener("click", () => refreshChanged());
        $("wdr-select-all-matching").addEventListener("click", () => {
            if (allMatchingSelected) {
                allMatchingSelected = false; excludedOrderNumbers.clear();
            } else {
                allMatchingSelected = true; excludedOrderNumbers.clear();
                selectedOrderNumbers.clear(); selectedRecords.clear();
            }
            render(currentRecords);
        });
        $("wdr-zeshun-execute").addEventListener("click", () => execute("zeshun")); $("wdr-zying-execute").addEventListener("click", () => execute("zying"));
        $("wdr-agent-refresh").addEventListener("click", loadAgents);
        $("wdr-export").addEventListener("click", () => { if (taskId) window.location.assign(`/api/weight-dimensions-records/${encodeURIComponent(taskId)}/export`); });
        const zone = $("wdr-dropzone");
        ["dragenter", "dragover"].forEach((eventName) => zone.addEventListener(eventName, (event) => { event.preventDefault(); zone.classList.add("wdr-drag-active"); }));
        ["dragleave", "drop"].forEach((eventName) => zone.addEventListener(eventName, (event) => { event.preventDefault(); zone.classList.remove("wdr-drag-active"); }));
        zone.addEventListener("drop", (event) => { const file = event.dataTransfer?.files?.[0]; if (!file) return; const transfer = new DataTransfer(); transfer.items.add(file); $("wdr-file").files = transfer.files; $("wdr-file-name").textContent = file.name; });
        $("wdr-file").addEventListener("change", () => { const file = $("wdr-file").files[0]; if (file) $("wdr-file-name").textContent = file.name; });
        $("wdr-filter-apply").addEventListener("click", loadSavedChanges);
        ["wdr-filter-date-from", "wdr-filter-date-to", "wdr-filter-source", "wdr-filter-salesperson", "wdr-filter-store", "wdr-filter-category", "wdr-filter-region", "wdr-filter-freight-min", "wdr-filter-freight-max"].forEach((id) => $(id)?.addEventListener("keydown", (event) => { if (event.key === "Enter") loadSavedChanges(); }));
        $("wdr-page-prev").addEventListener("click", () => { page = Math.max(1, page - 1); loadPage(); });
        $("wdr-page-next").addEventListener("click", () => { page++; loadPage(); });
        $("wdr-page-size").addEventListener("change", () => { pageSize = Number($("wdr-page-size").value) || 50; page = 1; loadPage(); });
        function activate() {
            if (!initialized && !document.hidden && $("tab-weight-dimensions-records")?.classList.contains("active")) {
                initialized = true; loadAgents(); loadSavedChanges();
            }
        }
        new MutationObserver(activate).observe($("tab-weight-dimensions-records"), {attributes: true, attributeFilter: ["class"]});
        document.addEventListener("visibilitychange", activate);
        updateButtons(); activate();
    });
})();
