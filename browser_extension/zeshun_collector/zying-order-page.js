"use strict";

// Runs in the logged-in page's MAIN world. Credentials stay in the browser.
async function zeshunReadZyingOrderBatch(rows) {
  const clean = value => String(value ?? "").replace(/<[^>]*>/g, "").trim();
  const identifiers = value => new Set(clean(value).match(/[A-Za-z0-9_-]+/g) || []);
  const keys = row => new Set([row.id, row.order_number, row.pack_id].map(clean).filter(Boolean));
  const intersects = (left, right) => [...left].some(key => right.has(key));
  const itemKeys = item => new Set([...identifiers(item.no), ...identifiers(item.key)]);
  const timeout = 30000;
  const field = document.querySelector('input[placeholder="订单、采购单、运单,多个编号可以逗号、空格分隔"]');
  if (!field) return {login_required: /login/i.test(location.hash + location.pathname),
    error: "未找到智赢订单搜索框，请确认已登录且可以查看订单列表"};
  // A MAIN-world call may outlive its service worker. Serialize resumed reads
  // in the page as well, so two search observers never overlap.
  if (window.__zeshunOrderBatchUntil > Date.now()) return {busy: true};
  const lease = Date.now() + 10 * 60 * 1000;
  window.__zeshunOrderBatchUntil = lease;
  const outcomes = {};
  const responseData = payload => {
    if (payload?.code !== 200 || !payload.data || typeof payload.data !== "object") {
      const error = new Error("智赢请求未成功，请检查登录状态与订单查看权限");
      error.loginRequired = [401, 403].includes(Number(payload?.code));
      throw error;
    }
    return payload.data;
  };
  // Observe only the response caused by this exact search/detail request.
  // Both fetch and XHR are used by versions of the official SPA.
  const observe = (command, matches, trigger) => new Promise((resolve, reject) => {
    const originalFetch = window.fetch;
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    const metadata = new WeakMap();
    const listeners = [];
    let settled = false;
    let requested = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (window.fetch === wrappedFetch) window.fetch = originalFetch;
      if (XMLHttpRequest.prototype.open === wrappedOpen) XMLHttpRequest.prototype.open = originalOpen;
      if (XMLHttpRequest.prototype.send === wrappedSend) XMLHttpRequest.prototype.send = originalSend;
      for (const [xhr, listener] of listeners) xhr.removeEventListener("load", listener);
      error ? reject(error) : resolve(value);
    };
    const wanted = (url, body) => {
      try { return new URL(String(url), location.href).searchParams.get("cmd") === command &&
        matches(typeof body === "string" ? JSON.parse(body) : body); } catch (_) { return false; }
    };
    const accept = payload => {
      try { finish(null, responseData(payload)); } catch (error) { finish(error); }
    };
    async function wrappedFetch(input, config) {
      let body = config?.body;
      if (body === undefined && input instanceof Request) {
        try { body = await input.clone().text(); } catch (_) {}
      }
      const match = wanted(input?.url || input, body);
      if (match) requested = true;
      const response = await originalFetch.call(this, input, config);
      if (match) response.clone().json().then(accept, error => finish(error));
      return response;
    }
    function wrappedOpen(method, url, ...args) {
      metadata.set(this, url);
      return originalOpen.call(this, method, url, ...args);
    }
    function wrappedSend(body) {
      if (wanted(metadata.get(this), body)) {
        requested = true;
        const xhr = this;
        const listener = () => {
          try { accept(xhr.responseType === "json" ? xhr.response : JSON.parse(xhr.responseText)); }
          catch (error) { finish(error); }
        };
        listeners.push([xhr, listener]);
        xhr.addEventListener("load", listener, {once: true});
      }
      return originalSend.call(this, body);
    }
    const timer = setTimeout(() => {
      const error = new Error(requested ? "智赢查询超时，已跳过并继续下一单"
        : "智赢未发起查询，请刷新订单页面后重试（订单未跳过）");
      error.lookupTimeout = requested;
      finish(error);
    }, timeout);
    window.fetch = wrappedFetch;
    XMLHttpRequest.prototype.open = wrappedOpen;
    XMLHttpRequest.prototype.send = wrappedSend;
    Promise.resolve().then(trigger).catch(error => finish(error));
  });
  const escape = () => document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", code: "Escape", bubbles: true}));
  const search = async group => {
    const key = [...new Set(group.flatMap(row => [...keys(row)].sort()))].join(",");
    escape();
    const data = await observe("orders.load", body => body?.key === key, async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(field, key);
      field.dispatchEvent(new Event("input", {bubbles: true}));
      field.dispatchEvent(new Event("change", {bubbles: true}));
      // Let the SPA commit its input model before invoking its search handler.
      await new Promise(resolve => setTimeout(resolve, 0));
      const usable = node => node.getClientRects().length && !node.disabled &&
        node.getAttribute("aria-disabled") !== "true";
      let button;
      // Prefer the control belonging to this input, never a global search button.
      for (let scope = field.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
        const candidates = [...scope.querySelectorAll('button, [role="button"], .ant-input-search-icon')]
          .filter(node => usable(node) && ( /^(订单搜索|搜索|查询)$/.test(clean(node.textContent)) ||
            /^(订单搜索|搜索|查询|search)$/i.test(node.getAttribute("aria-label") || "") ||
            node.matches('.ant-input-search-button, .ant-input-search-icon') ||
            node.querySelector('[aria-label="search"], .anticon-search')))
          .filter(node => !node.parentElement?.closest('button, [role="button"]'));
        if (candidates.length > 1) throw new Error("智赢订单搜索按钮不唯一，请确认订单页面（订单未跳过）");
        if (candidates.length === 1) {button = candidates[0]; break;}
      }
      if (button) button.click();
      else if (field.form) field.form.requestSubmit();
      else throw new Error("未找到可用的智赢订单搜索按钮，请刷新订单页面后重试（订单未跳过）");
    });
    if (!Array.isArray(data.list?.data) || !Number.isFinite(Number(data.list.maxcount || 0))) {
      throw new Error("智赢订单列表格式异常");
    }
    return data.list;
  };
  const discover = async () => {
    if (window.__mercadoZyingOrderApi) return window.__mercadoZyingOrderApi;
    const urls = [...new Set([...performance.getEntriesByType("resource").map(item => item.name),
      ...Array.from(document.scripts, script => script.src)])].filter(url => {
      try { const parsed = new URL(url, location.href); return parsed.origin === location.origin && parsed.pathname.endsWith(".js"); }
      catch (_) { return false; }
    });
    const controller = new AbortController();
    let timer;
    try {
      return await Promise.race([(async () => {
        for (const url of urls) {
          if (controller.signal.aborted) return null;
          try {
            const response = await fetch(url, {credentials: "same-origin", signal: controller.signal});
            if (!response.ok || !/getFreeStyleApi\s*:/.test(await response.text())) continue;
            const module = await import(url);
            if (controller.signal.aborted) return null;
            const api = Object.values(module).find(value => typeof value?.getFreeStyleApi === "function");
            if (api) return (window.__mercadoZyingOrderApi = api);
          } catch (_) {}
        }
        return null;
      })(), new Promise(resolve => {timer = setTimeout(() => {controller.abort(); resolve(null);}, 5000);})]);
    } finally { clearTimeout(timer); controller.abort(); }
  };
  let api;
  const readDetail = async id => {
    try {
      let detail;
      if (api) {
        const controller = new AbortController();
        let timer;
        try {
          detail = responseData(await Promise.race([
            api.getFreeStyleApi("order.view", {id: Number(id)}, undefined,
              {signal: controller.signal, timeout, silentError: true}),
            new Promise((_, reject) => {timer = setTimeout(() => {
              controller.abort(); const error = new Error("智赢详情响应超过 30 秒"); error.lookupTimeout = true; reject(error);
            }, timeout);})
          ]));
        } finally { clearTimeout(timer); controller.abort(); }
      } else {
        escape();
        const deadline = Date.now() + 5000;
        while (!document.querySelector(`tr[data-row-key="${id}"]`) && Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 50));
        }
        detail = await observe("order.view", body => String(body?.id) === id, () => {
          const row = document.querySelector(`tr[data-row-key="${id}"]`);
          if (!row) throw new Error("智赢列表未找到匹配订单");
          row.click();
        });
      }
      if (!Array.isArray(detail.root) || detail.root.length !== 1 || clean(detail.root[0]?.order_id) !== id) {
        return {error: "智赢返回了其他订单详情或详情不唯一"};
      }
      const pick = (row, fields) => Object.fromEntries(fields.filter(key => key in row).map(key => [key, row[key]]));
      return {detail: {root: detail.root.map(row => pick(row, ["order_id", "order_no", "order_key", "order_cost"])),
        buys: Array.isArray(detail.buys) ? detail.buys.map(row => pick(row, ["no", "trace", "amount", "cur"])) : detail.buys}};
    } catch (error) {
      if (error.lookupTimeout || ["ECONNABORTED", "ETIMEDOUT"].includes(error.code)) return {skip_reason: error.message};
      throw error;
    }
  };
  const group = async selected => {
    let listing;
    try { listing = await search(selected); }
    catch (error) {
      if (!error.lookupTimeout) throw error;
      for (const row of selected) outcomes[clean(row.id)] = {skip_reason: error.message};
      return;
    }
    if (Number(listing.maxcount || 0) > listing.data.length) {
      if (selected.length === 1) outcomes[clean(selected[0].id)] = {error: "智赢搜索结果超过一页，无法确认唯一订单"};
      else {const middle = Math.floor(selected.length / 2); await group(selected.slice(0, middle)); await group(selected.slice(middle));}
      return;
    }
    const matched = new Map();
    for (const row of selected) {
      const rowId = clean(row.id);
      let matches = listing.data.filter(item => intersects(keys(row), itemKeys(item)));
      const exact = matches.filter(item => itemKeys(item).has(rowId));
      if (exact.length) matches = exact;
      if (!matches.length) {outcomes[rowId] = {detail: null}; continue;}
      if (matches.length !== 1) {outcomes[rowId] = {error: "智赢订单匹配不唯一"}; continue;}
      const id = clean(matches[0].id);
      if (!/^\d+$/.test(id)) {outcomes[rowId] = {error: "智赢订单内部编号无效"}; continue;}
      matched.set(id, [...(matched.get(id) || []), rowId]);
    }
    if (matched.size && api === undefined) api = await discover();
    const ids = [...matched.keys()];
    const concurrency = api ? 4 : 1;
    for (let offset = 0; offset < ids.length; offset += concurrency) {
      const wave = ids.slice(offset, offset + concurrency);
      const settled = await Promise.allSettled(wave.map(readDetail));
      const failed = settled.find(result => result.status === "rejected");
      if (failed) throw failed.reason;
      const details = settled.map(result => result.value);
      wave.forEach((id, index) => matched.get(id).forEach(rowId => {outcomes[rowId] = details[index];}));
    }
  };
  try {
    await group(rows);
    return {rows: rows.map(row => ({order_id: clean(row.id), ...(outcomes[clean(row.id)] || {error: "未获取到查询结果"})}))};
  } catch (error) {
    return {error: error.message || String(error), login_required: Boolean(error.loginRequired)};
  } finally {
    if (window.__zeshunOrderBatchUntil === lease) delete window.__zeshunOrderBatchUntil;
  }
}
