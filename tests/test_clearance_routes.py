import ast
from functools import wraps
from pathlib import Path

import pytest
from flask import Flask, jsonify, request

from bit import clearance_routes


@pytest.fixture
def app(monkeypatch):
    application = Flask(__name__)
    application.config.update(TESTING=True)
    application.allowed = {11}
    application.user_permissions = {"order_analysis.view", "order_analysis.execute"}
    application.reject_internal = False

    def login_required(function):
        @wraps(function)
        def decorated(*args, **kwargs):
            if request.headers.get("X-Test-Login") != "yes":
                return jsonify(status="error"), 401
            return function(*args, **kwargs)
        return decorated

    def internal_required(function):
        @wraps(function)
        def decorated(*args, **kwargs):
            if request.headers.get("X-Test-Internal") != "yes":
                return jsonify(status="error"), 401
            return function(*args, **kwargs)
        return decorated

    clearance_routes.register_routes(application, login_required=login_required,
        internal_api_required=internal_required, authorized_token_ids=lambda: application.allowed,
        current_user=lambda: {"permissions": application.user_permissions},
        has_permission=lambda user, permission: permission in user["permissions"],
        reject_client_mode=lambda: (jsonify(status="error"), 409) if application.reject_internal else None)
    return application


def login():
    return {"X-Test-Login": "yes"}


def internal():
    return {"X-Test-Internal": "yes"}


def test_public_and_internal_routes_require_their_auth_decorators(app):
    client = app.test_client()
    assert client.get("/api/clearance-alerts").status_code == 401
    assert client.post("/api/clearance-alerts/refresh", json={}).status_code == 401
    assert client.patch("/api/clearance-alerts/1/facts", json={}).status_code == 401
    assert client.get("/api/db/clearance-alerts", headers=login()).status_code == 401


def test_public_listing_uses_authenticated_scope_and_passes_filters(app, monkeypatch):
    seen = {}
    monkeypatch.setattr(clearance_routes.bit_db_api, "list_clearance_alerts",
                        lambda **options: seen.update(options) or {"rows": []})
    response = app.test_client().get("/api/clearance-alerts?allowed_token_ids=22&token_id=11&site_id=MLM&"
                                    "page=2&page_size=25&search=sku&alert_status=risk", headers=login())
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert seen == {"allowed_token_ids": {11}, "token_id": 11, "site_id": "MLM",
                    "page": 2, "page_size": 25, "search": "sku", "alert_status": "risk"}


def test_empty_public_scope_is_not_expanded_to_all_tokens(app, monkeypatch):
    app.allowed = set()
    seen = {}
    monkeypatch.setattr(clearance_routes.bit_db_api, "list_clearance_alerts",
                        lambda **options: seen.update(options) or {"rows": []})
    assert app.test_client().get("/api/clearance-alerts", headers=login()).status_code == 200
    assert seen["allowed_token_ids"] == set()


@pytest.mark.parametrize("query", ["token_id=abc", "token_id=-1", "token_id=0", "page=no", "page=0",
                                    "page=1000001", "page_size=0", "page_size=201"])
def test_invalid_listing_parameters_return_400_without_database_call(app, monkeypatch, query):
    monkeypatch.setattr(clearance_routes.bit_db_api, "list_clearance_alerts",
                        lambda **_: pytest.fail("invalid request reached DB adapter"))
    assert app.test_client().get("/api/clearance-alerts?" + query, headers=login()).status_code == 400


def test_public_refresh_ignores_injected_scope(app, monkeypatch):
    seen = {}
    monkeypatch.setattr(clearance_routes.bit_db_api, "refresh_clearance_alerts",
                        lambda **options: seen.update(options) or {"started": True})
    response = app.test_client().post("/api/clearance-alerts/refresh",
        json={"token_id": 11, "site_id": "MLM", "allowed_token_ids": [22]}, headers=login())
    assert response.status_code == 200
    assert seen == {"allowed_token_ids": {11}, "token_id": 11, "site_id": "MLM"}


@pytest.mark.parametrize("payload", [None, [], {"token_id": True}, {"token_id": -1}, {"token_id": "11"}])
def test_refresh_rejects_invalid_json_and_token_types(app, monkeypatch, payload):
    monkeypatch.setattr(clearance_routes.bit_db_api, "refresh_clearance_alerts",
                        lambda **_: pytest.fail("invalid request reached DB adapter"))
    assert app.test_client().post("/api/clearance-alerts/refresh", json=payload, headers=login()).status_code == 400


def test_public_facts_keeps_sku_key_and_authenticated_scope(app, monkeypatch):
    seen = {}
    def save(link_id, facts, **options):
        seen.update(link_id=link_id, facts=facts, **options)
        return {"alert": {"status": "pending_data"}}
    monkeypatch.setattr(clearance_routes.bit_db_api, "save_clearance_facts", save)
    payload = {"sku_key": "MLM1:variant2", "restock_after_stockout": False}
    response = app.test_client().patch("/api/clearance-alerts/1/facts", json=payload, headers=login())
    assert response.status_code == 200
    assert seen == {"link_id": 1, "facts": payload, "allowed_token_ids": {11}}


@pytest.mark.parametrize("error,status", [(PermissionError("无权访问"), 403), (KeyError("other account"), 404),
                                         (ValueError("日期无效"), 400), (RuntimeError("secret DB details"), 502)])
def test_adapter_errors_have_safe_http_status_and_no_raw_system_error(app, monkeypatch, error, status):
    def fail(**_options):
        raise error
    monkeypatch.setattr(clearance_routes.bit_db_api, "list_clearance_alerts", fail)
    response = app.test_client().get("/api/clearance-alerts", headers=login())
    assert response.status_code == status
    assert "secret DB details" not in response.get_data(as_text=True)
    assert "other account" not in response.get_data(as_text=True)


def test_internal_empty_and_explicit_scope_are_preserved(app, monkeypatch):
    calls = []
    monkeypatch.setattr(clearance_routes.bit_db_api, "list_clearance_alerts",
                        lambda **options: calls.append(options) or {"rows": []})
    client = app.test_client()
    client.get("/api/db/clearance-alerts?allowed_token_ids=", headers=internal())
    client.get("/api/db/clearance-alerts?allowed_token_ids=11&allowed_token_ids=22", headers=internal())
    assert calls[0]["allowed_token_ids"] == set()
    assert calls[1]["allowed_token_ids"] == {11, 22}


def test_internal_facts_unwraps_payload_without_leaking_scope_into_fact_fields(app, monkeypatch):
    seen = {}
    def save(link_id, facts, **options):
        seen.update(link_id=link_id, facts=facts, **options)
        return {}
    monkeypatch.setattr(clearance_routes.bit_db_api, "save_clearance_facts", save)
    response = app.test_client().patch("/api/db/clearance-alerts/1/facts",
        json={"facts": {"sku_key": "MLM1:1", "notice_at": "2026-09-30"}, "allowed_token_ids": []},
        headers=internal())
    assert response.status_code == 200
    assert seen == {"link_id": 1, "facts": {"sku_key": "MLM1:1", "notice_at": "2026-09-30"},
                    "allowed_token_ids": set()}


@pytest.mark.parametrize("values", ["11", [True], [-1], ["11"]])
def test_internal_write_scope_requires_positive_integer_array(app, monkeypatch, values):
    monkeypatch.setattr(clearance_routes.bit_db_api, "refresh_clearance_alerts",
                        lambda **_: pytest.fail("invalid request reached DB adapter"))
    assert app.test_client().post("/api/db/clearance-alerts/refresh",
        json={"allowed_token_ids": values}, headers=internal()).status_code == 400


def test_internal_routes_reject_client_mode_without_recursive_forwarding(app, monkeypatch):
    app.reject_internal = True
    monkeypatch.setattr(clearance_routes.bit_db_api, "list_clearance_alerts",
                        lambda **_: pytest.fail("client attempted recursive adapter forwarding"))
    assert app.test_client().get("/api/db/clearance-alerts", headers=internal()).status_code == 409


def test_page_requires_view_permission(app):
    app.user_permissions = set()
    assert app.test_client().get("/clearance-alerts", headers=login()).status_code == 403


def test_api_client_adapter_preserves_explicit_empty_scope(monkeypatch):
    adapter = clearance_routes.bit_db_api
    monkeypatch.setattr(adapter, "DB_MODE", "api")
    calls = []
    monkeypatch.setattr(adapter, "_request", lambda *args, **kwargs: calls.append((args, kwargs)) or {})
    adapter.list_clearance_alerts(allowed_token_ids=set())
    adapter.refresh_clearance_alerts(allowed_token_ids=set())
    adapter.save_clearance_facts(1, {"sku_key": "MLM1:1"}, allowed_token_ids=set())
    assert calls[0][1]["params"]["allowed_token_ids"] == [""]
    assert calls[1][1]["json"]["allowed_token_ids"] == []
    assert calls[2][1]["json"]["allowed_token_ids"] == []


def test_real_workbench_permission_mapping_distinguishes_reads_and_mutations():
    # Importing bit_interface initializes unrelated MySQL stores. Compile its
    # actual pure mapping function without starting the application instead.
    source_path = Path(clearance_routes.__file__).with_name("bit_interface.py")
    parsed = ast.parse(source_path.read_text(encoding="utf-8"))
    function = next(node for node in parsed.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_required_workbench_permissions")
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {}
    exec(compile(module, str(source_path), "exec"), namespace)
    permissions = namespace[function.name]
    assert permissions("/api/clearance-alerts", "GET") == ("order_analysis.view",)
    assert permissions("/api/clearance-alerts/refresh", "POST") == ("order_analysis.execute",)
    assert permissions("/api/clearance-alerts/1/facts", "PATCH") == ("order_analysis.execute",)
