"""Keep list filters scoped to the selected platform across both DB transports."""
import ast
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest
from flask import Flask, jsonify, request

from bit import bit_db_api as api
from erp import mercadolibre_collection_store as store


class QueryConnection:
    def __init__(self):
        self.queries = []

    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params=None): self.queries.append((sql, params))
    def fetchone(self): return {"total": 0}
    def fetchall(self): return []
    def commit(self): pass
    def close(self): pass


@pytest.mark.parametrize("platform,path", [
    (None, "$.ai_original"),
    ("mercadolibre", "$.ai_original"),
    ("yandex", "$.marketplace_listings.yandex"),
    ("amazon", "$.marketplace_listings.amazon"),
])
def test_status_and_generator_filters_share_selected_platform_before_pagination(monkeypatch, platform, path):
    connection = QueryConnection()
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    kwargs = {"target_platform": platform} if platform is not None else {}
    store.list_product_items(
        source_type="ai_original", ai_status="pending", generator_salesperson="张三",
        collector_salesperson="李四", limit=20, offset=40,
        connection_factory=lambda: connection, **kwargs,
    )
    assert len(connection.queries) == 2
    for sql, params in connection.queries:
        assert f"'{path}.status'" in sql
        assert f"'{path}.generated_by'" in sql
        assert "'$.plugin_snapshot.created_by'" in sql
        assert "'pending') = %s" in sql
        assert params[:4] == ("ai_original", "pending", "%李四%", "张三")
        if platform in {"yandex", "amazon"}:
            assert "'$.ai_original.status'" not in sql
            assert "'$.ai_original.generated_by'" not in sql
    page_sql, page_params = connection.queries[1]
    assert page_sql.index(f"'{path}.generated_by'") < page_sql.index("LIMIT %s OFFSET %s")
    assert page_params[-2:] == (20, 40)


@pytest.mark.parametrize("platform", ["unknown", "amazon.status", "amazon') OR 1=1 --"])
def test_unknown_platform_rejected_before_database_connection(platform):
    connect = Mock(side_effect=AssertionError("must validate before connecting"))
    with pytest.raises(ValueError, match="目标平台"):
        store.list_product_items(target_platform=platform, connection_factory=connect)
    connect.assert_not_called()


@pytest.mark.parametrize("mode", ["mysql", "api"])
def test_list_transport_keeps_platform_with_its_filters(monkeypatch, mode):
    monkeypatch.setattr(api, "DB_MODE", mode)
    query = Mock(return_value={"rows": [], "total": 0})
    monkeypatch.setattr(api, "_request" if mode == "api" else "_collection_store_call", query)
    api.list_mercado_product_items(target_platform="yandex", ai_status="completed", generator_salesperson="张三")
    params = query.call_args.kwargs["params"] if mode == "api" else query.call_args.kwargs
    assert params["target_platform"] == "yandex"
    assert params["ai_status"] == "completed"
    assert params["generator_salesperson"] == "张三"


def load_route(name, query):
    node = next(node for node in ast.parse(Path("bit/bit_interface.py").read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    namespace = dict(request=request, jsonify=jsonify, logging=logging,
                     db_list_mercado_product_items=query,
                     reject_db_api_client_mode=lambda: None,
                     _parse_int_param=lambda args, key, default, low, high: int(args.get(key, default)))
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<platform-list-route>", "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("name", ["api_ai_original_products", "api_db_mercado_products"])
def test_list_routes_forward_platform_and_reject_unrecognized_value(name):
    query = Mock(return_value={"rows": [], "total": 0})
    route = load_route(name, query)
    app = Flask(__name__)
    with app.test_request_context("/?target_platform=amazon&ai_status=completed&generator_salesperson=张三"):
        response = route()
    assert response.status_code == 200
    assert query.call_args.kwargs["target_platform"] == "amazon"
    assert query.call_args.kwargs["ai_status"] == "completed"
    assert query.call_args.kwargs["generator_salesperson"] == "张三"
    query.reset_mock()
    with app.test_request_context("/?target_platform=unknown"):
        response, status = route()
    assert status == 400
    assert "目标平台" in response.get_json()["message"]
    query.assert_not_called()


def test_generator_options_ignore_search_and_selection_but_keep_access_scope(monkeypatch):
    connection = QueryConnection()
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    store.list_product_items(
        source_type="ai_original", search="产品", generator_salesperson="张三",
        target_platform="amazon", token_ids=[7], include_generator_options=True,
        connection_factory=lambda: connection,
    )
    sql, params = connection.queries[0]
    assert "SELECT DISTINCT" in sql
    assert "$.marketplace_listings.amazon.generated_by" in sql
    assert "LIMIT" not in sql
    assert "last_publish_token_id" in sql
    assert params == (7, 7, 7, "ai_original")
