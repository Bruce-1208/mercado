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
  const cache = window.__zeshunZyingDevelopersCache;
  if (cache && cache.expiresAt > Date.now()) return cache.rows;
  try {
    const chunks = window.webpackChunkzying;
    if (!chunks || typeof chunks.push !== "function") return [];
    if (!window.__zeshunZyingRequire) {
      const runtimeId = 910000000 + Math.floor(Math.random() * 89999999);
      chunks.push([
        [runtimeId],
        {},
        function(require) { window.__zeshunZyingRequire = require; }
      ]);
    }
    const requestModule = window.__zeshunZyingRequire(60730);
    const requestApi = requestModule && (requestModule.Z || requestModule.default);
    if (!requestApi || typeof requestApi.getFreeStyleApi !== "function") return [];
    const response = await requestApi.getFreeStyleApi("logins.select", {});
    const data = response?.data || response || {};
    const source = Array.isArray(data.logins) ? data.logins
      : Array.isArray(data.rows) ? data.rows
      : Array.isArray(data.data) ? data.data : [];
    const seen = new Set();
    const rows = source.filter(row => row && typeof row === "object").map(row => ({
      id: String(row.id ?? row.value ?? "").trim(),
      name: String(row.name ?? row.label ?? "").trim()
    })).filter(row => row.id && !seen.has(row.id) && seen.add(row.id));
    window.__zeshunZyingDevelopersCache = {expiresAt: Date.now() + 60000, rows};
    return rows;
  } catch (_) {
    return [];
  }
}
