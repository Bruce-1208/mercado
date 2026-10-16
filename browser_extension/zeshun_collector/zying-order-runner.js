"use strict";

globalThis.ZeshunOrderSync = (() => {
  const sessionKey = "zyingOrderSyncSession";
  const resumeAlarm = "zeshun-zying-order-sync-resume";
  const path = "/api/browser-extension/zying-order-sync/";
  let dependencies;
  let running;
  const orderUrl = "https://meli.zying.net/#/order";
  const api = (action, data) => dependencies.apiRequest(path + action,
    data === undefined ? {method: "GET"} : {method: "POST", body: JSON.stringify(data)});
  const save = session => dependencies.storageSet("local", {[sessionKey]: session});
  const session = async () => (await dependencies.storageGet("local", [sessionKey]))[sessionKey];
  const pageReady = async tabId => {
    const results = await chrome.scripting.executeScript({target: {tabId}, world: "MAIN", func: () =>
      !/login/i.test(location.hash + location.pathname) &&
      Boolean(document.querySelector('input[placeholder="订单、采购单、运单,多个编号可以逗号、空格分隔"]'))});
    return Boolean(results?.[0]?.result);
  };
  const waitTab = async tabId => {
    // Poll short probes: a redirect may destroy an injected execution context,
    // and unrelated page resources may never reach the tab's complete event.
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      await chrome.tabs.get(tabId);
      try {if (await pageReady(tabId)) return;} catch (_) {}
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    throw new Error("未检测到智赢订单列表，请在同步标签页登录并打开订单列表，再点击确定登录");
  };
  const open = async state => {
    let saved = await session();
    let tab;
    if (saved?.request_id === state.request_id) {
      try { tab = await chrome.tabs.get(saved.tab_id); } catch (_) {}
    }
    if (tab) await chrome.tabs.update(tab.id, {active: true});
    else tab = await chrome.tabs.create({url: orderUrl, active: true});
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
      // Keep an already usable login intact. Updating the URL and immediately
      // reloading used to race navigation and discard the page's ready state.
      let ready = false;
      try {ready = await pageReady(saved.tab_id);} catch (_) {}
      if (!ready) {
        let tab;
        try {tab = await chrome.tabs.get(saved.tab_id);} catch (_) {
          await open(state);
          throw new Error("原智赢标签页已关闭，已重新打开，请登录后再次点击确定登录");
        }
        if (tab.url === orderUrl) await chrome.tabs.reload(saved.tab_id);
        else await chrome.tabs.update(saved.tab_id, {url: orderUrl});
      }
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
