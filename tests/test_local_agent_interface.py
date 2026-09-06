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


def test_console_download_enroll_heartbeat_and_list(agent_interface, monkeypatch, tmp_path):
    user, store, client = agent_interface
    (tmp_path / "local_agent.py").write_text("# test source", encoding="utf-8")
    monkeypatch.setattr(bit_interface, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("BIT_LOCAL_AGENT_EXECUTABLE", raising=False)
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


@pytest.fixture
def daily_agent_interface(agent_interface):
    user, store, client = agent_interface
    user["permissions"] = ["tasks.view", "tasks.execute"]
    store.heartbeat("agent-daily-pc", name="任务电脑", capabilities=["appeal", "daily_task"])
    return user, store, client


def test_daily_task_uses_agent_queue_and_reports_logs(daily_agent_interface, monkeypatch):
    _user, store, client = daily_agent_interface
    def unexpected_local_run(**_kwargs):
        pytest.fail("Agent job must not run on the web server")
    monkeypatch.setattr(bit_interface.bit_daily_task, "acquire_daily_task_lock", unexpected_local_run)
    response = client.post("/api/tasks/daily/start", json={
        "execution_target": "agent", "agent_id": "agent-daily-pc", "mode": "once",
        "appeal_types": ["侵权", "延误率"], "salespeople": ["业务员A"], "max_workers": 4,
    })
    assert response.status_code == 200
    task = response.get_json()["data"]
    job_id = task["task_id"]
    assert task["execution_target"] == "agent"
    assert task["status"] == "queued"
    assert store.get_job(job_id)["payload"]["max_workers"] == 4
    claimed = store.claim_job("agent-daily-pc")
    assert claimed["job_type"] == "daily_task"
    store.append_event(job_id, "agent-daily-pc", content="来自执行电脑的日志\n", status="success",
                       message="部分完成", result={"status": "partial", "execution_counts": {"replied": 1, "failed": 1}})
    response = client.get("/api/tasks/daily/status", query_string={"execution_target": "agent", "task_id": job_id})
    state = response.get_json()["data"]
    assert state["status"] == "partial"
    assert state["running"] is False
    assert "来自执行电脑的日志" in state["log"]
    assert state["agent_name"] == "任务电脑"
    assert state["execution_counts"]["failed"] == 1
    summary = client.get("/api/tasks/daily/status?execution_target=agent&include_logs=0").get_json()["data"]
    assert summary["total_count"] == 1
    assert "log" not in summary["tasks"][0]


@pytest.mark.parametrize("claimed", [False, True])
def test_daily_agent_stop_reaches_the_selected_job(daily_agent_interface, claimed):
    _user, store, client = daily_agent_interface
    store.enqueue_job("daily-stop-job", "agent-daily-pc", "daily_task", {})
    if claimed:
        store.claim_job("agent-daily-pc")
    response = client.post("/api/tasks/daily/stop?execution_target=agent", json={"task_id": "daily-stop-job"})
    assert response.status_code == 200
    assert store.get_job("daily-stop-job")["status"] == ("stopping" if claimed else "stopped")
    assert store.claim_job("agent-daily-pc") is None


@pytest.mark.parametrize("agent_id,capabilities,now", [
    ("agent-missing", (), None), ("agent-old", ("appeal",), None),
    ("agent-offline", ("daily_task",), 1),
])
def test_daily_agent_rejects_unavailable_executor(daily_agent_interface, agent_id, capabilities, now):
    _user, store, client = daily_agent_interface
    if capabilities:
        store.heartbeat(agent_id, name=agent_id, capabilities=capabilities, now=now)
    response = client.post("/api/tasks/daily/start", json={"execution_target": "agent", "agent_id": agent_id})
    assert response.status_code == 409
    assert store.list_jobs() == []


def test_task_operator_can_enroll_and_list_daily_agents(daily_agent_interface, monkeypatch, tmp_path):
    user, _store, client = daily_agent_interface
    (tmp_path / "local_agent.py").write_text("# test source", encoding="utf-8")
    monkeypatch.setattr(bit_interface, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("BIT_LOCAL_AGENT_EXECUTABLE", raising=False)
    response = client.get("/api/local-agents/download")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        token = json.loads(archive.read("local-agent.json"))["enrollment_token"]
    assert bit_interface._local_agent_enrollment_user(token)["permission"] == "tasks.execute"
    agents = client.get("/api/execution-agents?capability=daily_task").get_json()["data"]["agents"]
    assert [agent["agent_id"] for agent in agents] == ["agent-daily-pc"]
    user["permissions"] = ["tasks.view"]
    assert bit_interface._local_agent_enrollment_user(token) is None
    assert client.post("/api/tasks/daily/start", json={"execution_target": "agent", "agent_id": "agent-daily-pc"}).status_code == 403


def test_daily_agent_endpoints_do_not_control_appeal_jobs(daily_agent_interface):
    _user, store, client = daily_agent_interface
    store.enqueue_job("appeal-other-job", "agent-daily-pc", "appeal", {})
    assert client.get("/api/tasks/daily/status?execution_target=agent&task_id=appeal-other-job").status_code == 404
    assert client.post("/api/tasks/daily/stop?execution_target=agent", json={"task_id": "appeal-other-job"}).status_code == 404
    assert store.get_job("appeal-other-job")["status"] == "queued"


@pytest.mark.parametrize("role,expected", [("server", "agent"), ("client", "local")])
def test_daily_page_defaults_to_the_appropriate_executor(daily_agent_interface, monkeypatch, tmp_path, role, expected):
    import re
    import shutil
    import subprocess
    from dataclasses import replace
    user, _store, client = daily_agent_interface
    with client.session_transaction() as browser_session:
        browser_session["workbench_user"] = user
    monkeypatch.setattr(bit_interface, "RUNTIME_SETTINGS", replace(bit_interface.RUNTIME_SETTINGS, role=role))
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    select = html.split('id="daily-task-execution-target"', 1)[1].split("</select>", 1)[0]
    assert re.findall(r'<option value="([^"]+)"\s+selected', select) == [expected]
    if node := shutil.which("node"):
        script = tmp_path / "workbench.js"
        script.write_text("\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)), encoding="utf-8")
        subprocess.run([node, "--check", str(script)], capture_output=True, check=True)
