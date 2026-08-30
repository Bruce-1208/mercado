"use strict";

const DEFAULTS = {
  consoleUrl: "http://127.0.0.1:5000",
  openConsoleAfterCollect: false
};
const urlInput = document.getElementById("console-url");
const usernameInput = document.getElementById("username");
const passwordInput = document.getElementById("password");
const openAfterInput = document.getElementById("open-after");
const authStatus = document.getElementById("auth-status");
const messageBox = document.getElementById("message");

function syncGet(keys) {
  return new Promise(resolve => chrome.storage.sync.get(keys, resolve));
}

function syncSet(values) {
  return new Promise(resolve => chrome.storage.sync.set(values, resolve));
}

function runtimeMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, response => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve(response || {});
    });
  });
}

function showMessage(message, error) {
  messageBox.hidden = false;
  messageBox.className = `message${error ? " error" : ""}`;
  messageBox.textContent = message;
}

function normalizedValues() {
  const parsed = new URL(urlInput.value.trim());
  if (!/^https?:$/.test(parsed.protocol)) throw new Error("控制台地址必须以 http:// 或 https:// 开头");
  return {
    consoleUrl: urlInput.value.trim().replace(/\/+$/, ""),
    openConsoleAfterCollect: openAfterInput.checked
  };
}

async function requestOriginPermission(consoleUrl) {
  const parsed = new URL(consoleUrl);
  const origin = `${parsed.protocol}//${parsed.host}/*`;
  const contains = await chrome.permissions.contains({origins: [origin]});
  if (contains) return true;
  return chrome.permissions.request({origins: [origin]});
}

async function saveSettings() {
  const values = normalizedValues();
  if (!await requestOriginPermission(values.consoleUrl)) {
    throw new Error("未获得控制台地址访问权限，无法连接该服务器");
  }
  const previous = await syncGet(["consoleUrl"]);
  if (previous.consoleUrl && previous.consoleUrl !== values.consoleUrl) {
    await runtimeMessage({type: "LOGOUT"});
  }
  await syncSet(values);
  return values;
}

function renderAuth(state) {
  if (state && state.authenticated && state.user) {
    const name = state.user.display_name || state.user.username || "已登录账号";
    authStatus.textContent = `已登录：${name}${state.compatibilityMode ? "（旧控制台兼容模式）" : ""}`;
    authStatus.className = "auth-status logged-in";
    usernameInput.value = state.user.username || usernameInput.value;
    return;
  }
  authStatus.textContent = "当前未登录，登录后才能采集商品";
  authStatus.className = "auth-status";
}

async function refreshAuth() {
  const state = await runtimeMessage({type: "GET_STATE"});
  renderAuth(state);
  return state;
}

document.getElementById("login").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "登录中…";
  try {
    await saveSettings();
    const username = usernameInput.value.trim();
    const password = passwordInput.value;
    if (!username || !password) throw new Error("请输入泽顺控制台账号和密码");
    const response = await runtimeMessage({type: "LOGIN", username, password});
    if (!response.ok) throw new Error(response.error || "登录失败");
    passwordInput.value = "";
    await refreshAuth();
    showMessage("登录成功，插件已经可以采集商品。", false);
  } catch (error) {
    showMessage(error.message || String(error), true);
  } finally {
    button.disabled = false;
    button.textContent = "登录并测试";
  }
});

document.getElementById("save").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await saveSettings();
    await refreshAuth();
    showMessage("控制台地址已保存。", false);
  } catch (error) {
    showMessage(error.message || String(error), true);
  } finally {
    button.disabled = false;
  }
});

document.getElementById("logout").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await runtimeMessage({type: "LOGOUT"});
    passwordInput.value = "";
    await refreshAuth();
    showMessage("已经退出插件登录。", false);
  } catch (error) {
    showMessage(error.message || String(error), true);
  } finally {
    button.disabled = false;
  }
});

syncGet(["consoleUrl", "openConsoleAfterCollect"]).then(values => {
  const config = {...DEFAULTS, ...values};
  urlInput.value = config.consoleUrl;
  openAfterInput.checked = Boolean(config.openConsoleAfterCollect);
  return refreshAuth();
}).catch(error => showMessage(error.message || String(error), true));
