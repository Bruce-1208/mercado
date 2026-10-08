"""Verify persistence and real worker isolation without contacting a marketplace."""

import io
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from bit import bit_runtime_lock, durable_tasks
from bit.durable_tasks import DurableTaskStore, FileStopEvent


@pytest.fixture
def task_store(tmp_path, monkeypatch):
    locks = tmp_path / "runtime-locks"
    monkeypatch.setenv("BIT_RUNTIME_LOCK_DIR", str(locks))
    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", locks)
    return DurableTaskStore(tmp_path / "tasks")


def wait_for(store, task_id, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = store.get(task_id)
        if state and predicate(state):
            return state
        time.sleep(0.05)
    pytest.fail(f"Task did not reach expected state: {store.get(task_id)}")


def launch_from_temporary_service(store, task_id, duration):
    # The launching service really exits while its detached worker continues.
    script = """
import json, sys, time
from bit.durable_tasks import DurableTaskStore
store = DurableTaskStore(sys.argv[1])
state = store.start('__noop__', {'duration': float(sys.argv[3])}, {'task_id': sys.argv[2]})
while not state.get('worker_started_at'):
    time.sleep(0.02)
    state = store.get(sys.argv[2])
print(json.dumps(state), flush=True)
"""
    launcher = subprocess.run([sys.executable, "-c", script, str(store.root), task_id, str(duration)],
                              cwd=str(Path(__file__).resolve().parents[1]),
                              capture_output=True, text=True, timeout=10)
    assert launcher.returncode == 0, launcher.stderr
    return json.loads(launcher.stdout)


def test_worker_survives_service_exit_and_reattaches_without_replay(task_store, monkeypatch):
    launched = launch_from_temporary_service(task_store, "survive-restart", 1.3)
    # A new release may move its ordinary checkout-relative runtime locks.
    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", task_store.root.parent / "new-release-locks")
    monkeypatch.setattr(durable_tasks, "START_GRACE_SECONDS", 0)
    restarted = DurableTaskStore(task_store.root)
    attached = restarted.get("survive-restart")
    assert attached["running"]
    assert attached["pid"] == launched["pid"]
    assert restarted._worker_owner("survive-restart")["pid"] == launched["pid"]
    assert attached["progress"] > 0
    with pytest.raises(ValueError, match="已存在"):
        restarted.start("__noop__", {}, {"task_id": "survive-restart"})
    finished = wait_for(restarted, "survive-restart", lambda state: not state["running"])
    assert finished["status"] == "success"
    assert finished["pid"] == launched["pid"]
    assert len(restarted.list("__noop__")) == 1
    assert Path(finished["log_path"]).read_text().count("生命周期检查已启动") == 1


def test_restarted_service_can_stop_existing_worker(task_store):
    launch_from_temporary_service(task_store, "stop-after-restart", 4)
    restarted = DurableTaskStore(task_store.root)
    assert restarted.request_stop("stop-after-restart")
    result = wait_for(restarted, "stop-after-restart", lambda state: not state["running"])
    assert result["status"] == "stopped"
    assert result["stop_requested"]
    assert not result["can_stop"]
    assert not restarted.request_stop("stop-after-restart")


def test_private_payload_never_written_to_database_or_logs(task_store):
    secret = "durable-private-token-do-not-persist"
    task_store.start("__noop__", {"duration": 0.1, "dashscope_api_key": secret},
                     {"task_id": "private-input", "params": {"dashscope_api_key": secret, "mode": "once"}})
    state = wait_for(task_store, "private-input", lambda item: not item["running"])
    assert state["params"] == {"mode": "once"}
    for path in task_store.root.iterdir():
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
    if os.name != "nt":
        assert task_store.root.stat().st_mode & 0o777 == 0o700
        assert task_store.path.stat().st_mode & 0o777 == 0o600
        assert Path(state["log_path"]).stat().st_mode & 0o777 == 0o600


def test_failed_launch_keeps_terminal_record_and_refuses_replay(task_store, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("no worker runtime")
    monkeypatch.setattr(durable_tasks.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="no worker runtime"):
        task_store.start("appeal", {}, {"task_id": "spawn-error"})
    state = DurableTaskStore(task_store.root).get("spawn-error")
    assert state["status"] == "error"
    assert not state["running"]
    assert "no worker runtime" in state["message"]
    with pytest.raises(ValueError, match="已存在"):
        task_store.start("appeal", {}, {"task_id": "spawn-error"})


def test_spawn_grace_and_dead_worker_do_not_automatically_replay(task_store, monkeypatch):
    class FakeProcess:
        pid = 999999
        stdin = io.BytesIO()
        def poll(self):
            return 0
    monkeypatch.setattr(durable_tasks.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    state = task_store.start("appeal", {}, {"task_id": "never-acquired-lease"})
    assert state["running"] and state["status"] == "starting"
    monkeypatch.setattr(durable_tasks, "START_GRACE_SECONDS", 0)
    state = DurableTaskStore(task_store.root).get("never-acquired-lease")
    assert state["status"] == "interrupted"
    assert not state["running"]
    assert len(task_store.list("appeal")) == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows has no SIGKILL")
def test_killed_worker_retains_progress_as_interrupted(task_store, monkeypatch):
    task_store.start("__noop__", {"duration": 6}, {"task_id": "killed-worker"})
    running = wait_for(task_store, "killed-worker", lambda item: item.get("progress", 0) > 1)
    os.kill(running["pid"], signal.SIGKILL)
    monkeypatch.setattr(durable_tasks, "START_GRACE_SECONDS", 0)
    ended = wait_for(task_store, "killed-worker", lambda item: not item["running"])
    assert ended["status"] == "interrupted"
    assert ended["progress"] >= running["progress"]
    assert Path(ended["log_path"]).exists()


def test_concurrent_state_updates_preserve_each_field(task_store):
    task_store.start("__noop__", {}, {"task_id": "concurrent-updates"})
    wait_for(task_store, "concurrent-updates", lambda item: not item["running"])
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda number: DurableTaskStore(task_store.root).update(
            "concurrent-updates", **{f"counter_{number}": number}), range(12)))
    state = task_store.get("concurrent-updates")
    assert all(state[f"counter_{number}"] == number for number in range(12))


def test_log_path_and_task_id_validation_happens_before_launch(task_store):
    for task_id, log_path in (("../escape", None), ("outside-log", task_store.root.parent / "outside.log"),
                              ("database-log", task_store.path)):
        with pytest.raises(ValueError):
            task_store.start("appeal", {}, {"task_id": task_id, "log_path": log_path})
    assert not task_store.list()


def test_prune_removes_only_terminal_records_and_owned_files(task_store):
    task_store.start("__noop__", {"duration": 2}, {"task_id": "keep-active"})
    task_store.start("__noop__", {}, {"task_id": "remove-terminal"})
    terminal = wait_for(task_store, "remove-terminal", lambda item: not item["running"])
    # The runner releases its lease just after writing its terminal record.
    wait_for(task_store, "remove-terminal", lambda item: not task_store._worker_owner(item["task_id"]))
    assert task_store.prune(retention_seconds=0) == 1
    assert task_store.get("remove-terminal") is None
    assert not Path(terminal["log_path"]).exists()
    assert task_store.get("keep-active")["running"]
    assert task_store.request_stop("keep-active")
    wait_for(task_store, "keep-active", lambda item: not item["running"])


def test_file_stop_event_survives_recreation(tmp_path):
    event = FileStopEvent(tmp_path / "stop")
    assert not event.wait(0.01)
    event.set()
    assert FileStopEvent(event.path).wait(0)


def test_adapter_failure_is_persisted_and_context_reaches_child_environment(task_store, monkeypatch):
    import bit
    class FakeProcess:
        def __init__(self):
            self.pid = 999999
            self.stdin = io.BytesIO()
        def poll(self):
            return 0
    monkeypatch.setattr(durable_tasks.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    task_store.start("appeal", {}, {"task_id": "adapter-error"})
    payload = {"execution_target": "local", "_usage_context": {
        "actor_id": 42, "actor_name": "操作员", "organization_key": "org-test"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    for key in ("BIT_DURABLE_TASK_WORKER", "BIT_BACKGROUND_SERVICES_DISABLED", "BIT_EXECUTION_JOB_ID",
                "BIT_EXECUTION_TARGET", "AI_BILLING_ACTOR_ID", "AI_BILLING_ACTOR_NAME", "AI_BILLING_ORGANIZATION_KEY"):
        monkeypatch.setenv(key, "previous")
    def fail(job_type, private_payload, state, store, stop_event):
        assert os.environ["BIT_DURABLE_TASK_WORKER"] == "1"
        assert os.environ["BIT_EXECUTION_TARGET"] == "local"
        assert os.environ["BIT_EXECUTION_JOB_ID"] == "adapter-error"
        assert os.environ["AI_BILLING_ACTOR_ID"] == "42"
        assert os.environ["AI_BILLING_ACTOR_NAME"] == "操作员"
        assert os.environ["AI_BILLING_ORGANIZATION_KEY"] == "org-test"
        store.update(state["task_id"], progress=3)
        raise RuntimeError("adapter business failure")
    monkeypatch.setattr(bit, "bit_interface", SimpleNamespace(execute_durable_task=fail), raising=False)
    assert durable_tasks.run_worker(task_store.root, "adapter-error") == 1
    state = task_store.get("adapter-error")
    assert state["status"] == "error" and not state["running"]
    assert state["progress"] == 3
    assert state["message"] == "adapter business failure"
    assert not task_store._worker_owner("adapter-error")


def test_frozen_launcher_uses_executable_worker_entrypoint(task_store, monkeypatch):
    commands = []
    class FakeProcess:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.pid = 999999
            self.stdin = io.BytesIO()
        def poll(self):
            return 0
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(durable_tasks.subprocess, "Popen", FakeProcess)
    task_store.start("appeal", {}, {"task_id": "frozen-worker"})
    assert commands == [[sys.executable, "--durable-task-worker", str(task_store.root), "frozen-worker"]]


@pytest.mark.parametrize("private_input", ["", "null", '{"dashscope_api_key": "incomplete-secret'])
def test_partial_private_input_fails_before_business_execution(task_store, monkeypatch, private_input):
    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.pid = 999999
            self.stdin = io.BytesIO()
        def poll(self):
            return 0
    monkeypatch.setattr(durable_tasks.subprocess, "Popen", FakeProcess)
    task_store.start("appeal", {}, {"task_id": "incomplete-input"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(private_input))
    assert durable_tasks.run_worker(task_store.root, "incomplete-input") == 1
    state = task_store.get("incomplete-input")
    assert state["status"] == "error" and not state["running"]
    assert state["message"] == "后台任务私密参数读取失败"
    assert "incomplete-secret" not in json.dumps(state)


def test_frozen_worker_restores_redirected_standard_streams(monkeypatch):
    reopened = {}
    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(sys, name, None)
    def reopen(descriptor, mode, **options):
        assert options == {"encoding": "utf-8", "errors": "replace", "buffering": 1, "closefd": False}
        reopened[descriptor] = (mode, io.StringIO())
        return reopened[descriptor][1]
    monkeypatch.setattr(durable_tasks, "open", reopen, raising=False)
    durable_tasks.restore_worker_stdio()
    assert sys.stdin is reopened[0][1] and reopened[0][0] == "r"
    assert sys.stdout is reopened[1][1] and reopened[1][0] == "w"
    assert sys.stderr is reopened[2][1] and reopened[2][0] == "w"
