"""Saved developer choices preserve product visibility without live ZYing access."""

import ast
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask, jsonify, request

from bit import bit_db_api as api
from erp import mercadolibre_collection_store as store


class Cursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.cursor = self.connection.db.cursor()
        return self

    def __exit__(self, *args):
        self.cursor.close()

    def execute(self, sql, params=()):
        self.connection.queries.append((sql, params))
        if "information_schema.TABLES" in sql:
            sql = "SELECT 1 AS present FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1"
        self.cursor.execute(sql.replace("%s", "?"), params)

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None


class Connection:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.queries = []
        self.closed = False
        self.db.executescript("""
            CREATE TABLE erp_mercadolibre_products (
                id INTEGER PRIMARY KEY, source_item_id TEXT,
                last_publish_token_id INTEGER, source_type TEXT,
                product_developer_id TEXT, product_developer_name TEXT,
                collection_item_id INTEGER, management_category_id INTEGER);
            CREATE TABLE erp_mercadolibre_publish_records (product_item_id INTEGER, token_id INTEGER);
            CREATE TABLE erp_mercadolibre_store_links (item_id TEXT, token_id INTEGER);
            CREATE TABLE erp_mercadolibre_management_categories (id INTEGER, name TEXT, listing_ratio_percent INTEGER);
            CREATE TABLE erp_mercadolibre_collection_items (id INTEGER, task_id INTEGER);
            CREATE TABLE erp_mercadolibre_collection_tasks (id INTEGER, created_by TEXT);
        """)

    def products(self, rows):
        self.db.executemany(
            "INSERT INTO erp_mercadolibre_products "
            "(id, source_item_id, last_publish_token_id, source_type, product_developer_id, product_developer_name) "
            "VALUES (?, ?, ?, ?, ?, ?)", rows,
        )

    def cursor(self):
        return Cursor(self)

    def close(self):
        self.closed = True

    def commit(self):
        self.db.commit()


@pytest.fixture(autouse=True)
def existing_schema(monkeypatch):
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)


def read(connection, **kwargs):
    return store.list_product_developers(connection_factory=lambda: connection, **kwargs)["rows"]


def test_all_saved_developers_are_available_beyond_a_product_page():
    connection = Connection()
    connection.products([
        (index, f"P{index}", None, "zying", str(index), f"开发{index:04}")
        for index in range(1, 1102)
    ])
    rows = read(connection)
    assert len(rows) == 1101
    assert rows[-1] == {"id": "1101", "name": "开发1101"}
    sql, params = connection.queries[0]
    assert "DISTINCT" in sql and "LIMIT" not in sql
    assert "MAX_EXECUTION_TIME(8000)" in sql
    assert params == ("zying",)
    assert connection.closed


def test_historical_names_empty_values_and_duplicate_ids_are_normalized():
    connection = Connection()
    long_name = "开" * 70
    connection.products([
        (1, "A", None, "zying", " 17 ", ""),
        (2, "B", None, "zying", "17", "17"),
        (3, "C", None, "zying", "17", " 开发甲 "),
        (4, "D", None, "zying", "17", "开发甲"),
        (5, "E", None, "zying", "", "历史开发"),
        (6, "F", None, "zying", None, " 历史开发 "),
        (7, "G", None, "zying", "18", None),
        (8, "H", None, "zying", None, ""),
        (9, "I", None, "zying", " ", " "),
        (10, "J", None, "pulled", "99", "不属于智赢"),
        (11, "K", None, "ai_original", "98", "AI产品"),
        (12, "L", None, "zying", None, long_name),
    ])
    assert read(connection) == [
        {"id": "18", "name": "18"},
        {"id": "历史开发", "name": "历史开发"},
        {"id": "17", "name": "开发甲"},
        {"id": long_name, "name": long_name},
    ]


@pytest.mark.parametrize("tokens,expected", [
    ([7], ["11", "12", "13"]), ([8], ["14"]),
    ([7, 8, 7], ["11", "12", "13", "14"]), ([999], []),
    (None, ["11", "12", "13", "14", "15"]),
])
def test_directory_shares_all_three_product_ownership_paths(tokens, expected):
    connection = Connection()
    connection.products([
        (1, "A", 7, "zying", "11", "开发11"),
        (2, "B", None, "zying", "12", "开发12"),
        (3, "C", None, "zying", "13", "开发13"),
        (4, "D", 8, "zying", "14", "开发14"),
        (5, "E", None, "zying", "15", "开发15"),
        (6, "F", 7, "pulled", "16", "开发16"),
    ])
    connection.db.executescript("""
        INSERT INTO erp_mercadolibre_publish_records VALUES (1,7),(2,7),(2,7),(99,7);
        INSERT INTO erp_mercadolibre_store_links VALUES ('A',7),('C',7),('C',7),('D',8),('missing',7);
    """)
    assert [row["id"] for row in read(connection, token_ids=tokens)] == expected
    assert len(connection.queries) == 1


def test_empty_scope_skips_database_and_empty_directory_is_valid():
    connect = Mock(side_effect=AssertionError("empty scope must not read global data"))
    assert store.list_product_developers(token_ids=[], connection_factory=connect) == {"rows": []}
    connect.assert_not_called()
    assert read(Connection()) == []


def add_legacy_directory(connection):
    connection.db.executescript("""
        CREATE TABLE zying_product (`产品开发编号` TEXT, `产品开发` TEXT);
        INSERT INTO zying_product VALUES
            (' 17 ', ' 开发甲 '), ('17', '17'),
            (NULL, '历史开发'), ('', ''), ('18', '开发乙');
    """)


def test_unrestricted_empty_saved_directory_uses_existing_legacy_developers():
    connection = Connection()
    add_legacy_directory(connection)
    assert read(connection) == [
        {"id": "历史开发", "name": "历史开发"},
        {"id": "18", "name": "开发乙"},
        {"id": "17", "name": "开发甲"},
    ]
    assert len(connection.queries) == 3
    assert "information_schema.TABLES" in connection.queries[1][0]
    assert "FROM `zying_product`" in connection.queries[2][0]
    assert all("MAX_EXECUTION_TIME(8000)" in sql for sql, _params in connection.queries)


def test_empty_scoped_directory_never_reads_unowned_legacy_developers():
    connection = Connection()
    add_legacy_directory(connection)
    assert read(connection, token_ids=[7]) == []
    assert len(connection.queries) == 1
    assert "zying_product" not in connection.queries[0][0]


def test_missing_legacy_table_returns_an_empty_unrestricted_directory():
    connection = Connection()
    assert read(connection) == []
    assert len(connection.queries) == 2
    assert "information_schema.TABLES" in connection.queries[1][0]
    assert connection.closed


def test_name_only_directory_option_filters_historical_products_with_a_long_name():
    connection = Connection()
    developer_name = "历史产品开发" * 12
    connection.products([
        (1, "A", None, "zying", None, developer_name),
        (2, "B", None, "zying", "17", "另一开发"),
    ])
    option = next(row for row in read(connection) if row["id"] == developer_name)
    result = store.list_product_items(source_type="zying", product_developer_id=option["id"],
                                      connection_factory=lambda: connection)
    assert result["total"] == 1
    assert [row["id"] for row in result["rows"]] == [1]


@pytest.mark.parametrize("saved_id,saved_name,expected_value", [
    (" 17 ", " 开发甲 ", "17"),
    (None, " 历史开发 ", "历史开发"),
])
def test_normalized_directory_option_matches_whitespace_in_historical_fields(saved_id, saved_name, expected_value):
    connection = Connection()
    connection.products([(1, "A", None, "zying", saved_id, saved_name)])
    option = read(connection)[0]
    assert option["id"] == expected_value
    result = store.list_product_items(source_type="zying", product_developer_id=option["id"],
                                      connection_factory=lambda: connection)
    assert result["total"] == 1
    assert [row["id"] for row in result["rows"]] == [1]


@pytest.mark.parametrize("tokens,expected", [(None, {}), ([], {"token_ids": [""]}), ([9, 7, 9], {"token_ids": [7, 9]})])
def test_directory_transport_preserves_scope_and_uses_one_bounded_attempt(monkeypatch, tokens, expected):
    monkeypatch.setattr(api, "DB_MODE", "api")
    get = Mock(return_value={"rows": []})
    monkeypatch.setattr(api, "_request", get)
    assert api.list_mercado_product_developers(token_ids=tokens) == {"rows": []}
    get.assert_called_once_with("GET", "/api/db/mercado-products/developers",
                                params=expected, timeout=(3, 25), max_attempts=1)


def test_local_directory_transport_preserves_empty_scope(monkeypatch):
    monkeypatch.setattr(api, "DB_MODE", "mysql")
    call = Mock(return_value={"rows": []})
    monkeypatch.setattr(api, "_collection_store_call", call)
    api.list_mercado_product_developers(token_ids=[])
    call.assert_called_once_with("list_product_developers", token_ids=[])


@pytest.fixture
def routes():
    path = Path(__file__).parents[1] / "bit" / "bit_interface.py"
    source = ast.parse(path.read_text())
    functions = [node for node in source.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"api_mercado_product_developers", "api_db_mercado_product_developers"}]
    for node in functions:
        node.decorator_list = []
    authorized = Mock(return_value={7, 9})
    user = Mock(return_value={"id": 22, "is_platform_admin": False})
    listing = Mock(return_value={"rows": [{"id": "17", "name": "开发甲"}]})
    namespace = {"request": request, "jsonify": jsonify, "logging": logging,
                 "get_current_workbench_user": user,
                 "_authorized_token_ids_for_user": authorized,
                 "db_list_mercado_product_developers": listing,
                 "reject_db_api_client_mode": lambda: None}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return SimpleNamespace(app=Flask(__name__), namespace=namespace, authorized=authorized, listing=listing, user=user)


@pytest.mark.parametrize("tokens,expected", [(None, {}), (set(), {"token_ids": []}), ({9, 7}, {"token_ids": [7, 9]})])
def test_public_directory_uses_current_users_product_scope(routes, tokens, expected):
    routes.authorized.return_value = tokens
    with routes.app.test_request_context("/api/mercado-products/developers"):
        response = routes.namespace["api_mercado_product_developers"]()
    assert response.get_json()["data"]["rows"] == [{"id": "17", "name": "开发甲"}]
    assert response.headers["Cache-Control"] == "no-store"
    routes.listing.assert_called_once_with(**expected)
    routes.authorized.assert_called_once_with()


@pytest.mark.parametrize("organization_query", ["", "?organization_key=", "?organization_key=%20"])
def test_platform_admin_all_company_directory_can_read_the_legacy_directory(routes, organization_query):
    routes.user.return_value = {"id": 1, "is_platform_admin": True}
    with routes.app.test_request_context("/api/mercado-products/developers" + organization_query):
        response = routes.namespace["api_mercado_product_developers"]()
    assert response.status_code == 200
    routes.listing.assert_called_once_with()
    routes.authorized.assert_not_called()


@pytest.mark.parametrize("is_platform_admin,organization_query", [
    (True, "?organization_key=wuhan-zeshun"),
    (False, ""), (False, "?organization_key=wuhan-zeshun"),
])
def test_selected_company_and_ordinary_user_directory_keep_the_original_product_scope(routes, is_platform_admin, organization_query):
    routes.user.return_value = {"id": 22, "is_platform_admin": is_platform_admin}
    with routes.app.test_request_context("/api/mercado-products/developers" + organization_query):
        response = routes.namespace["api_mercado_product_developers"]()
    assert response.status_code == 200
    routes.listing.assert_called_once_with(token_ids=[7, 9])
    routes.authorized.assert_called_once_with()


@pytest.mark.parametrize("query,expected", [("", None), ("?token_ids=", []), ("?token_ids=7&token_ids=9&token_ids=bad", [7, 9])])
def test_internal_directory_preserves_explicit_empty_scope(routes, query, expected):
    with routes.app.test_request_context("/api/db/mercado-products/developers" + query):
        response = routes.namespace["api_db_mercado_product_developers"]()
    assert response.status_code == 200
    routes.listing.assert_called_once_with(token_ids=expected)
    routes.authorized.assert_not_called()


def test_public_directory_returns_error_instead_of_a_successful_empty_list(routes):
    routes.listing.side_effect = RuntimeError("database unavailable")
    with routes.app.test_request_context("/api/mercado-products/developers"):
        response, status = routes.namespace["api_mercado_product_developers"]()
    assert status == 500
    assert response.get_json()["status"] == "error"


def test_new_directory_routes_retain_the_existing_authentication_decorators():
    path = Path(__file__).parents[1] / "bit" / "bit_interface.py"
    source = ast.parse(path.read_text())
    decorators = {
        node.name: [decorator.id for decorator in node.decorator_list if isinstance(decorator, ast.Name)]
        for node in source.body if isinstance(node, ast.FunctionDef)
    }
    assert decorators["api_mercado_product_developers"] == ["login_required"]
    assert decorators["api_db_mercado_product_developers"] == ["internal_api_required"]
