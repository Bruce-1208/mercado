import ast
import functools
import hmac
import os
from functools import wraps
from pathlib import Path

import pytest
from flask import Flask, g, jsonify, request

from bit import bit_db_api, size_chart_routes


@pytest.fixture
def app(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    application = Flask(__name__, template_folder=str(root / "bit" / "templates"),
                        static_folder=str(root / "bit" / "static"))
    application.config["TESTING"] = True
    application.permissions = {"size_charts.view", "size_charts.execute"}
    application.allowed = {11}
    application.calls = []

    def auth(header):
        def decorate(function):
            @wraps(function)
            def authenticated(*args, **kwargs):
                if request.headers.get(header) != "yes":
                    return jsonify(status="error"), 401
                return function(*args, **kwargs)
            return authenticated
        return decorate

    def dispatch(action, payload, **scope):
        application.calls.append((action, payload, scope))
        return {"templates": []}

    monkeypatch.setattr(bit_db_api, "size_chart_call", dispatch)
    size_chart_routes.register_routes(application, login_required=auth("X-Login"),
        internal_api_required=auth("X-Internal"), authorized_token_ids=lambda: application.allowed,
        current_user=lambda: {"permissions": application.permissions},
        has_permission=lambda user, permission: permission in user["permissions"],
        organization_key=lambda: "company-a", reject_client_mode=lambda: None)
    return application


LOGIN = {"X-Login": "yes"}
INTERNAL = {"X-Internal": "yes"}


@pytest.mark.parametrize("query,embedded", [("", False), ("?embedded=1", True), ("?embedded=0", False)])
def test_size_chart_page_supports_embedded_workbench_view(app, query, embedded):
    response = app.test_client().get("/size-charts" + query, headers=LOGIN)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert ('class="zs-standalone sc-embedded"' in html) == embedded
    assert ("返回工作台" in html) != embedded
    assert 'id="size-charts"' in html


def test_embedded_size_chart_page_keeps_authentication_and_permissions(app):
    client = app.test_client()
    assert client.get("/size-charts?embedded=1").status_code == 401
    app.permissions = set()
    assert client.get("/size-charts?embedded=1", headers=LOGIN).status_code == 403


def test_authentication_and_read_only_permissions(app):
    client = app.test_client()
    assert client.get("/api/size-charts/templates").status_code == 401
    assert client.post("/api/db/size-charts/list", json={}, headers=LOGIN).status_code == 401
    app.permissions = {"size_charts.view"}
    assert client.get("/api/size-charts/templates", headers=LOGIN).status_code == 200
    assert client.post("/api/size-charts/requirements", json={}, headers=LOGIN).status_code == 200
    for path in ("/api/size-charts/templates", "/api/size-charts/upload", "/api/size-charts/preview"):
        assert client.post(path, json={}, headers=LOGIN).status_code == 403
    assert client.delete("/api/size-charts/templates/1", json={}, headers=LOGIN).status_code == 403


def test_public_scope_is_computed_from_authenticated_user(app):
    client = app.test_client()
    response = client.post("/api/size-charts/upload", json={"preview_id": "p",
        "organization_key": "company-b", "allowed_token_ids": [22]}, headers=LOGIN)
    assert response.status_code == 200
    action, payload, scope = app.calls[-1]
    assert action == "upload" and payload["preview_id"] == "p"
    assert scope == {"organization_key": "company-a", "allowed_token_ids": {11}}
    app.allowed = set()
    client.get("/api/size-charts/options", headers=LOGIN)
    assert app.calls[-1][2]["allowed_token_ids"] == set()


def test_url_template_id_cannot_be_overridden(app):
    response = app.test_client().patch("/api/size-charts/templates/12",
        json={"template_id": 99, "revision": 1}, headers=LOGIN)
    assert response.status_code == 200
    assert app.calls[-1][1]["template_id"] == 12


@pytest.mark.parametrize("payload", [None, [], "text"])
def test_malformed_body_rejected_before_dispatch(app, payload):
    assert app.test_client().post("/api/size-charts/templates", json=payload,
                                  headers=LOGIN).status_code == 400
    assert not app.calls


def test_internal_company_and_empty_scope_are_preserved(app):
    response = app.test_client().post("/api/db/size-charts/list", headers=INTERNAL,
        json={"organization_key": "company-a", "allowed_token_ids": [], "payload": {}})
    assert response.status_code == 200
    assert app.calls[-1][2] == {"organization_key": "company-a", "allowed_token_ids": set()}


@pytest.mark.parametrize("bad_scope", ["11", [True], [0], [-1], ["11"]])
def test_internal_scope_validation(app, bad_scope):
    response = app.test_client().post("/api/db/size-charts/upload", headers=INTERNAL,
        json={"organization_key": "company-a", "allowed_token_ids": bad_scope, "payload": {}})
    assert response.status_code == 400 and not app.calls


@pytest.mark.parametrize("error,status", [(PermissionError("无权操作"), 403), (KeyError("private"), 404),
    (ValueError("请填写真实尺寸"), 400), (RuntimeError("secret token"), 502)])
def test_safe_error_mapping(app, monkeypatch, error, status):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(bit_db_api, "size_chart_call", fail)
    response = app.test_client().get("/api/size-charts/templates", headers=LOGIN)
    assert response.status_code == status
    assert "secret token" not in response.get_data(as_text=True)
    assert "private" not in response.get_data(as_text=True)


def test_client_proxy_forwards_tenant_scope_once(monkeypatch):
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    calls = []
    monkeypatch.setattr(bit_db_api, "_request", lambda *args, **kwargs: calls.append((args, kwargs)) or {})
    bit_db_api.size_chart_call("upload", {"preview_id": "p"}, organization_key="company-a", allowed_token_ids=set())
    args, kwargs = calls[0]
    assert args == ("POST", "/api/db/size-charts/upload")
    assert kwargs["json"]["allowed_token_ids"] == []
    assert kwargs["json"]["organization_key"] == "company-a"
    assert kwargs["max_attempts"] == 1


def test_size_charts_owned_by_worker():
    from bit.service_split import is_worker_path
    assert is_worker_path("/api/size-charts/upload")
    assert is_worker_path("/api/size-charts/records")
    assert is_worker_path("/api/db/size-charts/list")
    assert not is_worker_path("/size-charts")


def test_global_permission_hook_protects_size_chart_routes():
    source = (Path(__file__).resolve().parents[1] / "bit" / "bit_interface.py").read_text()
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_required_workbench_permissions")
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "permissions", "exec"), namespace)
    permission = namespace[function.name]
    assert permission("/api/size-charts/templates", "GET") == ("size_charts.view",)
    assert permission("/api/size-charts/requirements", "POST") == ("size_charts.view",)
    assert permission("/api/size-charts/preview", "POST") == ("size_charts.execute",)
    assert permission("/api/size-charts/upload", "POST") == ("size_charts.execute",)


def test_internal_size_chart_api_rejects_unauthenticated_loopback(monkeypatch):
    source = (Path(__file__).resolve().parents[1] / "bit" / "bit_interface.py").read_text()
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "internal_api_required")
    denied = next(node for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "INTERNAL_LOOPBACK_DENIED_PREFIXES"
                          for target in node.targets))
    namespace = {"functools": functools, "hmac": hmac, "os": os, "g": g,
                 "request": request, "jsonify": jsonify,
                 "_verify_local_agent_credential": lambda _: None}
    exec(compile(ast.Module(body=[denied, function], type_ignores=[]), "internal_auth", "exec"), namespace)
    application = Flask(__name__)
    application.add_url_rule("/api/db/size-charts/upload", view_func=namespace[function.name](
        lambda: jsonify(status="success")), methods=["POST"])
    monkeypatch.setenv("BIT_DB_API_TOKEN", "test-internal-auth")
    client = application.test_client()
    assert client.post("/api/db/size-charts/upload").status_code == 403
    assert client.post("/api/db/size-charts/upload", headers={"X-Internal-Token": "test-internal-auth"}).status_code == 200
