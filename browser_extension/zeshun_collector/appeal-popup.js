"use strict";

// The worker owns execution; reopening this window only reads progress.
(() => {
  const element = id => document.getElementById(`appeal-${id}`);
  const tabSelect = element("tab");
  const refresh = element("refresh");
  const start = element("start");
  const stop = element("stop");
  const fields = [element("customer-mode"), element("copy-mode"), element("loop-count"), element("message")];
  let task = null;
  let busy = false;
  let visible = false;
  let timer = null;
  const defaultSites = [
    ["MLM", "墨西哥"], ["MLB", "巴西"], ["MLC", "智利"],
    ["MCO", "哥伦比亚"], ["MLA", "阿根廷"], ["MLU", "乌拉圭"]
  ].map(([site_id, site_name]) => ({site_id, site_name}));
  const running = () => Boolean(task?.running || ["running", "starting", "stopping"].includes(task?.status));
  const siteInputs = () => [...element("sites").querySelectorAll("input")];
  const formInputs = () => [...element("forms").querySelectorAll("input")];

  function supportedTab(tab) {
    return [tab.url, tab.pendingUrl].some(value =>
      /^https:\/\/(?:global-selling|sellers)\.mercadolibre\.com(?:[\/:?#]|$)/i.test(String(value || "")));
  }

  function syncControls() {
    const locked = busy || running();
    const loggedIn = typeof authenticated !== "undefined" && authenticated;
    tabSelect.disabled = locked;
    refresh.disabled = !loggedIn || locked;
    start.disabled = !loggedIn || locked || !tabSelect.value ||
      !siteInputs().some(input => input.checked) || !formInputs().some(input => input.checked);
    stop.disabled = !loggedIn || !running();
    [...fields, ...siteInputs(), ...formInputs()].forEach(field => { field.disabled = locked; });
    const human = element("customer-mode").value === "人工客服";
    element("copy-mode").querySelector('option[value="AI话术模式"]').disabled = human;
    if (human) element("copy-mode").value = "普通模式";
    element("copy-hint").hidden = element("copy-mode").value !== "AI话术模式";
  }

  function renderSites() {
    element("sites").replaceChildren();
    element("shop").textContent = "开始后将直接打开当前店铺的声誉页面";
    element("shop").className = "plugin-login-status";
    for (const [index, site] of defaultSites.entries()) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = String(site.site_id || site.site_code || "");
      input.checked = index === 0;
      input.addEventListener("change", syncControls);
      label.append(input, document.createTextNode(site.site_name || site.site_id || site.site_code));
      element("sites").append(label);
    }
    syncControls();
  }

  function renderTask(state) {
    task = state || null;
    element("status").textContent = state?.message || (running() ? "申诉执行中…" : "等待启动");
    element("summary").textContent = `已处理 ${Number(state?.completed_groups ?? state?.processed ?? 0)} · 已发送 ${Number(state?.sent_groups ?? state?.sent ?? 0)} · 失败 ${Number(state?.failed_groups ?? state?.failed ?? 0)}`;
    const logs = state?.logs || state?.log || [];
    element("log").textContent = Array.isArray(logs)
      ? logs.map(entry => typeof entry === "string" ? entry : entry.message || entry.text || "").join("\n") || "暂无申诉日志"
      : String(logs || "暂无申诉日志");
    const identity = state?.identity || state?.shop;
    if (identity && running()) {
      element("shop").textContent = `${identity.shop_name || identity.name || identity.nickname || "当前店铺"} · 账号 ${identity.root_seller_id || identity.seller_id}`;
      element("shop").className = "plugin-login-status ok";
    }
    if (running() && state?.selection) {
      const selection = state.selection;
      siteInputs().forEach(input => { input.checked = (selection.sites || []).includes(input.value); });
      formInputs().forEach(input => { input.checked = (selection.forms || []).includes(input.value); });
      element("customer-mode").value = selection.mode;
      element("copy-mode").value = selection.appeal_copy_mode;
      element("loop-count").value = Number(selection.loop_count) === 0 ? "permanent" : String(selection.loop_count);
      element("message").value = selection.message || "";
    }
    syncControls();
  }

  async function loadStatus() {
    clearTimeout(timer);
    try {
      const response = await runtimeMessage({type: "GET_APPEAL_STATUS"});
      if (!response.ok) throw new Error(response.error || "读取申诉进度失败");
      renderTask(response.state);
    } catch (error) {
      element("status").textContent = error.message || String(error);
    }
    if (visible && running()) timer = setTimeout(loadStatus, 1500);
  }

  async function readContext() {
    if (busy || running()) return;
    busy = true;
    syncControls();
    try {
      const previous = tabSelect.value;
      const tabs = (await chrome.tabs.query({})).filter(supportedTab)
        .sort((left, right) => Number(right.active) - Number(left.active) || Number(right.lastAccessed || 0) - Number(left.lastAccessed || 0));
      tabSelect.replaceChildren(new Option("请选择当前店铺网页", ""));
      for (const tab of tabs) tabSelect.add(new Option(tab.title || "美客多店铺后台", String(tab.id)));
      tabSelect.value = tabs.some(tab => String(tab.id) === previous) ? previous : String(tabs[0]?.id || "");
      if (!tabSelect.value) throw new Error("请在此浏览器打开并登录 global-selling.mercadolibre.com 或 sellers.mercadolibre.com 店铺后台");
      element("shop").textContent = "开始后将直接打开当前店铺的声誉页面";
      element("shop").className = "plugin-login-status";
    } catch (error) {
      element("shop").textContent = error.message || String(error);
      element("shop").className = "plugin-login-status error";
    } finally {
      busy = false;
      syncControls();
    }
  }

  element("mode").addEventListener("click", () => showMode("appeal"));
  refresh.addEventListener("click", readContext);
  tabSelect.addEventListener("change", syncControls);
  [...fields, ...formInputs()].forEach(field => field.addEventListener("change", syncControls));
  start.addEventListener("click", async () => {
    if (busy || running() || !tabSelect.value) return;
    busy = true;
    syncControls();
    try {
      const response = await runtimeMessage({type: "START_APPEAL", tabId: Number(tabSelect.value), params: {
        sites: siteInputs().filter(input => input.checked).map(input => input.value),
        forms: formInputs().filter(input => input.checked).map(input => input.value),
        mode: element("customer-mode").value,
        appeal_copy_mode: element("copy-mode").value,
        loop_count: element("loop-count").value,
        message: element("message").value.trim()
      }});
      if (!response.ok) throw new Error(response.error || "启动申诉失败");
      renderTask(response.state);
      await loadStatus();
    } catch (error) { showResult(error.message || String(error), "error"); }
    finally { busy = false; syncControls(); }
  });
  stop.addEventListener("click", async () => {
    stop.disabled = true;
    try {
      const response = await runtimeMessage({type: "STOP_APPEAL"});
      if (!response.ok) throw new Error(response.error || "停止申诉失败");
      renderTask(response.state);
      await loadStatus();
    } catch (error) { showResult(error.message || String(error), "error"); }
    finally { syncControls(); }
  });
  element("open-records").addEventListener("click", () => runtimeMessage({type: "OPEN_CONSOLE", tab: "ai-appeals"}));
  globalThis.ZeshunAppealPopup = {
    syncControls,
    async setVisible(value) {
      visible = value;
      clearTimeout(timer);
      if (!visible) return;
      await loadStatus();
      if (!running()) await readContext();
    }
  };
  renderSites();
})();
