import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import pytest

from bit.local_agent_bundle import build_business_bundle
from bit.local_agent_distribution import build_agent_distribution
from bit.local_agent_hub import LocalAgentStore, migrate_local_agent_hub


def test_agent_heartbeat_queue_claim_log_and_completion(tmp_path):
    store = LocalAgentStore(tmp_path / "hub.sqlite3")
    agent = store.heartbeat(
        "agent-test-pc",
        name="测试电脑",
        hostname="TEST-PC",
        platform="Windows",
        agent_version="1.0.0",
        business_version="bundle-old",
        capabilities=["appeal"],
        now=100,
    )
    assert agent["online"] is True
    assert agent["capabilities"] == ["appeal"]

    queued = store.enqueue_job(
        "appeal-test-job",
        "agent-test-pc",
        "appeal",
        {"name": "测试店铺"},
        required_version="bundle-new",
        created_by_id=7,
        created_by_name="操作员",
        now=101,
    )
    assert queued["status"] == "queued"
    claimed = store.claim_job("agent-test-pc", now=102)
    assert claimed["job_id"] == "appeal-test-job"
    assert claimed["status"] == "running"

    updated = store.append_event(
        "appeal-test-job",
        "agent-test-pc",
        content="申诉日志\n",
        status="success",
        message="执行完成",
        result={"return_code": 0},
        now=103,
    )
    assert updated["status"] == "success"
    assert updated["result"] == {"return_code": 0}
    assert "申诉日志" in "".join(
        event["content"] for event in store.events_after("appeal-test-job")
    )
    event_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(103))
    assert f"[{event_time}] 申诉日志" in store.recent_log("appeal-test-job")


def test_queue_has_stable_identity_and_legacy_database_is_migrated(tmp_path):
    legacy_path = tmp_path / "old-checkout" / ".data" / "local-agent-hub.sqlite3"
    stable_path = tmp_path / "stable-data" / "local-agent-hub.sqlite3"
    legacy = LocalAgentStore(legacy_path)
    legacy.heartbeat("agent-migrated-pc", name="迁移电脑", now=100)
    legacy.enqueue_job(
        "migrated-job", "agent-migrated-pc", "daily_task", {}, now=101
    )
    legacy_queue_id = legacy.queue_id

    assert migrate_local_agent_hub(legacy_path, stable_path) is True
    migrated = LocalAgentStore(stable_path)
    assert migrated.queue_id == legacy_queue_id
    assert migrated.get_agent("agent-migrated-pc")["name"] == "迁移电脑"
    assert migrated.get_job("migrated-job")["status"] == "queued"
    assert migrate_local_agent_hub(legacy_path, stable_path) is False


def test_agent_cancel_is_reported_to_claimed_agent(tmp_path):
    store = LocalAgentStore(tmp_path / "hub.sqlite3")
    store.heartbeat("agent-cancel-pc", name="取消测试", now=100)
    store.enqueue_job(
        "appeal-cancel-job", "agent-cancel-pc", "appeal", {}, now=101
    )
    store.claim_job("agent-cancel-pc", now=102)

    assert store.request_cancel("appeal-cancel-job", now=103) is True
    assert store.cancellation_job_ids("agent-cancel-pc") == ["appeal-cancel-job"]
    assert store.get_job("appeal-cancel-job")["status"] == "stopping"


def test_new_agent_session_releases_crashed_job_and_claims_next(tmp_path):
    store = LocalAgentStore(tmp_path / "hub.sqlite3")
    store.heartbeat(
        "agent-restart-pc", name="重启测试", session_id="session-old", now=100
    )
    store.enqueue_job(
        "daily-crashed-job", "agent-restart-pc", "daily_task", {}, now=101
    )
    store.claim_job(
        "agent-restart-pc", session_id="session-old", now=102
    )
    store.enqueue_job(
        "daily-next-job", "agent-restart-pc", "daily_task", {}, now=103
    )

    store.heartbeat(
        "agent-restart-pc", name="重启测试", session_id="session-new", now=104
    )
    claimed = store.claim_job(
        "agent-restart-pc", session_id="session-new", now=105
    )

    crashed = store.get_job("daily-crashed-job")
    assert crashed["status"] == "error"
    assert crashed["finished_at"] == 104
    assert claimed["job_id"] == "daily-next-job"


def test_expired_legacy_job_lease_does_not_block_queue_forever(tmp_path):
    store = LocalAgentStore(tmp_path / "hub.sqlite3")
    store.heartbeat("agent-lease-pc", name="租约测试", now=100)
    store.enqueue_job("daily-old-job", "agent-lease-pc", "daily_task", {}, now=101)
    store.claim_job("agent-lease-pc", lease_seconds=60, now=102)
    store.enqueue_job("daily-new-job", "agent-lease-pc", "daily_task", {}, now=103)

    claimed = store.claim_job("agent-lease-pc", lease_seconds=60, now=163)

    assert store.get_job("daily-old-job")["status"] == "error"
    assert claimed["job_id"] == "daily-new-job"


def test_reaper_keeps_legacy_job_while_old_agent_is_still_online(tmp_path):
    store = LocalAgentStore(tmp_path / "hub.sqlite3")
    store.heartbeat("agent-legacy-pc", name="旧版 Agent", now=100)
    store.enqueue_job("daily-legacy-job", "agent-legacy-pc", "daily_task", {}, now=101)
    store.claim_job("agent-legacy-pc", now=102)
    with store._connect() as connection:
        connection.execute(
            "UPDATE local_agent_jobs SET lease_expires_at = NULL WHERE job_id = ?",
            ("daily-legacy-job",),
        )
    store.heartbeat("agent-legacy-pc", name="旧版 Agent", now=1000)

    assert store.reap_expired_jobs(now=1001, lease_seconds=60) == 0
    assert store.get_job("daily-legacy-job")["status"] == "running"
    assert store.reap_expired_jobs(now=1061, lease_seconds=60) == 1
    assert store.get_job("daily-legacy-job")["status"] == "error"


def test_claimed_deepseek_token_is_delivered_once_then_redacted(tmp_path):
    store = LocalAgentStore(tmp_path / "hub.sqlite3")
    store.heartbeat("agent-secret-pc", name="密钥测试", now=100)
    store.enqueue_job(
        "appeal-secret-job",
        "agent-secret-pc",
        "appeal",
        {
            "mode": "AI客服",
            "appeal_copy_mode": "AI话术模式",
            "deepseek_api_key": "manual-secret",
        },
        now=101,
    )

    claimed = store.claim_job("agent-secret-pc", now=102)

    assert claimed["payload"]["deepseek_api_key"] == "manual-secret"
    assert "deepseek_api_key" not in store.get_job("appeal-secret-job")["payload"]


def test_daily_agent_history_is_pruned_after_retention_window(tmp_path):
    store = LocalAgentStore(tmp_path / "hub.sqlite3")
    store.heartbeat("agent-retention-pc", name="日志保留测试", now=100)
    for job_id, finished_at in (("daily-old-job", 110), ("daily-current-job", 290)):
        store.enqueue_job(job_id, "agent-retention-pc", "daily_task", {}, now=finished_at - 2)
        store.claim_job("agent-retention-pc", now=finished_at - 1)
        store.append_event(
            job_id,
            "agent-retention-pc",
            content=f"{job_id} 日志\n",
            status="success",
            now=finished_at,
        )

    removed = store.prune_job_history(
        retention_seconds=100,
        job_type="daily_task",
        now=300,
    )

    assert removed == 1
    assert store.get_job("daily-old-job") is None
    assert store.events_after("daily-old-job") == []
    assert store.get_job("daily-current-job") is not None


def test_business_bundle_is_versioned_and_contains_worker(tmp_path):
    (tmp_path / "bit").mkdir()
    (tmp_path / "bit" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "local_agent_worker.py").write_text("print('ok')\n", encoding="utf-8")
    extension = tmp_path / "browser_extension" / "zeshun_collector"
    extension.mkdir(parents=True)
    (extension / "manifest.json").write_text('{"version":"1.0"}', encoding="utf-8")
    background = extension / "background.js"
    background.write_text("// worker\n", encoding="utf-8")

    bundle = build_business_bundle(tmp_path)

    assert hashlib.sha256(bundle["content"]).hexdigest() == bundle["sha256"]
    with zipfile.ZipFile(io.BytesIO(bundle["content"])) as archive:
        manifest = json.loads(archive.read("bundle-manifest.json"))
        assert manifest["version"] == bundle["version"]
        assert "local_agent_worker.py" in manifest["files"]
        assert "browser_extension/zeshun_collector/manifest.json" in manifest["files"]
        assert archive.read("browser_extension/zeshun_collector/background.js") == background.read_bytes()


def test_download_package_embeds_server_and_enrollment_token(monkeypatch, tmp_path):
    monkeypatch.delenv("BIT_LOCAL_AGENT_EXECUTABLE", raising=False)
    source = Path(__file__).resolve().parents[1] / "local_agent.py"
    (tmp_path / "local_agent.py").write_bytes(source.read_bytes())

    package = build_agent_distribution(
        tmp_path,
        server_url="https://workbench.example",
        enrollment_token="one-time-enrollment",
    )

    with zipfile.ZipFile(io.BytesIO(package["content"])) as archive:
        config = json.loads(archive.read("local-agent.json"))
        assert config["server_url"] == "https://workbench.example"
        assert config["enrollment_token"] == "one-time-enrollment"
        assert config["poll_seconds"] == 10
        assert "install-agent.ps1" in archive.namelist()
        assert "uninstall-agent.ps1" in archive.namelist()


def test_download_package_uses_configured_windows_executable(monkeypatch, tmp_path):
    executable = tmp_path / "artifacts" / "MercadoLocalAgent.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"windows-agent")
    monkeypatch.setenv("BIT_LOCAL_AGENT_EXECUTABLE", str(executable))

    package = build_agent_distribution(
        tmp_path,
        server_url="https://workbench.example",
        enrollment_token="enrollment",
    )

    assert package["format"] == "windows-exe"
    with zipfile.ZipFile(io.BytesIO(package["content"])) as archive:
        assert archive.read("MercadoLocalAgent.exe") == b"windows-agent"
        assert "local_agent.py" not in archive.namelist()
        start_script = archive.read("start-agent.bat").decode("utf-8")
        assert 'start "" "%~dp0MercadoLocalAgent.exe"' in start_script
        install_script = archive.read("install-agent.ps1").decode("utf-8")
        assert "-LogonType Interactive" in install_script
        assert "-AtLogOn -User $currentUser" in install_script


def test_download_package_supports_macos_source_install(monkeypatch, tmp_path):
    monkeypatch.delenv("BIT_LOCAL_AGENT_MACOS_EXECUTABLE", raising=False)
    source = Path(__file__).resolve().parents[1] / "local_agent.py"
    (tmp_path / "local_agent.py").write_bytes(source.read_bytes())

    package = build_agent_distribution(
        tmp_path,
        server_url="https://workbench.example",
        enrollment_token="mac-enrollment",
        target_platform="macos",
    )

    assert package["platform"] == "macos"
    assert package["format"] == "python-source"
    with zipfile.ZipFile(io.BytesIO(package["content"])) as archive:
        names = set(archive.namelist())
        assert {
            "local_agent.py",
            "run-agent.sh",
            "start-agent.command",
            "install-agent.command",
            "uninstall-agent.command",
        }.issubset(names)
        assert "start-agent.bat" not in names
        assert "LaunchAgents" in archive.read("install-agent.command").decode("utf-8")
        readme = archive.read("README.txt").decode("utf-8")
        assert "运行状态窗口会实时显示本机时间和日志" in readme
        for name in ("run-agent.sh", "start-agent.command", "install-agent.command"):
            assert archive.getinfo(name).external_attr >> 16 & 0o111


def test_download_package_uses_configured_macos_executable(monkeypatch, tmp_path):
    executable = tmp_path / "artifacts" / "MercadoLocalAgent"
    executable.parent.mkdir()
    executable.write_bytes(b"macos-agent")
    monkeypatch.setenv("BIT_LOCAL_AGENT_MACOS_EXECUTABLE", str(executable))

    package = build_agent_distribution(
        tmp_path,
        server_url="https://workbench.example",
        enrollment_token="enrollment",
        target_platform="darwin",
    )

    assert package["format"] == "macos-executable"
    with zipfile.ZipFile(io.BytesIO(package["content"])) as archive:
        assert archive.read("MercadoLocalAgent") == b"macos-agent"
        assert "local_agent.py" not in archive.namelist()


def test_download_package_rejects_unknown_platform(tmp_path):
    with pytest.raises(ValueError, match="windows 或 macos"):
        build_agent_distribution(
            tmp_path,
            server_url="https://workbench.example",
            enrollment_token="enrollment",
            target_platform="linux",
        )
