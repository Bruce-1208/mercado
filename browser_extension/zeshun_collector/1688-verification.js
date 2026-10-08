"use strict";

// Serialized by scripting.executeScript: keep this observer self-contained.
function zeshun1688VerificationSnapshot() {
  const visible = node => {
    if (!node?.getClientRects().length) return false;
    for (let parent = node; parent?.nodeType === 1; parent = parent.parentElement) {
      const style = parent.ownerDocument.defaultView.getComputedStyle(parent);
      if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    }
    return true;
  };
  const pattern = /滑动验证|请(?:按住|拖动).{0,30}滑块|人机(?:审核|验证)|请完成.{0,20}验证|安全验证|访问受限|操作[太过]?于?频繁/;
  const inspect = (doc, offsetX = 0, offsetY = 0, depth = 0) => {
    if (depth > 5) return {blocked: true, reason: "验证页面层级无法确认"};
    let blocked = false, controlCount = 0;
    const sliders = [];
    for (const track of doc.querySelectorAll('.nc_scale, [id$="_scale"]')) {
      if (!visible(track)) continue;
      if (/验证通过|验证成功/.test(track.textContent || "")) continue;
      const handles = [...track.querySelectorAll('.btn_slide, [id$="_n1z"]')].filter(visible);
      if (handles.length !== 1) continue;
      blocked = true;
      controlCount++;
      const handle = handles[0], a = handle.getBoundingClientRect(), b = track.getBoundingClientRect();
      // Only a plain full-width slider; image/puzzle challenges need the operator.
      const widget = track.closest('.nc-container, .nc_wrapper, [id$="_wrapper"]') || track;
      if ([...widget.querySelectorAll('img, canvas, .captcha-img, [class*="puzzle"], [class*="jigsaw"]')].some(visible) ||
          b.width <= a.width + 20 || b.height <= 0 || a.width <= 0 || a.height <= 0 ||
          a.left < b.left || a.right > b.right || b.right - a.width / 2 - 2 <= a.left + a.width / 2) continue;
      const x = a.left + a.width / 2, y = a.top + a.height / 2;
      const hit = doc.elementFromPoint(x, y);
      if (hit !== handle && !handle.contains(hit)) continue;
      sliders.push({startX: offsetX + x, endX: offsetX + b.right - a.width / 2 - 2, y: offsetY + y});
    }
    for (const node of doc.querySelectorAll('#baxia-dialog, .baxia-dialog, [id^="nc_"][id$="_wrapper"], .nc-container')) {
      if (visible(node) && !/验证通过|验证成功/.test(node.textContent || "")) blocked = true;
    }
    if (doc.body) {
      const walker = doc.createTreeWalker(doc.body, 4);
      let count = 0;
      for (let node = walker.nextNode(); node && count++ < 4000; node = walker.nextNode()) {
        const text = String(node.nodeValue || "").trim();
        if (text.length <= 240 && pattern.test(text) && !/验证通过|验证成功/.test(text) && visible(node.parentElement)) {
          blocked = true;
          break;
        }
      }
    }
    for (const frame of doc.querySelectorAll('iframe')) {
      if (!visible(frame)) continue;
      try {
        if (frame.contentDocument?.body) {
          const box = frame.getBoundingClientRect();
          const child = inspect(frame.contentDocument, offsetX + box.left + frame.clientLeft,
            offsetY + box.top + frame.clientTop, depth + 1);
          blocked ||= child.blocked;
          controlCount += child.controlCount || 0;
          sliders.push(...(child.sliders || []));
        } else if (/captcha|baxia|verify|challenge/i.test(frame.src || "")) blocked = true;
      } catch (_) {
        if (/captcha|baxia|verify|challenge/i.test(frame.src || "")) blocked = true;
      }
    }
    return {blocked, sliders, controlCount};
  };
  const result = inspect(document);
  const slider = result.controlCount === 1 && result.sliders?.length === 1 ? result.sliders[0] : null;
  const inViewport = slider && slider.startX >= 0 && slider.endX < innerWidth && slider.y >= 0 && slider.y < innerHeight;
  return {blocked: result.blocked, slider: inViewport ? slider : null, url: location.href};
}

const AI_WEIGHT_PRICE_VERIFICATION_KEY = "aiWeightPriceVerificationAttempt";

async function aiWeightPriceVerificationState(tabId) {
  let saved;
  try { saved = (await storageGet("session", [AI_WEIGHT_PRICE_VERIFICATION_KEY]))[AI_WEIGHT_PRICE_VERIFICATION_KEY] || {}; }
  catch (_) { return aiWeightPricePauseVerification(tabId, "无法读取验证尝试状态"); }
  return saved[`${aiWeightPriceVerificationScope}:${tabId}`] || {};
}

async function aiWeightPriceSaveVerificationState(tabId, state) {
  let saved;
  try { saved = (await storageGet("session", [AI_WEIGHT_PRICE_VERIFICATION_KEY]))[AI_WEIGHT_PRICE_VERIFICATION_KEY] || {}; }
  catch (_) { return aiWeightPricePauseVerification(tabId, "无法保留验证尝试状态"); }
  // Only the current item needs an attempt ledger; keep it across manual Continue.
  const prefix = `${aiWeightPriceVerificationScope}:`;
  const current = Object.fromEntries(Object.entries(saved).filter(([key]) => key.startsWith(prefix)));
  current[`${prefix}${tabId}`] = state;
  try { await storageSet("session", {[AI_WEIGHT_PRICE_VERIFICATION_KEY]: current}); }
  catch (_) { return aiWeightPricePauseVerification(tabId, "无法保留验证尝试状态"); }
  const persisted = await aiWeightPriceVerificationState(tabId);
  if (persisted.at !== state.at || persisted.attempted !== state.attempted || persisted.verified !== state.verified) {
    return aiWeightPricePauseVerification(tabId, "验证尝试状态保存未确认");
  }
}

async function aiWeightPricePauseVerification(tabId, reason) {
  try {
    const tab = await chrome.tabs.update(tabId, {active: true});
    if (tab?.windowId) await chrome.windows.update(tab.windowId, {focused: true});
  } catch (_) {}
  throw new Error(`1688滑块验证未确认通过：${reason}；已暂停，请手动完成验证后继续核重核价`);
}

async function aiWeightPriceObserveVerification(tabId) {
  let tab;
  try { tab = await chrome.tabs.get(tabId); }
  catch (_) { return aiWeightPricePauseVerification(tabId, "验证标签页不可用"); }
  if (!aiWeightPriceIs1688Tab(tab)) {
    return aiWeightPricePauseVerification(tabId, "当前页面已进入登录或验证地址");
  }
  let result;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (aiWeightPriceCancelled) throw new Error("任务已停止");
    try {
      result = await chrome.scripting.executeScript({target: {tabId}, func: zeshun1688VerificationSnapshot});
      break;
    } catch (error) {
      if (attempt === 2 || !/frame.*removed|context.*destroyed|document.*unloaded|Cannot access.*page/i.test(error.message || "")) {
        return aiWeightPricePauseVerification(tabId, "无法读取验证页面，请手动接手");
      }
      await new Promise(resolve => setTimeout(resolve, 200));
    }
  }
  if (!result?.[0]?.result || typeof result[0].result.blocked !== "boolean") {
    return aiWeightPricePauseVerification(tabId, "无法读取验证状态");
  }
  return result[0].result;
}

async function aiWeightPriceCheck1688Verification(tabId) {
  if (aiWeightPriceCancelled) throw new Error("任务已停止");
  let snapshot = await aiWeightPriceObserveVerification(tabId);
  if (!snapshot.blocked) return;
  const state = await aiWeightPriceVerificationState(tabId);
  if (state.attempted) return aiWeightPricePauseVerification(tabId, "本商品已尝试一次，验证仍未解除");
  if (!snapshot.slider) return aiWeightPricePauseVerification(tabId, "无法唯一定位可拖动的普通滑块");
  // Persist before the first input, so navigation or worker restart cannot retry it.
  await aiWeightPriceSaveVerificationState(tabId, {attempted: true, verified: false, at: Date.now()});
  const target = {tabId};
  let attached = false, pressed = false;
  try {
    if (!chrome.debugger?.attach) throw new Error("插件鼠标控制权限不可用");
    await chrome.debugger.attach(target, "1.3");
    attached = true;
    snapshot = await aiWeightPriceObserveVerification(tabId);
    if (!snapshot.blocked || !snapshot.slider) throw new Error("拖动前验证控件已变化");
    const {startX, endX, y} = snapshot.slider;
    const send = params => chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", params);
    await send({type: "mouseMoved", x: startX, y});
    pressed = true;
    await send({type: "mousePressed", x: startX, y, button: "left", buttons: 1, clickCount: 1});
    for (let step = 1; step <= 12; step++) {
      if (aiWeightPriceCancelled) throw new Error("任务已停止");
      await send({type: "mouseMoved", x: startX + (endX - startX) * step / 12, y, button: "left", buttons: 1});
      await new Promise(resolve => setTimeout(resolve, 40));
    }
    await send({type: "mouseReleased", x: endX, y, button: "left", buttons: 0, clickCount: 1});
    pressed = false;
  } catch (error) {
    if (aiWeightPriceCancelled) throw new Error("任务已停止");
    return aiWeightPricePauseVerification(tabId, `拖动未完成（${error.message || error}）`);
  } finally {
    if (attached) {
      if (pressed) {
        try { await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {type: "mouseReleased", x: 0, y: 0, button: "left", buttons: 0}); } catch (_) {}
      }
      try { await chrome.debugger.detach(target); } catch (_) {}
    }
  }
  const deadline = Date.now() + 5000;
  let clearSamples = 0;
  while (Date.now() < deadline) {
    if (aiWeightPriceCancelled) throw new Error("任务已停止");
    snapshot = await aiWeightPriceObserveVerification(tabId);
    clearSamples = snapshot.blocked ? 0 : clearSamples + 1;
    if (clearSamples >= 2) return; // Still pending real search/detail readback.
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  return aiWeightPricePauseVerification(tabId, "拖动后仍有验证提示或页面未恢复");
}

async function aiWeightPriceWith1688Verification(tabId, operation) {
  await aiWeightPriceCheck1688Verification(tabId);
  let settled = false, result, failure;
  const pending = Promise.resolve().then(operation).then(value => {result = value;}, error => {failure = error;})
    .finally(() => {settled = true;});
  try {
    while (!settled) {
      await Promise.race([pending, new Promise(resolve => setTimeout(resolve, 400))]);
      await aiWeightPriceCheck1688Verification(tabId);
    }
  } catch (error) {
    // Discard the pending result, and stop delayed uploads/search clicks on the page.
    const frames = typeof aiWeightPriceFrameIds === "function" ? await aiWeightPriceFrameIds(tabId) : [0];
    await Promise.allSettled(frames.map(frameId =>
      sendTabMessage(tabId, {type: "AI_WEIGHT_PRICE_CANCEL_SUPPLIER_ACTION"}, frameId)));
    throw error;
  }
  if (failure) {
    if (aiWeightPriceMessageLostDuringNavigation(failure)) throw failure;
    const state = await aiWeightPriceVerificationState(tabId);
    if (state.attempted && !state.verified) {
      return aiWeightPricePauseVerification(tabId, "拖动后目标页面未能读取");
    }
    throw failure;
  }
  return result;
}

async function aiWeightPriceConfirm1688Readable(tabId, readable) {
  const state = await aiWeightPriceVerificationState(tabId);
  if (!state.attempted || state.verified) return;
  if (!readable) return aiWeightPricePauseVerification(tabId, "验证提示已消失，但未读到目标商品或搜索结果");
  await aiWeightPriceCheck1688Verification(tabId);
  await aiWeightPriceSaveVerificationState(tabId, {...state, verified: true, verifiedAt: Date.now()});
}
