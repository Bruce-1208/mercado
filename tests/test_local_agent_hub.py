import hashlib
import io
import json
import zipfile
from pathlib import Path

from bit.local_agent_bundle import build_business_bundle
from bit.local_agent_distribution import build_agent_distribution
from bit.local_agent_hub import LocalAgentStore


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


def test_business_bundle_is_versioned_and_contains_worker(tmp_path):
    (tmp_path / "bit").mkdir()
    (tmp_path / "bit" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "local_agent_worker.py").write_text("print('ok')\n", encoding="utf-8")

    bundle = build_business_bundle(tmp_path)

    assert hashlib.sha256(bundle["content"]).hexdigest() == bundle["sha256"]
    with zipfile.ZipFile(io.BytesIO(bundle["content"])) as archive:
        manifest = json.loads(archive.read("bundle-manifest.json"))
        assert manifest["version"] == bundle["version"]
        assert "local_agent_worker.py" in manifest["files"]


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
        assert "install-agent.ps1" in archive.namelist()


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
