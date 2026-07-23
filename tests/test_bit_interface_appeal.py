import pytest
from pathlib import Path

from bit import bit_interface


def test_mercado_login_console_command_supports_all_and_single_shop():
    all_command = bit_interface._build_mercado_login_command(workers=3)
    single_command = bit_interface._build_mercado_login_command(shop_name="四季如春")

    assert all_command[0] == bit_interface.sys.executable
    assert "--all-active-login" in all_command
    assert all_command[all_command.index("--workers") + 1] == "3"
    assert single_command[single_command.index("--shop") + 1] == "四季如春"
    assert "--auto-login" in single_command


def test_mercado_login_console_does_not_start_duplicate_task(monkeypatch):
    with bit_interface._mercado_login_task_lock:
        previous = dict(bit_interface._mercado_login_task_state)
        bit_interface._mercado_login_task_state["running"] = True
    try:
        started, state = bit_interface.start_mercado_login_console_job()
    finally:
        with bit_interface._mercado_login_task_lock:
            bit_interface._mercado_login_task_state.clear()
            bit_interface._mercado_login_task_state.update(previous)

    assert started is False
    assert state["running"] is True


def test_window_anomaly_can_restart_single_mercado_login(monkeypatch):
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


def test_resolve_selected_appeal_sites_and_remove_duplicates():
    assert bit_interface.resolve_appeal_sites(["墨西哥", "巴西", "墨西哥"]) == (
        "墨西哥", "巴西"
    )
    assert bit_interface.resolve_appeal_sites("巴西") == ("巴西",)
    with pytest.raises(ValueError, match="至少选择一个站点"):
        bit_interface.resolve_appeal_sites([])
    with pytest.raises(ValueError, match="不支持的站点"):
        bit_interface.resolve_appeal_sites(["全部站点"])


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
@pytest.mark.parametrize("form", ["侵权", "延误", "取消率"])
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
    assert '<select id="loop-count">' in template
    assert '<option value="取消率">取消率</option>' in template
    assert '<option value="10" selected>10 次</option>' in template
    assert '<option value="20">20 次</option>' in template
    assert '<option value="50">50 次</option>' in template
    assert '<option value="permanent">永久</option>' in template


def test_run_appeal_api_accepts_multiple_site_parameters(monkeypatch):
    captured = {}

    def fake_shensu_logic(
        name,
        sites,
        form,
        message,
        mode,
        loop_count=10,
        stop_event=None,
    ):
        captured["sites"] = sites
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
            ("mode", "AI客服"),
            ("loop_count", "20"),
            ("task_id", "multi-site-api-test"),
        ],
        buffered=True,
    )

    assert response.status_code == 200
    assert captured["sites"] == ("墨西哥", "智利")
    assert captured["loop_count"] == 20
