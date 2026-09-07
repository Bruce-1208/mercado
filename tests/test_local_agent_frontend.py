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


def test_daily_task_filters_group_statuses_and_keep_agent_computers_separate():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to exercise task filter JavaScript")
    template = (Path(__file__).resolve().parents[1] / "bit/templates/index.html").read_text(
        encoding="utf-8"
    )
    start = template.index("        function dailyTaskStatusCategory(task)")
    end = template.index("        function syncDailyTaskComputerFilter(tasks)", start)
    helpers = template[start:end]
    script = """
const assert = require('node:assert/strict');
""" + helpers + """
assert.equal(dailyTaskStatusCategory({status: 'queued', running: true}), 'queued');
assert.equal(dailyTaskStatusCategory({status: 'starting', running: true}), 'running');
assert.equal(dailyTaskStatusCategory({status: 'running', running: true, stop_requested: true}), 'stopping');
assert.equal(dailyTaskStatusCategory({status: 'success', running: false}), 'completed');
assert.equal(dailyTaskStatusCategory({status: 'completed', running: false}), 'completed');
assert.equal(dailyTaskStatusCategory({status: 'partial', running: false}), 'partial');
assert.equal(dailyTaskStatusCategory({status: 'stopped', running: false}), 'stopped');
assert.equal(dailyTaskStatusCategory({status: 'error', running: false}), 'error');
assert.equal(dailyTaskComputerKey({execution_target: 'server'}), 'server');
assert.equal(dailyTaskComputerKey({execution_target: 'local'}), 'local');
assert.equal(
    dailyTaskComputerKey({execution_target: 'agent', agent_id: 'pc-a', agent_name: '电脑 A'}),
    'agent:pc-a',
);
assert.equal(
    dailyTaskComputerKey({execution_target: 'agent', agent_id: 'pc-b', agent_name: '电脑 A'}),
    'agent:pc-b',
);
"""
    subprocess.run([node, "-"], input=script, encoding="utf-8", check=True, capture_output=True)
