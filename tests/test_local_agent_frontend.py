import shutil
import subprocess
from pathlib import Path

import pytest


def test_console_offers_windows_and_macos_agent_downloads():
    template = (Path(__file__).resolve().parents[1] / "bit/templates/index.html").read_text(
        encoding="utf-8"
    )

    assert "下载 Windows Agent" in template
    assert "下载 macOS Agent" in template
    assert "/api/local-agents/download?platform=windows" in template
    assert "/api/local-agents/download?platform=macos" in template


def test_daily_task_shows_each_agent_logged_out_shop_status():
    template = (Path(__file__).resolve().parents[1] / "bit/templates/index.html").read_text(
        encoding="utf-8"
    )

    assert 'data-role="login-status"' in template
    assert "function updateDailyTaskLoginStatus(card, task)" in template
    assert "dailyTaskLogoutInfo(task)" in template
    assert "logged_out_shops" in template
    assert "退出登录 ${shops.length} 家" in template


def test_daily_task_defaults_to_active_cards_without_rendering_history():
    template = (Path(__file__).resolve().parents[1] / "bit/templates/index.html").read_text(
        encoding="utf-8"
    )

    assert '<option value="active" selected>运行中</option>' in template
    assert 'class="daily-task-history-panel" style="margin-top: 20px;" hidden' in template
    assert 'dailyTaskHistoryPanel.hidden = statusFilter === "active"' in template
    assert '&& statusFilter !== "active"' in template


def test_daily_task_logout_status_is_scoped_to_its_execution_computer():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to exercise task login-status JavaScript")
    template = (Path(__file__).resolve().parents[1] / "bit/templates/index.html").read_text(
        encoding="utf-8"
    )
    start = template.index("        function dailyTaskLogoutInfo(task)")
    end = template.index("        function updateDailyTaskLoginStatus", start)
    helper = template[start:end]
    script = """
const assert = require('node:assert/strict');
let dailyTaskLoginStatusError = '';
let dailyTaskAgentStatuses = [{
    agent_id: 'pc-a', name: '电脑 A',
    logged_out_shops: [{window_name: '店铺 A'}],
}];
let dailyTaskUnassignedLogoutShops = [
    {window_name: '店铺 B', execution_target: 'server'},
    {window_name: '店铺 C', execution_target: 'local'},
];
""" + helper + """
assert.deepEqual(
    dailyTaskLogoutInfo({execution_target: 'agent', agent_id: 'pc-a'}).shops.map(x => x.window_name),
    ['店铺 A'],
);
assert.deepEqual(
    dailyTaskLogoutInfo({execution_target: 'server'}).shops.map(x => x.window_name),
    ['店铺 B'],
);
assert.deepEqual(
    dailyTaskLogoutInfo({execution_target: 'local'}).shops.map(x => x.window_name),
    ['店铺 C'],
);
assert.equal(
    dailyTaskLogoutInfo({execution_target: 'agent', agent_id: 'missing'}).unavailable,
    true,
);
"""
    subprocess.run([node, "-"], input=script, encoding="utf-8", check=True, capture_output=True)


def test_daily_task_stop_request_stays_stopping_until_worker_acknowledges():
    template = (Path(__file__).resolve().parents[1] / "bit/templates/index.html").read_text(
        encoding="utf-8"
    )

    assert '? "停止中"' in template
    assert 'stopButton.textContent = "停止中"' in template
    assert 'stoppingOrPending ? "已停止"' not in template


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
assert.equal(dailyTaskStatusCategory({status: 'running', running: true, stop_requested: true}), 'running');
assert.equal(dailyTaskStatusCategory({status: 'success', running: false}), 'completed');
assert.equal(dailyTaskStatusCategory({status: 'completed', running: false}), 'completed');
assert.equal(dailyTaskStatusCategory({status: 'partial', running: false}), 'partial');
assert.equal(dailyTaskStatusCategory({status: 'stopped', running: false}), 'stopped');
assert.equal(dailyTaskStatusCategory({status: 'error', running: false}), 'error');
assert.equal(dailyTaskMatchesStatusFilter({status: 'queued', running: true}, 'active'), true);
assert.equal(dailyTaskMatchesStatusFilter({status: 'stopping', running: true}, 'active'), true);
assert.equal(dailyTaskMatchesStatusFilter({status: 'stopping', running: true}, 'stopped'), false);
assert.equal(dailyTaskMatchesStatusFilter({status: 'success', running: false}, 'active'), false);
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
