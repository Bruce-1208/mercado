"use strict";

// A single runner is owned by this extension/browser profile. The console only
// supplies task-bound identifiers/copy and persists records; every browser action
// is performed in the tab/window selected when the run started.
globalThis.ZeshunAppealRunner = (() => {
  const KEY = "zeshunAppealRun";
  const API = "/api/browser-extension/appeal/";
  const HOST = "https://global-selling.mercadolibre.com";
  let deps = {}, active = null, starting = false;
  const siteMap = {MX: "MLM", BR: "MLB", CL: "MLC", CO: "MCO", AR: "MLA", UY: "MLU", 墨西哥: "MLM", 巴西: "MLB", 智利: "MLC", 哥伦比亚: "MCO", 阿根廷: "MLA", 乌拉圭: "MLU"};
  const siteReplies = {MLM: "Mexico (Direct to consumer)", MLB: "Brazil", MLC: "Chile", MCO: "Colombia", MLA: "Argentina", MLU: "Uruguay"};
  const norm = text => String(text || "").replace(/\s+/g, " ").trim();
  const siteId = value => siteMap[value] || siteMap[String(value || "").toUpperCase()] || String(value || "").toUpperCase();
  const failure = (message, status = "pre_send_failed", sent = false) => Object.assign(new Error(message), {status, sent});
  const executionStatus = value => {
    if (["logged_out", "auth_required"].includes(value)) return "login_required";
    return ["no_data", "sent", "replied", "needs_human", "reply_timeout", "sent_unknown", "pre_send_failed", "failed", "partial", "stopped", "deadline_exceeded", "window_busy", "login_required", "rate_limited"].includes(value) ? value : "pre_send_failed";
  };
  function mercadoUrl(value) {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password ||
      !/(^|\.)(mercadolibre\.com(?:\.(?:mx|br|ar|co|uy))?|mercadolivre\.com\.br|mercadolibre\.cl)$/i.test(url.hostname)) throw failure("请在当前店铺的 Mercado Libre 后台打开插件");
    return url;
  }
  function publicStatus(run) {
    if (!run) return {status: "idle", running: false, processed: 0, sent: 0, failed: 0, logs: []};
    const {params, stopped, owner, owner_digest, heartbeat, in_flight, ...status} = run;
    return {...status, running: ["preparing", "running", "waiting", "stopping"].includes(run.status),
      selection: params ? {forms: params.forms, sites: params.sites, mode: params.mode,
        appeal_copy_mode: params.appeal_copy_mode, loop_count: params.loop_count, message: params.message} : run.selection,
      loop_count: params?.loop_count ?? run.loop_count, in_flight: in_flight || null};
  }
  async function persist(run) {
    await deps.storageSet("local", {[KEY]: {...publicStatus(run), scope_digest: run.owner_digest}});
  }
  async function scopeDigest(owner) {
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(owner));
    return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
  }
  async function log(run, message, extra = {}) {
    run.message = message;
    run.logs.push({message, time: new Date().toISOString(), ...extra});
    run.logs = run.logs.slice(-120);
    await persist(run);
  }
  async function accountScope() {
    const [local, sync] = await Promise.all([deps.storageGet("local", ["browserExtensionAuth"]), deps.storageGet("sync", ["consoleUrl"])]);
    const auth = local.browserExtensionAuth;
    if (!auth || (!auth.token && auth.mode !== "legacy")) throw failure("泽顺账号已退出登录，请重新登录后开始申诉", "auth_required");
    // Token refresh for the same account is harmless. If the server did not
    // expose a stable account ID, conservatively bind the original token.
    const userId = auth.user?.id ?? auth.user?.user_id ?? auth.user?.username;
    return JSON.stringify([sync.consoleUrl || "https://wuhanzeshun.com", auth.mode, userId || auth.token || auth.username]);
  }
  async function post(name, payload, run = null) {
    if (run && await accountScope() !== run.owner) throw failure("泽顺账号或控制台地址已改变，申诉已停止", "context_changed");
    return deps.apiRequest(API + name, {method: "POST", body: JSON.stringify(payload)}, {retryAuth: false});
  }
  function check(run) {
    if (run.stopped) throw failure("已停止当前店铺申诉", "stopped");
    if (run.site_deadline && Date.now() > run.site_deadline) throw failure("本站点超过执行时间预算", "deadline_exceeded");
  }
  async function pause(run, milliseconds) {
    const deadline = Date.now() + milliseconds;
    while (Date.now() < deadline) {
      check(run);
      await new Promise(resolve => setTimeout(resolve, Math.min(500, deadline - Date.now())));
    }
    check(run);
  }
  async function page(tabId, command, args = {}, frameId = 0, allFrames = false) {
    let target = allFrames ? {tabId, allFrames: true} : {tabId, frameIds: [frameId]};
    if (allFrames && chrome.webNavigation?.getAllFrames) {
      const frames = await chrome.webNavigation.getAllFrames({tabId});
      const frameIds = (frames || []).filter(frame => { try { mercadoUrl(frame.url); return true; } catch (_) { return false; } }).map(frame => frame.frameId);
      if (!frameIds.length) throw failure("当前页面没有可读取的美客多客服框架");
      target = {tabId, frameIds};
    }
    let timer;
    const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(failure("读取美客多页面超时，请检查当前店铺页面")), 15000); });
    let values;
    try { values = await Promise.race([chrome.scripting.executeScript({target, world: "MAIN", func: MercadoAppealPage.action, args: [command, args]}), timeout]); }
    finally { clearTimeout(timer); }
    const results = values.filter(value => value.result && !value.result.unsupported).map(value => ({frame_id: value.frameId, ...value.result}));
    if (allFrames) return results;
    if (!results.length) throw failure("当前页面不属于可执行申诉的 Mercado Libre 店铺环境");
    if (results[0].error) throw failure(results[0].error);
    return results[0];
  }
  async function readContext(tabId) {
    const tab = await chrome.tabs.get(Number(tabId));
    mercadoUrl(tab.url);
    const state = await page(tab.id, "context");
    if (state.logged_out) throw failure("当前美客多店铺未登录，请完成登录后重试", "logged_out");
    if (state.rate_limited) throw failure("美客多页面暂时限频，请稍后在当前店铺重试", "rate_limited");
    if (state.identity_conflict || !state.seller_id) throw failure("未能确认当前店铺登录账号，请打开美客多全球销售后台后重试", "identity_unknown");
    return {...state, tab_id: tab.id, window_id: tab.windowId, incognito: Boolean(tab.incognito)};
  }
  async function getAppealContext(tabId) {
    const browser = await readContext(tabId);
    const data = await post("context", {seller_id: browser.seller_id, nickname: browser.nickname, site_id: browser.site_id});
    return {...data, tab_id: browser.tab_id, page: browser};
  }
  async function assertBinding(run, targetSite = "") {
    check(run);
    if (await accountScope() !== run.owner) throw failure("泽顺账号或控制台地址已改变，申诉已停止", "context_changed");
    const state = await readContext(run.tab_id);
    if (state.window_id !== run.window_id || state.incognito !== run.incognito) throw failure("当前浏览器窗口环境已改变，申诉已停止", "context_changed");
    const allowed = (run.identity.allowed_seller_ids || [run.identity.root_seller_id, run.identity.seller_id,
      ...(run.identity.sites || []).map(site => site.seller_id)]).map(String);
    if (!allowed.includes(state.seller_id)) throw failure("当前登录店铺与申诉绑定店铺不一致，已停止", "seller_mismatch");
    if (run.source_tab_id && run.source_tab_id !== run.tab_id) {
      const source = await readContext(run.source_tab_id);
      if (source.window_id !== run.window_id || source.incognito !== run.incognito || !allowed.includes(source.seller_id)) throw failure("启动申诉的店铺窗口环境已改变，已停止", "seller_mismatch");
    }
    if (targetSite && (state.site_conflict || state.site_id !== siteId(targetSite))) throw failure("发送前校验发现当前站点已经改变，已停止", "site_mismatch");
    return state;
  }
  async function navigate(run, url) {
    check(run);
    await assertBinding(run);
    mercadoUrl(url);
    await chrome.tabs.update(run.tab_id, {url});
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      check(run);
      const tab = await chrome.tabs.get(run.tab_id);
      mercadoUrl(tab.url);
      if (tab.status === "complete") { await assertBinding(run); return; }
      await pause(run, 500);
    }
    throw failure("美客多页面打开超时");
  }
  async function selectSite(run, target) {
    const code = siteId(target);
    if (!(run.identity.sites || []).some(site => siteId(site.site_id) === code)) throw failure("目标站点未授权申诉", "site_mismatch");
    const current = await assertBinding(run);
    if (current.site_id === code && !current.site_conflict) return;
    for (let attempts = 0; attempts < 3; attempts++) {
      check(run);
      let selected;
      try { selected = await page(run.tab_id, "select_site", {site_id: code}); }
      catch (error) { if (!/站点切换器/.test(error.message)) throw error; break; }
      await pause(run, selected.opened ? 500 : 2000);
      const fresh = await assertBinding(run);
      if (fresh.site_id === code && !fresh.site_conflict) return;
    }
    // Mirror the agent's cbtSiteId fallback in this exact tab's cookie store.
    // The bound OAuth shop/site list is checked above; Full-only shop options
    // must never be converted to remote by writing a cookie.
    const state = await assertBinding(run);
    if (new URL(state.url).hostname !== "global-selling.mercadolibre.com" ||
        (state.available?.length && !state.available.includes(`${code}-remote`))) throw failure("当前店铺没有目标自发货站点，停止发送", "site_mismatch");
    if (!chrome.cookies?.getAllCookieStores) throw failure(`当前店铺未能确认切换到 ${code}，停止发送`, "site_mismatch");
    const stores = await chrome.cookies.getAllCookieStores();
    const store = stores.find(item => item.tabIds.includes(run.tab_id));
    if (!store) throw failure("未能确认当前窗口的 Cookie 环境，停止发送", "context_changed");
    const existing = await chrome.cookies.get({url: HOST, name: "cbtSiteId", storeId: store.id});
    const cookie = {url: HOST, name: "cbtSiteId", value: `${code}-remote`, path: "/", storeId: store.id, secure: true,
      sameSite: existing?.sameSite || "lax"};
    if (existing?.domain) cookie.domain = existing.domain;
    await chrome.cookies.set(cookie);
    await chrome.tabs.reload(run.tab_id);
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      await pause(run, 1000);
      const fresh = await assertBinding(run);
      if (fresh.site_id === code && !fresh.site_conflict) return;
    }
    throw failure(`当前店铺未能确认切换到 ${code}，停止发送`, "site_mismatch");
  }
  async function findChat(run, timeout = 45000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      await assertBinding(run, run.current_site);
      const frames = await page(run.tab_id, "probe", {mode: run.params.mode}, 0, true);
      const ready = frames.find(frame => frame.ready);
      if (ready) return ready;
      await pause(run, 1000);
    }
    throw failure("未找到当前店铺客服聊天输入框");
  }
  async function openChat(run) {
    if (run.params.mode === "人工客服") {
      // Keep every submitted human conversation available for the operator.
      await assertBinding(run);
      const tab = await chrome.tabs.create({windowId: run.window_id, url: `${HOST}/help/hub/30928?source`, active: true});
      run.tab_id = tab.id; (run.chat_tabs ||= []).push(tab.id);
      const deadline = Date.now() + 60000;
      while ((await chrome.tabs.get(tab.id)).status !== "complete" && Date.now() < deadline) await pause(run, 500);
      await assertBinding(run);
    } else await navigate(run, `${HOST}/help`);
    await selectSite(run, run.current_site);
    for (let attempts = 0; attempts < 3; attempts++) {
      await assertBinding(run, run.current_site);
      const frames = await page(run.tab_id, "probe", {mode: run.params.mode}, 0, true);
      const ready = frames.find(frame => frame.ready);
      if (ready) return ready;
      await page(run.tab_id, "open_chat", {mode: run.params.mode, allow_contact: attempts === 2});
      await pause(run, 2000);
    }
    try { return await findChat(run, 15000); }
    catch (error) {
      if (run.params.mode !== "人工客服" || ["stopped", "seller_mismatch", "context_changed", "logged_out"].includes(error.status)) throw error;
      await navigate(run, `${HOST}/help/chat/v2`);
      await selectSite(run, run.current_site);
      return findChat(run);
    }
  }
  function changedMessages(before, after, role) {
    if (!before || !after || before.epoch !== after.epoch || before.conversation_id !== after.conversation_id) throw failure("等待期间客服会话已更换", "sent_unknown", true);
    const old = new Map(before.messages.map(message => [`${message.role}:${message.id}`, norm(message.text)]));
    return after.messages.filter(message => message.role === role && old.get(`${role}:${message.id}`) !== norm(message.text));
  }
  function siteQuestion(text) {
    const lower = norm(text).toLowerCase().replace(/（/g, "(").replace(/）/g, ")");
    const options = [...Object.values(siteReplies), "Mexico (Fulfillment)"].map(value => value.toLowerCase());
    const compact = value => value.replace(/[\s。．.、,，:：;；]+/g, "");
    const matches = options.filter(option => compact(lower).includes(compact(option))).length;
    return matches >= 5 || (matches >= 3 && /可选|option|站点/.test(lower)) ||
      (options.some(option => lower.includes(option)) && /which (country|site|option)|(?:select|choose|confirm) (?:your |the |a )?(country|site)|qual pa[ií]s|qu[eé] pa[ií]s|qu[eé] sitio|哪个国家|哪个站点|针对哪个|哪个选项|请选择站点|请选择国家|请确认站点|请确认国家/.test(lower));
  }
  async function resetChat(run, chat) {
    await assertBinding(run, run.current_site);
    const reset = await page(run.tab_id, "new_conversation", {mode: run.params.mode}, chat.frame_id);
    if (!reset.clicked) throw failure("未能建立新的客服会话，停止下一组发送");
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      const fresh = await findChat(run, 5000);
      const a = reset.before, b = fresh.snapshot;
      if (a.epoch !== b.epoch || a.conversation_id !== b.conversation_id || (a.messages.length && !b.messages.length)) return fresh;
      await pause(run, 500);
    }
    throw failure("未能确认客服会话已重建，停止下一组发送");
  }
  async function waitReply(run, chat, baseline) {
    const deadline = Date.now() + 300000;
    let candidate = "", stableSince = 0, latest = baseline;
    while (Date.now() < deadline) {
      await assertBinding(run, run.current_site);
      const state = await page(run.tab_id, "snapshot", {mode: run.params.mode}, chat.frame_id);
      latest = state.snapshot;
      const responses = changedMessages(baseline, latest, "assistant");
      const response = responses.at(-1)?.text || "";
      if (response && !latest.busy) {
        if (candidate !== response) { candidate = response; stableSince = Date.now(); }
        else if (Date.now() - stableSince >= 2000) return {response, snapshot: latest};
      } else { candidate = ""; stableSince = 0; }
      await pause(run, 2000);
    }
    return {response: "", snapshot: latest};
  }
  async function send(run, chat, message, group, followup = false) {
    await assertBinding(run, run.current_site);
    const filled = await page(run.tab_id, "fill", {mode: run.params.mode, message}, chat.frame_id);
    if (!filled.filled) throw failure("客服输入框未接收完整申诉话术");
    await assertBinding(run, run.current_site);
    run.in_flight = {group_id: group.group_id, phase: "sending", followup, time: new Date().toISOString()};
    await persist(run);
    try { await assertBinding(run, run.current_site); }
    catch (error) {
      // The click command has not been dispatched yet, so this marker can be
      // safely cleared when a stop/login change arrives during persistence.
      run.in_flight = null; await persist(run); throw error;
    }
    let acknowledgement;
    try { acknowledgement = await page(run.tab_id, "send", {mode: run.params.mode, message}, chat.frame_id); }
    catch (error) { throw failure(`发送结果不确定，停止避免重复发送：${error.message}`, "sent_unknown", true); }
    if (!acknowledgement.clicked) { run.in_flight = null; await persist(run); throw failure(acknowledgement.error || "没有找到客服发送按钮"); }
    run.in_flight = {...run.in_flight, phase: "sent"};
    await persist(run);
    return acknowledgement;
  }
  async function executeGroup(run, group, chat) {
    const records = [];
    const execution = {status: "pre_send_failed", execution_status: "pre_send_failed", sent: false, acknowledged: false, retryable: false};
    const add = (event, message = "", response = "", snapshot = null, extra = {}) => records.push({event, message, response, chat: snapshot, extra: {group_index: group.group_index, total_groups: group.total_groups, appeal_kind: group.form, ...extra}});
    let message = group.message, replies = 0;
    try {
      if (group.needs_copy) message = (await post("copy", {task_id: run.task_id, group_id: group.group_id}, run)).message;
      if (!norm(message)) throw failure("未生成有效的申诉话术");
      const initial = message;
      while (true) {
        let ack;
        for (let attempt = 1; attempt <= 2; attempt++) {
          try { ack = await send(run, chat, message, group, replies > 0); break; }
          catch (error) {
            // Only failures proven to precede submission may retry. An unknown
            // transport response after clicking retains its in-flight marker.
            add("send_appeal_error", message, "", null, {attempt, error: error.message});
            if (error.sent || run.in_flight || attempt === 2 || !["pre_send_failed", "failed"].includes(error.status || "failed")) throw error;
            await log(run, `发送前暂时失败，正在重试当前组：${error.message}`);
            await pause(run, 5000);
            chat = await findChat(run, 15000);
          }
        }
        if (!replies) {
          Object.assign(execution, {status: "sent", execution_status: "sent", message: "发送成功", sent: true, acknowledged: true, conversation_id: ack.snapshot.conversation_id, send_method: "button_click"});
          run.sent++;
          await log(run, `${group.site_name || group.site_id} ${group.form}：发送成功，等待客服回复`);
        }
        add(replies ? "send_followup" : "send_appeal", message, "", ack.snapshot, {acknowledged: true});
        if (run.params.mode === "人工客服") {
          execution.reply_status = "needs_human"; execution.needs_human = true;
          add("human_chat_preserved", initial, "", ack.snapshot, {tab_id: run.tab_id});
          break;
        }
        const reply = await waitReply(run, chat, ack.snapshot);
        add(replies ? "agent_reply_after_site_option" : "agent_reply", initial, reply.response, reply.snapshot);
        if (!reply.response) { execution.reply_status = "reply_timeout"; execution.reply_timed_out = true; break; }
        Object.assign(execution, {response: reply.response, reply_received: true, reply_status: "replied"});
        if (!siteQuestion(reply.response)) break;
        if (replies >= 2) { execution.reply_status = "needs_human"; execution.needs_human = true; break; }
        message = siteReplies[run.current_site];
        if (!message) throw failure("无法确认客服所需站点", "needs_human");
        replies++;
      }
    } catch (error) {
      if (execution.acknowledged) Object.assign(execution, {post_send_status: error.status || "failed", post_send_error: error.message});
      else Object.assign(execution, {status: executionStatus(error.status || "failed"), execution_status: executionStatus(error.status || "failed"), source_status: error.status || "failed", message: error.message, sent: Boolean(error.sent), error: error.message});
    }
    add("group_result", message, execution.response || "", null, {result: execution});
    // Result persistence is also bounded to the original console account. A
    // failed POST leaves its payload locally for inspection, never a resend.
    run.pending_result = {task_id: run.task_id, group_id: group.group_id, log_records: records, execution, error: execution.error || ""};
    await persist(run);
    await post("result", run.pending_result, run);
    run.pending_result = null; run.in_flight = null;
    run.processed++; if (!execution.acknowledged) run.failed++;
    await log(run, `${group.site_name || group.site_id} ${group.form} 第 ${group.group_index}/${group.total_groups} 组：${execution.message || execution.status}`);
    run.reset_before_group = execution.reply_status === "reply_timeout" || execution.reply_status === "needs_human" || Boolean(execution.post_send_status);
    if (!execution.acknowledged) throw failure(execution.error || execution.message || "本组发送未确认", execution.status, execution.sent);
    check(run);
  }
  function parseCsv(text) {
    const rows = []; let row = [], field = "", quoted = false;
    for (let i = 0; i < text.length; i++) {
      const char = text[i];
      if (char === '"') { if (quoted && text[i + 1] === '"') { field += '"'; i++; } else quoted = !quoted; }
      else if (char === "," && !quoted) { row.push(field); field = ""; }
      else if ((char === "\n" || char === "\r") && !quoted) { if (char === "\r" && text[i + 1] === "\n") i++; row.push(field); if (row.some(Boolean)) rows.push(row); row = []; field = ""; }
      else field += char;
    }
    row.push(field); if (row.some(Boolean)) rows.push(row);
    if (quoted) throw failure("延误 CSV 报表不完整");
    return rows;
  }
  function delayIdentifiers(csv, now = Date.now()) {
    const rows = parseCsv(String(csv || "").replace(/^\uFEFF/, ""));
    if (rows.length < 2) throw failure("最新延误 CSV 报表为空或格式不完整");
    const identifiers = new Set(), cutoff = now - 15 * 24 * 60 * 60 * 1000;
    for (const row of rows.slice(1)) {
      if (row.length < 7) throw failure("延误报表列结构已改变，停止以免申诉错误订单");
      const order = norm(row[1]).replace(/^'/, ""), dispatched = norm(row[6]);
      if (!/^\d{10,20}$/.test(order)) continue;
      // Agent CSV dates are e.g. "Wednesday 07 Oct 2026 - 08:30";
      // the agent deliberately uses the date before the trailing time segment.
      const rawDate = norm(row[0]);
      const date = Date.parse(/^[A-Za-z]+\s+\d{1,2}\s+[A-Za-z]+\s+\d{4}\s*-/.test(rawDate) ? rawDate.split("-")[0].trim() : rawDate);
      if (!Number.isFinite(date)) throw failure(`无法解析延误订单日期：${row[0]}`);
      if (date > cutoff && date <= now && dispatched && !/not yet dispatched/i.test(dispatched)) identifiers.add(order);
    }
    return [...identifiers];
  }
  async function collect(run, requirements) {
    if (run.params.mode === "人工客服" && run.source_tab_id) run.tab_id = run.source_tab_id;
    const result = {};
    for (const request of requirements) {
      check(run);
      const code = siteId(request.site_id);
      run.current_site = code; run.site_deadline = Date.now() + 7200000;
      await log(run, `${code}：正在读取${request.form}订单`);
      await navigate(run, `${HOST}/reputation`);
      await selectSite(run, code);
      let link;
      const cardDeadline = Date.now() + 30000;
      while (true) {
        try { link = await page(run.tab_id, "metric_link", {form: request.form}); break; }
        catch (error) {
          if (/指标卡片/.test(error.message) && Date.now() < cardDeadline) { await pause(run, 1000); continue; }
          if (request.form !== "延误") throw error;
          link = {error: error.message}; break;
        }
      }
      const ids = [];
      if (!link.empty && request.form === "延误") {
        let csv;
        if (link.url) {
          try { csv = (await page(run.tab_id, "csv_report", {url: link.url})).csv; } catch (_) {}
        }
        if (!csv) {
          await assertBinding(run, code);
          const requested = await page(run.tab_id, "request_delay_report", {form: "延误"});
          if (!requested.clicked) throw failure("当前店铺延误报表未就绪");
          if (!globalThis.ZeshunAppealMail?.readReport) throw failure("延误报表需从当前店铺登录邮箱读取，请更新插件后重试");
          const site = run.identity.sites.find(item => siteId(item.site_id) === code);
          const report = await ZeshunAppealMail.readReport({tabId: run.tab_id, siteId: code,
            sellerId: site?.seller_id || run.identity.seller_id, allowedSellerIds: run.identity.allowed_seller_ids,
            requestedAt: requested.requested_at, identityGuard: () => assertBinding(run, code),
            isStopped: () => run.stopped, onProgress: message => log(run, message)});
          csv = report.csv;
        }
        ids.push(...delayIdentifiers(csv));
      } else if (!link.empty) {
        if (link.url) {
          if (!/\/metrics(?:[/?#]|$)/.test(new URL(link.url).pathname)) throw failure("未找到当前指标的 Metrics 订单页面");
          await navigate(run, link.url);
        } else if (link.clicked) {
          const deadline = Date.now() + 30000;
          while (Date.now() < deadline) {
            check(run);
            const tab = await chrome.tabs.get(run.tab_id);
            const url = mercadoUrl(tab.url);
            if (tab.status === "complete" && /\/metrics(?:\/|$)/.test(url.pathname)) break;
            await pause(run, 500);
          }
          if (!/\/metrics(?:\/|$)/.test(mercadoUrl((await chrome.tabs.get(run.tab_id)).url).pathname)) throw failure("Metrics 入口未打开当前指标订单页面");
        } else throw failure("未找到当前指标的 Metrics 订单页面");
        await assertBinding(run, code);
        let previous = "", stagnant = 0;
        const seen = new Set();
        for (let pageIndex = 0; pageIndex < 100; pageIndex++) {
          await assertBinding(run, code);
          const state = await page(run.tab_id, "orders");
          state.ids.forEach(id => seen.add(id));
          stagnant = state.fingerprint === previous ? stagnant + 1 : 0; previous = state.fingerprint;
          if (stagnant >= 2) break;
          if (!(await page(run.tab_id, "next_orders")).advanced) break;
          await pause(run, 2000);
        }
        ids.push(...seen);
      }
      (result[code] ||= {})[request.form] = ids;
      await log(run, `${code} ${request.form}：读取 ${ids.length} 个可申诉订单`);
    }
    return result;
  }
  async function runTask(run, initialPlan) {
    let terminal = "completed", problem = "";
    try {
      let plan = initialPlan;
      const loopValue = run.params.loop_count;
      const rounds = loopValue === 0 || loopValue === "0" || loopValue === "永久" || loopValue === "permanent" ? Infinity : Number(loopValue || 1);
      for (let round = 1; round <= rounds; round++) {
        check(run); run.round_number = round; run.status = "running";
        if (round > 1) plan = await post("plan", {task_id: run.task_id, round_number: round}, run);
        if (plan.browser_collections?.length) {
          const reputation = await collect(run, plan.browser_collections);
          plan = await post("plan", {task_id: run.task_id, round_number: round, reputation_identifiers: reputation}, run);
        }
        await log(run, `开始第 ${round} 轮，共 ${plan.groups.length} 组申诉`);
        let chat = null, siteGroup = 0, previousSite = "", previousForm = "";
        for (const group of plan.groups) {
          check(run);
          run.current_group = {group_id: group.group_id, site_id: group.site_id, form: group.form, group_index: group.group_index, total_groups: group.total_groups};
          run.current_site = siteId(group.site_id);
          const changedSite = previousSite !== run.current_site;
          const changedForm = previousForm !== group.form;
          if (changedSite || (run.params.mode === "人工客服" && changedForm)) {
            run.site_deadline = Date.now() + 7200000; previousSite = run.current_site; siteGroup = 0;
            chat = await openChat(run); run.reset_before_group = false;
          } else if (changedForm) {
            try { chat = await resetChat(run, chat); }
            catch (error) {
              if (error.status !== "pre_send_failed" || !/新的客服会话|会话已重建/.test(error.message)) throw error;
              // The regular agent opens Help separately for each appeal type.
              // Reopening that entry is safe before the next type's first send.
              chat = await openChat(run);
            }
            siteGroup = 0; run.reset_before_group = false;
          }
          previousForm = group.form;
          siteGroup++;
          if (run.reset_before_group || (siteGroup > 1 && (siteGroup - 1) % 3 === 0)) chat = await resetChat(run, chat);
          await assertBinding(run, run.current_site);
          await executeGroup(run, group, chat);
          if (group.group_index < group.total_groups) await pause(run, 20000);
        }
        run.site_deadline = null;
        if (round < rounds) {
          run.status = "waiting";
          const seconds = Number(plan.round_interval_seconds || 300);
          await log(run, `第 ${round} 轮完成，${seconds} 秒后继续当前店铺`);
          await pause(run, seconds * 1000);
        }
      }
    } catch (error) { terminal = error.status === "stopped" || run.stopped ? "stopped" : "failed"; problem = error.message; }
    finally {
      clearInterval(run.heartbeat);
      if (run.task_id) {
        try { await post("finish", {task_id: run.task_id, status: terminal, error: problem}, run); }
        catch (error) { if (!problem) problem = `申诉记录保存失败：${error.message}`; if (terminal === "completed") terminal = "failed"; }
      }
      run.status = terminal; run.error = problem; run.finished_at = new Date().toISOString();
      await log(run, problem || (terminal === "completed" ? "当前店铺自动化申诉已完成" : "当前店铺自动化申诉已停止"));
    }
  }
  async function startAppealRun({tabId, params = {}} = {}) {
    if (starting || (active && publicStatus(active).running)) throw failure("当前店铺申诉正在执行，请先停止后再开始");
    starting = true;
    try {
      const owner = await accountScope();
      const tab = await chrome.tabs.get(Number(tabId));
      mercadoUrl(tab.pendingUrl || tab.url);
      // The originating page may not expose shop identity (for example Orders).
      // Open Reputation first; no popup identity preflight is required.
      await chrome.tabs.update(tab.id, {url: `${HOST}/reputation`});
      const deadline = Date.now() + 60000;
      while (true) {
        const current = await chrome.tabs.get(tab.id);
        mercadoUrl(current.url);
        if (current.status === "complete") break;
        if (Date.now() >= deadline) throw failure("美客多声誉页面打开超时");
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      const browser = await readContext(tab.id);
      const plan = await post("plan", {...params, context: {seller_id: browser.seller_id}});
      if (await accountScope() !== owner) throw failure("申诉准备期间泽顺账号已改变，请重新开始申诉", "context_changed");
      const run = {run_id: crypto.randomUUID(), task_id: plan.task_id, status: "preparing", params: plan.params || params,
        identity: plan.identity, shop: plan.shop, sites: plan.sites, tab_id: browser.tab_id, source_tab_id: browser.tab_id, window_id: browser.window_id,
        incognito: browser.incognito, owner, owner_digest: await scopeDigest(owner), stopped: false, processed: 0, sent: 0, failed: 0,
        round_number: 1, logs: [], error: "", started_at: new Date().toISOString(), in_flight: null, pending_result: null};
      active = run;
      await persist(run);
      // Extension API traffic keeps the MV3 worker alive during bounded waits.
      // On an actual worker/browser restart getStatus reports interruption;
      // neither plans nor pending clicks are automatically replayed.
      run.heartbeat = setInterval(() => { void deps.storageGet("local", [KEY]).catch(() => {}); }, 20000);
      void runTask(run, plan);
      return publicStatus(run);
    } finally { starting = false; }
  }
  async function getAppealStatus() {
    let owner;
    try { owner = await accountScope(); } catch (_) { if (active) active.stopped = true; return publicStatus(null); }
    if (active) {
      if (active.owner !== owner) { active.stopped = true; return publicStatus(null); }
      return publicStatus(active);
    }
    const saved = (await deps.storageGet("local", [KEY]))[KEY];
    if (!saved) return publicStatus(null);
    if (!saved.scope_digest || saved.scope_digest !== await scopeDigest(owner)) return publicStatus(null);
    if (saved.running) {
      const interrupted = {...saved, status: "interrupted", running: false, finished_at: new Date().toISOString(),
        error: "浏览器或插件后台已重启，任务已停止；已发送或发送结果不确定的组不会自动重发", message: "任务已中断，请检查发送记录后重新开始"};
      await deps.storageSet("local", {[KEY]: interrupted});
      const {scope_digest, ...status} = interrupted;
      return status;
    }
    const {scope_digest, ...status} = saved;
    return status;
  }
  async function stopAppealRun() {
    if (!active || !publicStatus(active).running) return getAppealStatus();
    active.stopped = true; active.status = "stopping";
    await log(active, "已收到停止请求，正在保存当前组发送记录");
    return publicStatus(active);
  }
  return {configure: values => { deps = values; }, getAppealContext, startAppealRun, getAppealStatus, stopAppealRun,
    // Pure protocol helpers also make replay and report filtering testable.
    normalizeSite: siteId, siteQuestion, changedMessages, parseCsv, delayIdentifiers, mercadoUrl, executionStatus};
})();
