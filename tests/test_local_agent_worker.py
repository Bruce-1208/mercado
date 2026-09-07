import json
import os
import threading

import pytest

from bit import bit_interface
import local_agent_worker
from local_agent_worker import run_daily_task


def test_worker_exports_agent_identity_for_business_processes(monkeypatch):
    monkeypatch.setattr(local_agent_worker.socket, "gethostname", lambda: "OFFICE-PC")

    identity = local_agent_worker.configure_execution_context(
        {
            "agent_id": "agent-office-01",
            "payload": {"agent_name": "办公室电脑"},
        }
    )

    assert identity == {
        "target": "agent",
        "agent_id": "agent-office-01",
        "agent_name": "办公室电脑",
        "hostname": "OFFICE-PC",
    }
    assert os.environ["BIT_EXECUTION_TARGET"] == "agent"
    assert os.environ["BIT_EXECUTION_AGENT_ID"] == "agent-office-01"
    assert os.environ["BIT_EXECUTION_AGENT_NAME"] == "办公室电脑"
    assert os.environ["BIT_EXECUTION_HOSTNAME"] == "OFFICE-PC"


@pytest.mark.parametrize("stop", [False, True])
def test_daily_worker_runs_with_shared_process_controls(monkeypatch, tmp_path, stop):
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps({"job_id": "daily-worker-test"}), encoding="utf-8")
    released = []
    class Lock:
        def release(self):
            released.append(True)
    monkeypatch.setattr(bit_interface.bit_daily_task, "acquire_daily_task_lock", lambda **kwargs: Lock())
    outer_stop = threading.Event()
    def execute(params, task_lock, shared_stop, task_id, log_path, windows):
        assert params["mode"] == "once"
        assert task_id == "daily-worker-test"
        windows["browser-1"] = True
        assert windows["browser-1"]
        if stop:
            outer_stop.set()
            assert shared_stop.wait(5), "Cancellation must reach the process-shared Event"
        bit_interface._append_daily_task_log("worker progress\n", log_path)
        return {"status": "stopped" if stop else "success", "message": "finished"}
    monkeypatch.setattr(bit_interface, "execute_daily_task", execute)
    result = run_daily_task({"mode": "once"}, outer_stop, job_file)
    assert result["status"] == ("stopped" if stop else "success")
    assert released == [True]


def test_daily_worker_releases_lock_on_business_failure(monkeypatch, tmp_path):
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps({"job_id": "daily-worker-failure"}), encoding="utf-8")
    released = []
    class Lock:
        def release(self):
            released.append(True)
    monkeypatch.setattr(bit_interface.bit_daily_task, "acquire_daily_task_lock", lambda **kwargs: Lock())
    def fail(*args):
        raise RuntimeError("business failed")
    monkeypatch.setattr(bit_interface, "execute_daily_task", fail)
    with pytest.raises(RuntimeError, match="business failed"):
        run_daily_task({"mode": "once"}, threading.Event(), job_file)
    assert released == [True]
