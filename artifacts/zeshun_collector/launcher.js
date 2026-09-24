"use strict";

(function () {
  const HOST_ID = "zeshun-plugin-launcher";
  const POSITION_KEY = "zeshunLauncherPosition";
  const BUTTON_SIZE = 52;
  const EDGE_GAP = 10;
  let host = null;
  let button = null;
  let position = null;
  let dragState = null;
  let suppressClick = false;

  function storageGet(key) {
    return new Promise(resolve => chrome.storage.local.get([key], resolve));
  }

  function storageSet(values) {
    return new Promise(resolve => chrome.storage.local.set(values, resolve));
  }

  function defaultPosition() {
    return {
      left: Math.max(EDGE_GAP, window.innerWidth - BUTTON_SIZE - 20),
      top: Math.max(EDGE_GAP, Math.round(window.innerHeight * 0.45))
    };
  }

  function clampPosition(next) {
    const width = host?.getBoundingClientRect().width || BUTTON_SIZE;
    const height = host?.getBoundingClientRect().height || BUTTON_SIZE;
    const maxLeft = Math.max(EDGE_GAP, window.innerWidth - width - EDGE_GAP);
    const maxTop = Math.max(EDGE_GAP, window.innerHeight - height - EDGE_GAP);
    return {
      left: Math.min(Math.max(EDGE_GAP, Number(next?.left) || 0), maxLeft),
      top: Math.min(Math.max(EDGE_GAP, Number(next?.top) || 0), maxTop)
    };
  }

  function renderPosition(save) {
    if (!host) return;
    position = clampPosition(position || defaultPosition());
    host.style.left = `${position.left}px`;
    host.style.top = `${position.top}px`;
    if (save) void storageSet({[POSITION_KEY]: position});
  }

  function openPlugin() {
    if (!button) return;
    button.classList.add("is-opening");
    chrome.runtime.sendMessage({type: "OPEN_FLOATING_WINDOW"}, () => {
      // Reading lastError prevents Chrome from logging an unchecked error when
      // the extension is reloaded while a page is still open.
      void chrome.runtime.lastError;
      window.setTimeout(() => button?.classList.remove("is-opening"), 260);
    });
  }

  function finishDrag(event) {
    if (!dragState || event.pointerId !== dragState.pointerId) return;
    if (button?.hasPointerCapture?.(event.pointerId)) button.releasePointerCapture(event.pointerId);
    button.style.cursor = "grab";
    suppressClick = dragState.moved;
    if (dragState.moved) renderPosition(true);
    dragState = null;
  }

  function createLauncher() {
    if (host?.isConnected) return;
    host = document.createElement("div");
    host.id = HOST_ID;
    host.style.cssText = [
      "position:fixed",
      "z-index:2147483647",
      "width:52px",
      "height:52px",
      "margin:0",
      "padding:0",
      "border:0",
      "background:transparent",
      "pointer-events:none",
    ].join(";");
    const shadow = host.attachShadow({mode: "open"});
    shadow.innerHTML = `
      <style>
        button {
          align-items: center;
          background: linear-gradient(145deg, #166ee8, #0a449f);
          border: 2px solid rgba(255, 255, 255, .92);
          border-radius: 17px;
          box-shadow: 0 8px 22px rgba(10, 61, 145, .34), 0 2px 5px rgba(0, 0, 0, .18);
          color: #fff;
          cursor: grab;
          display: flex;
          flex-direction: column;
          gap: 1px;
          height: 52px;
          justify-content: center;
          padding: 4px;
          pointer-events: auto;
          touch-action: none;
          user-select: none;
          width: 52px;
          transition: transform 120ms ease, box-shadow 120ms ease, filter 120ms ease;
        }
        button:hover { box-shadow: 0 10px 27px rgba(10, 61, 145, .44), 0 2px 5px rgba(0, 0, 0, .18); transform: scale(1.05); }
        button:focus-visible { outline: 3px solid #8fc4ff; outline-offset: 2px; }
        button.is-opening { filter: brightness(1.16); transform: scale(.96); }
        img { border-radius: 8px; display: block; height: 25px; object-fit: contain; width: 25px; }
        span { color: #fff; font: 700 9px/1 "Microsoft YaHei", "PingFang SC", sans-serif; letter-spacing: .2px; }
      </style>
      <button type="button" title="打开泽顺插件（可拖动）" aria-label="打开泽顺插件（可拖动）">
        <img alt="" src="${chrome.runtime.getURL("icons/icon32.png")}">
        <span>泽顺</span>
      </button>
    `;
    button = shadow.querySelector("button");
    button.addEventListener("pointerdown", event => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      const rect = host.getBoundingClientRect();
      dragState = {
        pointerId: event.pointerId,
        offsetX: event.clientX - rect.left,
        offsetY: event.clientY - rect.top,
        moved: false
      };
      button.setPointerCapture?.(event.pointerId);
      button.style.cursor = "grabbing";
    });
    button.addEventListener("pointermove", event => {
      if (!dragState || event.pointerId !== dragState.pointerId) return;
      const next = clampPosition({
        left: event.clientX - dragState.offsetX,
        top: event.clientY - dragState.offsetY
      });
      if (Math.abs(next.left - position.left) > 2 || Math.abs(next.top - position.top) > 2) {
        dragState.moved = true;
      }
      position = next;
      renderPosition(false);
      if (dragState.moved) event.preventDefault();
    });
    button.addEventListener("pointerup", finishDrag);
    button.addEventListener("pointercancel", finishDrag);
    button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      if (suppressClick) {
        suppressClick = false;
        return;
      }
      openPlugin();
    });
    document.documentElement.appendChild(host);
    renderPosition(false);
  }

  async function restorePosition() {
    const saved = await storageGet(POSITION_KEY);
    if (saved?.[POSITION_KEY] && typeof saved[POSITION_KEY] === "object") {
      position = saved[POSITION_KEY];
    }
    renderPosition(false);
  }

  createLauncher();
  void restorePosition();
  window.addEventListener("resize", () => renderPosition(true));
  window.setInterval(createLauncher, 4000);
})();
