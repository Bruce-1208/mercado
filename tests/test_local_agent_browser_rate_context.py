import os
from pathlib import Path

import local_agent_worker as worker


def test_agent_uses_same_lock_directory_across_release_updates(monkeypatch, tmp_path):
    monkeypatch.delenv("BIT_RUNTIME_LOCK_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    worker.configure_execution_context({}, tmp_path / "agent" / "jobs" / "one" / "job.json")
    first = Path(os.environ["BIT_RUNTIME_LOCK_DIR"])
    monkeypatch.delenv("BIT_RUNTIME_LOCK_DIR")
    worker.configure_execution_context({}, tmp_path / "agent" / "jobs" / "two" / "job.json")
    assert Path(os.environ["BIT_RUNTIME_LOCK_DIR"]) == first == tmp_path / "agent" / "runtime_locks"


def test_explicit_shared_runtime_directory_is_preserved(monkeypatch, tmp_path):
    configured = str(tmp_path / "existing-shared-locks")
    monkeypatch.setenv("BIT_RUNTIME_LOCK_DIR", configured)
    worker.configure_execution_context({}, tmp_path / "agent" / "jobs" / "one" / "job.json")
    assert os.environ["BIT_RUNTIME_LOCK_DIR"] == configured
