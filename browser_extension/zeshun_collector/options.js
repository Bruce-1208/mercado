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
const mailEnabled = document.getElementById("mail-enabled");
const senderEmail = document.getElementById("sender-email");
const receiverEmail = document.getElementById("receiver-email");
const smtpHost = document.getElementById("smtp-host");
const smtpPort = document.getElementById("smtp-port");
const smtpSecurity = document.getElementById("smtp-security");
const smtpPassword = document.getElementById("smtp-password");
const mailStatus = document.getElementById("mail-status");

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

function renderMailSettings(config = {}) {
  mailEnabled.checked = Boolean(config.enabled);
  senderEmail.value = config.sender_email || "";
  receiverEmail.value = config.receiver_email || "";
  smtpHost.value = config.smtp_host || "";
  smtpPort.value = Number(config.smtp_port || 465);
  smtpSecurity.value = config.smtp_security || "ssl";
  smtpPassword.value = "";
  mailStatus.textContent = config.configured
    ? `已配置${config.enabled ? "并启用" : "但未启用"}；授权码已加密保存。`
    : "尚未完成邮件通知配置";
  mailStatus.className = `auth-status${config.configured ? " logged-in" : ""}`;
}

async function refreshMailSettings() {
  const response = await runtimeMessage({type: "GET_NOTIFICATION_SETTINGS"});
  if (!response.ok) throw new Error(response.error || "读取邮件通知配置失败");
  renderMailSettings(response.settings || {});
  return response.settings || {};
}

function mailValues() {
  const sender = senderEmail.value.trim();
  const receiver = receiverEmail.value.trim();
  if (!sender || !senderEmail.validity.valid) throw new Error("请填写有效的发件邮箱");
  if (!receiver || !receiverEmail.validity.valid) throw new Error("请填写有效的收件邮箱");
  const port = Number(smtpPort.value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("SMTP 端口无效");
  return {
    enabled: mailEnabled.checked,
    sender_email: sender,
    receiver_email: receiver,
    smtp_host: smtpHost.value.trim(),
    smtp_port: port,
    smtp_security: smtpSecurity.value,
    smtp_password: smtpPassword.value
  };
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
    await refreshMailSettings();
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

document.getElementById("save-mail").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const response = await runtimeMessage({type: "SAVE_NOTIFICATION_SETTINGS", settings: mailValues()});
    if (!response.ok) throw new Error(response.error || "邮件通知配置保存失败");
    renderMailSettings(response.settings || {});
    showMessage("邮件通知设置已保存。", false);
  } catch (error) {
    mailStatus.textContent = error.message || String(error);
    mailStatus.className = "auth-status error";
    showMessage(error.message || String(error), true);
  } finally {
    button.disabled = false;
  }
});

document.getElementById("test-mail").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "发送中…";
  try {
    const response = await runtimeMessage({type: "TEST_NOTIFICATION_EMAIL"});
    if (!response.ok) throw new Error(response.error || "测试邮件发送失败");
    mailStatus.textContent = response.message || "测试邮件已发送";
    mailStatus.className = "auth-status logged-in";
    showMessage(response.message || "测试邮件已发送，请检查收件箱。", false);
  } catch (error) {
    mailStatus.textContent = error.message || String(error);
    mailStatus.className = "auth-status error";
    showMessage(error.message || String(error), true);
  } finally {
    button.disabled = false;
    button.textContent = "发送测试邮件";
  }
});

syncGet(["consoleUrl", "openConsoleAfterCollect"]).then(values => {
  const config = {...DEFAULTS, ...values};
  urlInput.value = config.consoleUrl;
  openAfterInput.checked = Boolean(config.openConsoleAfterCollect);
  return refreshAuth();
}).then(state => {
  if (state?.authenticated) return refreshMailSettings();
  return null;
}).catch(error => showMessage(error.message || String(error), true));
