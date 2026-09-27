from datetime import datetime, timedelta

import pytest

from bit import bit_interface as web
from bit.local_agent_hub import LocalAgentStore


@pytest.fixture
def member_console(monkeypatch, tmp_path):
    user = {"id": 71, "username": "member", "display_name": "本人", "role_key": "member",
            "permissions": ["*"], "access_version": 1, "organization_key": "wuhan-zeshun"}
    store = LocalAgentStore(tmp_path / "hub.sqlite3")
    monkeypatch.setattr(web.app, "testing", True)
    monkeypatch.setattr(web, "get_current_workbench_user", lambda: user)
    monkeypatch.setattr(web, "get_local_agent_store", lambda: store)
    monkeypatch.setattr(web, "db_get_window_anomalies", lambda **kw: {"rows": []})
    return user, store, web.app.test_client()


def test_register_link_with_remembered_session_still_renders_form(member_console):
    user, store, client = member_console
    with client.session_transaction() as session:
        session["workbench_user"] = user
    response = client.get("/register")
    assert response.status_code == 200
    assert b'id="register-form"' in response.data


def test_registration_does_not_create_session(member_console, monkeypatch):
    _, _, client = member_console
    monkeypatch.setattr(web, "_workbench_backend", lambda *args: {"id": 88})
    assert client.post("/api/register", json={}).status_code == 200
    with client.session_transaction() as session:
        assert "workbench_user" not in session


def test_member_scope_cannot_be_disabled_by_role_record():
    user = web.build_workbench_session_user({"id": 71, "username": "member", "role_key": "member", "own_store_only": 0})
    assert user["own_store_only"] is True


def test_members_only_list_and_dispatch_their_agents(member_console, monkeypatch):
    user, store, client = member_console
    for agent_id, owner in (("own-agent", 71), ("other-agent", 72), ("old-agent", None)):
        store.heartbeat(agent_id, name=agent_id, owner_user_id=owner, capabilities=["appeal"])
    monkeypatch.setattr(web, "_filter_store_rows_for_user", lambda data, **kw: data)
    response = client.get("/api/execution-agents")
    assert response.status_code == 200
    assert [a["agent_id"] for a in response.json["data"]["agents"]] == ["own-agent"]
    for agent_id in ("other-agent", "old-agent", "missing-agent"):
        response = client.post("/api/run_shensu", json={"agent_id": agent_id, "execution_target": "agent"})
        assert response.status_code == 403
    with web.app.test_request_context():
        assert web.workbench_user_can_use_agent(store.get_agent("own-agent"))
        response, status = web.enqueue_local_agent_daily_task("other-agent", {})
        assert status == 403
    assert not store.list_jobs()


def test_owner_persists_and_cannot_be_reassigned(tmp_path):
    path = tmp_path / "hub.sqlite3"
    store = LocalAgentStore(path)
    store.heartbeat("owned-agent", name="my pc", owner_user_id=71)
    store.heartbeat("owned-agent", name="my pc")
    assert LocalAgentStore(path).get_agent("owned-agent")["owner_user_id"] == 71
    with pytest.raises(ValueError):
        store.heartbeat("owned-agent", name="hijack", owner_user_id=72)
    assert store.get_agent("owned-agent")["name"] == "my pc"
    store.heartbeat("legacy-agent", name="legacy")
    with pytest.raises(ValueError):
        store.heartbeat("legacy-agent", name="hijack", owner_user_id=72)
    store.heartbeat("legacy-agent", name="legacy", owner_user_id=71, claim_unowned=True)
    assert store.get_agent("legacy-agent")["owner_user_id"] == 71


def test_public_registration_fixes_company_role_and_pending_state(monkeypatch):
    inserts = []
    class Cursor:
        lastrowid = 71
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params=None):
            self.sql = sql
            if "INSERT INTO `workbench_users`" in sql:
                inserts.append((sql, params))
        def fetchone(self):
            if "code_hash" in self.sql:
                return {"code_hash": web._workbench_registration_code_hash("new@example.com", "123456"),
                        "expires_at": datetime.utcnow() + timedelta(minutes=5), "attempts": 0}
            return None
    class Connection:
        def cursor(self): return Cursor()
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass
    monkeypatch.setattr(web.pymysql, "connect", lambda **kw: Connection())
    monkeypatch.setattr(web, "_require_workbench_role", lambda *args: None)
    monkeypatch.setattr(web, "build_workbench_session_user", lambda row: {})
    monkeypatch.setenv("WORKBENCH_DEFAULT_ORGANIZATION_KEY", "other-company")
    web.register_workbench_user_local({"username": "new-user", "password": "test-password-123",
        "email": "new@example.com", "verification_code": "123456", "display_name": "新成员",
        "wechat_id": "wechat-new", "phone": "13800138000", "role_key": "super_admin",
        "organization_key": "other-company", "is_active": True})
    sql, params = inserts[0]
    assert "'member', 0" in sql
    assert params[6] == "wuhan-zeshun"


def test_member_can_enqueue_own_agent(member_console, monkeypatch):
    user, store, _ = member_console
    store.heartbeat("own-agent", name="my pc", owner_user_id=user["id"], capabilities=["daily_task"])
    monkeypatch.setattr(web, "current_local_agent_bundle", lambda: {"version": "test"})
    with web.app.test_request_context():
        response = web.enqueue_local_agent_daily_task("own-agent", {})
        assert response.status_code == 200
    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["created_by_id"] == user["id"]


def test_enrollment_binds_verified_identity_and_rejects_other_owner(member_console, monkeypatch):
    user, store, client = member_console
    monkeypatch.setattr(web, "USE_DB_API", False)
    monkeypatch.setattr(web, "_local_agent_enrollment_user", lambda token: user)
    response = client.post("/api/local-agents/enroll", json={"agent_id": "own-agent", "name": "my pc", "owner_user_id": 72})
    assert response.status_code == 200
    assert store.get_agent("own-agent")["owner_user_id"] == 71
    with web.app.test_request_context():
        token = response.json["data"]["agent_token"]
        assert web._verify_local_agent_credential(token)["user_id"] == 71
        assert web._verify_local_agent_credential(web.create_local_agent_credential("own-agent", 72)) is None
    monkeypatch.setattr(web, "_local_agent_enrollment_user", lambda token: {**user, "id": 72})
    assert client.post("/api/local-agents/enroll", json={"agent_id": "own-agent", "name": "other pc"}).status_code == 400
    assert store.get_agent("own-agent")["name"] == "my pc"


def test_legacy_signed_heartbeat_migrates_owner(member_console, monkeypatch):
    user, store, client = member_console
    monkeypatch.setattr(web, "USE_DB_API", False)
    monkeypatch.setattr(web, "current_local_agent_bundle", lambda: {"version": "test", "sha256": "test", "size": 0})
    store.heartbeat("legacy-agent", name="old pc")
    token = web.create_local_agent_credential("legacy-agent", user["id"])
    response = client.post("/api/local-agents/heartbeat", headers={"X-Local-Agent-Token": token},
                           json={"agent_id": "legacy-agent", "name": "old pc"})
    assert response.status_code == 200
    assert store.get_agent("legacy-agent")["owner_user_id"] == user["id"]
