import shutil
import subprocess
from pathlib import Path

import pytest


def test_agent_requests_use_public_routes_instead_of_loopback():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to exercise browser routing")
    template = (Path(__file__).resolve().parents[1] / "bit/templates/index.html").read_text(encoding="utf-8")
    start = template.index("        function fetchExecutionTarget(")
    end = template.index("        async function refreshLocalExecutorHint", start)
    script = '''
const assert = require('node:assert/strict');
let workbenchRuntimeRole = 'server';
const window = {location: {origin: 'https://workbench.example'}};
const calls = [];
function fetch(url, options) { calls.push({url: new URL(url, window.location.origin), options}); }
function fetchLocalExecutor() { throw new Error('Unexpected loopback request'); }
''' + template[start:end] + '''
fetchExecutionTarget('agent', '/api/tasks/daily/start', {method: 'POST'}, 'tasks.execute');
fetchExecutionTarget('agent', '/api/tasks/daily/status?task_id=daily-job', {}, 'tasks.view');
fetchExecutionTarget('agent', '/api/tasks/daily/stop', {method: 'POST'}, 'tasks.execute');
assert.equal(calls.length, 3);
for (const call of calls) {
    assert.equal(call.url.origin, window.location.origin);
    assert.equal(call.url.searchParams.get('execution_target'), 'agent');
    assert.ok(!call.url.pathname.includes('local-executor'));
}
assert.equal(calls[1].url.searchParams.get('task_id'), 'daily-job');
workbenchRuntimeRole = 'client';
assert.throws(() => fetchExecutionTarget('agent', '/api/tasks/daily/start'));
assert.equal(calls.length, 3);
'''
    subprocess.run([node, "-"], input=script, encoding="utf-8", check=True, capture_output=True)
