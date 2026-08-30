import pytest
from collections import deque
from pathlib import Path

from bit import bit_interface


def test_mercado_login_status_only_returns_latest_task_log(monkeypatch):
    with bit_interface._mercado_login_task_lock:
        previous_state = dict(bit_interface._mercado_login_task_state)
        previous_tasks = dict(bit_interface._mercado_login_tasks)
        previous_logs = deque(bit_interface._mercado_login_task_logs, maxlen=800)
        bit_interface._mercado_login_tasks.clear()
        bit_interface._mercado_login_task_logs.clear()
        bit_interface._mercado_login_task_logs.extend(("旧任务\n", "最新任务\n"))
        bit_interface._mercado_login_tasks["old"] = {
            "task_id": "old",
            "running": False,
            "started_at": "2026-07-26 10:00:00",
            "finished_at": "2026-07-26 10:01:00",
            "status": "success",
            "message": "旧任务完成",
            "target": "旧任务",
            "window_ids": ["window-old"],
            "scope": "windows",
            "scope_keys": ("window:window-old",),
            "log_chunks": deque(("旧任务\n",), maxlen=800),
        }
        bit_interface._mercado_login_tasks["latest"] = {
            "task_id": "latest",
            "running": True,
            "started_at": "2026-07-26 10:02:00",
            "finished_at": "",
            "status": "running",
            "message": "最新任务运行中",
            "target": "最新任务",
            "window_ids": ["window-latest"],
            "scope": "windows",
            "scope_keys": ("window:window-latest",),
            "log_chunks": deque(("最新任务\n",), maxlen=800),
        }
    monkeypatch.setattr(bit_interface, "get_lock_owner", lambda key: {})
    try:
        snapshot = bit_interface._mercado_login_task_snapshot()
    finally:
        with bit_interface._mercado_login_task_lock:
            bit_interface._mercado_login_tasks.clear()
            bit_interface._mercado_login_tasks.update(previous_tasks)
            bit_interface._mercado_login_task_logs.clear()
            bit_interface._mercado_login_task_logs.extend(previous_logs)
            bit_interface._mercado_login_task_state.clear()
            bit_interface._mercado_login_task_state.update(previous_state)

    assert snapshot["log"] == "最新任务\n"
    assert "旧任务" not in snapshot["log"]
    assert snapshot["current_task_id"] == "latest"
    assert snapshot["current_task_target"] == "最新任务"
    assert snapshot["can_stop"] is True
    assert all("log_chunks" not in task for task in snapshot["tasks"])


def test_request_mercado_login_task_stop_targets_only_requested_task(monkeypatch):
    task_id = "stop-login-task"
    fake_process = object()
    started = []
    logs = []
    with bit_interface._mercado_login_task_lock:
        previous_state = dict(bit_interface._mercado_login_task_state)
        previous_tasks = dict(bit_interface._mercado_login_tasks)
        previous_processes = dict(bit_interface._mercado_login_task_processes)
        bit_interface._mercado_login_tasks.clear()
        bit_interface._mercado_login_task_processes.clear()
        bit_interface._mercado_login_tasks[task_id] = {
            "task_id": task_id,
            "running": True,
            "started_at": "2026-07-26 10:02:00",
            "finished_at": "",
            "status": "running",
            "message": "四季如春 登录任务运行中",
            "target": "四季如春",
            "window_id": "window-1",
            "window_ids": ["window-1"],
            "scope": "windows",
            "scope_keys": ("window:window-1",),
            "log_chunks": deque(maxlen=800),
        }
        bit_interface._mercado_login_task_processes[task_id] = fake_process
    monkeypatch.setattr(bit_interface, "get_lock_owner", lambda key: {})
    monkeypatch.setattr(
        bit_interface,
        "_append_mercado_login_task_log",
        lambda text, task_id="": logs.append((task_id, text)),
    )
    monkeypatch.setattr(
        bit_interface,
        "_start_mercado_login_stop_worker",
        lambda requested_task_id, process: started.append(
            (requested_task_id, process)
        ),
    )
    try:
        stopped, snapshot = bit_interface.request_mercado_login_task_stop(task_id)
    finally:
        with bit_interface._mercado_login_task_lock:
            bit_interface._mercado_login_tasks.clear()
            bit_interface._mercado_login_tasks.update(previous_tasks)
            bit_interface._mercado_login_task_processes.clear()
            bit_interface._mercado_login_task_processes.update(previous_processes)
            bit_interface._mercado_login_task_state.clear()
            bit_interface._mercado_login_task_state.update(previous_state)

    assert stopped is True
    assert snapshot["status"] == "stopping"
    assert snapshot["current_task_stopping"] is True
    assert snapshot["can_stop"] is True
    assert started == [(task_id, fake_process)]
    assert logs[0][0] == task_id
    assert "正在终止" in logs[0][1]


def test_terminate_mercado_login_process_tree_stops_process_group(monkeypatch):
    class FakeProcess:
        pid = 456

        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            raise AssertionError("进程组任务不应只终止父进程")

        def kill(self):
            raise AssertionError("SIGTERM 已结束任务，不应执行强制终止")

    process = FakeProcess()
    signals = []
    monkeypatch.setattr(bit_interface.os, "getpgid", lambda pid: pid)

    def fake_killpg(pid, sent_signal):
        signals.append((pid, sent_signal))
        process.returncode = -sent_signal

    monkeypatch.setattr(bit_interface.os, "killpg", fake_killpg)

    bit_interface._terminate_mercado_login_process_tree(process)

    assert signals == [(456, bit_interface.signal.SIGTERM)]


def test_mercado_login_console_command_supports_all_and_single_shop():
    all_command = bit_interface._build_mercado_login_command(workers=3)
    single_command = bit_interface._build_mercado_login_command(shop_name="四季如春")
    selected_command = bit_interface._build_mercado_login_command(
        workers=2,
        window_ids=["window-1", "window-2", "window-3", "window-4", "window-1"],
    )
    capped_all_command = bit_interface._build_mercado_login_command(workers=99)

    assert all_command[0] == bit_interface.sys.executable
    assert "--all-active-login" in all_command
    assert all_command[all_command.index("--workers") + 1] == "3"
    assert "--manual-login-wait-seconds" not in all_command
    assert "--keep-browser-open" not in all_command
    assert single_command[single_command.index("--shop") + 1] == "四季如春"
    assert "--auto-login" in single_command
    assert "--keep-browser-open" in single_command
    assert single_command[single_command.index("--manual-login-wait-seconds") + 1] == "1200"
    assert selected_command.count("--window-id") == 4
    assert selected_command[selected_command.index("--workers") + 1] == "2"
    assert selected_command[selected_command.index("--manual-login-wait-seconds") + 1] == "1200"
    assert "--all-active-login" not in selected_command
    assert "--no-email" in selected_command
    assert "--keep-browser-open" in selected_command
    assert capped_all_command[capped_all_command.index("--workers") + 1] == "10"


def test_mercado_login_console_runs_distinct_windows_asynchronously(monkeypatch):
    started_threads = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            started_threads.append(self)

    with bit_interface._mercado_login_task_lock:
        previous = dict(bit_interface._mercado_login_task_state)
        previous_tasks = dict(bit_interface._mercado_login_tasks)
        bit_interface._mercado_login_tasks.clear()
        bit_interface._mercado_login_task_state["running"] = False
    monkeypatch.setattr(bit_interface.threading, "Thread", FakeThread)
    monkeypatch.setattr(bit_interface, "get_lock_owner", lambda key: {})
    try:
        first_started, first_state = bit_interface.start_mercado_login_console_job(
            shop_name="四季如春",
            window_id="window-1",
        )
        second_started, second_state = bit_interface.start_mercado_login_console_job(
            shop_name="龙凤呈祥",
            window_id="window-2",
        )
        duplicate_started, duplicate_state = bit_interface.start_mercado_login_console_job(
            shop_name="四季如春",
            window_id="window-1",
        )
        all_started, all_state = bit_interface.start_mercado_login_console_job()
    finally:
        with bit_interface._mercado_login_task_lock:
            bit_interface._mercado_login_tasks.clear()
            bit_interface._mercado_login_tasks.update(previous_tasks)
            bit_interface._mercado_login_task_state.clear()
            bit_interface._mercado_login_task_state.update(previous)

    assert first_started is True
    assert second_started is True
    assert duplicate_started is False
    assert all_started is False
    assert first_state["running_count"] == 1
    assert second_state["running_count"] == 2
    assert set(second_state["active_window_ids"]) == {"window-1", "window-2"}
    assert "已有自动登录任务" in duplicate_state["message"]
    assert "不能同时运行" in all_state["message"]
    assert len(started_threads) == 2


def test_mercado_login_console_detects_job_from_another_process(monkeypatch):
    with bit_interface._mercado_login_task_lock:
        previous = dict(bit_interface._mercado_login_task_state)
        previous_tasks = dict(bit_interface._mercado_login_tasks)
        bit_interface._mercado_login_tasks.clear()
        bit_interface._mercado_login_task_state["running"] = False
    monkeypatch.setattr(
        bit_interface,
        "get_lock_owner",
        lambda key: {
            "owner": "bit_mercado_login:全部未忽略店铺",
            "pid": 456,
            "metadata": {"target": "全部未忽略店铺"},
        },
    )
    try:
        started, state = bit_interface.start_mercado_login_console_job(
            shop_name="四季如春",
            window_id="window-1",
        )
        assert bit_interface._mercado_login_task_state["running"] is False
    finally:
        with bit_interface._mercado_login_task_lock:
            bit_interface._mercado_login_tasks.clear()
            bit_interface._mercado_login_tasks.update(previous_tasks)
            bit_interface._mercado_login_task_state.clear()
            bit_interface._mercado_login_task_state.update(previous)

    assert started is False
    assert state["running"] is True
    assert state["pid"] == 456
    assert "另一个进程" in state["message"]


def test_shop_status_can_restart_single_shop_auto_login(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        bit_interface,
        "db_get_window_anomalies",
        lambda active_only=True, limit=500: {
            "total": 1,
            "rows": [
                {
                    "window_id": "window-1",
                    "window_name": "四季如春",
                    "anomaly_type": "需要人机验证",
                }
            ],
        },
    )

    def fake_start(shop_name="", window_id="", workers=3):
        captured.update(
            shop_name=shop_name,
            window_id=window_id,
            workers=workers,
        )
        return True, {
            "running": True,
            "status": "running",
            "target": shop_name,
        }

    monkeypatch.setattr(
        bit_interface,
        "start_mercado_login_console_job",
        fake_start,
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}

    response = client.post(
        "/api/window-anomalies/mercado-login/start",
        json={"window_id": "window-1"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "success"
    assert captured == {
        "shop_name": "四季如春",
        "window_id": "window-1",
        "workers": 3,
    }


def test_shop_status_starts_selected_shops_as_one_worker_limited_batch(monkeypatch):
    captured = []
    monkeypatch.setattr(
        bit_interface,
        "db_get_window_anomalies",
        lambda active_only=True, limit=500: {
            "total": 2,
            "rows": [
                {
                    "window_id": "window-1",
                    "window_name": "四季如春",
                    "anomaly_type": "需要人机验证",
                },
                {
                    "window_id": "window-2",
                    "window_name": "龙凤呈祥",
                    "anomaly_type": "需要人机验证",
                },
            ],
        },
    )

    def fake_start(**kwargs):
        captured.append(kwargs)
        return True, {
            "running": True,
            "status": "running",
            "target": "所选 2 家店铺",
            "started_task_id": "task-selected-batch",
        }

    monkeypatch.setattr(
        bit_interface,
        "start_mercado_login_console_job",
        fake_start,
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}

    response = client.post(
        "/api/window-anomalies/mercado-login/start",
        json={"window_ids": ["window-2", "window-1", "window-2"], "workers": 1},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "success"
    assert captured == [{
        "selected_shops": [
            {"window_id": "window-2", "window_name": "龙凤呈祥"},
            {"window_id": "window-1", "window_name": "四季如春"},
        ],
        "workers": 1,
    }]
    assert response.get_json()["data"]["started_count"] == 2
    assert response.get_json()["data"]["workers"] == 1
    assert response.get_json()["data"]["started_task_ids"] == [
        "task-selected-batch"
    ]


def test_shop_status_all_shops_uses_requested_worker_count(monkeypatch):
    captured = {}

    def fake_start(**kwargs):
        captured.update(kwargs)
        return True, {
            "running": True,
            "status": "running",
            "target": "全部未忽略店铺",
        }

    monkeypatch.setattr(bit_interface, "start_mercado_login_console_job", fake_start)
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}

    response = client.post(
        "/api/window-anomalies/mercado-login/start",
        json={"workers": 7},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "success"
    assert captured == {"shop_name": "", "window_id": "", "workers": 7}


def test_resolve_shop_status_stops_only_the_matching_login_task(monkeypatch):
    captured = []
    monkeypatch.setattr(
        bit_interface,
        "request_mercado_login_window_tasks_stop",
        lambda window_id: captured.append(window_id) or ["task-window-1"],
    )
    monkeypatch.setattr(
        bit_interface,
        "db_resolve_window_anomaly",
        lambda window_id: 1,
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}

    response = client.post("/api/window-anomalies/window-1/resolve")

    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "affected": 1,
        "stopped_count": 1,
        "stopped_task_ids": ["task-window-1"],
    }
    assert captured == ["window-1"]


def test_stop_window_login_tasks_does_not_stop_other_or_grouped_tasks(monkeypatch):
    stopped = []
    with bit_interface._mercado_login_task_lock:
        previous_tasks = dict(bit_interface._mercado_login_tasks)
        bit_interface._mercado_login_tasks.clear()
        bit_interface._mercado_login_tasks.update(
            {
                "matching": {
                    "running": True,
                    "window_ids": ["window-1"],
                },
                "other": {
                    "running": True,
                    "window_ids": ["window-2"],
                },
                "grouped": {
                    "running": True,
                    "window_ids": ["window-1", "window-2"],
                },
                "finished": {
                    "running": False,
                    "window_ids": ["window-1"],
                },
            }
        )
    monkeypatch.setattr(
        bit_interface,
        "request_mercado_login_task_stop",
        lambda task_id: (stopped.append(task_id) or True, {}),
    )
    try:
        stopped_task_ids = bit_interface.request_mercado_login_window_tasks_stop(
            "window-1"
        )
    finally:
        with bit_interface._mercado_login_task_lock:
            bit_interface._mercado_login_tasks.clear()
            bit_interface._mercado_login_tasks.update(previous_tasks)

    assert stopped_task_ids == ["matching"]
    assert stopped == ["matching"]


def test_shop_status_ui_uses_selected_worker_count_for_batch():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert "JSON.stringify({window_ids: windowIds, workers})" in template
    assert "lastMercadoLoginWindowRefreshAt" in template
    assert "Date.now() - lastMercadoLoginWindowRefreshAt >= 4000" in template
    assert "await loadWindowAnomalies()" in template
    assert "已人工处理并移除" in template
    assert "await loadMercadoLoginStatus();" in template
    assert '{cache: "no-store"}' in template


def test_shop_status_exposes_stop_current_login_task_button():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="stop-mercado-login-btn"' in template
    assert 'onclick="stopMercadoLoginTask()"' in template
    assert "async function stopMercadoLoginTask()" in template
    assert 'fetch("/api/window-anomalies/mercado-login/stop"' in template


def test_shop_status_stop_login_api_submits_current_task(monkeypatch):
    captured = []
    monkeypatch.setattr(
        bit_interface,
        "request_mercado_login_task_stop",
        lambda task_id: (
            captured.append(task_id) or True,
            {
                "running": True,
                "status": "stopping",
                "current_task_id": task_id,
                "current_task_stopping": True,
            },
        ),
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}

    response = client.post(
        "/api/window-anomalies/mercado-login/stop",
        json={"task_id": "current-login-task"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["status"] == "stopping"
    assert captured == ["current-login-task"]


def test_shop_status_rejects_selected_shop_that_is_no_longer_pending(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "db_get_window_anomalies",
        lambda active_only=True, limit=500: {
            "total": 1,
            "rows": [
                {
                    "window_id": "window-1",
                    "window_name": "四季如春",
                    "anomaly_type": "需要人机验证",
                }
            ],
        },
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}

    response = client.post(
        "/api/window-anomalies/mercado-login/start",
        json={"window_ids": ["window-1", "window-recovered"]},
    )

    assert response.status_code == 404
    assert "已恢复" in response.get_json()["message"]


def test_shop_status_enriches_salesperson_from_browser_configs(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "db_list_bit_browser_configs",
        lambda include_ignored=True: [
            {
                "window_id": "window-1",
                "shop_name": "四季如春",
                "salesperson": "张三",
                "email": "spring@example.com",
            },
            {
                "window_id": "window-2",
                "shop_name": "龙凤呈祥",
                "salesperson": "李四",
                "email": "dragon@example.com",
            },
        ],
    )

    data = bit_interface.enrich_window_anomaly_salespersons(
        {
            "total": 3,
            "rows": [
                {"window_id": "window-1", "window_name": "四季如春"},
                {"window_id": "window-2", "window_name": "历史店铺名"},
                {
                    "window_id": "window-3",
                    "window_name": "已有归属人",
                    "salesperson": "王五",
                    "email": "existing@example.com",
                },
            ],
        }
    )

    assert [row["salesperson"] for row in data["rows"]] == ["张三", "李四", "王五"]
    assert [row["email"] for row in data["rows"]] == [
        "spring@example.com",
        "dragon@example.com",
        "existing@example.com",
    ]


def test_shop_status_only_returns_human_verification_rows(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "db_get_window_anomalies",
        lambda active_only=True, limit=500: {
            "total": 4,
            "rows": [
                {
                    "window_id": "window-captcha",
                    "window_name": "人机验证店铺",
                    "anomaly_type": "需要人机验证",
                    "reason": "检测到人机验证，需要人工处理",
                },
                {
                    "window_id": "window-legacy-captcha",
                    "window_name": "历史人机验证店铺",
                    "anomaly_type": "需要登录",
                    "reason": "自动登录遇到 captcha",
                },
                {
                    "window_id": "window-code",
                    "window_name": "验证码店铺",
                    "anomaly_type": "需要验证码",
                    "reason": "需要邮箱验证码",
                },
                {
                    "window_id": "window-limit",
                    "window_name": "限频店铺",
                    "anomaly_type": "美客多限频",
                    "reason": "访问限频",
                },
            ],
        },
    )
    monkeypatch.setattr(
        bit_interface,
        "db_list_bit_browser_configs",
        lambda include_ignored=True: [],
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}

    response = client.get("/api/window-anomalies?active_only=1&limit=500")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["total"] == 2
    assert [row["window_id"] for row in data["rows"]] == [
        "window-captcha",
        "window-legacy-captcha",
    ]


def test_shop_status_ui_uses_single_shop_auto_login_action():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert '>店铺状态</button>' in template
    assert "<h2>店铺状态</h2>" in template
    assert "重新检测" in template
    assert "startSingleShopAutoLogin" in template
    assert "startSelectedShopsAutoLogin" in template
    assert "window-anomaly-select-all" in template
    assert "重新检测所选店铺" in template
    assert 'id="mercado-login-workers" type="number" min="1" max="10" value="3"' in template
    assert "selectedMercadoLoginWorkerCount" in template
    assert "JSON.stringify({window_ids: windowIds, workers})" in template
    assert "<th>店铺归属人</th>" in template
    assert "<th>邮箱</th>" in template
    assert "row.salesperson" in template
    assert "row.email" in template
    assert 'class="reason-cell" data-tooltip=' in template
    assert ">重新检测</button>" in template


def test_reputation_rate_cells_use_requested_warning_thresholds():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert "function parseReputationPercentage(value)" in template
    assert 'if (rate > 1.5) return "rate-red"' in template
    assert 'if (rate > 1) return "rate-yellow"' in template
    assert 'if (rate > 10) return "rate-red"' in template
    assert 'if (rate > 8) return "rate-yellow"' in template
    assert 'metric === "取消率" && rate > 1' in template
    assert 'renderReputationRateCell("投诉率", row["投诉率"])' in template
    assert 'renderReputationRateCell("延误率", row["延误率"])' in template
    assert 'renderReputationRateCell("取消率", row["取消率"])' in template
    assert ".reputation-rate-cell.rate-yellow" in template
    assert ".reputation-rate-cell.rate-red" in template


def test_reputation_traffic_change_cells_use_thirty_percent_threshold():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert "function reputationTrafficChangeClass(direction, value)" in template
    assert "Math.abs(signedRate) <= 30" in template
    assert 'return "change-green"' in template
    assert 'return "change-red"' in template
    assert 'renderReputationTrafficChangeCell(row["增加或减少"], row["近七天变化率"])' in template
    assert ".reputation-change-cell.change-green" in template
    assert ".reputation-change-cell.change-red" in template


def test_daily_task_console_exposes_mixed_mode_salesperson_and_rate_threshold():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="daily-task-appeal-type"' in template
    assert '<option value="侵权" selected>侵权</option>' in template
    assert '<option value="延误率">延误率</option>' in template
    assert '<option value="取消率">取消率</option>' in template
    assert '<option value="投诉">投诉</option>' in template
    assert '<option value="混合模式">混合模式（其他任务后执行侵权）</option>' in template
    assert 'id="daily-task-salesperson"' in template
    assert '<option value="">所有业务员</option>' in template
    assert 'id="daily-task-only-active"' not in template
    assert 'id="daily-task-min-rate"' in template
    assert "appeal_type: document.getElementById(\"daily-task-appeal-type\").value" in template
    assert "min_rate: `${minRatePercent}%`" in template


@pytest.mark.parametrize("appeal_type", ["侵权", "延误率", "取消率", "投诉", "混合模式"])
def test_build_daily_task_params_accepts_all_appeal_modes(appeal_type):
    params = bit_interface.build_daily_task_params(
        {"appeal_type": appeal_type, "min_rate": "7.5%"}
    )

    assert params["appeal_type"] == appeal_type
    assert params["min_rate"] == pytest.approx(0.075)


def test_build_daily_task_params_supports_one_or_all_salespeople():
    selected = bit_interface.build_daily_task_params(
        {"salespeople": ["张三", "张三"]}
    )
    all_salespeople = bit_interface.build_daily_task_params(
        {"salesperson": "所有业务员"}
    )

    assert selected["salespeople"] == ["张三"]
    assert all_salespeople["salespeople"] == []
    assert "only_active" not in selected


def test_daily_task_options_returns_all_active_salespeople(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "_workbench_backend",
        lambda name: [
            {"display_name": "张三", "is_active": True},
            {"display_name": "停用人员", "is_active": False},
        ],
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {"site_settings": [{"salesperson": "李四"}]},
            ]
        },
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"id": 1, "username": "tester"}

    response = client.get("/api/tasks/daily/options")

    assert response.status_code == 200
    assert response.get_json()["data"]["salespeople"] == ["张三", "李四"]


def test_build_daily_task_params_rejects_invalid_appeal_settings():
    with pytest.raises(ValueError, match="不支持的申诉类型"):
        bit_interface.build_daily_task_params({"appeal_type": "退款"})
    with pytest.raises(ValueError, match="min_rate"):
        bit_interface.build_daily_task_params({"min_rate": "101%"})


@pytest.mark.parametrize(
    ("mode", "target_name"),
    [("once", "run_ai_appeal_once"), ("loop", "loop_ai_appeal")],
)
def test_daily_task_console_dispatches_selected_appeal_type(
    monkeypatch,
    mode,
    target_name,
):
    calls = []

    class FakeTaskLock:
        def __init__(self):
            self.released = False

        def release(self):
            self.released = True

    def capture(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(bit_interface.bit_daily_task, target_name, capture)
    monkeypatch.setattr(
        bit_interface,
        "_daily_task_state",
        {"running": True, "status": "running"},
    )
    params = bit_interface.build_daily_task_params(
        {
            "mode": mode,
            "appeal_type": "延误率",
            "min_rate": "7%",
            "stop_after_minutes": 0,
        }
    )
    task_lock = FakeTaskLock()

    bit_interface.run_daily_task_job(params, task_lock)

    assert calls[0][0] == ("延误率",)
    assert calls[0][1]["min_rate"] == pytest.approx(0.07)
    assert task_lock.released is True
    assert bit_interface._daily_task_state["status"] == "success"


def test_resolve_selected_appeal_sites_and_remove_duplicates():
    assert bit_interface.resolve_appeal_sites(["墨西哥", "巴西", "墨西哥"]) == (
        "墨西哥", "巴西"
    )
    assert bit_interface.resolve_appeal_sites("巴西") == ("巴西",)
    with pytest.raises(ValueError, match="至少选择一个站点"):
        bit_interface.resolve_appeal_sites([])
    with pytest.raises(ValueError, match="不支持的站点"):
        bit_interface.resolve_appeal_sites(["全部站点"])


def test_resolve_selected_appeal_forms_and_use_fixed_execution_order():
    assert bit_interface.resolve_appeal_forms(["取消率", "延误", "取消率"]) == (
        "延误", "取消率"
    )
    assert bit_interface.resolve_appeal_forms("投诉") == ("投诉",)
    with pytest.raises(ValueError, match="至少选择一个任务类型"):
        bit_interface.resolve_appeal_forms([])
    with pytest.raises(ValueError, match="不支持的任务类型"):
        bit_interface.resolve_appeal_forms(["未知类型"])


def test_normalize_appeal_loop_count():
    assert bit_interface.normalize_appeal_loop_count(None) == 10
    assert bit_interface.normalize_appeal_loop_count("10") == 10
    assert bit_interface.normalize_appeal_loop_count("20") == 20
    assert bit_interface.normalize_appeal_loop_count("50") == 50
    assert bit_interface.normalize_appeal_loop_count(0) == 0
    assert bit_interface.normalize_appeal_loop_count("permanent") == 0
    assert bit_interface.normalize_appeal_loop_count("永久") == 0
    with pytest.raises(ValueError, match="循环次数只支持"):
        bit_interface.normalize_appeal_loop_count("5")


@pytest.mark.parametrize(
    ("mode", "expected_interval"),
    [("AI客服", 60), ("人工客服", 600)],
)
@pytest.mark.parametrize("form", ["侵权", "延误", "取消率", "投诉"])
def test_selected_sites_run_sequentially_for_every_mode_and_form(
    monkeypatch,
    mode,
    expected_interval,
    form,
):
    calls = []
    sleeps = []

    class ImmediateThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    class Lease:
        def acquire(self, timeout=0):
            return True

        def release(self):
            return None

    class StopAfterFirstRound(Exception):
        pass

    def stop_on_sleep(seconds):
        sleeps.append(seconds)
        raise StopAfterFirstRound

    monkeypatch.setattr(bit_interface.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(bit_interface, "getWindowidByName", lambda name: "window-id")
    monkeypatch.setattr(bit_interface, "create_window_lease", lambda *args, **kwargs: Lease())
    monkeypatch.setattr(
        bit_interface.bit_appeal_ai,
        "shensu",
        lambda name, site, appeal_form, message: calls.append(
            ("AI客服", site, appeal_form)
        ),
    )
    monkeypatch.setattr(
        bit_interface,
        "shensu",
        lambda name, site, appeal_form, message, service_mode: calls.append(
            (service_mode, site, appeal_form)
        ),
    )
    monkeypatch.setattr(bit_interface.time, "sleep", stop_on_sleep)
    monkeypatch.setattr(
        bit_interface,
        "APPEAL_STREAM_HEARTBEAT_SECONDS",
        bit_interface.APPEAL_ROUND_INTERVAL_SECONDS,
    )

    selected_sites = ("墨西哥", "哥伦比亚", "乌拉圭")
    stream = bit_interface.shensu_logic("测试店铺", selected_sites, form, "", mode)
    output = []
    with pytest.raises(StopAfterFirstRound):
        while True:
            output.append(next(stream))

    assert calls == [
        (mode, site, form)
        for site in selected_sites
    ]
    assert sleeps == [expected_interval]
    assert any(
        f"等待 {expected_interval // 60} 分钟后开始下一轮" in line
        for line in output
    )


def test_selected_forms_run_in_fixed_order_within_one_round(monkeypatch):
    calls = []

    class ImmediateThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    class Lease:
        def acquire(self, timeout=0):
            return True

        def release(self):
            return None

    class StopAfterFirstRound(Exception):
        pass

    monkeypatch.setattr(bit_interface.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(bit_interface, "getWindowidByName", lambda name: "window-id")
    monkeypatch.setattr(bit_interface, "create_window_lease", lambda *args, **kwargs: Lease())
    monkeypatch.setattr(
        bit_interface.bit_appeal_ai,
        "shensu",
        lambda name, site, appeal_form, message: calls.append((site, appeal_form)),
    )
    monkeypatch.setattr(
        bit_interface.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(StopAfterFirstRound()),
    )
    monkeypatch.setattr(
        bit_interface,
        "APPEAL_STREAM_HEARTBEAT_SECONDS",
        bit_interface.APPEAL_ROUND_INTERVAL_SECONDS,
    )

    selected_sites = ("墨西哥", "智利")
    stream = bit_interface.shensu_logic(
        "测试店铺",
        selected_sites,
        ("取消率", "延误"),
        "",
        "AI客服",
    )
    output = []
    with pytest.raises(StopAfterFirstRound):
        while True:
            output.append(next(stream))

    assert calls == [
        ("墨西哥", "延误"),
        ("智利", "延误"),
        ("墨西哥", "取消率"),
        ("智利", "取消率"),
    ]
    assert any("延误 → 取消率" in line for line in output)


def test_stream_task_output_sends_heartbeat_while_worker_is_quiet():
    class QuietThenDoneQueue:
        def __init__(self):
            self.calls = 0

        def get(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise bit_interface.queue.Empty
            return None

    output = list(bit_interface.stream_task_output(QuietThenDoneQueue()))

    assert len(output) == 1
    assert "申诉任务仍在运行（保持连接）" in output[0]


@pytest.mark.parametrize("loop_count", [10, 20, 50])
def test_finite_loop_count_stops_after_configured_rounds(monkeypatch, loop_count):
    calls = []

    class ImmediateThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    class Lease:
        def acquire(self, timeout=0):
            return True

        def release(self):
            return None

    def no_wait(seconds, stop_event=None):
        if False:
            yield ""
        return True

    monkeypatch.setattr(bit_interface.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(bit_interface, "getWindowidByName", lambda name: "window-id")
    monkeypatch.setattr(bit_interface, "create_window_lease", lambda *args, **kwargs: Lease())
    monkeypatch.setattr(
        bit_interface.bit_appeal_ai,
        "shensu",
        lambda name, site, form, message: calls.append(site) or "完成",
    )
    monkeypatch.setattr(bit_interface, "stream_appeal_round_wait", no_wait)

    output = list(
        bit_interface.shensu_logic(
            "测试店铺",
            ("墨西哥", "巴西"),
            "侵权",
            "",
            "AI客服",
            loop_count=loop_count,
        )
    )

    assert len(calls) == loop_count * 2
    assert any(f"已完成规定的 {loop_count} 轮" in line for line in output)


def test_round_wait_sends_heartbeats(monkeypatch):
    sleeps = []
    monkeypatch.setattr(bit_interface, "APPEAL_STREAM_HEARTBEAT_SECONDS", 4)
    monkeypatch.setattr(bit_interface.time, "sleep", sleeps.append)

    output = list(bit_interface.stream_appeal_round_wait(10))

    assert sleeps == [4, 4, 2]
    assert len(output) == 2
    assert "剩余 6 秒" in output[0]
    assert "剩余 2 秒" in output[1]


def test_appeal_task_stop_registry_lifecycle():
    task_id = "test-stop-task"
    bit_interface.finish_appeal_task(task_id)

    stop_event = bit_interface.register_appeal_task(
        task_id,
        {"name": "测试店铺"},
    )

    assert stop_event is not None
    assert not stop_event.is_set()
    assert bit_interface.register_appeal_task(task_id) is None
    assert bit_interface.request_appeal_task_stop(task_id) is True
    assert stop_event.is_set()

    bit_interface.finish_appeal_task(task_id)
    assert bit_interface.request_appeal_task_stop(task_id) is False


def test_stop_request_prevents_remaining_sites(monkeypatch):
    calls = []
    releases = []
    stop_event = bit_interface.threading.Event()

    class ImmediateThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    class Lease:
        def acquire(self, timeout=0):
            return True

        def release(self):
            releases.append(True)

    def stop_after_first_site(name, site, form, message):
        calls.append(site)
        stop_event.set()
        return "完成"

    monkeypatch.setattr(bit_interface.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(bit_interface, "getWindowidByName", lambda name: "window-id")
    monkeypatch.setattr(bit_interface, "create_window_lease", lambda *args, **kwargs: Lease())
    monkeypatch.setattr(bit_interface.bit_appeal_ai, "shensu", stop_after_first_site)

    output = list(
        bit_interface.shensu_logic(
            "测试店铺",
            ("墨西哥", "巴西", "智利"),
            "侵权",
            "",
            "AI客服",
            loop_count="permanent",
            stop_event=stop_event,
        )
    )

    assert calls == ["墨西哥"]
    assert releases == [True]
    assert any("本次任务已终结" in line for line in output)


def test_login_verification_stops_remaining_sites(monkeypatch):
    calls = []

    class ImmediateThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    class Lease:
        def acquire(self, timeout=0):
            return True

        def release(self):
            return None

    monkeypatch.setattr(bit_interface.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(bit_interface, "getWindowidByName", lambda name: "window-id")
    monkeypatch.setattr(bit_interface, "create_window_lease", lambda *args, **kwargs: Lease())
    monkeypatch.setattr(
        bit_interface.bit_appeal_ai,
        "shensu",
        lambda name, site, form, message: calls.append(site) or "需要验证码",
    )

    output = list(
        bit_interface.shensu_logic(
            "测试店铺",
            ("墨西哥", "巴西", "智利"),
            "侵权",
            "",
            "AI客服",
            stop_event=bit_interface.threading.Event(),
        )
    )

    assert calls == ["墨西哥"]
    assert any("已停止该店铺后续站点" in line for line in output)


def test_stop_request_interrupts_round_wait_without_sleep(monkeypatch):
    stop_event = bit_interface.threading.Event()
    stop_event.set()
    sleeps = []
    monkeypatch.setattr(bit_interface.time, "sleep", sleeps.append)

    output = list(
        bit_interface.stream_appeal_round_wait(
            bit_interface.APPEAL_ROUND_INTERVAL_SECONDS,
            stop_event=stop_event,
        )
    )

    assert sleeps == []
    assert len(output) == 1
    assert "已终结本次申诉任务" in output[0]


def test_stop_api_sets_registered_task_event():
    task_id = "api-stop-task"
    bit_interface.finish_appeal_task(task_id)
    stop_event = bit_interface.register_appeal_task(task_id)
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {
            "id": 1,
            "username": "tester",
            "display_name": "Tester",
        }

    response = client.post(
        "/api/run_shensu/stop",
        json={"task_id": task_id},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "success"
    assert stop_event.is_set()
    bit_interface.finish_appeal_task(task_id)


def test_appeal_page_contains_stop_button_and_handler():
    template = (
        Path(bit_interface.__file__).resolve().parent / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="stop-btn"' in template
    assert 'onclick="stopTask()"' in template
    assert 'fetch("/api/run_shensu/stop"' in template
    assert '<div class="site-picker" id="site-picker"' in template
    assert 'input type="checkbox" name="site" value="墨西哥" checked' in template
    assert 'input[name="site"]:checked' in template
    assert '<option value="全部站点">' not in template
    assert 'params.append("site", site)' in template
    assert '<div class="site-picker" id="form-picker"' in template
    assert 'input type="checkbox" name="form" value="延误" checked' in template
    assert 'input[name="form"]:checked' in template
    assert 'params.append("form", form)' in template
    assert '<select id="loop-count">' in template
    assert '<option value="取消率">取消率</option>' in template
    assert '<option value="10" selected>10 次</option>' in template
    assert '<option value="20">20 次</option>' in template
    assert '<option value="50">50 次</option>' in template
    assert '<option value="permanent">永久</option>' in template


def test_appeal_shop_name_supports_fuzzy_search_and_keyboard_selection():
    template = (
        Path(bit_interface.__file__).resolve().parent / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="name" value="跃马扬鞭" placeholder="输入部分店铺名搜索"' in template
    assert 'id="appeal-shop-options" role="listbox"' in template
    assert "function appealShopMatchScore(shopName, query)" in template
    assert "name.indexOf(keyword)" in template
    assert "function renderAppealShopOptions(queryOverride = null)" in template
    assert "function handleAppealShopKeydown(event)" in template
    assert 'event.key === "ArrowDown" || event.key === "ArrowUp"' in template
    assert "selectAppealShop(appealShopActiveIndex)" in template
    assert 'name: shopName' in template


def test_collection_page_uses_shop_status_style_checkbox_multiselect():
    template = (
        Path(bit_interface.__file__).resolve().parent / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    for prefix in ("order-print", "infraction", "reputation"):
        assert f'id="{prefix}-collection-shops-all" type="checkbox"' in template
        assert f'id="{prefix}-collection-shops"' in template
        assert f"toggleAllCollectionOptions('{prefix}', 'shops', this.checked)" in template
    assert 'id="order-print-collection-sites-all" type="checkbox"' in template
    assert 'id="order-print-collection-sites"' in template
    assert "toggleAllCollectionOptions('order-print', 'sites', this.checked)" in template
    for prefix in ("infraction", "reputation"):
        assert f'id="{prefix}-collection-workers" type="number" min="1" max="10" value="3"' in template
        assert f'id="{prefix}-collection-sites-all"' not in template
        assert f'id="{prefix}-collection-sites"' not in template
    assert "Ctrl/Cmd 可多选" not in template
    assert 'querySelectorAll(".collection-option-checkbox:checked")' in template
    assert "selectAll.indeterminate" in template
    assert 'fetch("/api/collections/options"' in template
    assert "JSON.stringify(requestPayload)" in template
    assert "max_workers: maxWorkers" in template
    assert template.count(">补跑已开启店铺</button>") >= 2
    assert template.count("<label>失败店铺</label>") == 2
    assert "失败站点（随已开启店铺联动）" not in template
    assert "将补跑 ${shops.length} 家失败店铺，并发 ${workers}" in template
    assert 'class="collection-rerun-switch"' in template
    assert "collectionFailedShopOptions" in template
    assert 'id="reputation-select-all" type="checkbox"' in template
    assert 'id="reputation-selection-summary">已选择 0 家店铺' in template
    assert 'id="update-selected-reputation-btn"' in template
    assert 'class="reputation-shop-select reputation-row-select"' in template
    assert 'fetch("/api/reputation/update-selected"' in template
    assert "toggleReputationShopSelection(this.value, this.checked)" in template


def test_collection_options_returns_active_shop_site_mapping(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "db_list_bit_browser_configs",
        lambda include_ignored=False: [
            {
                "window_id": "window-1",
                "shop_name": "店铺甲",
                "salesperson": "业务员甲",
                "sites": "墨西哥，巴西",
            },
            {
                "window_id": "window-2",
                "shop_name": "店铺乙",
                "salesperson": "业务员乙",
                "sites": "巴西/智利",
            },
        ],
    )
    monkeypatch.setattr(
        bit_interface,
        "db_get_latest_infraction_info",
        lambda _days=30: {
            "summary": [
                {"店铺名": "店铺甲", "站点": "墨西哥", "状态": "成功"},
                {
                    "店铺名": "店铺甲",
                    "站点": "巴西",
                    "状态": "失败：页面结构不匹配",
                    "状态时间": "2026-07-29 10:00:00",
                },
                {"店铺名": "店铺乙", "站点": "智利", "状态": "成功"},
            ]
        },
    )
    monkeypatch.setattr(
        bit_interface,
        "db_get_latest_reputation_info",
        lambda: {
            "summary": [
                {"店铺名": "店铺甲", "站点": "墨西哥", "状态": "成功"},
                {"店铺名": "店铺甲", "站点": "巴西", "状态": "成功"},
                {
                    "店铺名": "店铺乙",
                    "站点": "智利",
                    "状态": "跳过：窗口被其他任务占用",
                    "状态时间": "2026-07-29 10:05:00",
                },
            ]
        },
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}

    response = client.get("/api/collections/options")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["data"] == {
        "shops": [
            {
                "shop_name": "店铺甲",
                "salesperson": "业务员甲",
                "sites": ["墨西哥", "巴西"],
            },
            {
                "shop_name": "店铺乙",
                "salesperson": "业务员乙",
                "sites": ["巴西", "智利"],
            },
        ],
        "sites": ["墨西哥", "巴西", "智利"],
        "failed_shops": {
            "infraction": [
                {
                    "shop_name": "店铺甲",
                    "salesperson": "业务员甲",
                    "sites": ["巴西"],
                    "failures": [
                        {
                            "site": "巴西",
                            "status": "失败：页面结构不匹配",
                            "status_time": "2026-07-29 10:00:00",
                        }
                    ],
                }
            ],
            "reputation": [
                {
                    "shop_name": "店铺乙",
                    "salesperson": "业务员乙",
                    "sites": ["智利"],
                    "failures": [
                        {
                            "site": "智利",
                            "status": "跳过：窗口被其他任务占用",
                            "status_time": "2026-07-29 10:05:00",
                        }
                    ],
                }
            ],
        },
    }


@pytest.mark.parametrize(
    ("endpoint", "state_name", "target_name"),
    [
        ("/api/infractions/collect", "_infraction_collect_state", "run_infraction_collect_job"),
        ("/api/reputation/collect", "_reputation_collect_state", "run_reputation_collect_job"),
        ("/api/reputation/update-selected", "_reputation_collect_state", "run_reputation_collect_job"),
    ],
)
def test_collection_start_passes_selected_scope_and_defaults_to_ten_workers(
    monkeypatch,
    endpoint,
    state_name,
    target_name,
):
    monkeypatch.setattr(
        bit_interface,
        "db_list_bit_browser_configs",
        lambda include_ignored=False: [
            {
                "window_id": "window-1",
                "shop_name": "店铺甲",
                "salesperson": "",
                "sites": "墨西哥，巴西",
            },
            {
                "window_id": "window-2",
                "shop_name": "店铺乙",
                "salesperson": "",
                "sites": "智利",
            },
        ],
    )
    captured = {}

    class FakeThread:
        def __init__(self, target, args, daemon):
            captured.update(target=target, args=args, daemon=daemon)

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(bit_interface.threading, "Thread", FakeThread)
    state = getattr(bit_interface, state_name)
    previous_state = dict(state)
    state.update({"running": False, "status": "idle", "message": "等待启动"})
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}
    try:
        response = client.post(
            endpoint,
            json={"shops": ["店铺甲"], "sites": ["巴西"]},
        )
        payload = response.get_json()
    finally:
        state.clear()
        state.update(previous_state)

    assert response.status_code == 200
    assert payload["status"] == "success"
    assert payload["data"]["params"] == {
        "shops": ["店铺甲"],
        "sites": ["巴西"],
        "max_workers": 10,
        "target": "1 家店铺 / 1 个站点",
    }
    assert captured == {
        "target": getattr(bit_interface, target_name),
        "args": (("店铺甲",), ("巴西",), 10),
        "daemon": True,
        "started": True,
    }


def test_collection_start_rejects_shop_site_without_configured_intersection(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "db_list_bit_browser_configs",
        lambda include_ignored=False: [
            {
                "window_id": "window-1",
                "shop_name": "店铺甲",
                "salesperson": "",
                "sites": "墨西哥",
            },
            {
                "window_id": "window-2",
                "shop_name": "店铺乙",
                "salesperson": "",
                "sites": "巴西",
            },
        ],
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}

    response = client.post(
        "/api/reputation/collect",
        json={"shops": ["店铺甲"], "sites": ["巴西"]},
    )

    assert response.status_code == 400
    assert "没有配置所选站点" in response.get_json()["message"]


def test_run_appeal_api_accepts_multiple_site_and_form_parameters(monkeypatch):
    captured = {}

    def fake_shensu_logic(
        name,
        sites,
        forms,
        message,
        mode,
        loop_count=10,
        stop_event=None,
    ):
        captured["sites"] = sites
        captured["forms"] = forms
        captured["loop_count"] = loop_count
        yield "完成\n"

    monkeypatch.setattr(bit_interface, "shensu_logic", fake_shensu_logic)
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {
            "id": 1,
            "username": "tester",
            "display_name": "Tester",
        }

    response = client.get(
        "/api/run_shensu",
        query_string=[
            ("name", "测试店铺"),
            ("site", "墨西哥"),
            ("site", "智利"),
            ("form", "侵权"),
            ("form", "取消率"),
            ("mode", "AI客服"),
            ("loop_count", "20"),
            ("task_id", "multi-site-api-test"),
        ],
        buffered=True,
    )

    assert response.status_code == 200
    assert captured["sites"] == ("墨西哥", "智利")
    assert captured["forms"] == ("侵权", "取消率")
    assert captured["loop_count"] == 20
