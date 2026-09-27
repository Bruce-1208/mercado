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
        session["workbench_user"] = {"id": 71, "username": "test"}
    monkeypatch.setattr(app_module, "_authorized_token_ids_for_user", lambda: {7, 8})
    monkeypatch.setattr(app_module, "workbench_user_own_store_only", lambda: False)
    captured = []
    monkeypatch.setattr(manager, "start", lambda *args: captured.append(args) or {"running": True})
    result = client.post("/api/orders/zying-sync/start?store_id=7&store_id=99&salesperson=A&salesperson=B&status=待采&page=3", json={})
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
        session["workbench_user"] = {"id": 71, "username": "test"}
    monkeypatch.setattr(app_module, "_authorized_token_ids_for_user", lambda: {7})
    monkeypatch.setattr(app_module, "workbench_user_own_store_only", lambda: False)
    def start(*args):
        assert args[2] == {"browser_type": "edge", "require_login": True}
        raise RuntimeError("worker failed")
    monkeypatch.setattr(manager, "start", start)
    response = client.post("/api/orders/zying-sync/start", json={"browser_type": "bitbrowser"})
    assert response.status_code == 500
    assert response.is_json
    assert response.json["status"] == "error"
    assert client.post("/api/orders/zying-sync/confirm-login").status_code == 400
    assert app_module.app.test_client().post("/api/orders/zying-sync/confirm-login").status_code == 401
