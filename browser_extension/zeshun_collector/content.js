(function () {
  "use strict";

  const core = globalThis.ZeshunCollectorCore;
  if (!core) return;

  let lastUrl = location.href;
  let mutationTimer = null;

  function sendMessage(message) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(message, response => {
        const runtimeError = chrome.runtime.lastError;
        if (runtimeError) reject(new Error(runtimeError.message));
        else resolve(response || {});
      });
    });
  }

  function showToast(message, kind) {
    let toast = document.getElementById("zeshun-collector-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "zeshun-collector-toast";
      document.documentElement.appendChild(toast);
    }
    toast.className = `zeshun-collector-toast is-${kind || "info"}`;
    toast.textContent = message;
    toast.classList.add("is-visible");
    clearTimeout(toast._hideTimer);
    toast._hideTimer = setTimeout(() => toast.classList.remove("is-visible"), 4200);
  }

  async function collectCurrent(button) {
    const original = button ? button.textContent : "";
    try {
      if (button) {
        button.disabled = true;
        button.textContent = "正在读取…";
      }
      const product = core.extractProduct(document, location.href);
      if (button) button.textContent = "正在上传…";
      const response = await sendMessage({type: "SUBMIT_PRODUCT", product});
      if (!response.ok) throw new Error(response.error || "采集失败");
      const message = response.queued ? "控制台暂不可用，已加入待传队列" : "已采集到泽顺控制台";
      showToast(message, response.queued ? "warning" : "success");
      if (button) button.textContent = response.queued ? "已待传" : "已采集";
    } catch (error) {
      showToast(error.message || String(error), "error");
      if (button) button.textContent = "采集失败";
    } finally {
      if (button) {
        setTimeout(() => {
          button.disabled = false;
          button.textContent = original || "采集到泽顺";
        }, 1800);
      }
    }
  }

  function looksLikeDetailPage() {
    return Boolean(document.querySelector("h1.ui-pdp-title, .ui-pdp-container, [data-testid='vip-container']"));
  }

  function installFloatingButton() {
    const existing = document.getElementById("zeshun-collector-floating");
    if (!looksLikeDetailPage()) {
      if (existing) existing.remove();
      return;
    }
    if (existing) return;
    const wrapper = document.createElement("div");
    wrapper.id = "zeshun-collector-floating";
    wrapper.className = "zeshun-collector-floating";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "zeshun-collector-primary";
    button.textContent = "采集到泽顺";
    button.title = "把当前商品保存到武汉泽顺综合服务台的商品采集列表";
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      collectCurrent(button);
    });
    wrapper.appendChild(button);
    document.documentElement.appendChild(wrapper);
  }

  function installCardButtons() {
    for (const candidate of core.cardCandidates(document)) {
      if (candidate.card.querySelector(":scope > .zeshun-card-collect")) continue;
      candidate.card.classList.add("zeshun-collector-card-host");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "zeshun-card-collect";
      button.textContent = "采集";
      button.title = "在后台读取详情并采集到泽顺控制台";
      button.addEventListener("click", async event => {
        event.preventDefault();
        event.stopPropagation();
        const original = button.textContent;
        button.disabled = true;
        button.textContent = "采集中…";
        try {
          const product = core.extractCardProduct(candidate.card, candidate.url);
          const response = await sendMessage({type: "SUBMIT_PRODUCT", product});
          if (!response.ok) throw new Error(response.error || "采集失败");
          button.textContent = response.queued ? "已待传" : "已采集";
          showToast(
            response.queued ? "控制台暂不可用，商品已加入待传队列" : "商品已采集到泽顺控制台",
            response.queued ? "warning" : "success"
          );
        } catch (error) {
          button.textContent = "重试";
          showToast(error.message || String(error), "error");
        } finally {
          setTimeout(() => {
            button.disabled = false;
            button.textContent = original;
          }, 2200);
        }
      });
      candidate.card.appendChild(button);
    }
  }

  function refreshUi() {
    installFloatingButton();
    installCardButtons();
  }

  const observer = new MutationObserver(() => {
    clearTimeout(mutationTimer);
    mutationTimer = setTimeout(refreshUi, 180);
  });
  observer.observe(document.documentElement, {childList: true, subtree: true});
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      setTimeout(refreshUi, 300);
    }
  }, 700);

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message && message.type === "PING_PAGE") {
      sendResponse({ok: true, detail: looksLikeDetailPage(), url: location.href});
      return false;
    }
    if (message && message.type === "EXTRACT_PRODUCT") {
      Promise.resolve().then(() => core.extractProduct(document, location.href)).then(
        product => sendResponse({ok: true, product}),
        error => sendResponse({ok: false, error: error.message || String(error)})
      );
      return true;
    }
    return false;
  });

  refreshUi();
})();
