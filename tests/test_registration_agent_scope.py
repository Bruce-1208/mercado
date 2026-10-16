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


def test_members_cannot_list_or_dispatch_even_their_own_agents(member_console):
    user, store, client = member_console
    store.heartbeat("own-agent", name="own", owner_user_id=user["id"], capabilities=["appeal"])
    assert client.get("/api/execution-agents").status_code == 403
    assert client.get("/api/local-agents/download").status_code == 403
    assert client.post("/api/run_shensu", json={"agent_id": "own-agent", "execution_target": "agent"}).status_code == 403
    with web.app.test_request_context():
        assert not web.workbench_user_can_use_agent(store.get_agent("own-agent"))
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
    assert "'basic_member', 0" in sql
    assert params[6] == "wuhan-zeshun"


def test_enterprise_admin_can_enqueue_own_agent(member_console, monkeypatch):
    user, store, _ = member_console
    user.update(role_key="enterprise_admin")
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
    user.update(role_key="enterprise_admin", is_active=True)
    monkeypatch.setattr(web, "get_workbench_user", lambda **kw: user)
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
    user.update(role_key="enterprise_admin", is_active=True)
    monkeypatch.setattr(web, "get_workbench_user", lambda **kw: user)
    monkeypatch.setattr(web, "USE_DB_API", False)
    monkeypatch.setattr(web, "current_local_agent_bundle", lambda: {"version": "test", "sha256": "test", "size": 0})
    store.heartbeat("legacy-agent", name="old pc")
    token = web.create_local_agent_credential("legacy-agent", user["id"])
    response = client.post("/api/local-agents/heartbeat", headers={"X-Local-Agent-Token": token},
                           json={"agent_id": "legacy-agent", "name": "old pc"})
    assert response.status_code == 200
    assert store.get_agent("legacy-agent")["owner_user_id"] == user["id"]


def test_enterprise_admin_agent_scope_uses_owner_company(member_console, monkeypatch):
    user, store, client = member_console
    user.update(role_key="enterprise_admin")
    monkeypatch.setattr(web, "_workbench_backend", lambda *args: [
        {"id": 72, "organization_key": "wuhan-zeshun"},
        {"id": 73, "organization_key": "other-company"},
    ])
    monkeypatch.setattr(web, "_filter_store_rows_for_user", lambda data, **kw: data)
    for name, owner in (("own", 71), ("company", 72), ("foreign", 73), ("unowned", None)):
        store.heartbeat(name, name=name, owner_user_id=owner, capabilities=["appeal"])
    response = client.get("/api/execution-agents")
    assert response.status_code == 200
    assert {a["agent_id"] for a in response.json["data"]["agents"]} == {"own", "company"}
    assert client.post("/api/run_shensu", json={"execution_target": "agent", "agent_id": "foreign"}).status_code == 403
    user.update(role_key="super_admin")
    assert {a["agent_id"] for a in client.get("/api/execution-agents").json["data"]["agents"]} == {"own", "company"}


@pytest.mark.parametrize("role", ["member", "enterprise_admin", "operator"])
def test_non_super_admin_cannot_request_server_execution(member_console, role):
    user, _, client = member_console
    user.update(role_key=role, is_platform_admin=True)
    assert client.post("/api/run_shensu", json={"execution_target": "server"}).status_code == 403
    assert client.get("/api/tasks/daily/status?execution_target=server").status_code == 403
    assert client.get("/api/tasks/daily/status").status_code == 403


def test_daily_jobs_follow_agent_visibility(member_console, monkeypatch):
    user, store, client = member_console
    user.update(role_key="enterprise_admin")
    monkeypatch.setattr(web, "_workbench_backend", lambda *args: [{"id": 72, "organization_key": "other-company"}])
    for name, owner in (("own", 71), ("other", 72)):
        store.heartbeat(name, name=name, owner_user_id=owner, capabilities=["daily_task"])
        store.enqueue_job(name + "-job", name, "daily_task", {})
    response = client.get("/api/tasks/daily/status?execution_target=agent")
    assert response.status_code == 200
    assert response.json["data"]["total_count"] == 1
    assert client.get("/api/tasks/daily/status?execution_target=agent&task_id=other-job").status_code == 403
    assert client.post("/api/tasks/daily/stop", json={"execution_target": "agent", "task_id": "other-job"}).status_code == 403
    assert store.get_job("other-job")["status"] == "queued"


@pytest.mark.parametrize("role", ["member", "enterprise_admin", "super_admin"])
def test_foreign_agent_job_ids_cannot_bypass_scope(member_console, monkeypatch, role):
    user, store, client = member_console
    user.update(role_key=role, is_platform_admin=role == "super_admin")
    monkeypatch.setattr(web, "_workbench_backend", lambda *args: [
        {"id": 72, "organization_key": "other-company"},
    ])
    store.heartbeat("foreign", name="foreign", owner_user_id=72, capabilities=["appeal"])
    store.enqueue_job("foreign-job", "foreign", "appeal", {}, created_by_id=72)
    assert client.post("/api/run_shensu/stop", json={"task_id": "foreign-job"}).status_code == 403
    assert client.get("/api/run_shensu/foreign-job/events").status_code == 403
    assert not store.get_job("foreign-job")["cancel_requested"]


def test_super_admin_default_and_explicit_company_scope(member_console, monkeypatch):
    user, store, client = member_console
    user.update(role_key="super_admin", is_platform_admin=True)
    monkeypatch.setattr(web, "_workbench_backend", lambda *args: [
        {"id": 72, "organization_key": "other-company"},
    ])
    monkeypatch.setattr(web, "_filter_store_rows_for_user", lambda data, **kw: data)
    store.heartbeat("own", name="own", owner_user_id=71, capabilities=["appeal"])
    store.heartbeat("foreign", name="foreign", owner_user_id=72, capabilities=["appeal"])
    assert {a["agent_id"] for a in client.get("/api/execution-agents?organization_key=other-company").json["data"]["agents"]} == {"foreign"}
    assert client.post("/api/run_shensu", json={"agent_id": "foreign", "execution_target": "agent"}).status_code == 403
    tokens = {"rows": [{"id": 1, "organization_key": "wuhan-zeshun"}, {"id": 2, "organization_key": "other-company"}]}
    with web.app.test_request_context():
        assert [r["id"] for r in web._filter_mercado_tokens_for_user(tokens)["rows"]] == [1]
    with web.app.test_request_context("/?organization_key=other-company"):
        assert [r["id"] for r in web._filter_mercado_tokens_for_user(tokens)["rows"]] == [2]


def test_invalid_agent_token_cannot_fall_back_to_loopback(member_console, monkeypatch):
    _, _, client = member_console
    monkeypatch.setattr(web, "USE_DB_API", False)
    assert client.get("/api/db/reputation/latest", headers={"X-Internal-Token": "agent:invalid"}).status_code == 403


@pytest.fixture
def scoped_worker(member_console, monkeypatch):
    user, store, client = member_console
    user.update(is_active=True, role_key="enterprise_admin")
    monkeypatch.setattr(web, "USE_DB_API", False)
    monkeypatch.setattr(web, "get_workbench_user", lambda **kw: user)
    monkeypatch.setattr(web.bit_db_api, "list_mercado_store_tokens", lambda: {"rows": [
        {"id": 1, "organization_key": "wuhan-zeshun", "display_name": "own shop"},
        {"id": 2, "organization_key": "other-company", "display_name": "foreign shop"},
    ]})
    store.heartbeat("worker", name="worker", owner_user_id=71, capabilities=["daily_task", "ai_weight_price"])
    store.enqueue_job("worker-job", "worker", "daily_task", {}, created_by_id=71)
    store.claim_job("worker")
    headers = {"X-Internal-Token": web.create_local_agent_credential("worker", 71)}
    return user, store, client, headers


def test_worker_tokens_and_rows_are_company_scoped(scoped_worker, monkeypatch):
    _, _, client, headers = scoped_worker
    response = client.get("/api/db/mercado-tokens?organization_key=other-company", headers=headers)
    assert response.status_code == 200
    assert [row["id"] for row in response.json["data"]["rows"]] == [1]
    monkeypatch.setattr(web, "db_get_latest_reputation_info", lambda: {"rows": [
        {"店铺名": "own shop"}, {"店铺名": "foreign shop"}], "total": 2})
    response = client.get("/api/db/reputation/latest", headers=headers)
    assert response.status_code == 200
    assert response.json["data"]["rows"] == [{"店铺名": "own shop"}]


@pytest.mark.parametrize("path,payload", [
    ("/api/db/official-infractions/live", {"targets": [{"token_id": 2}]}),
    ("/api/db/ai-appeal-product-contexts", {"shop_name": "foreign shop", "product_ids": []}),
    ("/api/db/ai-appeal-records", {"record": {"shop_name": "foreign shop"}}),
])
def test_worker_cannot_write_foreign_company(scoped_worker, path, payload):
    _, _, client, headers = scoped_worker
    assert client.post(path, json=payload, headers=headers).status_code == 403


def test_worker_actor_is_bound_to_active_job(scoped_worker, monkeypatch):
    _, store, client, headers = scoped_worker
    store.append_event("worker-job", "worker", status="success")
    store.enqueue_job("awp-job", "worker", "ai_weight_price", {}, created_by_id=71)
    store.claim_job("worker")
    for actor in ({"id": 72}, None):
        assert client.post("/api/db/ai-weight-price/store", headers=headers,
            json={"method": "state", "args": ["probe"], "actor": actor}).status_code == 403
    seen = []
    monkeypatch.setattr(web.ai_weight_price_service.store, "state", lambda *args: seen.append(web.ai_weight_price_service.store.actor()) or {})
    response = client.post("/api/db/ai-weight-price/store", headers=headers,
        json={"method": "state", "args": ["probe"], "actor": {"id": 71, "view_all": True}})
    assert response.status_code == 200
    assert seen[0]["id"] == 71 and seen[0]["view_all"] is False


def test_store_rows_do_not_join_companies_by_salesperson(member_console, monkeypatch):
    user, _, _ = member_console
    user.update(role_key="enterprise_admin")
    monkeypatch.setattr(web.bit_db_api, "list_mercado_store_tokens", lambda: {"rows": [
        {"id": 1, "organization_key": "wuhan-zeshun", "display_name": "own", "site_settings": [{"salesperson": "same"}]},
        {"id": 2, "organization_key": "other-company", "display_name": "foreign", "site_settings": [{"salesperson": "same"}]},
    ]})
    rows = [{"store_name": "own"}, {"store_name": "foreign", "salesperson": "same"},
            {"salesperson": "same"}, {"store_name": "own", "token_id": 2},
            {"store_name": "own", "organization_key": "other-company"}]
    with web.app.test_request_context():
        assert web._filter_store_rows_for_user({"rows": rows})["rows"] == [{"store_name": "own"}]


def test_worker_cannot_read_another_accounts_live_job(scoped_worker, monkeypatch):
    _, _, client, headers = scoped_worker
    monkeypatch.setattr(web, "_live_infraction_jobs", {
        "other-live": {"job_id": "other-live", "status": "running", "_agent_scope": 72},
        "own-live": {"job_id": "own-live", "status": "running", "_agent_scope": 71},
    })
    assert client.get("/api/db/official-infractions/live/jobs/other-live", headers=headers).status_code == 403
    assert client.get("/api/db/official-infractions/live/jobs/own-live", headers=headers).status_code == 200


def test_worker_can_write_own_company_record(scoped_worker, monkeypatch):
    _, _, client, headers = scoped_worker
    records = []
    monkeypatch.setattr(web, "db_insert_ai_appeal_record", lambda record: records.append(record) or 1)
    response = client.post("/api/db/ai-appeal-records", headers=headers,
                           json={"record": {"shop_name": "own shop"}})
    assert response.status_code == 200
    assert records == [{"shop_name": "own shop"}]


def test_invalid_worker_company_cannot_use_data(scoped_worker):
    user, _, client, headers = scoped_worker
    user["is_active"] = False
    assert client.get("/api/db/mercado-tokens", headers=headers).status_code == 403


pytestmark = pytest.mark.usefixtures("subscribed_workbench_account")
