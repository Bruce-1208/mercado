import ast
from functools import wraps
from pathlib import Path
import pytest
from flask import Flask, jsonify, request
from bit import price_competition_routes as routes
from bit import price_competition as service


@pytest.fixture
def app(monkeypatch):
    app = Flask(__name__, template_folder=str(Path(routes.__file__).parent / "templates"))
    app.config["TESTING"] = True
    app.scope = {11}
    app.permissions = {"price_competition.view", "price_competition.execute"}
    app.reject = False
    def authenticated(header):
        def decorator(function):
            @wraps(function)
            def wrapper(*args, **kwargs):
                if request.headers.get(header) != "yes":
                    return jsonify(status="error"), 401
                return function(*args, **kwargs)
            return wrapper
        return decorator
    routes.register_routes(app, login_required=authenticated("X-Login"), internal_api_required=authenticated("X-Internal"),
        authorized_token_ids=lambda: app.scope, current_user=lambda: {"id": 7},
        has_permission=lambda user, permission: permission in app.permissions,
        reject_client_mode=lambda: (jsonify(status="error"), 409) if app.reject else None)
    return app


def test_login_permissions_and_page(app, monkeypatch):
    client = app.test_client()
    assert client.get("/api/price-competition").status_code == 401
    assert client.post("/api/price-competition/confirm", json={}).status_code == 401
    assert client.post("/api/db/price-competition/confirm", headers={"X-Login": "yes"}, json={}).status_code == 401
    assert client.get("/price-competition", headers={"X-Login": "yes"}).status_code == 200
    app.permissions = set()
    assert client.get("/price-competition", headers={"X-Login": "yes"}).status_code == 403
    assert client.get("/api/price-competition", headers={"X-Login": "yes"}).status_code == 403
    app.permissions = {"price_competition.view"}
    assert client.post("/api/price-competition/costs", headers={"X-Login": "yes"}, json={}).status_code == 403


def test_scope_and_actor_are_from_login_and_empty_scope_stays_empty(app, monkeypatch):
    seen = []
    monkeypatch.setattr(routes.bit_db_api, "price_competition_call", lambda *args, **kwargs: seen.append((args,kwargs)) or {})
    client = app.test_client()
    response = client.post("/api/price-competition/preview", headers={"X-Login": "yes"},
        json={"link_id": 1, "actor": "admin", "allowed_token_ids": [22]})
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert seen[-1][1] == {"allowed_token_ids": {11}, "actor": "7"}
    app.scope = set()
    client.get("/api/price-competition?allowed_token_ids=22", headers={"X-Login": "yes"})
    assert seen[-1][1]["allowed_token_ids"] == set()


@pytest.mark.parametrize("values", ["11", [True], [-1], ["11"]])
def test_invalid_internal_scope(app, monkeypatch, values):
    monkeypatch.setattr(routes.bit_db_api, "price_competition_call", lambda *a, **k: pytest.fail("invalid scope reached adapter"))
    assert app.test_client().post("/api/db/price-competition/list", headers={"X-Internal": "yes"},
        json={"payload": {}, "allowed_token_ids": values}).status_code == 400


def test_internal_forwarding_cannot_loop_and_unknown_action_rejected(app, monkeypatch):
    app.reject = True
    assert app.test_client().post("/api/db/price-competition/list", headers={"X-Internal": "yes"}, json={}).status_code == 409
    assert app.test_client().post("/api/price-competition/auto-reprice", headers={"X-Login": "yes"}, json={}).status_code == 400


def test_remote_adapter_no_retry_and_preserves_empty_scope(monkeypatch):
    adapter = routes.bit_db_api
    monkeypatch.setattr(adapter, "DB_MODE", "api")
    calls = []
    monkeypatch.setattr(adapter, "_request", lambda *args, **kwargs: calls.append((args,kwargs)) or {})
    adapter.price_competition_call("confirm", {}, allowed_token_ids=set(), actor="7")
    assert calls[0][0] == ("POST", "/api/db/price-competition/confirm")
    assert calls[0][1]["json"]["allowed_token_ids"] == []
    assert calls[0][1]["max_attempts"] == 1


def test_listing_scope_and_page_size(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(routes.bit_db_api, "list_mercado_store_links", lambda **kwargs: seen.update(kwargs) or {"rows": []})
    from erp.price_competition_store import PriceCompetitionStore
    store = PriceCompetitionStore(tmp_path / "pricing.sqlite3")
    service.dispatch("list", {}, allowed_token_ids=set(), store=store)
    assert seen["token_ids"] == set() and seen["page_size"] == 20
    with pytest.raises(PermissionError):
        service.dispatch("list", {"token_id": 22}, allowed_token_ids={11}, store=store)


def test_real_link_access_checks_current_store_permissions_before_official_read(monkeypatch):
    from erp import mercadolibre_store_link_store as links
    row = {"token_id": 11, "is_current": 1, "status": "active"}
    monkeypatch.setattr(links, "get_store_links_by_ids", lambda _: [row])
    with pytest.raises(PermissionError):
        service._link(1, set())
    assert service._link(1, {11}) == row
    row["is_current"] = 0
    with pytest.raises(ValueError, match="失效"):
        service._link(1, {11})


def test_workbench_permission_mapping_and_worker_routing():
    path = Path(routes.__file__).with_name("bit_interface.py")
    tree = ast.parse(path.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_required_workbench_permissions")
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    assert namespace[function.name]("/api/price-competition", "GET") == ("price_competition.view",)
    assert namespace[function.name]("/api/price-competition/confirm", "POST") == ("price_competition.execute",)
    from bit.service_split import is_worker_path
    assert is_worker_path("/api/price-competition", "GET")
    assert is_worker_path("/api/db/price-competition/confirm", "POST")
