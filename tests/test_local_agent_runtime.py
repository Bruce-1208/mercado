import hashlib
import io
import json
import re
import threading
import time
import zipfile
from types import SimpleNamespace

import pytest

import local_agent
from local_agent import (
    STATUS_WINDOW_PLATFORMS,
    AgentRuntimeLog,
    LocalAgent,
    _status_window_enabled,
    _tail_text,
)


class BundleResponse:
    status_code = 200

    def __init__(self, content, version, sha256):
        self.content = content
        self.headers = {
            "X-Business-Version": version,
            "X-Bundle-SHA256": sha256,
        }

    def raise_for_status(self):
        return None


class BundleSession:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return self.response


def make_bundle(version="business-v1", files=None):
    files = files or {"local_agent_worker.py": "print('worker')\n"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr(
            "bundle-manifest.json",
            json.dumps({"version": version, "files": list(files)}),
        )
    content = buffer.getvalue()
    return content, hashlib.sha256(content).hexdigest()


def test_agent_runtime_messages_have_local_time_and_persist(tmp_path, capsys):
    config = SimpleNamespace(
        data_dir=tmp_path,
        server_url="https://workbench.example",
        agent_token="",
    )
    agent = LocalAgent(config)

    agent.log("Agent 测试日志")

    output = capsys.readouterr().out.strip()
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Agent 测试日志", output)
    assert (tmp_path / "agent.log").read_text(encoding="utf-8").strip() == output


def test_agent_runtime_messages_are_forwarded_to_status_listeners(tmp_path):
    runtime_log = AgentRuntimeLog(tmp_path / "agent.log")
    received = []
    runtime_log.add_listener(received.append)

    runtime_log.write("实时日志")

    assert len(received) == 1
    assert received[0].endswith("实时日志")


def test_agent_runtime_log_works_without_windowed_stdout(monkeypatch, tmp_path):
    runtime_log = AgentRuntimeLog(tmp_path / "agent.log")
    monkeypatch.setattr("local_agent.sys.stdout", None)

    runtime_log.write("无控制台日志")

    assert (tmp_path / "agent.log").read_text(encoding="utf-8").endswith(
        "无控制台日志\n"
    )


def test_status_history_reads_a_bounded_utf8_tail(tmp_path):
    path = tmp_path / "agent.log"
    path.write_text("第一行\n第二行\n第三行\n", encoding="utf-8")

    assert _tail_text(path, max_bytes=18).endswith("第三行")
    assert not _tail_text(tmp_path / "missing.log")


def test_status_window_supports_windows_and_macos():
    assert STATUS_WINDOW_PLATFORMS == {"win32", "darwin"}


def test_status_window_is_disabled_for_noninteractive_modes(monkeypatch):
    monkeypatch.setattr("local_agent.sys.platform", "darwin")
    args = SimpleNamespace(no_window=False, once=False, worker=False)
    assert _status_window_enabled(args)

    args.once = True
    assert not _status_window_enabled(args)
    args.once = False
    args.no_window = True
    assert not _status_window_enabled(args)
    args.no_window = False
    args.worker = True
    assert not _status_window_enabled(args)


def test_status_window_opens_macos_log_directory_with_finder(monkeypatch, tmp_path):
    window = object.__new__(local_agent.AgentStatusWindow)
    window.agent = SimpleNamespace(
        runtime_log=SimpleNamespace(path=tmp_path / "agent.log")
    )
    opened = []
    monkeypatch.setattr("local_agent.sys.platform", "darwin")
    monkeypatch.setattr(
        "local_agent.subprocess.Popen",
        lambda *args, **kwargs: opened.append((args, kwargs)),
    )

    window._open_log_directory()

    assert opened == [
        (
            (["open", str(tmp_path)],),
            {
                "stdout": local_agent.subprocess.DEVNULL,
                "stderr": local_agent.subprocess.DEVNULL,
            },
        )
    ]


def test_status_window_close_requests_real_agent_shutdown():
    window = object.__new__(local_agent.AgentStatusWindow)
    requested = []
    destroyed = []
    window.closing = False
    window.agent_thread = None
    window.agent = SimpleNamespace(request_shutdown=requested.append)
    window.status_text = SimpleNamespace(set=lambda _value: None)
    window.messagebox = SimpleNamespace(askyesno=lambda *_args, **_kwargs: True)
    window.root = SimpleNamespace(
        after=lambda _delay, callback: callback(),
        destroy=lambda: destroyed.append(True),
    )

    window._on_close()

    assert requested == ["用户关闭 Agent 窗口"]
    assert destroyed == [True]


def test_status_window_stop_button_only_stops_current_job():
    window = object.__new__(local_agent.AgentStatusWindow)
    requested = []
    statuses = []
    window.closing = False
    window.stopping_job_id = ""
    window.agent = SimpleNamespace(
        current_job_id=lambda: "job-123",
        request_stop_current_job=lambda reason: requested.append(reason) or "job-123",
    )
    window.status_text = SimpleNamespace(set=statuses.append)
    window.stop_task_button = SimpleNamespace(configure=lambda **_kwargs: None)
    window.messagebox = SimpleNamespace(askyesno=lambda *_args, **_kwargs: True)
    window.root = object()

    window._on_stop_current_task()

    assert requested == ["用户点击结束任务"]
    assert window.stopping_job_id == "job-123"
    assert statuses == ["正在结束当前任务…"]


def test_agent_local_stop_request_writes_cancel_file_without_shutting_down(tmp_path):
    config = SimpleNamespace(
        data_dir=tmp_path,
        server_url="https://workbench.example",
        agent_token="",
    )
    agent = LocalAgent(config)
    cancel_file = tmp_path / "jobs" / "job-123" / "cancel.requested"
    cancel_file.parent.mkdir(parents=True)
    process = SimpleNamespace(poll=lambda: None)
    guard = SimpleNamespace(process=process)
    agent._set_current_worker("job-123", guard, cancel_file)

    stopped_job_id = agent.request_stop_current_job("用户点击结束任务")

    assert stopped_job_id == "job-123"
    assert cancel_file.read_text(encoding="utf-8") == "用户点击结束任务"
    assert not agent.shutdown_event.is_set()


def test_agent_downloads_verifies_and_atomically_activates_release(tmp_path):
    content, sha256 = make_bundle()
    config = SimpleNamespace(
        data_dir=tmp_path,
        server_url="https://workbench.example",
        agent_token="",
    )
    agent = LocalAgent(config)
    session = BundleSession(BundleResponse(content, "business-v1", sha256))
    agent.session = session

    release = agent.ensure_release({"version": "business-v1", "sha256": sha256})

    assert (release / "local_agent_worker.py").read_text(
        encoding="utf-8"
    ) == "print('worker')\n"
    current = json.loads((tmp_path / "current-release.json").read_text())
    assert current["version"] == "business-v1"
    assert agent.current_release == "business-v1"
    assert session.calls == 1

    assert agent.ensure_release({"version": "business-v1", "sha256": sha256}) == release
    assert session.calls == 1


def test_agent_rejects_bundle_when_hash_does_not_match(tmp_path):
    content, sha256 = make_bundle()
    config = SimpleNamespace(
        data_dir=tmp_path,
        server_url="https://workbench.example",
        agent_token="",
    )
    agent = LocalAgent(config)
    agent.session = BundleSession(BundleResponse(content + b"changed", "business-v1", sha256))

    with pytest.raises(RuntimeError, match="完整性校验失败"):
        agent.ensure_release({"version": "business-v1", "sha256": sha256})

    assert not (tmp_path / "current-release.json").exists()


def test_agent_rejects_zip_path_traversal(tmp_path):
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../outside.py", "bad")
    destination = tmp_path / "destination"
    destination.mkdir()
    config = SimpleNamespace(
        data_dir=tmp_path,
        server_url="https://workbench.example",
        agent_token="",
    )
    agent = LocalAgent(config)

    with pytest.raises(RuntimeError, match="不安全"):
        agent._safe_extract(archive_path, destination)

    assert not (tmp_path / "outside.py").exists()


def test_agent_uploads_live_logs_and_worker_result(tmp_path, monkeypatch):
    config = SimpleNamespace(data_dir=tmp_path, server_url="https://workbench.example",
                             agent_token="", db_api_token="test-only", heartbeat_seconds=0.1)
    agent = LocalAgent(config)
    agent.current_release = "runtime-test"
    release = tmp_path / "releases" / agent.current_release
    release.mkdir(parents=True)
    (release / "local_agent_worker.py").write_text('''
import argparse, json, time
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--job-file')
parser.add_argument('--cancel-file')
args = parser.parse_args()
print('live daily progress', flush=True)
time.sleep(2.5)
Path(args.job_file).with_name('result.json').write_text(json.dumps({
    'status': 'partial', 'message': 'one task needs attention', 'execution_counts': {'failed': 1}
}), encoding='utf-8')
''', encoding="utf-8")
    events = []
    def event(job_id, **data):
        events.append(data)
        if data.get("content"):
            assert not (tmp_path / "jobs" / job_id / "result.json").exists(), "Small logs must arrive before the worker exits"
    monkeypatch.setattr(agent, "heartbeat", lambda **_kwargs: {})
    monkeypatch.setattr(agent, "send_event", event)
    agent.run_job({"job_id": "daily-runtime-job", "job_type": "daily_task", "payload": {}})
    assert "live daily progress" in events[0]["content"]
    assert not events[0]["content"].startswith("[")
    assert events[-1]["result"]["execution_counts"] == {"failed": 1}
    assert events[-1]["result"]["return_code"] == 0
    assert events[-1]["message"] == "one task needs attention"
    local_log = (tmp_path / "agent.log").read_text(encoding="utf-8")
    assert re.search(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
        r"任务 daily-runtime-job｜live daily progress",
        local_log,
    )
    assert "任务 daily-runtime-job 已结束：one task needs attention" in local_log


def test_agent_log_upload_failure_uses_backoff_without_dropping_content(
    tmp_path, monkeypatch
):
    config = SimpleNamespace(
        data_dir=tmp_path,
        server_url="https://workbench.example",
        agent_token="",
        db_api_token="test-only",
        heartbeat_seconds=10,
        agent_id="agent-test",
        name="测试电脑",
    )
    agent = LocalAgent(config)
    agent.current_release = "runtime-backoff-test"
    release = tmp_path / "releases" / agent.current_release
    release.mkdir(parents=True)
    (release / "local_agent_worker.py").write_text('''
import argparse, json, time
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--job-file')
parser.add_argument('--cancel-file')
args = parser.parse_args()
print('x' * 40000, flush=True)
time.sleep(0.8)
Path(args.job_file).with_name('result.json').write_text(json.dumps({}), encoding='utf-8')
''', encoding="utf-8")
    content_attempts = []

    def event(_job_id, **data):
        if data.get("content"):
            content_attempts.append((time.monotonic(), data["content"]))
            if len(content_attempts) == 1:
                raise RuntimeError("temporary 502")

    monkeypatch.setattr(agent, "heartbeat", lambda **_kwargs: {})
    monkeypatch.setattr(agent, "send_event", event)
    monkeypatch.setattr(agent, "_retry_delay", lambda _failures: 0.3)

    agent.run_job({"job_id": "daily-backoff-job", "job_type": "daily_task", "payload": {}})

    assert len(content_attempts) == 2
    assert content_attempts[1][0] - content_attempts[0][0] >= 0.25
    assert content_attempts[1][1] == content_attempts[0][1]


def test_agent_shutdown_terminates_active_worker_tree(tmp_path, monkeypatch):
    config = SimpleNamespace(
        data_dir=tmp_path,
        server_url="https://workbench.example",
        agent_token="",
        db_api_token="test-only",
        heartbeat_seconds=0.1,
        agent_id="agent-shutdown-test",
        name="关闭测试",
    )
    agent = LocalAgent(config)
    agent.current_release = "runtime-shutdown-test"
    release = tmp_path / "releases" / agent.current_release
    release.mkdir(parents=True)
    (release / "local_agent_worker.py").write_text(
        "import time\nprint('worker started', flush=True)\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    events = []
    monkeypatch.setattr(agent, "heartbeat", lambda **_kwargs: {})
    monkeypatch.setattr(agent, "send_event", lambda _job_id, **data: events.append(data))

    runner = threading.Thread(
        target=agent.run_job,
        args=({"job_id": "shutdown-job", "job_type": "daily_task", "payload": {}},),
    )
    runner.start()
    deadline = time.monotonic() + 5
    while agent._current_worker is None and time.monotonic() < deadline:
        time.sleep(0.02)

    agent.request_shutdown("test")
    runner.join(timeout=10)

    assert not runner.is_alive()
    assert any(event.get("status") == "stopped" for event in events)


def test_agent_local_stop_terminates_worker_but_keeps_agent_running(tmp_path, monkeypatch):
    config = SimpleNamespace(
        data_dir=tmp_path,
        server_url="https://workbench.example",
        agent_token="",
        db_api_token="test-only",
        heartbeat_seconds=0.1,
        agent_id="agent-local-stop-test",
        name="本机停止测试",
    )
    agent = LocalAgent(config)
    agent.current_release = "runtime-local-stop-test"
    release = tmp_path / "releases" / agent.current_release
    release.mkdir(parents=True)
    (release / "local_agent_worker.py").write_text(
        "import time\nprint('worker started', flush=True)\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    events = []
    monkeypatch.setattr(agent, "heartbeat", lambda **_kwargs: {})
    monkeypatch.setattr(agent, "send_event", lambda _job_id, **data: events.append(data))

    runner = threading.Thread(
        target=agent.run_job,
        args=({"job_id": "local-stop-job", "job_type": "daily_task", "payload": {}},),
    )
    runner.start()
    deadline = time.monotonic() + 5
    while not agent.current_job_id() and time.monotonic() < deadline:
        time.sleep(0.02)

    assert agent.request_stop_current_job("test") == "local-stop-job"
    runner.join(timeout=10)

    assert not runner.is_alive()
    assert not agent.shutdown_event.is_set()
    assert any(event.get("status") == "stopped" for event in events)


def test_agent_stops_worker_when_control_lease_cannot_be_renewed(tmp_path, monkeypatch):
    config = SimpleNamespace(
        data_dir=tmp_path,
        server_url="https://workbench.example",
        agent_token="",
        db_api_token="test-only",
        heartbeat_seconds=0.1,
        agent_id="agent-lease-loss-test",
        name="租约中断测试",
    )
    agent = LocalAgent(config)
    agent.current_release = "runtime-lease-loss-test"
    release = tmp_path / "releases" / agent.current_release
    release.mkdir(parents=True)
    (release / "local_agent_worker.py").write_text(
        "import time\nprint('worker started', flush=True)\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    events = []

    def failed_heartbeat(**_kwargs):
        raise RuntimeError("control plane offline")

    monkeypatch.setattr(agent, "heartbeat", failed_heartbeat)
    monkeypatch.setattr(agent, "send_event", lambda _job_id, **data: events.append(data))
    monkeypatch.setattr(agent, "_retry_delay", lambda _failures: 0.05)
    monkeypatch.setattr(local_agent, "JOB_CONTROL_LOSS_STOP_SECONDS", 0.2)

    agent.run_job(
        {"job_id": "lease-loss-job", "job_type": "daily_task", "payload": {}}
    )

    assert any(event.get("status") == "stopped" for event in events)
    assert (tmp_path / "jobs" / "lease-loss-job" / "cancel.requested").exists()
