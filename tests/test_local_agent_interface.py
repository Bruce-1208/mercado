import io
import json
import zipfile

import pytest

from bit import bit_interface
from bit.local_agent_hub import LocalAgentStore


@pytest.fixture
def agent_interface(monkeypatch, tmp_path):
    user = {
        "id": 7,
        "username": "operator",
        "display_name": "操作员",
        "permissions": ["appeal.view", "appeal.execute"],
        "access_version": 1,
        "is_active": True,
    }
    store = LocalAgentStore(tmp_path / "hub.sqlite3")
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setattr(bit_interface.app, "testing", True)
    monkeypatch.setattr(bit_interface.app, "secret_key", "agent-interface-test-secret")
    monkeypatch.setattr(bit_interface, "get_current_workbench_user", lambda: user)
    monkeypatch.setattr(bit_interface, "get_workbench_user", lambda **_kwargs: user)
    monkeypatch.setattr(bit_interface, "build_workbench_session_user", lambda row: dict(row))
    monkeypatch.setattr(bit_interface, "get_local_agent_store", lambda: store)
    monkeypatch.setattr(
        bit_interface,
        "current_local_agent_bundle",
        lambda force=False: {
            "version": "bundle-test",
            "sha256": "a" * 64,
            "size": 123,
            "content": b"test-bundle",
        },
    )
    monkeypatch.setattr(
        bit_interface,
        "validate_authorized_appeal_sites",
        lambda _name, sites: tuple(sites),
    )
    return user, store, bit_interface.app.test_client()


def test_console_download_enroll_heartbeat_and_list(agent_interface):
    user, store, client = agent_interface
    download = client.get("/api/local-agents/download")
    assert download.status_code == 200
    assert download.headers["X-Agent-Package-Format"] == "python-source"
    with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
        config = json.loads(archive.read("local-agent.json"))
    assert config["server_url"] == "https://zeshun.nat100.top"

    enrollment_token = config["enrollment_token"]
    enrolled = client.post(
        "/api/local-agents/enroll",
        headers={"Authorization": f"Bearer {enrollment_token}"},
        json={
            "agent_id": "agent-office-pc",
            "name": "办公室电脑",
            "hostname": "OFFICE-PC",
            "platform": "Windows",
            "agent_version": "1.0.0",
            "capabilities": ["appeal"],
        },
    )
    assert enrolled.status_code == 200
    agent_token = enrolled.get_json()["data"]["agent_token"]
    assert agent_token.startswith("agent:")

    heartbeat = client.post(
        "/api/local-agents/heartbeat",
        headers={"X-Local-Agent-Token": agent_token},
        json={
            "agent_id": "agent-office-pc",
            "name": "办公室电脑",
            "business_version": "bundle-test",
            "capabilities": ["appeal"],
        },
    )
    assert heartbeat.status_code == 200
    assert heartbeat.get_json()["data"]["bundle"]["version"] == "bundle-test"

    database_health = client.get(
        "/api/db/health",
        headers={"X-Internal-Token": agent_token},
    )
    assert database_health.status_code == 200
    assert database_health.get_json()["data"]["role"] == "server"

    listed = client.get("/api/execution-agents")
    agents = listed.get_json()["data"]["agents"]
    assert [(row["agent_id"], row["online"]) for row in agents] == [
        ("agent-office-pc", True)
    ]


def test_console_renders_agent_as_default_appeal_execution_target(agent_interface):
    user, _store, client = agent_interface
    with client.session_transaction() as browser_session:
        browser_session["workbench_user"] = user

    response = client.get("/")

    assert response.status_code == 200
    assert b'<option value="agent" selected>' in response.data
    assert b'id="appeal-agent-id"' in response.data
    assert b'id="download-local-agent"' in response.data


def test_public_appeal_is_queued_for_selected_agent_and_streamed(agent_interface):
    _user, store, client = agent_interface
    store.heartbeat(
        "agent-appeal-pc",
        name="申诉电脑",
        capabilities=["appeal"],
    )
    response = client.get(
        "/api/run_shensu",
        query_string=[
            ("name", "测试店铺"),
            ("site", "墨西哥"),
            ("form", "侵权"),
            ("loop_count", "10"),
            ("mode", "AI客服"),
            ("task_id", "agent-appeal-job"),
            ("execution_target", "agent"),
            ("agent_id", "agent-appeal-pc"),
        ],
        buffered=False,
    )
    assert response.status_code == 200
    assert response.headers["X-Execution-Target"] == "agent"
    queued = store.get_job("agent-appeal-job")
    assert queued["status"] == "queued"
    assert queued["payload"]["name"] == "测试店铺"

    claimed = store.claim_job("agent-appeal-pc")
    assert claimed["status"] == "running"
    store.append_event(
        "agent-appeal-job",
        "agent-appeal-pc",
        content="本机申诉完成\n",
        status="success",
        message="完成",
    )
    body = response.get_data(as_text=True)
    assert "任务已进入本机 Agent 队列" in body
    assert "本机申诉完成" in body


def test_public_stop_marks_agent_job_for_cancellation(agent_interface):
    _user, store, client = agent_interface
    store.heartbeat("agent-stop-pc", name="停止电脑", capabilities=["appeal"])
    store.enqueue_job("agent-stop-job", "agent-stop-pc", "appeal", {})
    store.claim_job("agent-stop-pc")

    response = client.post(
        "/api/run_shensu/stop", json={"task_id": "agent-stop-job"}
    )

    assert response.status_code == 200
    assert store.get_job("agent-stop-job")["status"] == "stopping"
    assert store.cancellation_job_ids("agent-stop-pc") == ["agent-stop-job"]
