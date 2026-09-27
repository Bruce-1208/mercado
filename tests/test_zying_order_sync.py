from contextlib import contextmanager
import threading

import pytest
from bit.zying_order_sync import SyncManager, parse_purchase, snapshot_orders


def detail(**root):
    return {"root": [{"order_id": "123", "order_no": "2000001", "order_key": "4000001",
                       "order_cost": "18.50", **root}],
            "buys": [{"no": "P1", "trace": "T1"}, {"no": "P2", "trace": "T2"}]}


def test_purchase_identifiers_and_cny_cost():
    assert parse_purchase(detail(), {"id": "2000001", "pack_id": "4000001"}) == {
        "purchase_order": "P1,P2", "purchase_tracking": "T1,T2", "purchase_cost": "18.50"}


@pytest.mark.parametrize("cost", [None, "***", "NaN", "Infinity", "-1"])
def test_unreadable_cost_is_not_written(cost):
    with pytest.raises(ValueError):
        parse_purchase(detail(order_cost=cost), {"id": "2000001"})


def test_pack_only_match_does_not_duplicate_cost():
    with pytest.raises(ValueError, match="子订单"):
        parse_purchase(detail(), {"id": "2000002", "pack_id": "4000001"})


def test_empty_purchase_fields_do_not_erase_local_values():
    data = detail(order_cost=0)
    data["buys"] = [{"no": "", "trace": None}]
    assert parse_purchase(data, {"id": "2000001"}) == {"purchase_cost": "0.00"}
    data["buys"] = []
    assert parse_purchase(data, {"id": "2000001"}) is None


def test_filtered_snapshot_reads_all_pages_before_writing():
    calls = []
    def read(**params):
        calls.append(params)
        return {"rows": [{"id": str(params["page"])}], "total": 2}
    assert len(snapshot_orders(read, {"page": 12, "status": "待采", "store_ids": [7]})) == 2
    assert [item["page"] for item in calls] == [1, 2]
    assert all(item["status"] == "待采" and item["store_ids"] == [7] for item in calls)


def test_partial_failure_and_user_isolation():
    done = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    writes = []
    @contextmanager
    def factory(options):
        entered.set()
        release.wait(3)
        class Reader:
            def lookup(self, row):
                if row["id"] == "missing":
                    return None
                if row["id"] == "bad":
                    raise ValueError("匹配冲突")
                return detail()
        try:
            yield Reader()
        finally:
            done.set()
    manager = SyncManager()
    def writer(order_id, changes):
        writes.append(order_id)
        return {"matched": 1}
    manager.start(1, {}, {}, lambda **kw: {"rows": [{"id": value} for value in ["2000001", "bad", "missing"]], "total": 3}, writer, factory)
    assert entered.wait(3)
    assert manager.status(2)["phase"] == "idle"
    with pytest.raises(ValueError, match="正在运行"):
        manager.start(2, {}, {}, None, None)
    release.set()
    assert done.wait(3)
    # Join the named worker rather than sleeping and racing the final update.
    for thread in threading.enumerate():
        if thread.name == "zying-order-sync":
            thread.join(3)
    state = manager.status(1)
    assert (state["updated"], state["failed"], state["skipped"]) == (1, 1, 1)
    assert not state["running"]
    assert writes == ["2000001"]


@pytest.mark.usefixtures("isolated_legacy_console_user")
def test_route_scopes_filters_and_status(monkeypatch):
    from bit import bit_interface as app_module
    from bit.zying_order_sync import manager
    app_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = {"id": 71, "username": "test", "role_key": "super_admin"}
    monkeypatch.setattr(app_module, "_authorized_token_ids_for_user", lambda: {7, 8})
    monkeypatch.setattr(app_module, "workbench_user_own_store_only", lambda: False)
    captured = []
    monkeypatch.setattr(manager, "start", lambda *args: captured.append(args) or {"running": True})
    result = client.post("/api/orders/zying-sync/start?store_id=7&store_id=99&salesperson=A&salesperson=B&status=待采&page=3", json={"execution_target": "server"})
    assert result.status_code == 202
    owner, filters, options, reader, writer = captured[0]
    assert owner == 71
    assert filters["store_ids"] == [7]
    assert filters["salespeople"] == ["A", "B"]
    assert filters["status"] == "待采"
    assert client.post("/api/orders/zying-sync/start?store_id=99", json={}).status_code == 400
    assert app_module._required_workbench_permissions("/api/orders/zying-sync/start", "POST") == ("order_analysis.execute",)
    assert app_module.app.test_client().post("/api/orders/zying-sync/start").status_code == 401


def test_playwright_search_and_detail_flow():
    """Real browser exercises search submit, response correlation and detail click offline."""
    from playwright.sync_api import sync_playwright
    from bit.zying_order_sync import ZyingPage, SEARCH_PLACEHOLDER
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        calls = []
        def route_request(route):
            request = route.request
            if "cmd=" in request.url:
                body = request.post_data_json
                calls.append((request.url, body))
                if "orders.load" in request.url:
                    data = {"list": {"data": [{"id": "<b>123</b>", "no": "2000001", "key": "4000001"}], "maxcount": 1}}
                else:
                    data = detail()
                route.fulfill(json={"code": 200, "data": data})
            else:
                route.fulfill(content_type="text/html; charset=utf-8", body=f'''<meta charset="utf-8"><form><input placeholder="{SEARCH_PLACEHOLDER}"></form><table><tbody></tbody></table>
                <script>
                document.querySelector('form').onsubmit=async e=>{{
                  e.preventDefault();
                  await fetch('/api/CmdHandler?cmd=orders.load', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{key:document.querySelector('input').value}})}});
                  document.querySelector('tbody').innerHTML='<tr data-row-key="123"><td>123</td></tr>';
                  document.querySelector('tr').onclick=()=>fetch('/api/CmdHandler?cmd=order.view',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id:123}})}});
                }};
                </script>''')
        page.route("**/*", route_request)
        try:
            result = ZyingPage(page).lookup({"id": "2000001", "pack_id": "4000001"})
            assert result == detail()
            assert calls[0][1]["key"] == "2000001,4000001"
            assert calls[1][1]["id"] == 123
        finally:
            browser.close()


def test_login_confirmation_gates_writes_and_is_owner_scoped():
    manager = SyncManager()
    writes = []
    @contextmanager
    def factory(options):
        class Reader:
            def lookup(self, row):
                return detail()
        yield Reader()
    manager.start(71, {}, {"require_login": True},
                  lambda **kw: {"rows": [{"id": "2000001"}], "total": 1},
                  lambda *args: writes.append(args) or {"matched": 1}, factory)
    import time
    deadline = time.monotonic() + 3
    while manager.status(71)["phase"] != "waiting_login" and time.monotonic() < deadline:
        time.sleep(.01)
    try:
        assert manager.status(71)["phase"] == "waiting_login"
        assert writes == []
        with pytest.raises(ValueError):
            manager.confirm_login(72)
    finally:
        manager.confirm_login(71)
    for thread in threading.enumerate():
        if thread.name == "zying-order-sync":
            thread.join(3)
    assert len(writes) == 1
    assert manager.status(71)["phase"] == "completed"


@pytest.mark.usefixtures("isolated_legacy_console_user")
def test_start_failure_returns_json_and_edge_is_mandatory(monkeypatch):
    from bit import bit_interface as app_module
    from bit.zying_order_sync import manager
    app_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = {"id": 71, "username": "test", "role_key": "super_admin"}
    monkeypatch.setattr(app_module, "_authorized_token_ids_for_user", lambda: {7})
    monkeypatch.setattr(app_module, "workbench_user_own_store_only", lambda: False)
    def start(*args):
        assert args[2] == {"browser_type": "edge", "require_login": True}
        raise RuntimeError("worker failed")
    monkeypatch.setattr(manager, "start", start)
    response = client.post("/api/orders/zying-sync/start", json={"browser_type": "bitbrowser", "execution_target": "server"})
    assert response.status_code == 500
    assert response.is_json
    assert response.json["status"] == "error"
    assert client.post("/api/orders/zying-sync/confirm-login").status_code == 400
    assert app_module.app.test_client().post("/api/orders/zying-sync/confirm-login").status_code == 401


@pytest.fixture
def agent_routes(monkeypatch, tmp_path, isolated_legacy_console_user):
    from bit import bit_interface as app_module
    from bit import zying_order_sync as sync
    from bit.local_agent_hub import LocalAgentStore
    store = LocalAgentStore(tmp_path / "agents.sqlite3")
    store.heartbeat("agent-001", name="测试电脑", capabilities=["daily_task"])
    monkeypatch.setattr(app_module, "get_local_agent_store", lambda: store)
    monkeypatch.setattr(app_module, "current_local_agent_bundle", lambda: {"version": "test"})
    monkeypatch.setattr(app_module, "_authorized_token_ids_for_user", lambda: {7})
    monkeypatch.setattr(app_module, "workbench_user_own_store_only", lambda: False)
    monkeypatch.setattr(app_module, "USE_DB_API", False)
    monkeypatch.setattr(sync, "manager", SyncManager())
    monkeypatch.setattr(sync, "agent_sync", sync.AgentSync())
    monkeypatch.setattr(app_module, "_verify_local_agent_credential",
                        lambda token: {"agent_id": token, "user_id": 71} if token else None)
    calls = []
    def read(**params):
        assert params["store_ids"] == [7]
        return {"rows": [{"id": "2000001", "order_number": "2000001", "secret": "not sent"}], "total": 1}
    monkeypatch.setattr(app_module, "db_list_orders", read)
    monkeypatch.setattr(app_module.bit_db_api, "bulk_update_orders", lambda *a, **kw: calls.append((a, kw)) or {"matched": 1})
    app_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = {"id": 71, "username": "operator", "role_key": "operator"}
    return app_module, client, store, calls


def test_default_agent_permission_and_filtered_payload(agent_routes):
    app_module, client, store, calls = agent_routes
    assert client.post("/api/orders/zying-sync/start", json={"execution_target": "server"}).status_code == 403
    assert client.post("/api/orders/zying-sync/start", json={}).status_code == 400
    assert client.post("/api/orders/zying-sync/start", json={"agent_id": "offline-agent"}).status_code == 400
    response = client.post("/api/orders/zying-sync/start?store_id=7&store_id=99", json={"agent_id": "agent-001"})
    assert response.status_code == 202
    job = store.get_job(response.json["data"]["task_id"])
    assert job["job_type"] == "zying_order_sync"
    assert job["created_by_id"] == 71
    assert job["payload"]["rows"] == [{"id": "2000001", "order_number": "2000001", "pack_id": None}]
    assert not calls
    assert client.post("/api/orders/zying-sync/start", json={"agent_id": "agent-001"}).status_code == 400
    assert client.post("/api/orders/zying-sync/stop").json["data"]["phase"] == "stopped"
    assert store.get_job(job["job_id"])["cancel_requested"]


def test_agent_login_owner_scope_persistence_and_stop_blocks_writes(agent_routes, monkeypatch):
    app_module, client, store, calls = agent_routes
    from bit import zying_order_sync as sync
    response = client.post("/api/orders/zying-sync/start", json={"agent_id": "agent-001"})
    job_id = response.json["data"]["task_id"]
    store.claim_job("agent-001")
    path = "/api/local-agents/zying-sync/" + job_id
    headers = {"X-Internal-Token": "agent-001"}
    outsider = app_module.app.test_client()
    assert outsider.post(path, json={"action": "ready"}).status_code == 401
    assert outsider.post(path, headers={"X-Internal-Token": "agent-002"}, json={"action": "ready"}).status_code == 403
    assert outsider.post(path, headers=headers, json={"action": "ready"}).json["data"]["phase"] == "waiting_login"
    row = {"action": "row", "order_id": "2000001", "detail": detail()}
    assert outsider.post(path, headers=headers, json=row).status_code == 400
    with outsider.session_transaction() as session:
        session["workbench_user"] = {"id": 72, "username": "other"}
    assert outsider.post("/api/orders/zying-sync/confirm-login").status_code == 400
    assert outsider.get("/api/orders/zying-sync/status").json["data"]["phase"] == "idle"
    outsider.post("/api/orders/zying-sync/stop")
    assert not store.get_job(job_id)["cancel_requested"]
    # Worker restart must retain ownership, progress and the login gate.
    monkeypatch.setattr(sync, "agent_sync", sync.AgentSync())
    assert client.get("/api/orders/zying-sync/status").json["data"]["phase"] == "waiting_login"
    assert client.post("/api/orders/zying-sync/confirm-login").status_code == 200
    assert outsider.post(path, headers=headers, json={**row, "order_id": "999"}).status_code == 400
    state = outsider.post(path, headers=headers, json=row).json["data"]
    assert state["updated"] == 1
    assert calls[0][1]["operator_id"] == 71
    assert calls[0][1]["purchase_cost"] == "18.50"
    outsider.post(path, headers=headers, json=row)
    assert len(calls) == 1
    assert client.post("/api/orders/zying-sync/stop").json["data"]["phase"] == "stopping"
    assert outsider.post(path, headers=headers, json=row).json["data"]["stop"] is True
    assert len(calls) == 1
    # Forced worker shutdown may provide no result.json; keep saved progress.
    response = outsider.post(f"/api/local-agents/jobs/{job_id}/events", headers=headers,
                             json={"agent_id": "agent-001", "status": "stopped", "result": {}})
    assert response.status_code == 200
    state = client.get("/api/orders/zying-sync/status").json["data"]
    assert state["phase"] == "stopped"
    assert state["updated"] == 1
    assert state["running"] is False


@pytest.mark.parametrize("during_lookup", [False, True])
def test_server_stop_unblocks_login_and_prevents_late_write(during_lookup):
    import time
    manager = SyncManager()
    entered = threading.Event()
    release = threading.Event()
    writes = []
    @contextmanager
    def factory(options):
        class Reader:
            def lookup(self, row):
                entered.set()
                assert release.wait(3)
                return detail()
        yield Reader()
    manager.start(1, {}, {"require_login": not during_lookup},
                  lambda **kw: {"rows": [{"id": "2000001"}], "total": 1},
                  lambda *a: writes.append(a) or {"matched": 1}, factory)
    if during_lookup:
        assert entered.wait(3)
    else:
        deadline = time.monotonic() + 3
        while manager.status(1)["phase"] != "waiting_login" and time.monotonic() < deadline:
            time.sleep(.01)
        assert manager.status(1)["phase"] == "waiting_login"
    manager.stop(1)
    release.set()
    for thread in threading.enumerate():
        if thread.name == "zying-order-sync":
            thread.join(3)
    assert manager.status(1)["phase"] == "stopped"
    assert not manager.status(1)["running"]
    assert not writes


def test_agent_worker_executes_browser_and_reports_rows():
    from bit.zying_order_sync import run_agent_sync
    from bit.service_split import is_worker_path
    actions = []
    @contextmanager
    def browser(options):
        class Reader:
            def lookup(self, row):
                return detail()
        yield Reader()
    def request(method, path, **kw):
        assert is_worker_path(path)
        data = kw["json"]
        actions.append(data)
        return {"phase": "syncing", "updated": int(data["action"] == "row")}
    state = run_agent_sync({"job_id": "test-job", "payload": {"rows": [{"id": "2000001"}]}},
                           threading.Event(), request, browser)
    assert state["phase"] == "completed"
    assert [a["action"] for a in actions] == ["ready", "control", "row"]
    assert actions[-1]["detail"] == detail()


def test_server_option_only_rendered_for_super_admin():
    from pathlib import Path
    from jinja2 import Environment
    html = Path("bit/templates/index.html").read_text()
    start = html.index('<dialog id="order-zying-sync-dialog"')
    fragment = html[start:html.index('</dialog>', start)]
    template = Environment().from_string(fragment)
    for role in ["operator", "enterprise_admin", "super_admin"]:
        rendered = template.render(current_user={"role_key": role})
        assert ('value="server"' in rendered) == (role == "super_admin")
        assert 'id="order-zying-stop"' in rendered
