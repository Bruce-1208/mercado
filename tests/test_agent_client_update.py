"""Offline executable update transactions: no production downloads or jobs."""
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import agent_update as update
import local_agent
from bit.local_agent_distribution import load_agent_client_update


def release(tmp_path, monkeypatch, content=b"new binary"):
    monkeypatch.delenv("BIT_LOCAL_AGENT_EXECUTABLE", raising=False)
    dist = tmp_path / "dist"
    dist.mkdir()
    binary = dist / "MercadoLocalAgent.exe"
    binary.write_bytes(content)
    metadata = {"agent_version": "1.3.1", "architecture": "x86_64",
                "exe_sha256": hashlib.sha256(content).hexdigest()}
    (dist / "windows-agent-build.json").write_text(json.dumps(metadata))
    return binary


def test_server_rejects_partial_release_and_does_not_serve_configuration(tmp_path, monkeypatch):
    binary = release(tmp_path, monkeypatch)
    (tmp_path / "local-agent.json").write_text('{"secret":"not for updates"}')
    manifest, content = load_agent_client_update(tmp_path, "windows")
    assert content == b"new binary"
    assert manifest["size"] == len(content)
    assert manifest["sha256"] == hashlib.sha256(content).hexdigest()
    binary.write_bytes(b"incomplete build")
    with pytest.raises(ValueError, match="校验信息不一致"):
        load_agent_client_update(tmp_path, "windows")


def agent(tmp_path):
    args = local_agent.build_argument_parser().parse_args([
        "--data-dir", str(tmp_path), "--config", str(tmp_path / "local-agent.json"), "--token", "test-token"])
    return local_agent.LocalAgent(local_agent.AgentConfig(args))


def test_update_requires_confirmation_and_runs_before_next_claim(tmp_path, monkeypatch):
    instance = agent(tmp_path)
    instance.latest_agent_version = "1.3.1"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert not instance.update_requested.is_set()
    assert instance.request_client_update()
    assert not instance.request_client_update()
    instance.ensure_enrolled = lambda: pytest.fail("must update before enrolling/claiming")
    called = []
    monkeypatch.setattr(update, "prepare_update", lambda *args: called.append(args))
    assert instance.run() == 0
    assert called[0][1] == "1.3.1"
    assert instance.update_exiting


def test_confirmation_during_job_waits_for_completion(tmp_path, monkeypatch):
    instance = agent(tmp_path)
    instance.latest_agent_version = "1.3.1"
    instance.config.poll_seconds = 0
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    instance.ensure_enrolled = lambda: None
    instance.ensure_release = lambda _: None
    instance.heartbeat = lambda: {"job": {"job_id": "test-job"}, "bundle": {}}
    events = []
    def run_job(_):
        events.append("job started")
        instance.request_client_update()
        assert not instance.update_exiting
        events.append("result uploaded")
    instance.run_job = run_job
    monkeypatch.setattr(update, "prepare_update", lambda *args: events.append("update"))
    assert instance.run() == 0
    assert events == ["job started", "result uploaded", "update"]


def test_failed_download_keeps_agent_running_and_allows_retry(tmp_path, monkeypatch):
    instance = agent(tmp_path)
    instance.latest_agent_version = "1.3.1"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    instance.request_client_update()
    def fail(*_):
        raise OSError("disk full")
    monkeypatch.setattr(update, "prepare_update", fail)
    assert not instance.perform_client_update()
    assert not instance.update_exiting
    assert not instance.update_requested.is_set()
    assert "disk full" in instance.update_status
    assert instance.request_client_update()


@pytest.mark.parametrize("corrupt", [False, True])
def test_download_validates_hash_and_never_replaces_running_binary(tmp_path, monkeypatch, corrupt):
    instance = agent(tmp_path / "data")
    target = tmp_path / "Agent with spaces"
    target.write_bytes(b"current")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", str(target))
    monkeypatch.setattr(update.platform, "machine", lambda: "arm64")
    instance._request = lambda *args, **kwargs: {
        "version": "1.3.1", "platform": "macos", "architecture": "arm64", "size": 3,
        "sha256": hashlib.sha256(b"new").hexdigest()}
    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def iter_content(self, _): yield b"bad" if corrupt else b"new"
    instance.session = SimpleNamespace(get=lambda *args, **kwargs: Response())
    instance._check_response = lambda _: None
    def spawn(command, _):
        (Path(command[-1]).parent / "ready").touch()
        return SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(update, "spawn", spawn)
    if corrupt:
        with pytest.raises(RuntimeError, match="校验失败"):
            update.prepare_update(instance, "1.3.1")
        assert not update.update_marker(instance.config.data_dir).exists()
    else:
        update.prepare_update(instance, "1.3.1")
        pending = update.read_pending(instance.config.data_dir)
        assert pending["version"] == "1.3.1"
        assert not update.startup_allowed(instance.config.data_dir)
        assert update.startup_allowed(instance.config.data_dir, pending["token"])
    assert target.read_bytes() == b"current"


@pytest.mark.parametrize("healthy", [True, False])
def test_transaction_commits_or_rolls_back_and_preserves_identity(tmp_path, monkeypatch, healthy):
    target = tmp_path / "Agent"
    target.write_bytes(b"old")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    identity = data_dir / "identity.json"
    identity.write_text('{"agent_id":"same-device"}')
    directory = tmp_path / ".agent-update-test"
    directory.mkdir()
    (directory / "Agent").write_bytes(b"new")
    transaction = {"token": "test-token", "created_at": time.time(), "version": "1.3.1",
                   "target": str(target), "data_dir": str(data_dir), "directory": str(directory),
                   "sha256": hashlib.sha256(b"new").hexdigest(),
                   "arguments": ["--config", "a path with spaces"], "cwd": str(tmp_path)}
    update.write_json(directory / "transaction.json", transaction)
    update.write_json(update.update_marker(data_dir), transaction)
    launches = []
    def spawn(command, cwd):
        launches.append(command)
        if healthy:
            update.acknowledge_startup(data_dir, "test-token", "1.3.1")
        return SimpleNamespace(poll=lambda: None if healthy else 1)
    monkeypatch.setattr(update, "spawn", spawn)
    result = update.apply_update(directory / "transaction.json", local_agent.AgentProcessLock, local_agent.AgentRuntimeLog)
    assert result == (0 if healthy else 1)
    assert target.read_bytes() == (b"new" if healthy else b"old")
    assert identity.read_text() == '{"agent_id":"same-device"}'
    assert not update.update_marker(data_dir).exists()
    assert len(launches) == (1 if healthy else 2)
    assert launches[0][1:3] == ["--config", "a path with spaces"]
    if not healthy:
        assert "--update-token" not in launches[-1]


def test_stale_transaction_does_not_block_supervisor_forever(tmp_path):
    update.write_json(update.update_marker(tmp_path), {"created_at": time.time() - 601, "token": "old"})
    assert update.startup_allowed(tmp_path)


def test_update_endpoint_uses_agent_auth_and_returns_no_enrollment_secrets(tmp_path, monkeypatch):
    from bit import bit_interface
    release(tmp_path, monkeypatch)
    monkeypatch.setattr(bit_interface, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setenv("BIT_LOCAL_AGENT_TOKEN", "offline-update-token")
    client = bit_interface.app.test_client()
    url = "/api/local-agents/client-update?platform=windows"
    assert client.get(url).status_code == 401
    headers = {"X-Local-Agent-Token": "offline-update-token"}
    response = client.get(url, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["data"]["version"] == "1.3.1"
    assert response.headers["Cache-Control"] == "no-store"
    response = client.get(url + "&binary=1", headers=headers)
    assert response.status_code == 200
    assert response.data == b"new binary"
    assert client.get("/api/local-agents/client-update?platform=invalid", headers=headers).status_code == 409


def test_helper_waits_for_original_process_lock(tmp_path, monkeypatch):
    # Exercise the real OS lock separately from the mocked restart above.
    lock = local_agent.AgentProcessLock(tmp_path / "agent.lock")
    assert lock.acquire()
    other = local_agent.AgentProcessLock(tmp_path / "agent.lock")
    assert not other.acquire()
    lock.release()
    assert other.acquire()
    other.release()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fixture")
def test_real_process_restart_waits_for_old_lock_and_confirms_startup(tmp_path, monkeypatch):
    target = tmp_path / "Agent with spaces"
    target.write_bytes(b"previous executable")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    directory = tmp_path / ".agent-update-real"
    directory.mkdir()
    # Run the actual Agent entry point under an executable wrapper. Its only
    # server is an unused loopback port; no production tasks can be claimed.
    candidate = directory / target.name
    root = Path(local_agent.__file__).resolve().parent
    candidate.write_text(f"#!{sys.executable}\nimport runpy, sys\n"
                         f"sys.path.insert(0, {str(root)!r})\n"
                         f"runpy.run_path({str(root / 'local_agent.py')!r}, run_name='__main__')\n")
    candidate.chmod(0o755)
    transaction = {"token": "real-test", "created_at": time.time(), "version": local_agent.AGENT_VERSION,
                   "target": str(target), "data_dir": str(data_dir), "directory": str(directory),
                   "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                   "arguments": ["--data-dir", str(data_dir), "--config", str(tmp_path / "config.json"),
                                 "--server", "http://127.0.0.1:1", "--token", "offline", "--no-window"],
                   "cwd": str(tmp_path)}
    update.write_json(directory / "transaction.json", transaction)
    update.write_json(update.update_marker(data_dir), transaction)
    original_lock = local_agent.AgentProcessLock(data_dir / "agent.lock")
    assert original_lock.acquire()
    timer = threading.Timer(0.3, original_lock.release)
    timer.start()
    children = []
    actual_spawn = update.spawn
    def spawn(*args):
        child = actual_spawn(*args)
        children.append(child)
        return child
    monkeypatch.setattr(update, "spawn", spawn)
    try:
        assert update.apply_update(directory / "transaction.json", local_agent.AgentProcessLock, local_agent.AgentRuntimeLog) == 0
        assert target.read_text().startswith("#!")
        assert (directory / "previous").read_bytes() == b"previous executable"
        assert not update.update_marker(data_dir).exists()
    finally:
        timer.join()
        original_lock.release()
        for child in children:
            child.terminate()
            child.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fixture")
def test_real_failed_executable_restores_and_restarts_previous_program(tmp_path, monkeypatch):
    target = tmp_path / "Agent"
    restored = tmp_path / "restored"
    old = f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(restored)!r}).touch()\n"
    target.write_text(old)
    target.chmod(0o755)
    directory = tmp_path / ".agent-update-rollback"
    directory.mkdir()
    candidate = directory / target.name
    candidate.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
    candidate.chmod(0o755)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    transaction = {"token": "real-rollback", "created_at": time.time(), "version": "1.3.1",
                   "target": str(target), "data_dir": str(data_dir), "directory": str(directory),
                   "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                   "arguments": [], "cwd": str(tmp_path)}
    update.write_json(directory / "transaction.json", transaction)
    update.write_json(update.update_marker(data_dir), transaction)
    children = []
    actual_spawn = update.spawn
    def spawn(*args):
        child = actual_spawn(*args)
        children.append(child)
        return child
    monkeypatch.setattr(update, "spawn", spawn)
    try:
        assert update.apply_update(directory / "transaction.json", local_agent.AgentProcessLock, local_agent.AgentRuntimeLog) == 1
        for child in children:
            child.wait(timeout=5)
        assert target.read_text() == old
        assert restored.exists()
        assert not update.update_marker(data_dir).exists()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
