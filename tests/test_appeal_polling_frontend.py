import shutil
import subprocess
from pathlib import Path

import pytest


def test_polling_retries_from_cursor_and_pauses_hidden_pages():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for browser polling tests")
    source = (Path(__file__).resolve().parents[1] / "bit/static/appeal-polling.js").read_text()
    harness = r"""
const assert = require('node:assert/strict');
const delays = [];
const listeners = new Set();
const document = {
    hidden: false,
    addEventListener: (_, fn) => listeners.add(fn),
    removeEventListener: (_, fn) => listeners.delete(fn),
};
const window = {
    setTimeout(fn, ms) {
        if (ms === 15000) return null; // no real network timeout in this test
        delays.push(ms);
        return setTimeout(() => {
            if (document.hidden) document.hidden = false;
            fn();
        }, 0);
    },
    clearTimeout,
};
let calls = [], active = 0, peak = 0;
let fetch = async url => {
    calls.push(url);
    active++; peak = Math.max(peak, active);
    await Promise.resolve();
    active--;
    if (calls.length === 2) throw new Error('temporary disconnect');
    return {ok: true, status: 200, json: async () => ({status: 'success', data: calls.length === 1
        ? {log: 'first', next_after: 10, done: false, has_more: false, status: 'running'}
        : {log: 'last', next_after: 11, done: true, has_more: false, status: 'error'}
    })};
};
"""
    assertions = r"""
(async () => {
    const logs = [], states = [];
    let retries = 0, recovered = 0;
    document.hidden = true;
    const result = await window.ZeshunAppealPolling.follow('job-1', {
        onLog: text => logs.push(text), onState: state => states.push(state.status),
        onRetry: () => retries++, onRecovered: () => recovered++,
    });
    assert.equal(result.status, 'error');
    assert.deepEqual(logs, ['first', 'last']);
    assert.deepEqual(states, ['running', 'error']);
    assert.equal(retries, 1); assert.equal(recovered, 1);
    assert.equal(peak, 1);
    assert.equal(delays[0], 30000);
    assert.ok(delays[1] >= 3000 && delays[1] < 4000);
    assert.deepEqual(calls.map(url => url.split('after=')[1]), ['0', '10', '10']);
    assert.equal(listeners.size, 0);
    fetch = async () => ({status: 403, ok: false});
    await assert.rejects(window.ZeshunAppealPolling.follow('job-2', {onLog() {}}),
        error => error.permanent === true);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-"], input=harness + source + assertions, text=True,
                   capture_output=True, check=True, timeout=15)
