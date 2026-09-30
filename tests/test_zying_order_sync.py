from contextlib import contextmanager
import threading
import time

import pytest
from bit.zying_order_sync import AgentSync, SyncManager, parse_purchase, snapshot_orders


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


def test_agent_sync_preparation_returns_before_order_snapshot_finishes():
    entered = threading.Event()
    release = threading.Event()
    enqueued = threading.Event()
    jobs = []

    class Store:
        def enqueue_job(self, *args, **kwargs):
            jobs.append((args, kwargs))
            enqueued.set()

    def read(**params):
        entered.set()
        assert release.wait(3)
        return {"rows": [{"id": "2000001", "order_number": "1001", "pack_id": "4001"}], "total": 1}

    agent_sync = AgentSync()
    initial = agent_sync.start_preparing(
        71, "agent-123456789", {"status": "待采"}, read, Store(),
        required_version=lambda: {"version": "bundle-v1"},
        created_by_id=71, created_by_name="测试账号",
    )
    assert initial["running"] and initial["phase"] == "loading"
    assert entered.wait(1)
    assert not enqueued.is_set()

    release.set()
    assert enqueued.wait(3)
    args, kwargs = jobs[0]
    assert args[2] == "zying_order_sync"
    assert args[3]["rows"] == [{"id": "2000001", "order_number": "1001", "pack_id": "4001"}]
    assert kwargs["required_version"] == "bundle-v1"
    assert agent_sync.preparing_status(71) is None


def test_stopping_agent_sync_during_order_snapshot_prevents_enqueue():
    entered = threading.Event()
    release = threading.Event()
    enqueued = threading.Event()

    class Store:
        def enqueue_job(self, *args, **kwargs):
            enqueued.set()

    def read(**params):
        entered.set()
        assert release.wait(3)
        return {"rows": [{"id": "2000001"}], "total": 1}

    agent_sync = AgentSync()
    agent_sync.start_preparing(71, "agent-123456789", {}, read, Store(), required_version="v1")
    assert entered.wait(1)
    stopping = agent_sync.stop_preparing(71)
    assert stopping["phase"] == "stopping"
    release.set()
    deadline = time.monotonic() + 3
    while agent_sync.preparing_status(71)["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    state = agent_sync.preparing_status(71)
    assert state["phase"] == "stopped"
    assert not enqueued.is_set()


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
    monkeypatch.setattr(app_module, "workbench_user_own_store_only", lambda *args: False)
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


@pytest.mark.parametrize("initial_route", [None, "/home", "/product", "/login", "/order-detail"])
def test_playwright_search_and_detail_flow(initial_route):
    """Real browser exercises search submit, response correlation and detail click offline."""
    from playwright.sync_api import sync_playwright
    from bit.zying_order_sync import ZyingPage, SEARCH_PLACEHOLDER, ORDER_URL
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
                function showOrderSearch() {{ document.querySelector('form').hidden = location.hash !== '#/order'; }}
                addEventListener('hashchange', showOrderSearch);
                showOrderSearch();
                document.querySelector('form').onsubmit=async e=>{{
                  e.preventDefault();
                  await fetch('/api/CmdHandler?cmd=orders.load', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{key:document.querySelector('input').value}})}});
                  document.querySelector('tbody').innerHTML='<tr data-row-key="123"><td>123</td></tr>';
                  document.querySelector('tr').onclick=()=>fetch('/api/CmdHandler?cmd=order.view',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id:123}})}});
                }};
                </script>''')
        page.route("**/*", route_request)
        try:
            if initial_route:
                page.goto(ORDER_URL.split('#')[0] + '#' + initial_route)
            reader = ZyingPage(page)
            result = reader.lookup({"id": "2000001", "pack_id": "4000001"})
            assert result == detail()
            assert page.url == ORDER_URL
            assert calls[0][1]["key"] == "2000001,4000001"
            assert calls[1][1]["id"] == 123
            # A later navigation must not be hidden by the cached ready flag.
            page.goto(ORDER_URL.split('#')[0] + '#/product')
            reader._ensure_order_page()
            assert page.url == ORDER_URL
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
    monkeypatch.setattr(app_module, "workbench_user_own_store_only", lambda *args: False)
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
    monkeypatch.setattr(app_module, "workbench_user_own_store_only", lambda *args: False)
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


def wait_for_agent_job(store, owner=71):
    import time
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        jobs = store.list_jobs(job_type="zying_order_sync", created_by_id=owner, limit=1)
        if jobs:
            return jobs[0]
        time.sleep(0.01)
    raise AssertionError("后台订单准备完成后没有创建 Agent 任务")


def test_default_agent_permission_and_filtered_payload(agent_routes):
    app_module, client, store, calls = agent_routes
    assert client.post("/api/orders/zying-sync/start", json={"execution_target": "server"}).status_code == 403
    assert client.post("/api/orders/zying-sync/start", json={}).status_code == 400
    assert client.post("/api/orders/zying-sync/start", json={"agent_id": "offline-agent"}).status_code == 400
    response = client.post("/api/orders/zying-sync/start?store_id=7&store_id=99", json={"agent_id": "agent-001"})
    assert response.status_code == 202
    assert response.json["data"]["phase"] == "loading"
    job = wait_for_agent_job(store)
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
    job_id = wait_for_agent_job(store)["job_id"]
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
    assert [a["action"] for a in actions] == ["ready", "row"]
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


def test_detail_timeout_preserves_batch_results_and_continues():
    from playwright.sync_api import TimeoutError as BrowserTimeout
    from bit.zying_order_sync import ZyingPage
    reader = ZyingPage(None)
    reader._search = lambda rows: ([{"id": str(i), "no": row["id"]} for i, row in enumerate(rows, 1)], False)
    visited = []
    def read_detail(internal_id):
        visited.append(internal_id)
        if internal_id == "2":
            raise BrowserTimeout('Timeout 30000ms exceeded while waiting for event "response"')
        return detail(order_no=str(2000000 + int(internal_id)))
    reader._detail = read_detail
    results = reader.lookup_many([{"id": str(i)} for i in range(2000001, 2000004)])
    assert visited == ["1", "2", "3"]
    assert results["2000001"]["detail"]
    assert "超时" in results["2000002"]["skip_reason"]
    assert results["2000003"]["detail"]


def test_search_timeout_skips_batch_and_searches_next_batch():
    from bit.zying_order_sync import SYNC_BATCH_SIZE
    from playwright.sync_api import TimeoutError as BrowserTimeout
    from bit.zying_order_sync import ZyingPage
    reader = ZyingPage(None)
    calls = []
    def search(rows):
        calls.append(rows)
        if len(calls) == 1:
            raise BrowserTimeout("response timeout")
        return [], False
    reader._search = search
    results = reader.lookup_many([{"id": str(i)} for i in range(SYNC_BATCH_SIZE + 1)])
    assert len(calls) == 2
    assert all(results[str(i)].get("skip_reason") for i in range(SYNC_BATCH_SIZE))
    assert results[str(SYNC_BATCH_SIZE)] == {"detail": None}


def test_server_timeout_skipped_and_next_order_written():
    manager = SyncManager()
    writes = []
    @contextmanager
    def browser(options):
        class Reader:
            def lookup(self, row):
                if row["id"] == "timeout":
                    raise TimeoutError("response timeout")
                return detail()
        yield Reader()
    manager.start(71, {}, {},
        lambda **kw: {"rows": [{"id": "timeout"}, {"id": "2000001"}], "total": 2},
        lambda order_id, changes: writes.append(order_id) or {"matched": 1}, browser)
    for thread in threading.enumerate():
        if thread.name == "zying-order-sync":
            thread.join(3)
    state = manager.status(71)
    assert not state["running"]
    assert (state["completed"], state["skipped"], state["updated"], state["failed"]) == (2, 1, 1, 0)
    assert writes == ["2000001"]


@pytest.mark.parametrize("release_mode", ["stop", "stale"])
def test_stopped_server_cleanup_cannot_unlock_new_job(release_mode):
    manager = SyncManager()
    old_cleanup = threading.Event()
    release_old = threading.Event()
    new_entered = threading.Event()
    release_new = threading.Event()
    @contextmanager
    def old_browser(options):
        class Reader:
            def lookup(self, row):
                return None
        try:
            yield Reader()
        finally:
            old_cleanup.set()
            assert release_old.wait(5)
    @contextmanager
    def new_browser(options):
        new_entered.set()
        assert release_new.wait(5)
        class Reader:
            def lookup(self, row):
                return None
        yield Reader()
    read = lambda **kw: {"rows": [{"id": "1"}], "total": 1}
    try:
        manager.start(71, {}, {}, read, None, old_browser)
        assert old_cleanup.wait(3)
        old_thread = next(t for t in threading.enumerate() if t.name == "zying-order-sync")
        if release_mode == "stop":
            assert not manager.stop(71)["running"]
        else:
            manager.jobs["71"]["progress_at"] = time.time() - 601
            assert not manager.status(71)["running"]
        manager.start(71, {}, {}, read, None, new_browser)
        assert new_entered.wait(3)
        release_old.set()
        old_thread.join(3)
        assert manager.status(71)["running"]
        with pytest.raises(ValueError, match="正在运行"):
            manager.start(72, {}, {}, read, None, new_browser)
    finally:
        release_old.set()
        release_new.set()
        for thread in threading.enumerate():
            if thread.name == "zying-order-sync":
                thread.join(3)


def test_agent_timeout_roundtrip_is_skipped_once(agent_routes):
    from bit.zying_order_sync import run_agent_sync
    app_module, client, store, writes = agent_routes
    client.post('/api/orders/zying-sync/start', json={"agent_id": "agent-001"})
    job = wait_for_agent_job(store)
    store.claim_job('agent-001')
    @contextmanager
    def browser(options):
        class Reader:
            def lookup(self, row):
                raise TimeoutError("response timeout")
        yield Reader()
    sent = []
    def request(method, path, **kwargs):
        data = kwargs['json']
        response = client.post(path, headers={"X-Internal-Token": "agent-001"}, json=data)
        assert response.status_code == 200
        if data['action'] == 'ready':
            assert client.post('/api/orders/zying-sync/confirm-login').status_code == 200
        if data['action'] == 'row':
            sent.append(data)
        return response.json['data']
    state = run_agent_sync(job, threading.Event(), request, browser)
    assert state['phase'] == 'completed'
    assert (state['skipped'], state['failed'], state['completed']) == (1, 0, 1)
    assert not writes
    duplicate = request('POST', '/api/local-agents/zying-sync/' + job['job_id'], json=sent[0])
    assert duplicate['completed'] == 1


def test_stale_progress_releases_agent_despite_fresh_heartbeat(agent_routes):
    from bit.zying_order_sync import SYNC_PROGRESS_TIMEOUT
    app_module, client, store, writes = agent_routes
    client.post('/api/orders/zying-sync/start', json={"agent_id": "agent-001"})
    job = wait_for_agent_job(store)
    store.claim_job('agent-001')
    state = {"phase": "syncing", "total": 1252, "completed": 75,
             "updated": 34, "skipped": 36, "failed": 5, "results": [],
             "progress_at": time.time() - SYNC_PROGRESS_TIMEOUT - 1}
    store.append_event(job['job_id'], job['agent_id'], status='running', result=state)
    store.heartbeat('agent-001', name='测试电脑', current_job_id=job['job_id'], capabilities=['daily_task'])
    response = client.get('/api/orders/zying-sync/status').json['data']
    assert response['running'] is False
    assert response['completed'] == 75
    assert job['job_id'] in store.cancellation_job_ids('agent-001')
    late = client.post('/api/local-agents/zying-sync/' + job['job_id'],
                      headers={"X-Internal-Token": "agent-001"},
                      json={"action": "row", "order_id": "2000001", "detail": detail()})
    assert late.json['data']['stop'] is True
    assert not writes
    assert client.post('/api/orders/zying-sync/start', json={"agent_id": "agent-001"}).status_code == 202


def test_agent_batches_ten_orders_and_stops_on_result_response():
    from bit.zying_order_sync import run_agent_sync
    actions, batches = [], []

    @contextmanager
    def browser(options):
        class Reader:
            def lookup_many(self, rows):
                batches.append(rows)
                return {row['id']: {'detail': None} for row in rows}
        yield Reader()

    def request(method, path, **kwargs):
        data = kwargs['json']
        assert data['compact'] is True
        actions.append(data['action'])
        return {'phase': 'syncing', 'stop': data['action'] == 'rows'}

    stop = threading.Event()
    state = run_agent_sync(
        {'job_id': 'batch-test', 'payload': {'rows': [{'id': str(i)} for i in range(21)]}},
        stop, request, browser,
    )
    assert [len(batch) for batch in batches] == [10]
    assert actions == ['ready', 'rows']
    assert stop.is_set() and state['stop']


def test_larger_search_batch_splits_overflow_without_losing_orders():
    from bit.zying_order_sync import ZyingPage
    reader = ZyingPage(None)
    searches = []

    def search(rows):
        searches.append(len(rows))
        return [{'id': row['id'], 'no': row['id']} for row in rows], len(rows) > 5

    reader._search = search
    reader._detail = lambda internal_id: detail(order_id=internal_id, order_no=internal_id)
    rows = [{'id': str(i)} for i in range(10)]
    outcomes = reader.lookup_many(rows)
    assert searches == [10, 5, 5]
    assert all(outcomes[row['id']]['detail']['root'][0]['order_no'] == row['id'] for row in rows)


def test_compact_agent_response_preserves_server_result_history(agent_routes):
    _, client, store, _ = agent_routes
    client.post('/api/orders/zying-sync/start', json={'agent_id': 'agent-001'})
    job = wait_for_agent_job(store)
    store.claim_job('agent-001')
    path = '/api/local-agents/zying-sync/' + job['job_id']
    headers = {'X-Internal-Token': 'agent-001'}
    client.post(path, headers=headers, json={'action': 'ready'})
    client.post('/api/orders/zying-sync/confirm-login')
    response = client.post(path, headers=headers, json={
        'action': 'row', 'order_id': job['payload']['rows'][0]['id'],
        'detail': None, 'compact': True,
    })
    assert response.status_code == 200
    assert response.json['data']['completed'] == 1
    assert 'results' not in response.json['data']
    assert len(store.get_job(job['job_id'])['result']['results']) == 1
    full = client.post(path, headers=headers, json={'action': 'control'})
    assert len(full.json['data']['results']) == 1
