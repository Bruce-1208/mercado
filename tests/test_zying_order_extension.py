import threading
import time

import pytest

from bit.zying_order_extension import ExtensionOrderSync


def detail(order_id="2001", cost="18.50", buys=None):
    return {"root": [{"order_id": "101", "order_no": order_id, "order_cost": cost}],
            "buys": buys if buys is not None else [{"no": "P1", "trace": "T1", "amount": cost, "cur": "CNY"}]}


@pytest.fixture
def manager(tmp_path):
    return ExtensionOrderSync(tmp_path / "orders.sqlite3")


def start(manager, rows=None):
    manager.start({"id": 7, "username": "tester"},
                         {"start_date": "2026-10-01", "end_date": "2026-10-08"},
                         lambda **params: {"rows": rows or [{"id": "2001"}], "total": len(rows or [1])})
    return ready(manager)


def ready(manager, owner=7):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = manager.status(owner)
        if state["phase"] != "loading":
            return state
        time.sleep(0.01)
    pytest.fail("preparation did not finish")


def test_sync_reuses_agent_rules_and_deduplicates_committed_results(manager):
    state = start(manager, [{"id": "2001"}, {"id": "2002"}, {"id": "2003", "pack_id": "2001"}])
    request_id = state["request_id"]
    assert manager.status(8)["phase"] == "idle"
    manager.action(7, request_id, "confirm")
    writes = []
    def write(order_id, changes, job):
        writes.append((order_id, changes, job["created_by_id"]))
        return {"matched": 1}
    entries = [{"order_id": "2001", "detail": detail(buys=[{"no": "", "trace": ""}])},
               {"order_id": "2002", "detail": None}]
    manager.action(7, request_id, "results", {"rows": entries}, write)
    manager.action(7, request_id, "results", {"rows": entries}, write)
    assert writes == [("2001", {"purchase_order": "", "purchase_tracking": "", "purchase_cost": "0.00"}, 7)]
    # Pack-only match is a failed result, with no cost assigned to its child.
    state = manager.action(7, request_id, "results", {"rows": [{"order_id": "2003", "detail": detail()}]}, write)
    assert (state["updated"], state["skipped"], state["failed"], state["completed"]) == (1, 1, 1, 3)
    assert state["phase"] == "completed" and not state["running"]
    assert "子订单" in state["results"][-1]["message"]
    assert ExtensionOrderSync(manager.path).status(7) == state


def test_extension_clears_cost_without_purchase_records(manager):
    request_id = start(manager)["request_id"]
    manager.action(7, request_id, "confirm")
    writes = []

    def write(order_id, changes, job):
        writes.append((order_id, changes))
        return {"matched": 1}

    state = manager.action(7, request_id, "results", {"rows": [
        {"order_id": "2001", "detail": detail(cost="28.75", buys=[])}
    ]}, write)
    assert writes == [("2001", {"purchase_order": "", "purchase_tracking": "", "purchase_cost": "0.00"})]
    assert state["updated"] == 1 and state["skipped"] == 0
    assert state["phase"] == "completed"


def test_out_of_scope_and_invalid_batches_never_write(manager):
    request_id = start(manager)["request_id"]
    manager.action(7, request_id, "confirm")
    def write(*args):
        pytest.fail("unauthorized result must not write")
    for entries in ([{"order_id": "999", "detail": detail()}],
                    [{"order_id": "2001"}, {"order_id": "2001"}], [], [None], [{}] * 11):
        with pytest.raises(ValueError):
            manager.action(7, request_id, "results", {"rows": entries}, write)
    with pytest.raises(ValueError, match="不存在"):
        manager.action(8, request_id, "confirm")
    assert manager.status(7)["completed"] == 0


def test_stop_survives_restart_and_blocks_late_write(manager):
    request_id = start(manager)["request_id"]
    manager.action(7, request_id, "confirm")
    assert manager.action(7, request_id, "batch")["rows"] == [{"id": "2001", "order_number": None, "pack_id": None}]
    manager.action(7, request_id, "stop")
    restored = ExtensionOrderSync(manager.path)
    assert restored.status(7)["phase"] == "stopped"
    assert restored.action(7, request_id, "batch")["rows"] == []
    with pytest.raises(ValueError, match="已结束"):
        restored.action(7, request_id, "results", {"rows": [{"order_id": "2001", "detail": detail()}]}, lambda *args: pytest.fail())
    assert start(restored)["request_id"] != request_id


def test_pagination_and_cancellation_during_snapshot(manager):
    entered = threading.Event()
    release = threading.Event()
    result = []
    pages = []
    def read(**params):
        pages.append(params["page"])
        entered.set()
        assert release.wait(3)
        return {"rows": [{"id": str(params["page"])}], "total": 2}
    result.append(manager.start(
        {"id": 7}, {"start_date": "2026-10-01", "end_date": "2026-10-08"}, read))
    assert result[0]["phase"] == "loading"
    assert entered.wait(3)
    state = manager.status(7)
    assert state["phase"] == "loading"
    manager.action(7, state["request_id"], "stop")
    release.set()
    assert ready(manager)["phase"] == "stopped"
    assert pages == [1]


def test_preparation_progress_duplicate_start_and_empty_range(manager):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    prepare = manager.prepare
    def tracked(*args):
        try:
            prepare(*args)
        finally:
            finished.set()
    manager.prepare = tracked
    def read(**params):
        if params["page"] == 2:
            entered.set()
            assert release.wait(3)
        return {"rows": [{"id": str(params["page"])}], "total": 2}
    state = manager.start({"id": 7}, {"start_date": "2026-10-01", "end_date": "2026-10-08"}, read)
    try:
        assert state["phase"] == "loading"
        assert entered.wait(3)
        state = manager.status(7)
        assert (state["prepared"], state["total"]) == (1, 2)
        with pytest.raises(ValueError, match="正在运行"):
            start(manager)
        with pytest.raises(ValueError, match="等待登录"):
            manager.action(7, state["request_id"], "confirm")
    finally:
        release.set()
        assert finished.wait(3)
    assert manager.status(7)["phase"] == "waiting_login"
    manager.action(7, state["request_id"], "stop")
    manager.start({"id": 7}, {"start_date": "2026-10-01", "end_date": "2026-10-08"},
                  lambda **params: {"rows": [], "total": 0})
    assert ready(manager)["phase"] == "completed"


@pytest.mark.parametrize("fail", [False, True])
def test_late_preparation_cannot_replace_a_new_run(manager, fail):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    prepare = manager.prepare
    def tracked(*args):
        try:
            prepare(*args)
        finally:
            finished.set()
    manager.prepare = tracked
    def old_reader(**params):
        entered.set()
        assert release.wait(3)
        if fail:
            raise RuntimeError("old reader failed")
        return {"rows": [{"id": "old"}], "total": 1}
    state = manager.start({"id": 7}, {"start_date": "2026-10-01", "end_date": "2026-10-08"}, old_reader)
    assert entered.wait(3)
    manager.action(7, state["request_id"], "stop")
    manager.prepare = prepare
    new = start(manager)
    try:
        assert new["request_id"] != state["request_id"]
    finally:
        release.set()
        assert finished.wait(3)
    assert manager.status(7) == new


@pytest.mark.parametrize("interrupted", ["restart", "timeout"])
def test_abandoned_preparation_releases_owner(manager, monkeypatch, interrupted):
    monkeypatch.setattr(manager, "prepare", lambda *args: None)
    state = manager.start({"id": 7}, {"start_date": "2026-10-01", "end_date": "2026-10-08"}, None)
    with manager.connection() as connection:
        job = manager.load(connection, 7)
        if interrupted == "restart":
            job["preparation_owner"] = "previous-process"
        else:
            job["preparation_updated_at"] = time.time() - 601
        manager.save(connection, 7, job)
    assert manager.status(7)["phase"] == "error"
    new = manager.start({"id": 7}, {"start_date": "2026-10-01", "end_date": "2026-10-08"}, None)
    assert new["request_id"] != state["request_id"]


@pytest.fixture
def route_client(monkeypatch, manager):
    from bit import bit_interface as web
    from bit import zying_order_extension
    user = {"id": 7, "username": "tester", "display_name": "业务员甲", "role_key": "member", "access_version": 1, "permissions": ["*"]}
    monkeypatch.setattr(web, "_browser_extension_user_from_token", lambda token: user if token == "test-token" else None)
    monkeypatch.setattr(web, "_workbench_billing_blocked", lambda user: None)
    monkeypatch.setattr(zying_order_extension, "extension_order_sync", manager)
    monkeypatch.setattr(web, "_authorized_token_ids_for_user", lambda user: {10, 20})
    return web.app.test_client(), web, user


def test_routes_enforce_bearer_permissions_dates_and_store_scope(route_client, monkeypatch, manager):
    client, web, user = route_client
    path = "/api/browser-extension/zying-order-sync/"
    headers = {"Authorization": "Bearer test-token"}
    calls = []
    monkeypatch.setattr(web, "db_list_orders", lambda **params: calls.append(params) or {"rows": [{"id": "2001"}], "total": 1})
    assert client.post(path + "start", json={}).status_code == 401
    for dates in ({}, {"date_start": "2026-10-09", "date_end": "2026-10-08"},
                  {"date_start": "2026-02-30", "date_end": "2026-10-08"}):
        assert client.post(path + "start", json=dates, headers=headers).status_code == 400
    assert calls == []
    response = client.post(path + "start", json={"date_start": "2026-10-01", "date_end": "2026-10-08"}, headers=headers)
    assert response.status_code == 202
    assert response.json["data"]["phase"] == "loading"
    assert ready(manager)["phase"] == "waiting_login"
    assert calls[0] == {"start_date": "2026-10-01", "end_date": "2026-10-08", "include_summary": False,
                        "sync_identifiers_only": True,
                        "store_ids": [10, 20], "allowed_store_ids": [10, 20], "salesperson": "业务员甲", "page": 1, "page_size": 2000}
    request_id = response.json["data"]["request_id"]
    assert client.post(path + "confirm", json={"request_id": request_id}, headers=headers).status_code == 200
    writes = []
    monkeypatch.setattr(web.bit_db_api, "bulk_update_orders", lambda *args, **kwargs: writes.append((args, kwargs)) or {"matched": 1})
    saved = client.post(path + "results", json={"request_id": request_id, "rows": [{"order_id": "2001", "detail": detail()}]}, headers=headers)
    assert saved.json["data"]["updated"] == 1
    assert writes[0][1] == {"operator_id": 7, "operator_name": "业务员甲", "purchase_order": "P1", "purchase_tracking": "T1", "purchase_cost": "18.50"}
    user["permissions"] = ["order_analysis.view"]
    assert client.get(path + "status", headers=headers).status_code == 200
    assert client.post(path + "start", json={}, headers=headers).status_code == 403


def test_routes_fail_closed_without_accessible_stores(route_client, monkeypatch):
    client, web, _ = route_client
    monkeypatch.setattr(web, "_authorized_token_ids_for_user", lambda user: set())
    monkeypatch.setattr(web, "db_list_orders", lambda **params: pytest.fail("must not query all stores"))
    response = client.post("/api/browser-extension/zying-order-sync/start", json={"date_start": "2026-10-01", "date_end": "2026-10-08"}, headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 400
    assert "没有可访问" in response.json["message"]


def test_order_sync_passes_selected_workflow_status_and_persists_it(route_client, monkeypatch, manager):
    client, web, _ = route_client
    calls = []
    monkeypatch.setattr(web, 'db_list_orders', lambda **params: calls.append(params) or {'rows':[{'id':'2001'}], 'total':1})
    response = client.post('/api/browser-extension/zying-order-sync/start', headers={'Authorization':'Bearer test-token'}, json={
        'date_start':'2026-10-01','date_end':'2026-10-08','order_status':'@转运'})
    assert response.status_code == 202
    assert ready(manager)['order_status'] == '@转运'
    assert calls[0]['status'] == '@转运'


def test_purchase_tracking_passes_workflow_and_access_scope(route_client, monkeypatch):
    from unittest.mock import Mock
    client, web, _ = route_client
    read = Mock(return_value=[{'order_id':'2001','purchase_order':'P1'}])
    monkeypatch.setattr(web.bit_db_api, 'get_purchase_tracking_orders', read)
    create = Mock(return_value={'phase':'waiting_plugin'})
    monkeypatch.setattr(web.plugin_purchase_tracking_manager, 'create', create)
    response = client.post('/api/browser-extension/purchase-tracking/start', headers={'Authorization':'Bearer test-token'}, json={
        'platform':'1688','date_start':'2026-10-01','date_end':'2026-10-08','order_status':'找货-已采购'})
    assert response.status_code == 202
    assert read.call_args.kwargs['status'] == '找货-已采购'
    assert read.call_args.kwargs['allowed_token_ids'] == [10,20]
    assert create.call_args.kwargs['order_status'] == '找货-已采购'


def test_plugin_rejects_invalid_workflow_before_reading(route_client, monkeypatch):
    client, web, _ = route_client
    monkeypatch.setattr(web.bit_db_api,'get_purchase_tracking_orders',lambda *a,**kw:pytest.fail('invalid status must not query'))
    for path in ('purchase-tracking/start','zying-order-sync/start'):
        response = client.post('/api/browser-extension/'+path,headers={'Authorization':'Bearer test-token'},json={
            'platform':'1688','date_start':'2026-10-01','date_end':'2026-10-08','order_status':'bad-status'})
        assert response.status_code == 400
