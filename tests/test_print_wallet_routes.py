"""Offline checks for the public print boundary and client/server forwarding."""
import ast
import functools
import hmac
import logging
import os
import secrets
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask, g, jsonify, redirect, request, send_file, url_for

from bit import bit_db_api, print_wallet


# Load the actual route functions without starting unrelated browser services,
# background jobs, or connecting to the application's production database.
root = Path(__file__).resolve().parents[1]
source = ast.parse((root / "bit/bit_interface.py").read_text())
app = Flask(__name__, template_folder=str(root / "bit/templates"))
workbench = SimpleNamespace(
    app=app, bit_db_api=bit_db_api, g=g, jsonify=jsonify, request=request,
    redirect=redirect, url_for=url_for, send_file=send_file, BytesIO=BytesIO,
    functools=functools, hmac=hmac, os=os, secrets=secrets, logging=logging,
    WalletConflict=print_wallet.WalletConflict,
    WalletInsufficientFunds=print_wallet.WalletInsufficientFunds,
    get_current_workbench_user=lambda: None,
    _authorized_token_ids_for_user=lambda *args: set(),
    workbench_selected_organization=lambda user: "",
    _order_warehouse_operator=lambda user: user.get("role_key") == "warehouse",
    _verify_local_agent_credential=lambda value: None,
    _agent_internal_api_allowed=lambda claims: False,
    reject_db_api_client_mode=lambda: None,
    INTERNAL_AGENT_ALLOWED_PATHS=frozenset(),
    YANDEX_CONSOLE_PROXY_PATH="/yandex-console",
)
needed = {"_workbench_billing_blocked", "login_required", "internal_api_required",
          "_print_wallet_error_response", "api_print_orders_pdf", "api_db_order_labels"}
for node in source.body:
    is_prefix = isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "INTERNAL_LOOPBACK_DENIED_PREFIXES"
        for target in node.targets)
    if is_prefix or isinstance(node, ast.FunctionDef) and node.name in needed:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "bit_interface.routes", "exec"), workbench.__dict__)
app.register_blueprint(print_wallet.create_blueprint(
    login_required=workbench.login_required,
    current_user=lambda: workbench.get_current_workbench_user(),
    storage=bit_db_api.print_wallet,
))


@pytest.fixture
def client(monkeypatch):
    user = {"id": 7, "username": "warehouse", "role_key": "warehouse",
            "display_name": "仓库人员", "organization_key": "alpha",
            "permissions": ["order_print.view", "order_print.execute"]}
    def current_user():
        g.workbench_user = user
        return user
    monkeypatch.setattr(workbench, "get_current_workbench_user", current_user)
    monkeypatch.setattr(workbench, "_authorized_token_ids_for_user", lambda *args: {10})
    monkeypatch.setattr(bit_db_api, "account_billing", lambda payload: {"status": "active"})
    workbench.app.config.update(TESTING=True, SECRET_KEY="wallet-route-test")
    return workbench.app.test_client()


@pytest.mark.parametrize("error, status", [
    (print_wallet.WalletInsufficientFunds("业务员甲余额不足，需要 3 元"), 402),
    (print_wallet.WalletConflict("本次打印已处理，请核实流水", request_status="settled"), 409),
    (PermissionError("无权打印其他企业订单"), 403),
])
def test_denied_print_never_releases_pdf_or_logs(client, monkeypatch, error, status):
    download = Mock(side_effect=error)
    logs = Mock()
    monkeypatch.setattr(bit_db_api, "download_order_labels", download)
    monkeypatch.setattr(bit_db_api, "record_order_print_logs", logs)
    response = client.post("/api/orders/print", json={"order_ids": ["20001"], "request_id": "route-print-001"})
    assert response.status_code == status
    assert response.is_json
    if status == 409:
        assert response.json["request_status"] == "settled"
    if status in {402, 409}:
        assert response.json["wallet_url"] == "/billing?section=print-wallet"
    assert not response.data.startswith(b"%PDF")
    logs.assert_not_called()
    download.assert_called_once_with(["20001"], actor_id=7, request_id="route-print-001", organization_key="")


def test_charge_metadata_and_actor_are_forwarded(client, monkeypatch):
    download = Mock(return_value={"content": b"%PDF-1.4\n%%EOF", "order_ids": ["20001"],
                                 "shipment_count": 1, "wallet_charge_cents": 300,
                                 "wallet_request_id": "route-print-002"})
    monkeypatch.setattr(bit_db_api, "download_order_labels", download)
    monkeypatch.setattr(bit_db_api, "record_order_print_logs", Mock())
    monkeypatch.setattr(bit_db_api, "bulk_update_orders", Mock())
    response = client.post("/api/orders/print", json={"order_ids": ["20001"], "request_id": "route-print-002", "actor_id": 999})
    assert response.status_code == 200
    assert response.headers["X-Print-Wallet-Charge-Cents"] == "300"
    assert response.headers["X-Print-Wallet-Request-Id"] == "route-print-002"
    assert response.headers["Cache-Control"] == "no-store"
    download.assert_called_once_with(["20001"], actor_id=7, request_id="route-print-002", organization_key="")


def test_client_mode_forwards_wallet_identity_and_requires_paid_server(monkeypatch):
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    response = Mock(ok=True, content=b"%PDF-1.4\n%%EOF", headers={
        "X-Print-Wallet-Request-Id": "proxy-print-001", "X-Print-Wallet-Charge-Cents": "300",
        "X-Mercado-Printed-Order-Ids": "20001", "X-Mercado-Shipment-Count": "1"})
    post = Mock(return_value=response)
    monkeypatch.setattr(bit_db_api.DB_API_SESSION, "post", post)
    result = bit_db_api.download_order_labels(["20001"], actor_id=7, request_id="proxy-print-001")
    assert result["wallet_charge_cents"] == 300
    assert post.call_args.kwargs["json"]["actor_id"] == 7
    assert post.call_args.kwargs["json"]["request_id"] == "proxy-print-001"
    response.headers.pop("X-Print-Wallet-Request-Id")
    with pytest.raises(RuntimeError, match="未确认"):
        bit_db_api.download_order_labels(["20001"], actor_id=7, request_id="proxy-print-001")


def test_client_mode_preserves_insufficient_balance_error(monkeypatch):
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    response = Mock(ok=False, status_code=402)
    response.json.return_value = {"status": "error", "message": "业务员甲余额不足"}
    monkeypatch.setattr(bit_db_api.DB_API_SESSION, "post", Mock(return_value=response))
    with pytest.raises(print_wallet.WalletInsufficientFunds, match="余额不足"):
        bit_db_api.download_order_labels(["20001"], actor_id=7, request_id="proxy-print-001")


def test_unpaid_user_can_read_wallet_without_subscription_check(client, monkeypatch):
    monkeypatch.setattr(bit_db_api, "account_billing", Mock(side_effect=AssertionError("wallet must remain accessible")))
    response = client.get("/print-wallet")
    assert response.status_code == 302
    assert response.location == "/billing?section=print-wallet"
    with workbench.app.test_request_context("/print-wallet"):
        assert workbench._workbench_billing_blocked({"id": 7, "role_key": "warehouse"}) is None
    with workbench.app.test_request_context("/api/print-wallet/me"):
        assert workbench._workbench_billing_blocked({"id": 7, "role_key": "warehouse"}) is None
    with workbench.app.test_request_context("/billing?section=print-wallet"):
        assert workbench._workbench_billing_blocked({"id": 7, "role_key": "warehouse"}) is None


def test_wallet_api_uses_logged_in_actor_and_revalidates_role(client, monkeypatch):
    monkeypatch.setattr(bit_db_api, "DB_MODE", "mysql")
    local = Mock(side_effect=PermissionError("仓库人员不能登记充值"))
    monkeypatch.setattr(print_wallet, "wallet_local", local)
    response = client.post("/api/print-wallet/accounts/2/recharges", json={
        "actor_id": 1, "amount": "30.00", "note": "伪造管理员", "request_id": "recharge-route-001"})
    assert response.status_code == 403
    payload = local.call_args.args[0]
    assert payload["actor_id"] == 7
    assert payload["user_id"] == 2
    assert payload["action"] == "recharge"


def test_internal_label_api_cannot_use_unauthenticated_loopback(client, monkeypatch):
    monkeypatch.delenv("BIT_DB_API_TOKEN", raising=False)
    paid = Mock()
    monkeypatch.setattr(print_wallet, "download_paid_labels", paid)
    response = client.post("/api/db/orders/labels", json={
        "actor_id": 7, "order_ids": ["20001"], "request_id": "loopback-print-001"})
    assert response.status_code == 403
    paid.assert_not_called()


def test_print_record_filters_keep_current_actor_identity(client, monkeypatch):
    storage = Mock(return_value={"account": {"user_id": 2}, "records": [], "total": 0, "limit": 20, "offset": 20})
    monkeypatch.setattr(bit_db_api, "DB_MODE", "mysql")
    monkeypatch.setattr(print_wallet, "wallet_local", storage)
    response = client.get("/api/print-wallet/accounts/2/records", query_string={
        "order_id": "1001", "date_from": "2026-10-01", "date_to": "2026-10-06",
        "printer": "仓库 A", "kind": "print", "limit": "20", "offset": "20", "actor_id": "999",
    })
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = storage.call_args.args[0]
    assert payload["actor_id"] == 7 and payload["user_id"] == 2 and payload["action"] == "records"
    assert payload["data"] == {
        "order_id": "1001", "date_from": "2026-10-01", "date_to": "2026-10-06",
        "printer": "仓库 A", "kind": "print", "limit": "20", "offset": "20",
    }
