"use strict";

// Runs in the extension service worker. Mail is read only in this browser's
// existing Outlook session; neither mailbox credentials nor message bodies are
// sent to the console.
(() => {
  const SUBJECT = "Your orders that you shipped with delay report is ready";
  const MAIL_HOSTS = new Set(["outlook.live.com", "outlook.office.com", "outlook.office365.com"]);
  const REPORT_HOST = "global-selling.mercadolibre.com";
  const SITE_CODES = {MLM: "MX", MLB: "BR", MLC: "CL", MCO: "CO", MLA: "AR", MLU: "UY"};
  const SITE_WORDS = {MLM: "Mexico|México|墨西哥", MLB: "Brazil|Brasil|巴西",
    MLC: "Chile|智利", MCO: "Colombia|哥伦比亚", MLA: "Argentina|阿根廷", MLU: "Uruguay|乌拉圭"};
  const MAX_CSV_BYTES = 8 * 1024 * 1024;

  function error(message, status = "pre_send_failed") {
    return Object.assign(new Error(message), {status});
  }

  function parseTimestamp(value) {
    const text = String(value || "").replace(/[\u200e\u200f\u202a-\u202e]/g, "").trim();
    if (!text) return 0;
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}.*(?:Z|[+-]\d{2}:?\d{2})$/.test(text)) {
      const parsed = Date.parse(text);
      return Number.isFinite(parsed) ? parsed : 0;
    }
    // Outlook's Chinese title contains a weekday before the absolute date.
    const match = text.match(/(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*(上午|下午|AM|PM)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?/i);
    if (match) {
      let hour = Number(match[5]);
      const period = match[4] || match[8] || "";
      if (/下午|PM/i.test(period) && hour < 12) hour += 12;
      if (/上午|AM/i.test(period) && hour === 12) hour = 0;
      const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]), hour, Number(match[6]), Number(match[7] || 0));
      if (date.getFullYear() !== Number(match[1]) || date.getMonth() !== Number(match[2]) - 1 || date.getDate() !== Number(match[3]) || hour > 23 || Number(match[6]) > 59) return 0;
      return date.getTime();
    }
    // Do not infer a date from relative list labels such as "Today" or "10:05".
    if (!/\d{4}/.test(text)) return 0;
    const parsed = Date.parse(text);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function trustedReportUrl(value) {
    try {
      let url = new URL(String(value || ""));
      if (url.hostname.endsWith(".safelinks.protection.outlook.com")) {
        url = new URL(url.searchParams.get("url") || "");
      }
      if (url.protocol !== "https:" || url.hostname !== REPORT_HOST || url.username || url.password || (url.port && url.port !== "443")) return "";
      return url.href;
    } catch (_) { return ""; }
  }

  function freshMessages(rows, requestedAt, now) {
    return (rows || []).map(row => {
      const timestamp = row.timestamp || parseTimestamp(row.date);
      const minutePrecision = !row.timestamp && /\d{1,2}:\d{2}/.test(row.date || "") && !/\d{1,2}:\d{2}:\d{2}/.test(row.date || "");
      // A minute label can describe a message received after the request in the
      // same minute. Such a candidate needs the report's own creation timestamp
      // before it can be used; older messages never become eligible by rounding.
      return {...row, timestamp, needsTimeProof: minutePrecision && timestamp < requestedAt,
        eligibleAfter: minutePrecision ? Math.floor(requestedAt / 60000) * 60000 : requestedAt};
    })
      .filter(row => row.subject === SUBJECT && row.timestamp >= row.eligibleAfter && row.timestamp <= now && now - row.timestamp <= 3600000)
      .sort((a, b) => b.timestamp - a.timestamp);
  }

  function reportTimestamp(evidence) {
    let decoded = String(evidence || "");
    try { decoded = decodeURIComponent(decoded); } catch (_) {}
    // This timestamp is part of the platform's official delay-report filename
    // (the agent reads the same field when selecting its latest downloaded file).
    const file = /Report_of_orders_that_you_shipped_with_delayed_handling_time[^\r\n";]*/i.exec(decoded)?.[0] || "";
    const times = [...file.matchAll(/_([12]\d{12})(?=\.csv|[_\s)]|$)/g)].map(match => Number(match[1]));
    return times.length ? Math.max(...times) : 0;
  }

  function contextEvidence(text, siteId, sellerIds) {
    let decoded = String(text || "");
    try { decoded = decodeURIComponent(decoded); } catch (_) {}
    const site = String(siteId || "").toUpperCase();
    const sites = new Set();
    for (const [id, code] of Object.entries(SITE_CODES)) {
      const country = new RegExp(`(?:^|[^a-z])(?:${SITE_WORDS[id]})(?:$|[^a-z])`, "i");
      const codePattern = new RegExp(`(?:^|[^a-z0-9])(?:${id}|${code})(?:$|[^a-z0-9])`, "i");
      if (country.test(decoded) || codePattern.test(decoded)) sites.add(id);
    }
    // Explicit seller identity, when present, must belong to the bound store.
    const ids = [...decoded.matchAll(/(?:seller(?:_id)?|user_?id|merchant_?id|meli_user_id)["'\s:=/?&]+(\d+)/gi)].map(match => match[1]);
    return {siteConfirmed: sites.size === 1 && sites.has(site),
      mismatch: [...sites].some(id => id !== site) || ids.some(id => !(sellerIds || []).includes(id))};
  }

  // Self-contained function, serialized by chrome.scripting.executeScript.
  function mailboxPage(action, argument) {
    const subject = "Your orders that you shipped with delay report is ready";
    const visible = node => !!node?.getClientRects().length;
    const nodes = [...document.querySelectorAll("[data-item-index], [role='option']")];
    const readRow = node => {
      const title = node.querySelector("span.TtcXM, [data-testid='message-subject'], [data-test-id='message-subject']");
      const subjectText = (title?.getAttribute("title") || title?.textContent || "").trim();
      // Old Outlook uses TtcXM; newer builds can change classes. A fallback
      // still requires one complete subject node, never a substring of Re:/Fwd:.
      const exactSubject = !title && [...node.querySelectorAll("span,[title]")].some(item =>
        (item.getAttribute("title") || item.textContent || "").trim() === subject);
      if (subjectText !== subject && !exactSubject) return null;
      const time = node.querySelector("span._rWRU, time, [data-testid='message-time'], [data-test-id='message-time']");
      const date = time?.getAttribute("datetime") || time?.getAttribute("title") || "";
      return {subject, date, key: node.getAttribute("data-item-index") || node.getAttribute("data-item-id") || node.id || "",
        text: (node.innerText || node.textContent || "").slice(0, 2000)};
    };
    if (action === "folder") {
      const label = argument === "junk" ? /^(?:垃圾邮件|垃圾郵件|Junk Email|Junk|Correo no deseado|Lixo eletrônico)(?:\s|$)/i : /^(?:收件箱|收件匣|Inbox|Bandeja de entrada|Caixa de Entrada)(?:\s|$)/i;
      const candidates = [...document.querySelectorAll("[role='treeitem'], [role='navigation'] [title], div[title], [data-folder-name]")];
      const node = candidates.find(item => visible(item) && label.test((item.getAttribute("title") || item.getAttribute("aria-label") || item.textContent || "").trim()));
      if (node) node.click();
      return {clicked: !!node};
    }
    if (action === "open") {
      const node = nodes.find(item => readRow(item)?.key === argument);
      if (node) node.click();
      return {clicked: !!node};
    }
    if (action === "body") {
      const host = document.querySelector(".wide-content-host, [data-testid='message-body'], [data-test-id='message-body']");
      const links = [...(host || document).querySelectorAll("a[href]")].filter(node => /Go to download report/i.test(node.textContent || ""));
      return {links: links.map(node => node.href), text: (host?.innerText || host?.textContent || "").slice(0, 12000)};
    }
    return {url: location.href, ready: !!document.querySelector("[data-item-index], [role='main'], [role='tree']"),
      rows: nodes.filter(visible).map(readRow).filter(Boolean)};
  }

  // Capture only a report tab owned by this task. Native download links and
  // blob responses are intercepted so reading a report does not write a file.
  function reportPage(action) {
    const key = "__zeshunAppealReportCapture";
    if (!window[key]) {
      const state = window[key] = {csv: "", urls: [], filename: "", clicked: false};
      const recordUrl = value => { if (value && !state.urls.includes(String(value))) state.urls.push(String(value)); };
      const captureBlob = blob => {
        if (blob?.size <= 8 * 1024 * 1024 && typeof blob.text === "function") blob.text().then(text => {
          if (/[,;\t]/.test(text.split(/\r?\n/)[0] || "") && !/^\s*<!?html/i.test(text)) state.csv = text;
        }).catch(() => {});
      };
      const originalObjectURL = URL.createObjectURL;
      URL.createObjectURL = function(blob) { captureBlob(blob); return originalObjectURL.call(this, blob); };
      const originalClick = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function() {
        if (this.download || /\.csv(?:[?#]|$)|download|report/i.test(this.href || "")) {
          state.filename = this.download || state.filename;
          recordUrl(this.href);
          return;
        }
        return originalClick.call(this);
      };
      document.addEventListener("click", event => {
        const link = event.target.closest?.("a[href]");
        if (link && (link.download || /\.csv(?:[?#]|$)/i.test(link.href))) {
          state.filename = link.download || state.filename; recordUrl(link.href);
          event.preventDefault(); event.stopImmediatePropagation();
        }
      }, true);
      window.open = function(url) { recordUrl(url); return null; };
      const originalFetch = window.fetch;
      window.fetch = async function(...args) {
        const response = await originalFetch.apply(this, args);
        const type = response.headers.get("content-type") || "";
        if (/csv|octet-stream/i.test(type) || /\.csv(?:[?#]|$)/i.test(response.url)) {
          recordUrl(response.url);
          state.filename = response.headers.get("content-disposition") || state.filename;
          captureBlob(await response.clone().blob());
        }
        return response;
      };
    }
    const state = window[key];
    if (action === "click" && !state.clicked) {
      const candidates = [...document.querySelectorAll("button,a,[role='button']")];
      const target = candidates.find(node => node.getClientRects().length && /^(?:Download|Download report|Descargar|Baixar|下载)$/i.test((node.innerText || node.textContent || "").trim()) && !node.disabled);
      if (target) { state.clicked = true; target.click(); }
    }
    const urls = [...document.querySelectorAll("a[href]")].filter(node => node.download || /\.csv(?:[?#]|$)|download.*report|report.*download/i.test(node.href)).map(node => node.href);
    return {csv: state.csv, urls: [...new Set([...state.urls, ...urls])], filename: state.filename, clicked: state.clicked,
      text: (document.body?.innerText || "").slice(0, 12000), url: location.href};
  }

  async function readReport(options) {
    const {tabId, siteId, requestedAt, identityGuard, onProgress} = options || {};
    const site = String(siteId || "").toUpperCase();
    const requested = Number(requestedAt);
    if (!Number.isInteger(tabId) || !SITE_CODES[site] || !(requested > 0) || typeof identityGuard !== "function") throw error("延误报表缺少当前店铺身份或请求时间");
    const sellerIds = [...new Set([options.sellerId, ...(options.allowedSellerIds || [])].map(value => String(value || "")).filter(Boolean))];
    const now = options.now || Date.now;
    const stop = () => { if (options.isStopped?.()) throw error("已停止延误报表读取", "stopped"); };
    const bounded = promise => new Promise((resolve, reject) => {
      let finished = false;
      const finish = (callback, value) => {
        if (finished) return;
        finished = true; clearTimeout(deadline); clearInterval(stopped); callback(value);
      };
      const deadline = setTimeout(() => finish(reject, error("延误报表页面读取超时")), 15000);
      const stopped = setInterval(() => {
        try { stop(); } catch (exception) { finish(reject, exception); }
      }, 250);
      Promise.resolve(promise).then(value => finish(resolve, value), exception => finish(reject, exception));
    });
    const check = async () => {
      stop();
      const binding = await bounded(identityGuard());
      if (!binding || (binding !== true && typeof binding !== "object")) throw error("无法确认当前店铺身份，停止读取延误报表", "login_required");
      stop();
    };
    const pause = async ms => {
      for (let elapsed = 0; elapsed < ms; elapsed += 250) {
        stop(); await (options.sleep || (duration => new Promise(resolve => setTimeout(resolve, duration))))(Math.min(250, ms - elapsed));
      }
      stop();
    };
    const execute = async (id, func, args = []) => {
      stop();
      const result = await bounded(chrome.scripting.executeScript({target: {tabId: id}, world: "MAIN", func, args}));
      stop();
      if (result?.[0]?.result == null) throw error("延误报表页面未返回可读取的状态");
      return result[0].result;
    };
    const ownedTabs = [];
    let debugTarget = null, debugListener = null;
    const network = {csv: "", evidence: ""};
    try {
      await check();
      const source = await bounded(chrome.tabs.get(tabId));
      const tabs = await bounded(chrome.tabs.query({windowId: source.windowId}));
      const existing = tabs.find(tab => {
        try { return MAIL_HOSTS.has(new URL(tab.url || "").hostname) && /\/mail\//.test(new URL(tab.url).pathname); } catch (_) { return false; }
      });
      const mail = await bounded(chrome.tabs.create({windowId: source.windowId, url: existing?.url || "https://outlook.live.com/mail/0/", active: false}));
      ownedTabs.push(mail.id);
      const readyAt = now() + 25000;
      let mailReady = false;
      while (now() < readyAt) {
        stop();
        const tab = await bounded(chrome.tabs.get(mail.id));
        let url;
        try { url = new URL(tab.url || ""); } catch (_) {}
        if (url && !MAIL_HOSTS.has(url.hostname) && /login|signin|microsoftonline/i.test(url.hostname)) throw error("当前浏览器的 Outlook 未登录，请登录邮箱后重试", "needs_human");
        try { if ((await execute(mail.id, mailboxPage, ["scan"]))?.ready) { mailReady = true; break; } } catch (_) { stop(); }
        await pause(500);
      }
      if (!mailReady) throw error("无法读取当前浏览器的 Outlook 邮箱，请确认已登录", "needs_human");

      const seen = new Set();
      let selected = null;
      const polls = Math.min(3, Math.max(1, Number(options.maxPolls) || 3));
      for (let attempt = 0; attempt < polls && !selected; attempt++) {
        onProgress?.(`等待当前店铺 ${site} 延误报表邮件（${attempt + 1}/${polls}）`);
        await pause(Math.max(0, options.pollIntervalMs ?? 60000));
        await check();
        for (const folder of ["inbox", "junk"]) {
          const changed = await execute(mail.id, mailboxPage, ["folder", folder]);
          if (!changed?.clicked) continue;
          await pause(1000);
          const snapshot = await execute(mail.id, mailboxPage, ["scan"]);
          for (const message of freshMessages(snapshot?.rows, requested, now())) {
            const key = `${folder}:${message.key}:${message.timestamp}`;
            if (seen.has(key)) continue;
            seen.add(key);
            const previousBody = await execute(mail.id, mailboxPage, ["body"]);
            if (!(await execute(mail.id, mailboxPage, ["open", message.key]))?.clicked) continue;
            let body;
            for (let wait = 0; wait < 10; wait++) {
              await pause(500);
              body = await execute(mail.id, mailboxPage, ["body"]);
              if (body?.links?.length && (body.text !== previousBody?.text || JSON.stringify(body.links) !== JSON.stringify(previousBody?.links))) break;
              body = null;
            }
            for (const value of body?.links || []) {
              const reportUrl = trustedReportUrl(value);
              if (!reportUrl) continue;
              const evidence = contextEvidence(`${message.text}\n${body.text}\n${reportUrl}`, site, sellerIds);
              if (evidence.mismatch) continue;
              selected = {url: reportUrl, evidence: `${message.text}\n${body.text}\n${reportUrl}`, siteConfirmed: evidence.siteConfirmed,
                needsTimeProof: message.needsTimeProof};
              break;
            }
            if (selected) break;
          }
          if (selected) break;
        }
      }
      if (!selected) throw error("未收到本次当前店铺站点的延误报表邮件，已停止本类型申诉");
      await check();
      const report = await bounded(chrome.tabs.create({windowId: source.windowId, url: selected.url, active: false}));
      ownedTabs.push(report.id);
      await pause(1500);

      // Response-stage interception reads the CSV and cancels the native file
      // download. HTML/JS responses proceed normally in this temporary tab.
      try {
        if (!chrome.debugger?.attach) throw new Error("debugger unavailable");
        debugTarget = {tabId: report.id};
        await bounded(chrome.debugger.attach(debugTarget, "1.3"));
        debugListener = (sourceTarget, method, params) => {
          if (sourceTarget.tabId !== report.id || method !== "Fetch.requestPaused") return;
          void (async () => {
            const headers = params.responseHeaders || [];
            const headerText = headers.map(header => `${header.name}:${header.value}`).join("\n");
            const url = params.request?.url || "";
            const downloadable = /(?:content-type:.*(?:csv|octet-stream)|content-disposition:.*attachment|\.csv(?:[?#]|$))/i.test(`${headerText}\n${url}`);
            if (downloadable) {
              try {
                const data = await chrome.debugger.sendCommand(debugTarget, "Fetch.getResponseBody", {requestId: params.requestId});
                network.csv = data.base64Encoded ? new TextDecoder().decode(Uint8Array.from(atob(data.body), character => character.charCodeAt(0))) : data.body;
                network.evidence = `${headerText}\n${url}`;
              } finally {
                await chrome.debugger.sendCommand(debugTarget, "Fetch.failRequest", {requestId: params.requestId, errorReason: "Aborted"}).catch(() => {});
              }
            } else await chrome.debugger.sendCommand(debugTarget, "Fetch.continueRequest", {requestId: params.requestId});
          })().catch(() => {});
        };
        chrome.debugger.onEvent.addListener(debugListener);
        await bounded(chrome.debugger.sendCommand(debugTarget, "Fetch.enable", {patterns: [{urlPattern: "*", requestStage: "Response"}]}));
      } catch (_) {
        // Direct links and blob capture below still work if DevTools is open.
        if (debugListener) chrome.debugger.onEvent.removeListener(debugListener);
        debugListener = null;
        if (debugTarget) await chrome.debugger.detach(debugTarget).catch(() => {});
        debugTarget = null;
      }
      const deadline = now() + 30000;
      const triedUrls = new Set();
      while (now() < deadline) {
        await check();
        const snapshot = await execute(report.id, reportPage, ["click"]);
        let csv = snapshot?.csv || network.csv;
        let evidence = `${selected.evidence}\n${snapshot?.text || ""}\n${snapshot?.filename || ""}\n${network.evidence}`;
        if (!csv) {
          for (const rawUrl of snapshot?.urls || []) {
            const url = trustedReportUrl(rawUrl);
            if (!url || triedUrls.has(url)) continue;
            triedUrls.add(url);
            const response = await bounded(fetch(url, {credentials: "include", cache: "no-store", signal: AbortSignal.timeout(10000)}));
            if (!response.ok || !trustedReportUrl(response.url || url)) continue;
            const type = response.headers.get("content-type") || "";
            if (/text\/html|application\/json/i.test(type)) continue;
            const length = Number(response.headers.get("content-length") || 0);
            if (length > MAX_CSV_BYTES) throw error("延误报表超过读取大小上限");
            csv = await bounded(response.text());
            evidence += `\n${response.headers.get("content-disposition") || ""}\n${url}`;
            break;
          }
        }
        if (csv) {
          if (csv.length > MAX_CSV_BYTES || /^\s*(?:<!?html|\{)/i.test(csv) || !/[,;\t]/.test(csv.split(/\r?\n/)[0] || "")) throw error("下载结果不是可读取的延误 CSV 报表");
          const context = contextEvidence(evidence, site, sellerIds);
          if (context.mismatch || !(selected.siteConfirmed || context.siteConfirmed)) throw error("延误报表的店铺或站点无法匹配本次请求，已停止申诉");
          if (selected.needsTimeProof) {
            const generated = reportTimestamp(evidence);
            if (generated < requested || generated > now()) throw error("邮件时间只精确到分钟，无法确认报表由本次请求生成，已停止申诉");
          }
          await check();
          onProgress?.("已读取本次当前店铺站点的延误 CSV 报表");
          return {csv, report_url: selected.url};
        }
        await pause(500);
      }
      throw error("本次延误报表下载未返回 CSV，请检查报表页面后重试");
    } finally {
      if (debugListener) chrome.debugger.onEvent.removeListener(debugListener);
      if (debugTarget) {
        await chrome.debugger.sendCommand(debugTarget, "Fetch.disable").catch(() => {});
        await chrome.debugger.detach(debugTarget).catch(() => {});
      }
      for (const id of ownedTabs.reverse()) await chrome.tabs.remove(id).catch(() => {});
    }
  }

  const api = {readReport, parseTimestamp, trustedReportUrl, freshMessages, reportTimestamp, contextEvidence};
  globalThis.ZeshunAppealMail = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
