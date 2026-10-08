"""HTTP views reconnect to persisted execution; they never own the worker."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bit import bit_interface, durable_tasks
from bit.workbench_runtime import RuntimeSettings
from bit.local_agent_hub import LocalAgentStore


@pytest.fixture
def console(monkeypatch, tmp_path, subscribed_workbench_account):
    actor = {"id": 7, "access_version": 1, "permissions": ["*"],
             "role_key": "super_admin", "is_platform_admin": True}
    monkeypatch.setattr(bit_interface, "get_current_workbench_user", lambda: actor)
    monkeypatch.setattr(bit_interface, "RUNTIME_SETTINGS", RuntimeSettings("server", "test"))
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    hub = LocalAgentStore(tmp_path / "hub.sqlite3")
    monkeypatch.setattr(bit_interface, "get_local_agent_store", lambda: hub)
    monkeypatch.setattr(bit_interface, "_authorized_token_ids_for_user", lambda *args: None)
    monkeypatch.setattr(bit_interface, "_daily_tasks", {})
    monkeypatch.setattr(bit_interface, "_daily_task_controls", {})
    monkeypatch.setattr(bit_interface, "_daily_task_state", {"running": False})
    monkeypatch.setattr(bit_interface, "_persist_daily_task_history", lambda: None)
    monkeypatch.setattr(bit_interface.bit_daily_task, "get_daily_task_lock_owner", lambda *args: {})
    monkeypatch.setattr(bit_interface, "validate_authorized_appeal_sites", lambda name, sites: sites)
    submitted = []

    class Process:
        pid = 999999
        def __init__(self):
            self.stdin = io.BytesIO()
        def poll(self):
            return None

    def spawn(command, **options):
        process = Process()
        submitted.append((command, options, process))
        return process

    monkeypatch.setattr(durable_tasks.subprocess, "Popen", spawn)
    return actor, submitted, bit_interface.app.test_client()


def appeal_payload(**changes):
    return {"name": "测试店铺", "sites": ["墨西哥"], "forms": ["侵权"],
            "mode": "AI客服", "loop_count": 10, "execution_target": "server",
            "task_id": "restart-appeal", "log_transport": "poll", **changes}


def test_appeal_runs_before_view_and_stream_disconnect_does_not_cancel(console):
    _, submitted, client = console
    response = client.post("/api/run_shensu", json=appeal_payload())
    assert response.status_code == 200
    assert len(submitted) == 1
    store = bit_interface.get_durable_task_store()
    state = store.get("restart-appeal")
    Path(state["log_path"]).write_text("继续执行申诉\n", encoding="utf-8")
    stream = bit_interface.stream_durable_task("restart-appeal")
    assert next(stream) == "继续执行申诉\n"
    stream.close()
    assert store.get("restart-appeal")["running"]
    assert not store.stop_event("restart-appeal").is_set()
    if durable_tasks.os.name != "nt":
        assert submitted[0][1]["start_new_session"] is True


def test_daily_status_and_stop_reattach_after_memory_is_lost(console, monkeypatch):
    _, submitted, client = console
    response = client.post("/api/tasks/daily/start", json={"mode": "once", "group_names": ["组甲"]})
    assert response.status_code == 200
    task_id = response.get_json()["data"]["task_id"]
    assert len(submitted) == 1
    assert not bit_interface._daily_task_controls
    monkeypatch.setattr(bit_interface, "_daily_tasks", {})
    monkeypatch.setattr(bit_interface, "_daily_task_state", {"running": False})
    status = client.get("/api/tasks/daily/status").get_json()["data"]
    assert status["running_count"] == 1
    assert status["tasks"][0]["task_id"] == task_id
    stopped = client.post("/api/tasks/daily/stop", json={"task_id": task_id})
    assert stopped.status_code == 200
    assert bit_interface.get_durable_task_store().stop_event(task_id).is_set()
    assert len(submitted) == 1


def test_poll_final_logs_are_drained_and_keep_unicode(console):
    _, _, client = console
    client.post("/api/run_shensu", json=appeal_payload())
    store = bit_interface.get_durable_task_store()
    state = store.get("restart-appeal")
    message = "完成申诉。\n" * 20000
    Path(state["log_path"]).write_text(message, encoding="utf-8")
    store.update("restart-appeal", running=False, status="success", can_stop=False)
    cursor, contents = 0, []
    for _ in range(20):
        response = client.get(f"/api/run_shensu/restart-appeal/events?after={cursor}")
        data = response.get_json()["data"]
        contents.append(data["log"])
        assert "\ufffd" not in data["log"]
        cursor = data["next_after"]
        if data["done"]:
            break
    assert data["done"]
    assert "".join(contents) == message
    assert "params" not in data


def test_restored_appeal_permission_checks(console):
    actor, _, client = console
    client.post("/api/run_shensu", json=appeal_payload())
    actor.update(id=8, role_key="employee", is_platform_admin=False)
    assert client.post("/api/run_shensu/stop", json={"task_id": "restart-appeal"}).status_code == 403
    assert client.get("/api/run_shensu/restart-appeal/events").status_code == 404
    assert not bit_interface.get_durable_task_store().stop_event("restart-appeal").is_set()


def test_daily_adapter_owns_and_releases_resources_on_error(console, monkeypatch):
    _, _, _ = console
    releases, shutdowns = [], []
    monkeypatch.setattr(bit_interface.bit_daily_task, "acquire_daily_task_lock",
                        lambda **kwargs: SimpleNamespace(release=lambda: releases.append(True)))
    class BrokenManager:
        def Event(self):
            raise RuntimeError("cannot start event")
        def shutdown(self):
            shutdowns.append(True)
    monkeypatch.setattr(bit_interface.multiprocessing, "Manager", BrokenManager)
    state = {"task_id": "adapter-test", "log_path": str(bit_interface.get_durable_task_store().root / "adapter-test.log")}
    with pytest.raises(RuntimeError, match="cannot start event"):
        bit_interface.execute_durable_task("daily_task", {"mode": "once"}, state, None,
                                          durable_tasks.FileStopEvent(Path(state["log_path"]).with_suffix(".stop")))
    assert releases == [True]
    assert shutdowns == [True]


def test_terminal_truncated_utf8_does_not_leave_poll_waiting_forever(console):
    _, _, client = console
    client.post("/api/run_shensu", json=appeal_payload())
    store = bit_interface.get_durable_task_store()
    state = store.get("restart-appeal")
    Path(state["log_path"]).write_bytes(b"end\n\xe5")
    store.update("restart-appeal", running=False, status="interrupted")
    data = client.get("/api/run_shensu/restart-appeal/events").get_json()["data"]
    assert data["done"]
    assert data["next_after"] == 5


def test_daily_rejects_invalid_task_id(console):
    _, _, client = console
    response = client.get("/api/tasks/daily/status", query_string={"task_id": "../invalid"})
    assert response.status_code == 400


def test_daily_restored_task_logs_only_visible_to_owner(console):
    actor, _, client = console
    state = client.post("/api/tasks/daily/start", json={"mode": "once"}).get_json()["data"]
    actor.update(id=8, role_key="employee", is_platform_admin=False)
    bit_interface._daily_tasks.clear()
    with bit_interface.app.test_request_context():
        assert bit_interface._daily_task_snapshot(state["task_id"]) == {}
        assert bit_interface._daily_tasks_snapshot()["tasks"] == []


def test_daily_owned_host_lock_does_not_double_count_durable_task(console, monkeypatch):
    _, _, client = console
    state = client.post("/api/tasks/daily/start", json={"mode": "once"}).get_json()["data"]
    monkeypatch.setattr(bit_interface.bit_daily_task, "get_daily_task_lock_owner",
                        lambda *args: {"metadata": {"task_id": state["task_id"]}})
    data = client.get("/api/tasks/daily/status").get_json()["data"]
    assert data["running_count"] == data["total_count"] == 1
    assert "external_task" not in data


def test_local_executor_poll_uses_same_durable_task_and_requires_credential(console, monkeypatch):
    _, _, client = console
    client.post("/api/run_shensu", json=appeal_payload())
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    monkeypatch.setattr(bit_interface, "RUNTIME_SETTINGS", RuntimeSettings("client", "test"))
    monkeypatch.setattr(bit_interface, "_local_executor_user_from_token",
                        lambda token: {"id": 7, "username": "test", "permission": "appeal.execute"}
                        if token == "local-credential" else None)
    url = "/api/local-executor/run_shensu/restart-appeal/events"
    assert client.get(url).status_code == 401
    response = client.get(url, headers={"Authorization": "Bearer local-credential"})
    assert response.status_code == 200
    assert response.get_json()["data"]["task_id"] == "restart-appeal"
    assert not response.get_json()["data"]["done"]
