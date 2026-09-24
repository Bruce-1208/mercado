"use strict";

// Self-contained: also injected into the page's MAIN world to read React props.
function zeshunReadZyingPageContext() {
  const text = value => {
    if (typeof value === "string" || typeof value === "number") return String(value).trim();
    if (Array.isArray(value)) return value.map(text).join("").trim();
    return value && typeof value === "object" ? text(value.props?.children) : "";
  };
  const cleanName = (value, id) => {
    let name = text(value);
    if (!name || name === id || /^\d+$/.test(name)) return "";
    // Some releases append the internal id to the human-readable label.
    for (const suffix of [` [${id}]`, `[${id}]`, `（${id}）`, `(${id})`]) {
      if (id && name.endsWith(suffix)) name = name.slice(0, -suffix.length).trim();
    }
    return name && name !== id ? name : "";
  };
  const categories = new Map();
  const visit = (options, fields, parents = []) => {
    for (const option of options) {
      if (!option || typeof option !== "object") continue;
      const id = text(option[fields.value || "value"] ?? option.category_id ?? option.id);
      const name = [option[fields.label || "label"], option.category_name, option.name,
        option.title, option.category_leaf_name].map(value => cleanName(value, id)).find(Boolean);
      const labels = name ? [...parents, name] : parents;
      if (id && name) categories.set(id, {
        category_id: id, category_name: labels.join("/"), category_leaf_name: name
      });
      const children = option[fields.children || "children"];
      if (Array.isArray(children)) visit(children, fields, labels);
    }
  };
  for (const cascader of document.querySelectorAll(".ant-cascader")) {
    const fiberKey = Object.keys(cascader).find(key => key.startsWith("__reactFiber$") || key.startsWith("__reactInternalInstance$"));
    let fiber = fiberKey ? cascader[fiberKey] : null;
    while (fiber) {
      const props = fiber.memoizedProps || {};
      if (Array.isArray(props.options) && props.options.length) {
        visit(props.options, props.fieldNames || {});
        if (categories.size) break;
      }
      fiber = fiber.return;
    }
    // A product page can contain several Ant cascaders. Once one of them
    // yielded a real category tree, walking the remaining React fibers only
    // adds work and can make the popup feel stuck on large pages.
    if (categories.size) break;
  }
  const normalizeToken = value => {
    if (!value) return "";
    let token = value;
    try { token = JSON.parse(value); } catch (_) {}
    if (token && typeof token === "object") token = token.token || token.access_token || token.accessToken || "";
    return String(token || "").trim();
  };
  let credential = normalizeToken(localStorage.getItem("token"));
  if (!credential) {
    const cookie = document.cookie.split(";").map(value => value.trim()).find(value => value.startsWith("token="));
    const token = cookie ? normalizeToken(decodeURIComponent(cookie.slice(6))) : "";
    if (token) credential = `cookie://${token}`;
  }
  return {credential, categories: [...categories.values()], url: location.href};
}

// Read the developer selector from the already loaded ZYing page when the
// frontend request bridge is available. This avoids starting a second,
// headless Edge just to call logins.select from the local workbench.
async function zeshunReadZyingProductDevelopers() {
  const normalizeRows = source => {
    const seen = new Set();
    return (Array.isArray(source) ? source : []).filter(row => row && typeof row === "object").map(row => ({
      id: String(row.id ?? row.value ?? row.loginid ?? "").trim(),
      name: String(row.name ?? row.label ?? row.loginname ?? "").trim()
    })).filter(row => row.id && row.name && !seen.has(row.id) && seen.add(row.id));
  };
  const rowsFrom = value => {
    if (!value || typeof value !== "object") return [];
    if (Array.isArray(value)) return normalizeRows(value);
    for (const key of ["logins", "list", "rows", "data"]) {
      const rows = rowsFrom(value[key]);
      if (rows.length) return rows;
    }
    return [];
  };
  // The current Vite build caches logins.select through localForage. Reading
  // that page-owned cache avoids coupling the extension to a minified module id.
  try {
    const databases = typeof indexedDB.databases === "function"
      ? await indexedDB.databases() : [{name: "localforage"}];
    for (const name of [...new Set(databases.map(row => row?.name).filter(Boolean).concat("localforage"))]) {
      const cached = await new Promise(resolve => {
        const timer = setTimeout(() => resolve(null), 1200);
        const request = indexedDB.open(name);
        request.onerror = () => { clearTimeout(timer); resolve(null); };
        request.onsuccess = () => {
          const db = request.result;
          const stores = [...db.objectStoreNames];
          if (!stores.length) { db.close(); clearTimeout(timer); return resolve(null); }
          let pending = stores.length;
          let found = null;
          for (const storeName of stores) {
            let get;
            try { get = db.transaction(storeName, "readonly").objectStore(storeName).get("logins"); }
            catch (_) { if (!--pending) { db.close(); clearTimeout(timer); resolve(found); } continue; }
            get.onsuccess = () => {
              if (rowsFrom(get.result).length) found = get.result;
              if (!--pending) { db.close(); clearTimeout(timer); resolve(found); }
            };
            get.onerror = () => {
              if (!--pending) { db.close(); clearTimeout(timer); resolve(found); }
            };
          }
        };
      });
      const cachedRows = rowsFrom(cached);
      if (cachedRows.length) return cachedRows;
    }
  } catch (_) {}
  // Read the live selector first: current product pages need not use webpack.
  const live = new Map();
  for (const node of document.querySelectorAll(".ant-select, select")) {
    const container = node.closest(".ant-form-item") || node.parentElement;
    const label = [node.getAttribute("aria-label"), node.getAttribute("placeholder"),
      container?.textContent, container?.querySelector("input")?.getAttribute("placeholder")].join(" ");
    if (!/产品开发/.test(label)) continue;
    const add = (id, name) => {
      id = String(id ?? "").trim(); name = String(name ?? "").trim();
      if (id && name && !/^(全部|请选择)/.test(name)) live.set(id, {id, name});
    };
    for (const option of node.querySelectorAll("option")) add(option.value, option.textContent);
    const key = Object.keys(node).find(key => key.startsWith("__reactFiber$") || key.startsWith("__reactInternalInstance$"));
    let fiber = key ? node[key] : null;
    for (let depth = 0; fiber && depth < 12; depth++, fiber = fiber.return) {
      const props = fiber.memoizedProps || {};
      if (!Array.isArray(props.options)) continue;
      const fields = props.fieldNames || {};
      for (const option of props.options) {
        const name = option[fields.label || "label"] ?? option.name;
        if (typeof name === "string" || typeof name === "number")
          add(option[fields.value || "value"] ?? option.id, name);
      }
      if (live.size) break;
    }
  }
  if (live.size) return [...live.values()];
  try {
    const chunks = window.webpackChunkzying || Object.entries(window)
      .find(([key, value]) => key.startsWith("webpackChunk") && Array.isArray(value))?.[1];
    if (!chunks || typeof chunks.push !== "function") return [];
    if (!window.__zeshunZyingRequire) {
      const runtimeId = 910000000 + Math.floor(Math.random() * 89999999);
      chunks.push([
        [runtimeId],
        {},
        function(require) { window.__zeshunZyingRequire = require; }
      ]);
    }
    const apis = [];
    const seenObjects = new WeakSet();
    const scan = (value, depth = 0) => {
      if (!value || typeof value !== "object" || depth > 3 || seenObjects.has(value)) return;
      seenObjects.add(value);
      if (typeof value.getFreeStyleApi === "function") apis.push(value);
      for (const child of Object.values(value)) scan(child, depth + 1);
    };
    try { scan(window.__zeshunZyingRequire(60730)); } catch (_) {}
    for (const module of Object.values(window.__zeshunZyingRequire.c || {})) scan(module?.exports);
    for (const requestApi of apis) {
      try {
        const rows = rowsFrom(await requestApi.getFreeStyleApi("logins.select", {}));
        if (rows.length) return rows;
      } catch (_) {}
    }
    return [];
  } catch (_) {
    return [];
  }
}
