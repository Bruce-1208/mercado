"use strict";

function normalizeToken(value) {
  if (!value) return "";
  let token = value;
  try { token = JSON.parse(value); } catch (_) {}
  if (token && typeof token === "object") {
    token = token.token || token.access_token || token.accessToken || "";
  }
  return String(token || "").trim();
}

function pageCredential() {
  const stored = normalizeToken(localStorage.getItem("token"));
  if (stored) return stored;
  const cookie = document.cookie.split(";").map(value => value.trim())
    .find(value => value.startsWith("token="));
  const value = cookie ? normalizeToken(decodeURIComponent(cookie.slice(6))) : "";
  return value ? `cookie://${value}` : "";
}

function copyCategoryOptions(options) {
  return options.map(option => ({
    value: option.value,
    label: String(option.label || "").trim(),
    children: copyCategoryOptions(Array.isArray(option.children) ? option.children : [])
  }));
}

function categoryTree() {
  const cascader = document.querySelector(".ant-cascader");
  if (!cascader) return [];
  const fiberKey = Object.keys(cascader).find(key => key.startsWith("__reactFiber$"));
  let fiber = fiberKey ? cascader[fiberKey] : null;
  while (fiber) {
    const props = fiber.memoizedProps || {};
    if (Array.isArray(props.options)) return copyCategoryOptions(props.options);
    fiber = fiber.return;
  }
  return [];
}

function flattenCategories(options, parents = [], rows = [], seen = new Set()) {
  for (const option of options || []) {
    const value = String(option.value ?? "").trim();
    const label = String(option.label || "").trim();
    const path = [...parents, {value, label}];
    const labels = path.map(item => item.label).filter(Boolean);
    if (value && labels.length && !seen.has(value)) {
      seen.add(value);
      rows.push({
        category_id: value,
        category_name: labels.join("/"),
        category_leaf_name: labels[labels.length - 1]
      });
    }
    flattenCategories(option.children, path, rows, seen);
  }
  return rows;
}

function zyingContext() {
  return {
    ok: true,
    zying: true,
    credential: pageCredential(),
    categories: flattenCategories(categoryTree()),
    url: location.href
  };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "PING_ZYING_PAGE") sendResponse({ok: true, zying: true});
  if (message?.type === "READ_ZYING_CONTEXT") sendResponse(zyingContext());
});
