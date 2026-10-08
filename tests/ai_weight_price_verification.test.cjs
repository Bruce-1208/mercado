const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function runtime({releases = true, slider = true, attachError = false} = {}) {
  let now = 0, blocked = true;
  const calls = [], stored = {};
  const chrome = {
    tabs: {get: async () => ({id: 7, url: 'https://www.1688.com/'}), update: async () => {calls.push('focus'); return {windowId: 1};}},
    windows: {update: async () => {}},
    scripting: {executeScript: async () => [{result: {blocked, slider: slider ? {startX: 10, endX: 200, y: 20} : null}}]},
    debugger: {
      attach: async () => {calls.push('attach'); if (attachError) throw new Error('another debugger');},
      detach: async () => {calls.push('detach');},
      sendCommand: async (_target, _method, params) => {
        calls.push(params.type);
        if (params.type === 'mouseReleased' && releases) blocked = false;
      }
    }
  };
  const context = vm.createContext({chrome, console,
    aiWeightPriceCancelled: false, aiWeightPriceVerificationScope: 'run:item',
    aiWeightPriceIs1688Tab: () => true,
    aiWeightPriceMessageLostDuringNavigation: () => false,
    sendTabMessage: async (_id, msg) => calls.push(msg.type),
    storageGet: async () => stored,
    storageSet: async (_area, value) => Object.assign(stored, value),
    Date: {now: () => now},
    setTimeout: (callback, ms) => {now += ms; queueMicrotask(callback);}
  });
  vm.runInContext(fs.readFileSync('browser_extension/zeshun_collector/1688-verification.js', 'utf8'), context);
  return {context, calls, stored, chrome, blocked: value => {blocked = value;}};
}

test('one real drag is not approval; readable target must be confirmed separately', async () => {
  const r = runtime();
  await r.context.aiWeightPriceCheck1688Verification(7);
  assert.equal(r.calls.filter(c => c === 'mousePressed').length, 1);
  assert.equal(r.calls.at(-1), 'detach');
  assert.equal((await r.context.aiWeightPriceVerificationState(7)).verified, false);
  await assert.rejects(r.context.aiWeightPriceConfirm1688Readable(7, false), /未读到目标商品/);
  await r.context.aiWeightPriceConfirm1688Readable(7, true);
  assert.equal((await r.context.aiWeightPriceVerificationState(7)).verified, true);
});

test('unchanged challenge pauses and manual Continue cannot repeat the drag', async () => {
  const r = runtime({releases: false});
  await assert.rejects(r.context.aiWeightPriceCheck1688Verification(7), /拖动后仍有验证/);
  await assert.rejects(r.context.aiWeightPriceCheck1688Verification(7), /已尝试一次/);
  assert.equal(r.calls.filter(c => c === 'mousePressed').length, 1);
  assert.equal(r.calls.filter(c => c === 'detach').length, 1);
  r.blocked(false); // User has resolved it manually.
  await r.context.aiWeightPriceCheck1688Verification(7);
  await r.context.aiWeightPriceConfirm1688Readable(7, true);
});

test('unsupported challenge and occupied debugger pause without input or stealing another session', async () => {
  const unknown = runtime({slider: false});
  await assert.rejects(unknown.context.aiWeightPriceCheck1688Verification(7), /无法唯一定位/);
  assert.equal(unknown.calls.includes('attach'), false);
  const occupied = runtime({attachError: true});
  await assert.rejects(occupied.context.aiWeightPriceCheck1688Verification(7), /拖动未完成/);
  assert.equal(occupied.calls.includes('mousePressed'), false);
  assert.equal(occupied.calls.includes('detach'), false);
});

test('stop during drag releases mouse and detaches without approval', async () => {
  const r = runtime();
  const send = r.chrome.debugger.sendCommand;
  r.chrome.debugger.sendCommand = async (...args) => {
    await send(...args);
    if (args[2].type === 'mousePressed') r.context.aiWeightPriceCancelled = true;
  };
  await assert.rejects(r.context.aiWeightPriceCheck1688Verification(7), /任务已停止/);
  assert.equal(r.calls.at(-1), 'detach');
  assert.equal(r.calls.includes('mouseReleased'), true);
  assert.equal((await r.context.aiWeightPriceVerificationState(7)).verified, false);
});

test('verification appearing during an in-flight read cancels the supplier action on failure', async () => {
  const r = runtime({slider: false});
  r.blocked(false);
  await assert.rejects(r.context.aiWeightPriceWith1688Verification(7, async () => {
    r.blocked(true);
    return {ok: true, skus: ['stale']};
  }), /滑块验证未确认通过/);
  assert.equal(r.calls.includes('AI_WEIGHT_PRICE_CANCEL_SUPPLIER_ACTION'), true);
});

test('unavailable observation is a manual pause, even when the read returned data', async () => {
  const r = runtime();
  r.chrome.scripting.executeScript = async () => {throw new Error('Cannot inspect document');};
  await assert.rejects(r.context.aiWeightPriceWith1688Verification(7, async () => ({ok:true})), /无法读取验证页面/);
  assert.equal(r.calls.includes('mousePressed'), false);
});

test('pause cancels every frame and a failed ledger write cannot start a drag', async () => {
  const r = runtime({slider: false});
  const cancelled = [];
  r.context.aiWeightPriceFrameIds = async () => [0, 3, 5];
  r.context.sendTabMessage = async (_id, _msg, frame) => cancelled.push(frame);
  r.blocked(false);
  await assert.rejects(r.context.aiWeightPriceWith1688Verification(7, async () => {
    r.blocked(true); return {ok: true};
  }), /滑块验证未确认通过/);
  assert.deepEqual(cancelled, [0, 3, 5]);
  const unsaved = runtime();
  unsaved.context.storageSet = async () => {};
  await assert.rejects(unsaved.context.aiWeightPriceCheck1688Verification(7), /状态保存未确认/);
  assert.equal(unsaved.calls.includes('mousePressed'), false);
});

test('a later ordinary item error after confirmed recovery keeps its original classification', async () => {
  const r = runtime();
  await r.context.aiWeightPriceCheck1688Verification(7);
  await r.context.aiWeightPriceConfirm1688Readable(7, true);
  await assert.rejects(r.context.aiWeightPriceWith1688Verification(7, async () => {
    throw new Error('商品价格缺失');
  }), /商品价格缺失/);
});
