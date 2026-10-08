from collections import deque

import pytest

from bit import bit_interface as console


def user(role="operator", **extra):
    return {"id": 12, "username": "tester", "display_name": "测试员", "role_key": role, "access_version": 1,
            "permissions": ["*"], "organization_key": "company-a", **extra}


@pytest.mark.parametrize("role", ["operator", "viewer", "member", "custom"])
@pytest.mark.parametrize("method,path,payload", [
    ("get", "/api/window-anomalies", None),
    ("get", "/api/window-anomalies/mercado-login/status", None),
    ("post", "/api/window-anomalies/foreign/resolve", {}),
    ("post", "/api/window-anomalies/mercado-login/start", {"window_id": "foreign"}),
    ("post", "/api/window-anomalies/mercado-login/stop", {"task_id": "abcdef1234567890"}),
])
def test_non_admin_cannot_use_shop_status_even_with_wildcard(monkeypatch, role, method, path, payload):
    monkeypatch.setattr(console, "get_current_workbench_user", lambda: user(role))
    def unexpected(*args, **kwargs):
        pytest.fail("Denied request accessed shop data or performed an operation")
    monkeypatch.setattr(console, "db_get_window_anomalies", unexpected)
    monkeypatch.setattr(console, "db_resolve_window_anomaly", unexpected)
    monkeypatch.setattr(console, "start_mercado_login_console_job", unexpected)
    monkeypatch.setattr(console, "request_mercado_login_task_stop", unexpected)
    response = getattr(console.app.test_client(), method)(path, json=payload)
    assert response.status_code == 403


def test_legacy_permissions_do_not_bypass_admin_requirement():
    assert not console.workbench_user_has_permission({"username": "legacy"}, "shop_status.view")
    assert console.workbench_user_has_permission(user("enterprise_admin"), "shop_status.view")
    assert console.workbench_user_has_permission(user("super_admin"), "shop_status.execute")
    assert not console.workbench_user_has_permission(user("enterprise_admin", permissions=[]), "shop_status.view")


@pytest.mark.parametrize("role,visible", [("operator", False), ("enterprise_admin", True)])
def test_shop_status_is_only_rendered_for_admin(monkeypatch, subscribed_workbench_account, role, visible):
    monkeypatch.setattr(console, "get_current_workbench_user", lambda: user(role))
    monkeypatch.setattr(console.mercado_tokens, "authorization_info", lambda: {})
    response = console.app.test_client().get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert ('data-tab="window-anomalies"' in html) is visible
    assert ('id="tab-window-anomalies"' in html) is visible


@pytest.fixture
def tenant_admin(monkeypatch, subscribed_workbench_account, tmp_path):
    from bit.local_agent_hub import LocalAgentStore
    hub = LocalAgentStore(tmp_path / "agents.sqlite3")
    monkeypatch.setattr(console, "get_local_agent_store", lambda: hub)
    monkeypatch.setattr(console, "get_current_workbench_user", lambda: user("enterprise_admin"))
    rows = [
        {"window_id": "own", "window_name": "own-shop", "anomaly_type": "logged_out"},
        {"window_id": "foreign", "window_name": "other-shop", "anomaly_type": "logged_out"},
    ]
    monkeypatch.setattr(console, "db_get_window_anomalies", lambda *a, **kw: {"rows": rows})
    monkeypatch.setattr(console, "list_shop_configs", lambda **kw: [])
    monkeypatch.setattr(console.bit_db_api, "list_mercado_store_tokens", lambda: {"rows": [
        {"id": 1, "display_name": "own-shop", "organization_key": "company-a"},
        {"id": 2, "display_name": "other-shop", "organization_key": "company-b"},
    ]})
    return console.app.test_client()


def test_tenant_admin_can_only_read_own_anomalies(tenant_admin):
    response = tenant_admin.get("/api/window-anomalies?organization_key=company-b")
    assert response.status_code == 200
    assert [row["window_id"] for row in response.get_json()["data"]["rows"]] == ["own"]


@pytest.mark.parametrize("path,payload", [
    ("/api/window-anomalies/foreign/resolve", {}),
    ("/api/window-anomalies/mercado-login/start", {"window_id": "foreign"}),
    ("/api/window-anomalies/mercado-login/start", {"window_ids": ["own", "foreign"]}),
])
def test_tenant_admin_cannot_mutate_foreign_shops(tenant_admin, monkeypatch, path, payload):
    def unexpected(*args, **kwargs):
        pytest.fail("Foreign shop operation was executed")
    monkeypatch.setattr(console, "db_resolve_window_anomaly", unexpected)
    monkeypatch.setattr(console, "request_mercado_login_window_tasks_stop", unexpected)
    monkeypatch.setattr(console, "start_mercado_login_console_job", unexpected)
    response = tenant_admin.post(path, json=payload)
    assert response.status_code in {403, 404}


def test_tenant_admin_can_resolve_own_shop(tenant_admin, monkeypatch):
    resolved = []
    monkeypatch.setattr(console, "request_mercado_login_window_tasks_stop", lambda window: [])
    monkeypatch.setattr(console, "db_resolve_window_anomaly", lambda window: resolved.append(window) or 1)
    assert tenant_admin.post("/api/window-anomalies/own/resolve").status_code == 200
    assert resolved == ["own"]


def test_tenant_task_status_and_stop_are_isolated(tenant_admin, monkeypatch):
    tasks = {
        "aaaaaaaaaaaaaaaa": {"task_id": "aaaaaaaaaaaaaaaa", "organization_key": "company-a", "running": True,
                             "target": "own-shop", "window_ids": ["own"], "log_chunks": deque(["own-log"])},
        "bbbbbbbbbbbbbbbb": {"task_id": "bbbbbbbbbbbbbbbb", "organization_key": "company-b", "running": True,
                             "target": "other-shop", "window_ids": ["foreign"], "log_chunks": deque(["SECRET"])},
    }
    monkeypatch.setattr(console, "_mercado_login_tasks", tasks)
    monkeypatch.setattr(console, "_mercado_login_task_state", {})
    monkeypatch.setattr(console, "get_lock_owner", lambda key: None)
    status = tenant_admin.get("/api/window-anomalies/mercado-login/status")
    assert status.status_code == 200
    data = status.get_json()["data"]
    assert data["running_count"] == 1
    assert data["active_window_ids"] == ["own"]
    assert data["log"] == "own-log"
    assert "SECRET" not in status.get_data(as_text=True)
    assert "other-shop" not in status.get_data(as_text=True)
    monkeypatch.setattr(console, "request_mercado_login_task_stop", lambda *a: pytest.fail("Stopped foreign task"))
    denied = tenant_admin.post("/api/window-anomalies/mercado-login/stop", json={"task_id": "bbbbbbbbbbbbbbbb"})
    assert denied.status_code == 403


@pytest.mark.parametrize("method,path", [
    ("get", "/api/db/window-anomalies"),
    ("post", "/api/db/window-anomalies"),
    ("post", "/api/db/window-anomalies/resolve"),
])
def test_internal_shop_status_routes_require_credentials(monkeypatch, method, path):
    monkeypatch.delenv("BIT_DB_API_TOKEN", raising=False)
    monkeypatch.setattr(console, "get_current_workbench_user", lambda: user())
    response = getattr(console.app.test_client(), method)(path, json={"window_id": "foreign"})
    assert response.status_code == 403


@pytest.mark.parametrize("payload", [{"window_id": "own"}, {"window_ids": ["own"]}])
def test_tenant_admin_can_start_own_shop(tenant_admin, monkeypatch, payload):
    calls = []
    monkeypatch.setattr(console, "start_mercado_login_console_job", lambda **kw: (
        calls.append(kw) or True, {"target": "own-shop", "started_task_id": "aaaaaaaaaaaaaaaa"}))
    response = tenant_admin.post("/api/window-anomalies/mercado-login/start", json=payload)
    assert response.status_code == 200
    assert len(calls) == 1
    if "window_ids" in payload:
        assert calls[0]["selected_shops"] == [{"window_id": "own", "window_name": "own-shop"}]
    else:
        assert calls[0]["window_id"] == "own"
        assert calls[0]["shop_name"] == "own-shop"


def test_all_shop_detection_expands_only_authorized_stores(tenant_admin, monkeypatch):
    calls = []
    monkeypatch.setattr(console, "list_shop_configs", lambda **kw: [
        {"window_id": "own", "shop_name": "own-shop"},
        {"window_id": "foreign", "shop_name": "other-shop"},
    ])
    monkeypatch.setattr(console, "start_mercado_login_console_job", lambda **kw: (
        calls.append(kw) or True, {"target": "own-shop", "started_task_id": "aaaaaaaaaaaaaaaa"}))
    response = tenant_admin.post("/api/window-anomalies/mercado-login/start", json={})
    assert response.status_code == 200
    assert calls[0]["selected_shops"] == [{"window_id": "own", "window_name": "own-shop"}]


def test_new_login_tasks_keep_authoritative_organization(tenant_admin, monkeypatch):
    class Thread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
    monkeypatch.setattr(console.threading, "Thread", Thread)
    monkeypatch.setattr(console, "_mercado_login_tasks", {})
    monkeypatch.setattr(console, "_mercado_login_task_state", {})
    monkeypatch.setattr(console, "get_lock_owner", lambda key: None)
    response = tenant_admin.post("/api/window-anomalies/mercado-login/start?organization_key=company-b", json={"window_id": "own"})
    assert response.status_code == 200
    task = next(iter(console._mercado_login_tasks.values()))
    assert task["organization_key"] == "company-a"
    assert task["window_ids"] == ["own"]


def test_execution_agent_list_does_not_leak_shop_status(monkeypatch, subscribed_workbench_account):
    class AgentStore:
        queue_id = "test-queue"
        def reap_expired_jobs(self):
            pass
        def list_agents(self, **kwargs):
            return [{"agent_id": "own-agent", "name": "电脑"}]
    monkeypatch.setattr(console, "get_current_workbench_user", lambda: user())
    monkeypatch.setattr(console, "get_local_agent_store", lambda: AgentStore())
    monkeypatch.setattr(console, "workbench_user_can_use_agent", lambda agent: True)
    monkeypatch.setattr(console.bit_db_api, "list_mercado_store_tokens", lambda: {"rows": []})
    monkeypatch.setattr(console, "db_get_window_anomalies", lambda **kw: pytest.fail("Non-admin read shop status through Agent list"))
    response = console.app.test_client().get("/api/execution-agents")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["agents"][0]["logged_out_shops"] == []
    assert data["unassigned_logout_shops"] == []
