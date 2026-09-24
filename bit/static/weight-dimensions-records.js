(function () {
    "use strict";

    const columns = [
        ["time", "时间"], ["image_url", "图片"], ["order_number", "编号"],
        ["salesperson", "业务员"], ["source", "来源"], ["company_store", "公司店铺"],
        ["product_id", "产品id"], ["category", "产品分类"], ["title", "标题"],
        ["shipment_id", "运单号"], ["tracking_number", "追踪号"], ["carrier", "运输商"],
        ["region", "地区"], ["declared_weight_g", "当前标记重量（克）"],
        ["declared_dimensions_cm", "当前标记尺寸（厘米）"], ["declared_freight", "当前运费"],
        ["actual_weight_g", "实际重量（克）"], ["actual_dimensions_cm", "实际尺寸（厘米）"],
        ["actual_freight", "实际运费"], ["marketplace_item_id", "美客多产品编号"],
        ["current_net_proceeds_usd", "本次净收益（USD）"], ["query_status", "查询状态"],
        ["query_error", "查询说明"], ["execution_status", "执行状态"], ["execution_error", "执行说明"]
    ];
    let taskId = "";
    let currentRecords = [];
    let busy = false;
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
    function render(records) {
        currentRecords = Array.isArray(records) ? records : [];
        const head = $("wdr-table").querySelector("thead");
        const body = $("wdr-table").querySelector("tbody");
        head.replaceChildren();
        body.replaceChildren();
        const header = document.createElement("tr");
        columns.forEach(([, label]) => {
            const th = document.createElement("th"); th.textContent = label; header.appendChild(th);
        });
        head.appendChild(header);
        if (!currentRecords.length) {
            const tr = document.createElement("tr"); const td = document.createElement("td");
            td.colSpan = columns.length; td.className = "wdr-empty"; td.textContent = "没有订单记录";
            tr.appendChild(td); body.appendChild(tr); return;
        }
        currentRecords.forEach((row) => {
            const tr = document.createElement("tr");
            columns.forEach(([key]) => {
                const td = document.createElement("td");
                if (key === "image_url") {
                    const src = safeImageUrl(row[key]);
                    if (src) { const img = document.createElement("img"); img.src = src; img.alt = "产品图片"; img.loading = "lazy"; td.appendChild(img); }
                    else td.textContent = "—";
                } else {
                    td.textContent = row[key] == null || row[key] === "" ? "—" : String(row[key]);
                }
                if (key === "execution_status" && row[key]) td.className = row[key] === "完成" ? "wdr-success" : "wdr-warning";
                tr.appendChild(td);
            });
            body.appendChild(tr);
        });
    }
    async function poll(mode) {
        while (busy && taskId) {
            const data = await api(`/api/weight-dimensions-records/${encodeURIComponent(taskId)}`);
            render(data.records);
            if (mode === "query") {
                status(`${data.message || "正在读取"}（${data.processed || 0}/${data.total || 0}）`);
                if (data.status === "ready") { busy = false; status(data.message); break; }
                if (data.status === "failed") throw new Error(data.message || "订单读取失败");
            } else {
                status(`${data.execute_message || "正在执行"}（${data.execute_processed || 0}/${data.execute_total || 0}）`);
                if (data.execute_status === "completed") { busy = false; status(data.execute_message); break; }
            }
            await new Promise((resolve) => window.setTimeout(resolve, 1200));
        }
    }
    function updateButtons() {
        const ready = Boolean(taskId) && !busy && currentRecords.length > 0;
        $("wdr-export").disabled = !ready;
        const canExecute = ready && currentRecords.some((row) => row.can_execute);
        $("wdr-execute").disabled = !canExecute || (typeof window.hasWorkbenchPermission === "function" && !window.hasWorkbenchPermission("order_analysis.execute"));
        $("wdr-upload").disabled = busy;
    }
    async function upload() {
        const file = $("wdr-file").files[0];
        if (!file) { status("请选择订单文件"); return; }
        busy = true; taskId = ""; render([]); updateButtons(); status("正在上传订单文件…");
        try {
            const form = new FormData(); form.append("file", file);
            const data = await api("/api/weight-dimensions-records/upload", { method: "POST", body: form });
            taskId = data.task_id; status("文件已接收，正在查询运单数据…");
            await poll("query");
        } catch (error) { busy = false; status(`失败：${errorMessage(error)}`); }
        updateButtons();
    }
    async function execute() {
        if (!taskId || busy) return;
        const count = currentRecords.filter((row) => row.can_execute).length;
        if (!window.confirm(`将更新 ${count} 条商品：先提交实际重量尺寸，再重新提交商品当前净收益（USD）。`)) return;
        busy = true; updateButtons(); status("正在启动更新…");
        try {
            await api(`/api/weight-dimensions-records/${encodeURIComponent(taskId)}/execute`, { method: "POST" });
            await poll("execute");
        } catch (error) { busy = false; status(`执行失败：${errorMessage(error)}`); }
        updateButtons();
    }
    document.addEventListener("DOMContentLoaded", () => {
        if (!( $("wdr-upload") && $("wdr-table") )) return;
        $("wdr-upload").addEventListener("click", upload);
        $("wdr-execute").addEventListener("click", execute);
        $("wdr-export").addEventListener("click", () => { if (taskId) window.location.assign(`/api/weight-dimensions-records/${encodeURIComponent(taskId)}/export`); });
        updateButtons();
    });
})();
