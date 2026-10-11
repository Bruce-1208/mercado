/* Restore account-scoped filter preferences before the first inbox read. */
(() => {
  const account = typeof document === 'undefined' ? '' : document.currentScript?.dataset.account;
  const storageKey = account ? `zeshun-today-filters:v1:${account}` : null;
  const clean = value => Object.fromEntries(['salesperson', 'group_name'].map(key => {
    const values = Array.isArray(value?.[key]) ? value[key] : [value?.[key]];
    return [key, [...new Set(values.filter(v => typeof v === 'string').map(v => v.trim().slice(0, 200)).filter(Boolean))].sort()];
  }));
  window.todayFilterPreferences = {
    read() {
      try {
        return clean(storageKey ? JSON.parse(window.localStorage.getItem(storageKey) || '{}') : {});
      } catch (_) { return clean({}); }
    },
    save(value) {
      try { if (storageKey) window.localStorage.setItem(storageKey, JSON.stringify(clean(value))); }
      catch (_) { /* Browsers may disable storage; filtering must still work. */ }
    },
  };
  const requests = new Map();
  const cacheKey = url => account ? `zeshun-today-snapshot:v2:${account}:${url}` : null;
  window.rememberTodaySnapshot = (url, result) => {
    try {
      const key = cacheKey(url);
      if (key && result.ok && result.payload?.status === 'success') {
        window.sessionStorage.setItem(key, JSON.stringify({at: Date.now(), result}));
      }
    } catch (_) { /* Storage quotas must never prevent loading. */ }
  };
  window.clearTodaySnapshots = () => {
    try {
      const prefix = `zeshun-today-snapshot:v2:${account}:`;
      for (const key of Object.keys(window.sessionStorage)) if (key.startsWith(prefix)) window.sessionStorage.removeItem(key);
    } catch (_) {}
    requests.clear();
  };
  function cached(url) {
    try {
      const saved = JSON.parse(window.sessionStorage.getItem(cacheKey(url)) || 'null');
      if (saved && Date.now() - saved.at < 300000) {
        // Render immediately, then revalidate permissions and current data.
        saved.result.payload.data.refreshing = true;
        return saved.result;
      }
    } catch (_) {}
    return null;
  }
  window.takeTodayPreload = url => {
    const promise = requests.get(url);
    requests.delete(url);
    return promise;
  };
  async function read(url) {
    try {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
      const result = {ok: response.ok, status: response.status, payload: await response.json()};
      window.rememberTodaySnapshot(url, result);
      return result;
    } catch (_) { return null; }
  }
  function preload() {
    const filters = window.todayFilterPreferences.read();
    const query = initial => {
      const params = new URLSearchParams(initial);
      for (const [key, values] of Object.entries(filters)) {
        for (const value of values.length ? values : ['']) params.append(key, value);
      }
      params.set('background', '1');
      return params;
    };
    const tasks = query({include_closed: '0'});
    const messages = query({});
    for (const url of [
      `/api/mercado/today/tasks?${tasks}`,
      `/api/mercado/today/claims?${messages}`,
    ]) {
      const snapshot = cached(url);
      requests.set(url, snapshot ? Promise.resolve(snapshot) : read(url));
      if (location.pathname !== '/mercado/today') {
        // Warm to completion while the workbench is open, not just the first pending response.
        const warm = async (attempt = 0) => {
          const result = await read(url);
          requests.set(url, Promise.resolve(result));
          if (result?.ok && result.payload?.data?.refreshing && attempt < 20 && !document.hidden) {
            setTimeout(() => warm(attempt + 1), 3000);
          }
        };
        requests.get(url).then(result => {
          if (snapshot || result?.payload?.data?.refreshing) setTimeout(warm, 3000);
        });
      }
    }
  }
  if (location.pathname === '/mercado/today') preload();
  else if ('requestIdleCallback' in window) window.requestIdleCallback(preload, {timeout: 1500});
  else setTimeout(preload, 500);
})();
