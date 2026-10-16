"""Authorization is resolved on every request, before sharing order reads."""
import ast
from pathlib import Path
from unittest.mock import Mock

from flask import Flask, has_request_context, jsonify, request

from bit import order_read_cache as cache


def test_route_shares_equal_visibility_but_rechecks_all_scope_restrictions():
    cache.invalidate()
    user = {"id": 1, "organization_key": "a", "role_key": "member"}
    allowed = [1]
    warehouse = [None]
    own_store = [False]
    loader = Mock(side_effect=lambda **params: {"rows": [{"store_id": params["store_ids"][0]}]})
    app = Flask(__name__)
    namespace = dict(app=app, request=request, jsonify=jsonify,
        has_request_context=has_request_context,
        workbench_user_own_store_only=lambda: own_store[0],
        workbench_user_salesperson=lambda: user["personal_name"],
        _authorized_order_store_ids=lambda: set(allowed),
        user_warehouse=lambda value: warehouse[0],
        get_current_workbench_user=lambda: dict(user), db_list_orders=loader)
    names = {"api_orders", "_order_list_query_params", "_parse_int_param"}
    nodes = [node for node in ast.parse(Path("bit/bit_interface.py").read_text()).body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<order-route>", "exec"), namespace)
    app.add_url_rule("/api/orders", view_func=namespace["api_orders"])
    client = app.test_client()
    assert client.get("/api/orders").json["data"]["rows"] == [{"store_id": 1}]
    user["id"] = 2
    assert client.get("/api/orders").status_code == 200
    assert loader.call_count == 1
    # Revocation changes the effective scope immediately, inside the cache TTL.
    allowed[:] = [2]
    assert client.get("/api/orders").json["data"]["rows"] == [{"store_id": 2}]
    assert loader.call_count == 2
    assert client.get("/api/orders?store_id=1").json["data"]["rows"] == []
    assert loader.call_count == 2
    user["organization_key"] = "b"
    assert client.get("/api/orders").status_code == 200
    assert loader.call_count == 3
    warehouse[0] = "shahu"
    assert client.get("/api/orders").status_code == 200
    assert loader.call_count == 4
    assert loader.call_args.kwargs["transfer_warehouse"] == "shahu"
    own_store[0] = True
    user["personal_name"] = "Alice"
    assert client.get("/api/orders").status_code == 200
    user["personal_name"] = "Bob"
    assert client.get("/api/orders").status_code == 200
    assert loader.call_count == 6
    assert loader.call_args.kwargs["salesperson"] == "Bob"
    assert client.get("/api/orders?refresh=1").status_code == 200
    assert loader.call_count == 7
    cache.invalidate()


def test_unknown_organization_keeps_account_isolation():
    cache.invalidate()
    loader = Mock(return_value={"rows": []})
    for uid in (1, 2):
        cache.read_authorized(loader, {}, user={"id": uid})
    assert loader.call_count == 2
    cache.invalidate()
