"use strict";

const pageStatus = document.getElementById("page-status");
const authStatus = document.getElementById("auth-status");
const collectButton = document.getElementById("collect");
const queueCount = document.getElementById("queue-count");
const resultBox = document.getElementById("result");
let activeTab = null;
let detailPage = false;
let authenticated = false;

function runtimeMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, response => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve(response || {});
    });
  });
}

function tabMessage(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, response => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve(response || {});
    });
  });
}

function showResult(message, kind) {
  resultBox.hidden = false;
  resultBox.className = `result ${kind || ""}`;
  resultBox.textContent = message;
}

async function refreshState() {
  const response = await runtimeMessage({type: "GET_STATE"});
  queueCount.textContent = String(response.queueLength || 0);
  authenticated = Boolean(response.authenticated);
  if (authenticated && response.user) {
    authStatus.textContent = `泽顺账号：${response.user.display_name || response.user.username}` +
      (response.compatibilityMode ? "（兼容模式）" : "");
    authStatus.className = "auth-line logged-in";
  } else {
    authStatus.textContent = "泽顺账号：未登录，请先打开设置登录";
    authStatus.className = "auth-line";
  }
  collectButton.disabled = !detailPage || !authenticated;
}

async function initialize() {
  try {
    [activeTab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (!activeTab || !activeTab.id) throw new Error("未找到当前标签页");
    const response = await tabMessage(activeTab.id, {type: "PING_PAGE"});
    if (response.ok && response.detail) {
      detailPage = true;
      pageStatus.textContent = "已识别 Mercado Libre 商品详情页，可以直接采集。";
    } else {
      pageStatus.textContent = "请打开 Mercado Libre 商品详情页；列表页可点击商品卡片上的“采集”按钮。";
    }
  } catch (_) {
    pageStatus.textContent = "当前页面不支持采集，请打开 Mercado Libre 商品详情页。";
  }
  await refreshState();
}

collectButton.addEventListener("click", async () => {
  collectButton.disabled = true;
  collectButton.textContent = "正在读取商品…";
  try {
    const extracted = await tabMessage(activeTab.id, {type: "EXTRACT_PRODUCT"});
    if (!extracted.ok) throw new Error(extracted.error || "无法读取商品详情");
    collectButton.textContent = "正在上传…";
    const response = await runtimeMessage({type: "SUBMIT_PRODUCT", product: extracted.product});
    if (!response.ok) throw new Error(response.error || "采集失败");
    if (response.queued) showResult("控制台暂不可用，商品已加入待传队列。", "warning");
    else showResult(`采集成功，任务编号：${response.taskId}`, "");
  } catch (error) {
    showResult(error.message || String(error), "error");
  } finally {
    collectButton.textContent = "采集当前商品";
    await refreshState();
  }
});

document.getElementById("retry").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "重试中…";
  try {
    const response = await runtimeMessage({type: "RETRY_QUEUE"});
    if (!response.ok) throw new Error(response.error || "重试失败");
    showResult(`本次上传 ${response.uploaded || 0} 件，剩余 ${response.remaining || 0} 件。`, response.remaining ? "warning" : "");
  } catch (error) {
    showResult(error.message || String(error), "error");
  } finally {
    button.disabled = false;
    button.textContent = "立即重试";
    await refreshState();
  }
});

document.getElementById("open-console").addEventListener("click", () => runtimeMessage({type: "OPEN_CONSOLE"}));
document.getElementById("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
initialize();
