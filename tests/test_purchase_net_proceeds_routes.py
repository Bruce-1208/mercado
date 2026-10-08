"""The purchase ratio survives both database modes and server routing."""
from unittest.mock import Mock

import pytest

from bit import bit_db_api, bit_interface as workbench


@pytest.fixture
def client(monkeypatch, isolated_legacy_console_user):
    workbench.app.config.update(TESTING=True, SECRET_KEY="purchase-net-test")
    monkeypatch.setattr(workbench, "_workbench_billing_blocked", lambda user: None)
    monkeypatch.setattr(workbench, "_authorized_order_store_ids", lambda: {11, 12})
    client = workbench.app.test_client()
    with client.session_transaction() as state:
        state["workbench_user"] = {"id": 8, "username": "buyer", "display_name": "采购员"}
    return client


@pytest.mark.parametrize("ratio", [150, 200])
def test_purchase_ratio_public_route_uses_server_scope(client, monkeypatch, ratio):
    sync = {"ratio": ratio, "product_matched": 3, "store_link_matched": 5}
    update = Mock(return_value={"matched": 1, "changed": 1, "net_proceeds_sync": sync})
    monkeypatch.setattr(bit_db_api, "bulk_update_orders", update)
    response = client.post("/api/orders/bulk-update", json={
        "order_ids": ["ORDER-1"], "purchase_order": "PO-1", "purchase_cost": "70",
        "purchase_net_proceeds_ratio": ratio, "allowed_token_ids": [999],
        "operator_id": 999, "operator_name": "forged",
    })
    assert response.status_code == 200
    assert response.json["data"]["net_proceeds_sync"] == sync
    update.assert_called_once_with(
        ["ORDER-1"], allowed_token_ids={11, 12}, operator_id=8, operator_name="采购员",
        purchase_order="PO-1", purchase_cost="70", purchase_net_proceeds_ratio=ratio,
    )


def test_purchase_ratio_internal_route_forwards_to_transaction(client, monkeypatch):
    monkeypatch.setattr(workbench, "reject_db_api_client_mode", lambda: None)
    update = Mock(return_value={"matched": 1, "changed": 1})
    monkeypatch.setattr(workbench.bit_order_sync.bit_mysql, "bulk_update_mercado_orders", update)
    response = client.post("/api/db/orders/bulk-update", json={
        "order_ids": ["ORDER-1"], "purchase_cost": "70", "purchase_net_proceeds_ratio": 200,
        "allowed_token_ids": [11], "operator_id": 8, "operator_name": "采购员",
    })
    assert response.status_code == 200
    update.assert_called_once_with(
        ["ORDER-1"], allowed_token_ids=[11], operator_id=8, operator_name="采购员",
        purchase_cost="70", purchase_net_proceeds_ratio=200,
    )


@pytest.mark.parametrize("mode", ["api", "mysql"])
def test_purchase_ratio_database_adapter_forwards_both_modes(monkeypatch, mode):
    monkeypatch.setattr(bit_db_api, "DB_MODE", mode)
    call = Mock(return_value={"matched": 1})
    monkeypatch.setattr(bit_db_api, "_request" if mode == "api" else "_local_call", call)
    bit_db_api.bulk_update_orders(
        [123], purchase_cost="70", purchase_net_proceeds_ratio=150,
        allowed_token_ids=[11], operator_id=8, operator_name="采购员",
    )
    expected = {"purchase_cost": "70", "purchase_net_proceeds_ratio": 150,
                "allowed_token_ids": [11], "operator_id": 8, "operator_name": "采购员"}
    if mode == "mysql":
        call.assert_called_once_with("bulk_update_mercado_orders", ["123"], **expected)
    else:
        call.assert_called_once_with("POST", "/api/db/orders/bulk-update",
                                     json={"order_ids": ["123"], **expected})


def test_purchase_ratio_validation_error_is_displayable(client, monkeypatch):
    monkeypatch.setattr(bit_db_api, "bulk_update_orders", Mock(side_effect=ValueError("净收益比例只能选择 150% 或 200%")))
    response = client.post("/api/orders/bulk-update", json={
        "order_ids": ["ORDER-1"], "purchase_cost": "70", "purchase_net_proceeds_ratio": 300,
    })
    assert response.status_code == 400
    assert "150%" in response.json["message"]
