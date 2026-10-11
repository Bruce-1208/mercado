"use strict";

globalThis.ZeshunOrderSync = (() => {
  const sessionKey = "zyingOrderSyncSession";
  const resumeAlarm = "zeshun-zying-order-sync-resume";
  const path = "/api/browser-extension/zying-order-sync/";
  let dependencies;
  let running;
  const api = (action, data) => dependencies.apiRequest(path + action,
    data === undefined ? {method: "GET"} : {method: "POST", body: JSON.stringify(data)});
  const save = session => dependencies.storageSet("local", {[sessionKey]: session});
  const session = async () => (await dependencies.storageGet("local", [sessionKey]))[sessionKey];
  const waitTab = async tabId => {
    await dependencies.waitTab(tabId);
    // The SPA's search field can mount after the document load event.
    const results = await chrome.scripting.executeScript({target: {tabId}, world: "MAIN", func: async () => {
      const deadline = Date.now() + 15000;
      while (Date.now() < deadline) {
        if (/login/i.test(location.hash + location.pathname)) return false;
        if (document.querySelector('input[placeholder="订单、采购单、运单,多个编号可以逗号、空格分隔"]')) return true;
        await new Promise(resolve => setTimeout(resolve, 200));
      }
      return false;
    }});
    if (!results?.[0]?.result) throw new Error("请先登录智赢，并确认可以打开订单列表，再点击确定登录");
  };
  const open = async state => {
    let saved = await session();
    let tab;
    if (saved?.request_id === state.request_id) {
      try { tab = await chrome.tabs.get(saved.tab_id); } catch (_) {}
    }
    if (tab) await chrome.tabs.update(tab.id, {active: true});
    else tab = await chrome.tabs.create({url: "https://meli.zying.net/#/order", active: true});
    await save({request_id: state.request_id, tab_id: tab.id});
    return state;
  };
  const run = async () => {
    const saved = await session();
    if (!saved) return;
    let state = await api("status");
    if (state.request_id !== saved.request_id || state.phase !== "syncing" || !state.running) return;
    try {
      await chrome.tabs.get(saved.tab_id);
    } catch (_) {
      await api("fail", {request_id: saved.request_id, message: "智赢同步标签页已关闭，请重新启动同步"});
      return;
    }
    try {
      await waitTab(saved.tab_id);
    } catch (error) {
      await api("login_required", {request_id: saved.request_id});
      return;
    }
    while (state.running && state.phase === "syncing") {
      state = await api("batch", {request_id: saved.request_id});
      if (!state.running || state.phase !== "syncing" || !state.rows?.length) return;
      const response = await chrome.scripting.executeScript({target: {tabId: saved.tab_id}, world: "MAIN",
        func: zeshunReadZyingOrderBatch, args: [state.rows]});
      const result = response?.[0]?.result;
      if (result?.busy) return;
      if (result?.login_required) {
        await api("login_required", {request_id: saved.request_id});
        return;
      }
      if (!Array.isArray(result?.rows)) {
        await api("fail", {request_id: saved.request_id, message: result?.error || "智赢页面读取失败"});
        return;
      }
      // Check cancellation after the browser request and before any writeback.
      state = await api("status");
      if (state.request_id !== saved.request_id || !state.running || state.phase !== "syncing") return;
      state = await api("results", {request_id: saved.request_id, rows: result.rows});
    }
  };
  const resume = () => {
    if (!running) {
      running = run().catch(error => {
        // Leave committed progress intact during network/server restarts. The
        // next alarm reads pending IDs; it never blindly replays a write.
        console.warn("智赢订单同步等待恢复：", error.message || error);
      }).finally(() => {running = null;});
    }
    return running;
  };
  return {
    configure(value) {dependencies = value; chrome.alarms.create(resumeAlarm, {periodInMinutes: 0.5});},
    async status() {const state = await api("status"); if (state.running && state.phase === "syncing") void resume(); return state;},
    async start(params) {
      let state;
      try {state = await api("start", params);}
      catch (error) {
        // A lost/slow response can leave a reserved task on the server. Read
        // it back instead of replaying the POST or leaving its login tab closed.
        if (error.status && error.status < 500) throw error;
        const saved = await api("status");
        if (!saved.running || saved.date_start !== params.date_start || saved.date_end !== params.date_end || (saved.order_status || "") !== (params.order_status || "")) throw error;
        state = saved;
      }
      return state.running ? open(state) : state;
    },
    async open() {const state = await api("status"); if (!state.running) throw new Error("请先启动同步任务"); return open(state);},
    async confirm() {
      const saved = await session();
      const state = await api("status");
      if (!saved || saved.request_id !== state.request_id) throw new Error("请先打开本次任务的智赢页面");
      // Reload to pick up a login completed in another tab, like the Agent.
      await chrome.tabs.update(saved.tab_id, {url: "https://meli.zying.net/#/order"});
      await chrome.tabs.reload(saved.tab_id);
      await waitTab(saved.tab_id);
      const claimed = await api("confirm", {request_id: saved.request_id});
      void resume();
      return claimed;
    },
    async stop() {const state = await api("status"); return api("stop", {request_id: state.request_id});},
    resume,
    alarm: resumeAlarm
  };
})();
