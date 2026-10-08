"use strict";

globalThis.ZeshunOrderPopup = (() => {
  const element = name => document.getElementById(`zying-order-${name}`);
  let state = {};
  let busy = false;
  let visible = false;
  let timer;
  const startDate = element("date-start");
  const endDate = element("date-end");
  const today = new Intl.DateTimeFormat("sv-SE", {timeZone: "Asia/Shanghai"}).format(new Date());
  endDate.value = today;
  startDate.value = new Date(Date.parse(today + "T12:00:00Z") - 6 * 86400000).toISOString().slice(0, 10);
  const controls = () => {
    const locked = busy || Boolean(state.running);
    element("start").disabled = !authenticated || locked;
    startDate.disabled = locked;
    endDate.disabled = locked;
    element("open").hidden = !state.running;
    element("open").disabled = !authenticated || busy;
    element("confirm").disabled = !authenticated || busy || state.phase !== "waiting_login";
    element("stop").disabled = !authenticated || busy || !state.running;
  };
  const render = value => {
    state = value || {};
    if (state.running && state.date_start) {startDate.value = state.date_start; endDate.value = state.date_end;}
    element("status").textContent = state.message || "选择下单日期范围后启动同步";
    element("summary").textContent = state.phase === "loading"
      ? `准备订单 ${state.prepared || 0} / ${state.total || 0} · 可先登录智赢`
      : `已处理 ${state.completed || 0} / ${state.total || 0} · 成功 ${state.updated || 0} · 跳过 ${state.skipped || 0} · 失败 ${state.failed || 0}`;
    element("results").innerHTML = (state.results || []).slice(-100).reverse().map(item =>
      `<div class="purchase-result"><strong>${escapeHtml(item.order_id || "")}</strong><span>${escapeHtml(item.message || "")}</span></div>`).join("");
    controls();
  };
  const load = async () => {
    clearTimeout(timer);
    try {
      const response = await runtimeMessage({type: "GET_ZYING_ORDER_STATUS"});
      if (!response.ok) throw new Error(response.error || "读取进度失败");
      render(response.state);
    } catch (error) {element("status").textContent = error.message || String(error); controls();}
    if (visible) timer = setTimeout(load, 1500);
  };
  const action = async (type, params = {}) => {
    busy = true;
    clearTimeout(timer);
    controls();
    try {
      const response = await runtimeMessage({type, ...params});
      if (!response.ok) throw new Error(response.error || "操作失败");
      render(response.state);
    } catch (error) {element("status").textContent = error.message || String(error);}
    finally {busy = false; controls(); if (visible) timer = setTimeout(load, 1500);}
  };
  element("mode").addEventListener("click", () => showMode("zying-order"));
  element("start").addEventListener("click", () => {
    if (!startDate.value || !endDate.value || startDate.value > endDate.value) {
      element("status").textContent = "请选择有效的订单开始和结束日期";
      return;
    }
    element("status").textContent = "正在启动同步并打开智赢…";
    void action("START_ZYING_ORDER_SYNC", {date_start: startDate.value, date_end: endDate.value});
  });
  element("open").addEventListener("click", () => void action("OPEN_ZYING_ORDER_LOGIN"));
  element("confirm").addEventListener("click", () => void action("CONFIRM_ZYING_ORDER_LOGIN"));
  element("stop").addEventListener("click", () => void action("STOP_ZYING_ORDER_SYNC"));
  return {syncControls: controls, setVisible(value) {visible = value; clearTimeout(timer); if (visible) void load();}};
})();
