"""Check list query routing without importing the server or opening databases."""

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask, jsonify, request


@pytest.fixture
def routes():
    source_path = Path(__file__).parents[1] / "bit" / "bit_interface.py"
    source = ast.parse(source_path.read_text())
    names = {
        "_parse_int_param", "_mercado_infringement_user_id",
        "_mercado_infringement_filters", "_decorate_mercado_infringement",
        "api_mercado_products", "api_mercado_collection_items",
    }
    functions = [node for node in source.body if isinstance(node, ast.FunctionDef)
                 and node.name in names]
    for node in functions:
        node.decorator_list = []
    app = Flask(__name__)
    service = SimpleNamespace(
        filtered=Mock(return_value={"rows": [{"id": 8}], "total": 1}),
        decorate=Mock(side_effect=lambda rows, *_: rows),
    )
    product_list = Mock(return_value={"rows": [{"id": 3}], "total": 1})
    collection_list = Mock(return_value={"rows": [{"id": 4}], "total": 1})
    authorized_tokens = Mock(return_value={7, 9})
    namespace = {
        "jsonify": jsonify, "request": request, "logging": logging,
        "get_mercado_infringement_service": lambda: service,
        "get_current_workbench_user": lambda: {"id": 22},
        "_authorized_token_ids_for_user": authorized_tokens,
        "db_list_mercado_product_items": product_list,
        "db_list_mercado_collection_items": collection_list,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source_path), "exec"), namespace)
    return SimpleNamespace(app=app, namespace=namespace, service=service,
                           products=product_list, collection=collection_list,
                           tokens=authorized_tokens)


def invoke(routes, mode, params):
    name = "api_mercado_products" if mode == "products" else "api_mercado_collection_items"
    with routes.app.test_request_context("/list", query_string=params):
        return routes.namespace[name]()


@pytest.mark.parametrize("mode", ["products", "collection"])
@pytest.mark.parametrize("risk_filter,value", [
    ("risk_status", "high"), ("risk_scope", "title_image"), ("review_decision", "released"),
])
def test_risk_filters_skip_discarded_list_query_and_preserve_scope(routes, mode, risk_filter, value):
    params = {risk_filter: value, "search": "shirt", "source_type": "zying",
              "weight_status": "missing", "review_status": "approved",
              "limit": "100", "offset": "200"}
    response = invoke(routes, mode, params)
    assert response.status_code == 200
    assert response.get_json()["data"] == {"rows": [{"id": 8}], "total": 1}
    routes.products.assert_not_called()
    routes.collection.assert_not_called()
    routes.service.decorate.assert_not_called()
    routes.service.filtered.assert_called_once_with(
        mode, params, 22, {7, 9} if mode == "products" else None,
        limit=100, offset=200,
    )
    assert routes.tokens.call_count == (1 if mode == "products" else 0)


@pytest.mark.parametrize("mode", ["products", "collection"])
def test_ordinary_list_loads_once_and_keeps_existing_filters(routes, mode):
    response = invoke(routes, mode, {"risk_status": "", "limit": "100", "offset": "200",
                                     "search": " shirt ", "weight_status": "missing"})
    assert response.status_code == 200
    listing = getattr(routes, mode)
    listing.assert_called_once()
    params = listing.call_args.kwargs
    assert params["search"] == "shirt"
    assert params["weight_status"] == "missing"
    assert (params["limit"], params["offset"]) == (100, 200)
    if mode == "products":
        assert params["token_ids"] == [7, 9]
        routes.collection.assert_not_called()
    else:
        assert params["exclude_added"] is True
        routes.products.assert_not_called()
    routes.service.filtered.assert_not_called()
    routes.service.decorate.assert_called_once_with(listing.return_value["rows"], mode, 22)


@pytest.mark.parametrize("mode", ["products", "collection"])
def test_risk_filter_errors_keep_the_existing_bad_request_response(routes, mode):
    routes.service.filtered.side_effect = ValueError("侵权风险筛选值无效")
    response, status = invoke(routes, mode, {"risk_status": "invalid"})
    assert status == 400
    assert response.get_json()["message"] == "侵权风险筛选值无效"
    routes.products.assert_not_called()
    routes.collection.assert_not_called()


def test_product_single_page_flag_reaches_storage(routes):
    response = invoke(routes, "products", {"include_total": "0", "limit": "100"})
    assert response.status_code == 200
    assert routes.products.call_args.kwargs["include_total"] is False
    assert routes.products.call_args.kwargs["token_ids"] == [7, 9]
