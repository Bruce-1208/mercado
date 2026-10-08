"""Warehouse assignment, API isolation, and atomic mutation regression tests."""
import json
from unittest.mock import MagicMock

import pytest
from flask import session

from bit import bit_db_api, bit_interface as app, bit_mysql, print_wallet
from bit.order_warehouses import user_warehouse
from test_purchase_net_proceeds import Connection, order, run_save


@pytest.fixture
def warehouse_client(monkeypatch):
    app.app.config.update(TESTING=True, SECRET_KEY="warehouse-test")
    monkeypatch.setattr(app, "get_current_workbench_user", lambda: session.get("workbench_user"))
    monkeypatch.setattr(app, "_authorized_order_store_ids", lambda: {7})
    monkeypatch.setattr(app, "_authorized_token_ids_for_user", lambda *args: {7})
    monkeypatch.setattr(app, "_workbench_billing_blocked", lambda user: None)
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["workbench_user"] = dict(id=9, username="warehouse", display_name="仓库员",
            role_key="warehouse", warehouse_key="dongguan", permissions=["*"],
            access_version=1, organization_key="wuhan-zeshun")
    return client


@pytest.mark.parametrize("warehouse", ["shahu", "dongguan", "yiwu"])
def test_list_uses_binding_and_ignores_client_override(warehouse_client, monkeypatch, warehouse):
    with warehouse_client.session_transaction() as sess:
        sess["workbench_user"] = {**sess["workbench_user"], "warehouse_key": warehouse}
    query = MagicMock(return_value={"rows": [], "total": 0})
    monkeypatch.setattr(app, "db_list_orders", query)
    response = warehouse_client.get("/api/orders?transfer_warehouse=another&page=2")
    assert response.status_code == 200
    assert query.call_args.kwargs["transfer_warehouse"] == warehouse
    assert query.call_args.kwargs["allowed_store_ids"] == [7]


@pytest.mark.parametrize("binding", [None, "", "unknown"])
def test_unbound_warehouse_has_no_order_access(warehouse_client, monkeypatch, binding):
    with warehouse_client.session_transaction() as sess:
        sess["workbench_user"] = {**sess["workbench_user"], "warehouse_key": binding}
    query = MagicMock()
    monkeypatch.setattr(app, "db_list_orders", query)
    response = warehouse_client.get("/api/orders")
    assert response.status_code == 403
    assert "必须绑定仓库" in response.json["message"]
    query.assert_not_called()


@pytest.mark.parametrize("path,method,data", [
    ("/api/orders/100/logs", "get", None),
    ("/api/orders/100/tracking", "get", None),
    ("/api/orders/product-detail?order_id=100&product_id=MLM1", "get", None),
    ("/api/orders/weight-quote", "post", {"order_ids": ["100"]}),
    ("/api/orders/print", "post", {"order_ids": ["100"]}),
    ("/api/orders/outbound/confirm", "post", {"order_ids": ["100"]}),
    ("/api/orders/bulk-update", "post", {"order_ids": ["100"], "purchase_order": "PO1"}),
    ("/api/orders/purchase-tracking/start", "post", {"order_ids": ["100"]}),
])
@pytest.mark.parametrize("row", [
    {"order_id": "100", "token_id": 7, "transfer_warehouse": "yiwu"},
    {"order_id": "100", "token_id": 8, "transfer_warehouse": "dongguan"},
])
def test_cross_warehouse_and_customer_orders_are_rejected(warehouse_client, monkeypatch, path, method, data, row):
    monkeypatch.setattr(bit_db_api, "get_order_access_contexts", lambda ids: [row])
    download = MagicMock()
    monkeypatch.setattr(bit_db_api, "download_order_labels", download)
    response = getattr(warehouse_client, method)(path, **({"json": data} if data else {}))
    assert response.status_code == 403
    download.assert_not_called()


def test_own_warehouse_mutation_passes_atomic_scope(warehouse_client, monkeypatch):
    monkeypatch.setattr(bit_db_api, "get_order_access_contexts", lambda ids: [
        {"order_id": "100", "token_id": 7, "transfer_warehouse": "dongguan"}])
    update = MagicMock(return_value={"matched": 1, "changed": 1})
    monkeypatch.setattr(bit_db_api, "bulk_update_orders", update)
    response = warehouse_client.post("/api/orders/bulk-update", json={
        "order_ids": ["100"], "purchase_order": "PO1", "transfer_warehouse": "dongguan"})
    assert response.status_code == 200
    assert update.call_args.kwargs["allowed_warehouse"] == "dongguan"
    assert update.call_args.kwargs["transfer_warehouse"] == "dongguan"


def test_warehouse_cannot_reassign_orders(warehouse_client, monkeypatch):
    update = MagicMock()
    monkeypatch.setattr(bit_db_api, "bulk_update_orders", update)
    response = warehouse_client.post("/api/orders/bulk-update", json={
        "order_ids": ["100"], "transfer_warehouse": "yiwu"})
    assert response.status_code == 403
    update.assert_not_called()


def test_reassignment_persists_and_has_audit_trail(run_save):
    conn = Connection(orders=[order(transfer_warehouse="shahu")])
    result, conn, _ = run_save(conn, transfer_warehouse="yiwu")
    assert result["matched"] == 1
    assert conn.orders[0]["transfer_warehouse"] == "yiwu"
    changes = json.loads(conn.logs[0][5])
    assert changes["transfer_warehouse"] == {"label": "转运商", "before": "shahu", "after": "yiwu"}


def test_mixed_warehouse_batch_rolls_back_before_writing(run_save):
    conn = Connection(orders=[order("100", transfer_warehouse="dongguan"),
                              order("101", transfer_warehouse="yiwu")])
    with pytest.raises(ValueError, match="绑定仓库"):
        run_save(conn, purchase_cost="50", allowed_warehouse="dongguan")
    assert conn.rollbacks == 1
    assert not any(sql.startswith("UPDATE") for sql, _ in conn.calls)
    assert not conn.logs


def test_invalid_destination_is_rejected_before_database_connect(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(bit_mysql.pymysql, "connect", connect)
    with pytest.raises(ValueError, match="请选择"):
        bit_mysql.bulk_update_mercado_orders(["100"], transfer_warehouse="invalid")
    connect.assert_not_called()


def test_new_warehouse_user_requires_explicit_binding(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(app.pymysql, "connect", connect)
    with pytest.raises(ValueError, match="请选择"):
        app.create_workbench_user_local(dict(username="new_warehouse", password="password123",
                                            role_key="warehouse"))
    connect.assert_not_called()


def test_session_includes_employee_binding():
    user = app.build_workbench_session_user(dict(id=9, username="warehouse", role_key="warehouse",
                                                warehouse_key="yiwu"))
    assert user_warehouse(user) == "yiwu"


def test_remote_proxy_keeps_assignment_and_atomic_scope(monkeypatch):
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    request = MagicMock(return_value={"matched": 1})
    monkeypatch.setattr(bit_db_api, "_request", request)
    bit_db_api.bulk_update_orders(["100"], transfer_warehouse="yiwu", allowed_warehouse="yiwu")
    payload = request.call_args.kwargs["json"]
    assert payload["transfer_warehouse"] == payload["allowed_warehouse"] == "yiwu"
    bit_db_api.list_orders(transfer_warehouse="yiwu", allowed_store_ids=[7])
    assert request.call_args.kwargs["params"]["transfer_warehouse"] == "yiwu"


def test_print_wallet_rechecks_actor_binding_before_charge():
    cursor = MagicMock()
    cursor.fetchall.return_value = [dict(order_id="100", token_id=7, transfer_warehouse="yiwu")]
    with pytest.raises(PermissionError, match="绑定仓库"):
        print_wallet._resolve_orders(cursor, ["100"],
            dict(role_key="warehouse", warehouse_key="dongguan"), "wuhan-zeshun")
    assert cursor.execute.call_count == 1


@pytest.mark.parametrize("warehouse", ["shahu", "dongguan", "yiwu"])
def test_binding_is_saved_by_employee_create_and_edit(monkeypatch, warehouse):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = dict(id=9, role_key="warehouse", warehouse_key="shahu")
    monkeypatch.setattr(app.pymysql, "connect", lambda **kwargs: connection)
    data = dict(username="new_warehouse", password="password123", role_key="warehouse",
                warehouse_key=warehouse)
    app.create_workbench_user_local(data)
    insert = next(call.args for call in cursor.execute.call_args_list
                  if "INSERT INTO `workbench_users`" in call.args[0])
    assert "`warehouse_key`" in insert[0]
    assert insert[1][9] == warehouse
    cursor.execute.reset_mock()
    app.update_workbench_user_local(9, data)
    update = next(call.args for call in cursor.execute.call_args_list
                  if "UPDATE `workbench_users`" in call.args[0])
    assert "`warehouse_key` = %s" in update[0]
    assert update[1][7] == warehouse


def test_warehouse_filter_applies_before_counts_facets_and_pagination(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [{"total": 0}, {}]
    cursor.fetchall.return_value = []
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: connection)
    for name in ("_ensure_mercado_synced_orders_table", "_ensure_mercado_store_tokens_table",
                 "_ensure_mercado_store_site_settings_table"):
        monkeypatch.setattr(bit_mysql, name, lambda cursor: None)
    monkeypatch.setattr("erp.mercadolibre_profitability_cache.ensure_profitability_cache_tables", lambda cursor: None)
    monkeypatch.setattr("erp.mercadolibre_store_link_store.ensure_store_link_table", lambda cursor: None)
    bit_mysql.list_orders(transfer_warehouse="yiwu", allowed_store_ids=[7])
    queries = [call.args for call in cursor.execute.call_args_list]
    seed_index = next(i for i, args in enumerate(queries)
                      if args[0].startswith("CREATE TEMPORARY TABLE `tmp_mercado_order_filter_source` AS"))
    seed_sql, seed_params = queries[seed_index]
    assert "synced.`transfer_warehouse` = %s" in seed_sql
    assert seed_params == ["yiwu", 7]
    for marker in ("SELECT COUNT(*) AS `total`", "GROUP BY COALESCE(`status`", "GROUP BY `store_id`",
                   "CREATE TEMPORARY TABLE `tmp_mercado_order_page` AS"):
        assert any(marker in args[0] and "tmp_mercado_order_filter_source" in args[0]
                   for args in queries[seed_index + 1:])



def test_scoped_mutation_locks_orders_inside_explicit_transaction(run_save):
    conn = Connection(orders=[order(transfer_warehouse="dongguan")])
    run_save(conn, allowed_warehouse="dongguan", purchase_cost="50")
    begin = next(i for i, (sql, _) in enumerate(conn.calls) if sql == "BEGIN")
    locked_read = next(i for i, (sql, _) in enumerate(conn.calls) if "FOR UPDATE" in sql)
    update = next(i for i, (sql, _) in enumerate(conn.calls) if sql.startswith("UPDATE"))
    assert begin < locked_read < update
