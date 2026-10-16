"""Source edits must survive listing generation, re-collection and clearing."""
import ast
from copy import deepcopy
from decimal import Decimal
import json
import logging
from pathlib import Path
import re

from flask import Flask, jsonify, request
import pytest

from erp import mercadolibre_collection_store as store


class ProductCursor:
    def __init__(self, row):
        self.row = deepcopy(row)
        self.queries = []
        self.rowcount = 1
        self.fail_update = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        query = " ".join(query.split())
        self.queries.append((query, params))
        if query.startswith("UPDATE"):
            if self.fail_update:
                raise RuntimeError("write failed")
            for key, value in zip(re.findall(r"`([^`]+)` = %s", query), params):
                self.row[key] = value
        elif query.startswith("INSERT"):
            completed = json.loads(self.row.get("source_snapshot_json") or "{}").get("ai_original", {}).get("status") == "completed"
            if not completed:
                self.row["source_snapshot_json"] = params[11]
            if self.row.get("currency_id") == "CNY":
                self.row["price"] = params[5]
            for field, value in zip(("weight_g", "package_length_cm", "package_width_cm", "package_height_cm"), params[6:10]):
                self.row[field] = value

    def fetchone(self):
        query = self.queries[-1][0]
        if "COUNT(*)" in query:
            return {"total": 0}
        if "'ai_original'" in query and self.row and self.row.get("source_type") != "ai_original":
            return None
        return deepcopy(self.row)

    def fetchall(self):
        return []


class ProductConnection:
    def __init__(self, row):
        self.cursor_object = ProductCursor(row)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_object

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.fixture
def row():
    return {
        "id": 7, "source_type": "ai_original", "source_item_id": "1688_907173727091",
        "currency_id": "CNY", "price": Decimal("12"), "net_proceeds_usd": Decimal("99"),
        "weight_g": Decimal("250"), "package_length_cm": Decimal("20"),
        "package_width_cm": Decimal("10"), "package_height_cm": Decimal("5"),
        "source_snapshot_json": json.dumps({
            "original_1688": {
                "title": "原商品", "price": 12, "weight_g": 250,
                "package_length_cm": 20, "package_width_cm": 10, "package_height_cm": 5,
                "variations": [{"sku_id": "sku-1", "price": 100, "available_quantity": 8}],
            },
            "ai_original": {"status": "completed", "title_es": "Caja", "generated_by": "李四"},
            "source": {"id": "CBT907173727091", "variations": [{"seller_sku": "CUSTOM"}]},
            "plugin_snapshot": {"created_by": "浏览器插件：张三"},
        }, ensure_ascii=False),
    }


@pytest.fixture(autouse=True)
def skip_schema(monkeypatch):
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)


@pytest.mark.parametrize("changes", [
    {}, {"title": "unrelated"}, {"weight_g": "250 g"}, {"price": True},
    {"price": "NaN"}, {"weight_g": "Infinity"}, {"net_proceeds_usd": -1},
    {"package_length_cm": 0}, {"price": "1e10000"}, {"price": "1e-10000"},
    {"price": "9999999999999999.9999"},
])
def test_invalid_source_edit_rejected_before_opening_connection(changes):
    with pytest.raises(ValueError):
        store.update_1688_product(7, changes, connection_factory=lambda: pytest.fail("connected"))


def test_edit_preserves_collected_ai_skus_and_updates_publish_inputs(row):
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["marketplace_listings"] = {
        "amazon": {"status": "completed", "review_status": "approved"},
        "yandex": {"status": "completed", "review_status": "approved"},
    }
    row["source_snapshot_json"] = json.dumps(snapshot)
    connection = ProductConnection(row)
    saved = store.update_1688_product(7, {
        "weight_g": "320", "package_length_cm": "22", "package_width_cm": "12",
        "package_height_cm": "7", "net_proceeds_usd": "15", "price": "30",
    }, connection_factory=lambda: connection)
    before = json.loads(row["source_snapshot_json"])
    after = json.loads(saved["source_snapshot_json"])
    persisted = json.loads(connection.cursor_object.row["source_snapshot_json"])
    for field in ("original_1688", "ai_original", "source", "plugin_snapshot"):
        assert persisted[field] == before[field]
        for key, value in before[field].items():
            assert after[field][key] == value
    for draft in (after["ai_original"], after["source"]):
        assert draft["weight_g"] == 320
        assert draft["package_length_cm"] == 22
        assert draft["net_proceeds_usd"] == 15
    assert saved["source_purchase_price_cny"] == saved["price"] == 30
    assert saved["source_weight_g"] == saved["weight_g"] == 320
    assert saved["source_package_length_cm"] == saved["package_length_cm"] == 22
    assert saved["net_proceeds_usd"] == saved["source_net_proceeds_usd"] == 15
    assert saved["source_variation_max_price_cny"] == 100
    assert saved["source_net_calculation_price_cny"] == 30
    assert saved["suggested_net_proceeds_usd"] == 6
    assert saved["suggested_net_price_basis"] == "manual_purchase_price"
    assert {draft["review_status"] for draft in after["marketplace_listings"].values()} == {"unreviewed"}
    assert "FOR UPDATE" in connection.cursor_object.queries[0][0]
    update_sql = connection.cursor_object.queries[1][0]
    assert "`source_type` = 'ai_original'" in update_sql
    assert "`volumetric_weight_kg` = CASE" in update_sql
    assert connection.committed and connection.closed and not connection.rolled_back


def test_clearing_is_explicit_and_does_not_fall_back_to_collected_values(row):
    connection = ProductConnection(row)
    saved = store.update_1688_product(7, {
        "weight_g": " ", "package_length_cm": None, "price": None, "net_proceeds_usd": "",
    }, connection_factory=lambda: connection)
    assert saved["original_1688"]["weight_g"] == 250
    assert saved["original_1688"]["price"] == 12
    for field in ("weight_g", "price", "net_proceeds_usd", "package_length_cm"):
        assert saved["source_1688_overrides"][field] is None
        assert saved[field] is None
    assert saved["source_weight_g"] is None
    assert saved["source_purchase_price_cny"] is None
    assert saved["source_net_proceeds_usd"] is None
    assert saved["suggested_net_proceeds_usd"] is None


def test_purchase_override_keeps_usd_listing_currency_and_price(row):
    row.update(currency_id="USD", price=Decimal("45"))
    connection = ProductConnection(row)
    saved = store.update_1688_product(7, {"price": "18.55"}, connection_factory=lambda: connection)
    assert saved["currency_id"] == "USD"
    assert saved["price"] == 45
    assert saved["source_purchase_price_cny"] == 18.55
    assert saved["source_net_calculation_price_cny"] == 18.55
    assert saved["suggested_net_proceeds_usd"] == 4
    assert "`price` = %s" not in connection.cursor_object.queries[1][0]
    with pytest.raises(ValueError):
        store.update_1688_product(7, {"price": "1e10000"}, connection_factory=lambda: pytest.fail("connected"))


@pytest.mark.parametrize("failure", ["missing", "foreign_source", "write"])
def test_source_edit_rolls_back_missing_foreign_and_failed_writes(row, failure):
    if failure == "missing":
        row = None
    elif failure == "foreign_source":
        row["source_type"] = "zying"
    connection = ProductConnection(row)
    connection.cursor_object.fail_update = failure == "write"
    with pytest.raises(RuntimeError if failure == "write" else KeyError):
        store.update_1688_product(7, {"weight_g": 350}, connection_factory=lambda: connection)
    assert connection.rolled_back and connection.closed and not connection.committed


def test_stale_ai_snapshot_save_keeps_latest_source_overrides(row):
    stale = json.loads(row["source_snapshot_json"])
    current = deepcopy(stale)
    current["source_1688_overrides"] = {"weight_g": 320, "price": 30, "net_proceeds_usd": None}
    row["source_snapshot_json"] = json.dumps(current)
    stale["ai_original"].update(status="completed", title_es="Caja nueva")
    stale["source_1688_overrides"] = {"weight_g": 1}
    connection = ProductConnection(row)
    saved = store.update_ai_original_product(7, {
        "source_snapshot_json": stale, "title": "Caja nueva", "currency_id": "USD",
    }, connection_factory=lambda: connection)
    assert saved["source_1688_overrides"] == current["source_1688_overrides"]
    assert saved["title_es"] == "Caja nueva"
    assert saved["source_net_proceeds_usd"] is None
    assert "FOR UPDATE" in connection.cursor_object.queries[0][0]


@pytest.mark.parametrize("status", ["pending", "completed"])
def test_recollection_keeps_explicit_source_edits_and_clears_other_raw_weights(row, status):
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["ai_original"]["status"] = status
    snapshot["marketplace_listings"] = {
        "amazon": {"status": "completed", "review_status": "approved"},
        "yandex": {"status": "completed", "review_status": "approved"},
    }
    snapshot["source_1688_overrides"] = {"price": 30, "weight_g": 320, "package_length_cm": None, "net_proceeds_usd": 15}
    row["source_snapshot_json"] = json.dumps(snapshot)
    connection = ProductConnection(row)
    saved = store.upsert_ai_original_product({
        "source_url": "https://detail.1688.com/offer/907173727091.html",
        "title": "重新采集商品", "price": 12, "weight_g": None,
        "images": ["https://cbu01.alicdn.com/product.jpg"],
    }, connection_factory=lambda: connection)
    assert saved["source_1688_overrides"] == snapshot["source_1688_overrides"]
    assert saved["weight_g"] == saved["source_weight_g"] == 320
    assert saved["price"] == saved["source_purchase_price_cny"] == 30
    assert saved["package_length_cm"] is None
    assert saved["package_width_cm"] is None
    assert saved["net_proceeds_usd"] == 15
    assert {saved["marketplace_listings"][name]["review_status"] for name in ("amazon", "yandex")} == {"unreviewed"}
    assert connection.committed


def test_collector_options_load_all_names_without_page_limits(row):
    connection = ProductConnection(row)
    connection.cursor_object.fetchall = lambda: [
        {"collector_salesperson": "张三"}, {"collector_salesperson": "李四"},
        {"collector_salesperson": "张三"}, {"collector_salesperson": ""},
    ]
    assert store.get_1688_product_options(connection_factory=lambda: connection) == {
        "collector_salespeople": ["张三", "李四"],
    }
    query = connection.cursor_object.queries[0][0]
    assert "SELECT DISTINCT" in query and "'ai_original'" in query
    assert "$.plugin_snapshot.created_by" in query and "浏览器插件：" in query
    assert "LIMIT" not in query
    assert connection.closed


def test_source_purchase_price_and_exact_collector_filters_apply_before_pagination(row):
    connection = ProductConnection(row)
    store.list_product_items(
        source_type="ai_original", collector_salesperson="张三", collector_salesperson_exact=True,
        management_category_id="uncategorized", price_min="18", price_max="31",
        limit=50, offset=50, connection_factory=lambda: connection,
    )
    for query, params in connection.cursor_object.queries:
        assert "$.source_1688_overrides.price" in query
        assert "JSON_CONTAINS_PATH" in query
        assert "`price` > 0" in query
        assert "`management_category_id` IS NULL" in query
        assert "张三" in params and "%张三%" not in params
    assert connection.cursor_object.queries[-1][1][-2:] == (50, 50)


def load_route(name, **dependencies):
    source = Path("bit/bit_interface.py").read_text(encoding="utf-8")
    node = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    namespace = {
        "request": request, "jsonify": jsonify, "logging": logging,
        "_parse_int_param": lambda args, key, default, low, high: int(args.get(key, default)),
        **dependencies,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<1688-route>", "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("net", [15, None])
def test_list_route_forwards_shared_category_and_preserves_explicit_source_net(net):
    calls = []
    def list_rows(**kwargs):
        calls.append(kwargs)
        return {"total": 1, "rows": [{"net_proceeds_usd": 99, "suggested_net_proceeds_usd": 3,
                                      "source_1688_overrides": {"net_proceeds_usd": net}}]}
    route = load_route("api_1688_products", db_list_mercado_product_items=list_rows)
    with Flask(__name__).test_request_context("/?collector_salesperson=张三&management_category_id=3"):
        response = route()
        assert response.get_json()["data"]["rows"][0]["net_proceeds_usd"] == net
    assert calls[0]["source_type"] == "ai_original"
    assert calls[0]["management_category_id"] == "3"
    assert calls[0]["collector_salesperson_exact"] is True


@pytest.mark.parametrize("name", ["api_update_1688_product", "api_db_update_1688_product"])
@pytest.mark.parametrize("payload,status", [([], 422), ({}, 400), ({"price": 0}, 400), ({"title": "x"}, 400)])
def test_edit_routes_reject_invalid_payloads_without_saving(name, payload, status):
    route = load_route(name, db_update_1688_product=lambda *args: pytest.fail("saved"),
                       reject_db_api_client_mode=lambda: None)
    with Flask(__name__).test_request_context("/", method="PATCH", json=payload):
        response, code = route(7)
        assert code == status
        assert response.get_json()["status"] == "error"


@pytest.mark.parametrize("name", ["api_update_1688_product", "api_db_update_1688_product"])
def test_edit_routes_return_missing_row_as_404(name):
    def missing(*args):
        raise KeyError("1688 产品不存在")
    route = load_route(name, db_update_1688_product=missing, reject_db_api_client_mode=lambda: None)
    with Flask(__name__).test_request_context("/", method="PATCH", json={"weight_g": 320}):
        response, status = route(7)
        assert status == 404


def test_patch_route_returns_effective_source_economics():
    changes = []
    def save(row_id, data):
        changes.append((row_id, data))
        return {"id": row_id, "net_proceeds_usd": 99, "suggested_net_proceeds_usd": 3,
                "source_1688_overrides": {"net_proceeds_usd": None}}
    route = load_route("api_update_1688_product", db_update_1688_product=save)
    with Flask(__name__).test_request_context("/", method="PATCH", json={"net_proceeds_usd": None}):
        assert route(7).get_json()["data"]["net_proceeds_usd"] is None
    assert changes == [(7, {"net_proceeds_usd": None})]


@pytest.mark.parametrize("mode", ["api", "mysql"])
def test_source_options_and_patch_use_database_adapter_in_both_modes(monkeypatch, mode):
    from bit import bit_db_api
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", mode)
    monkeypatch.setattr(bit_db_api, "_request", lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {})
    monkeypatch.setattr(bit_db_api, "_collection_store_call", lambda name, *args, **kwargs: calls.append((name, args, kwargs)) or {})
    bit_db_api.get_1688_product_options()
    bit_db_api.update_1688_product(7, {"price": 30})
    if mode == "api":
        assert calls[0][:2] == ("GET", "/api/db/1688-products/options")
        assert calls[1] == ("PATCH", "/api/db/1688-products/7", {"json": {"price": 30}})
    else:
        assert calls == [("get_1688_product_options", (), {}), ("update_1688_product", (7, {"price": 30}), {})]


def test_database_adapter_forwards_exact_collector_filter(monkeypatch):
    from bit import bit_db_api
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(bit_db_api, "_request", lambda method, path, **kwargs: calls.append(kwargs) or {})
    bit_db_api.list_mercado_product_items(source_type="ai_original", collector_salesperson="张三",
                                         collector_salesperson_exact=True, management_category_id="3")
    assert calls[0]["params"]["collector_salesperson_exact"] is True
    assert calls[0]["params"]["management_category_id"] == "3"


def test_database_adapter_preserves_missing_product_error_in_api_mode(monkeypatch):
    from bit import bit_db_api
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    def missing(*args, **kwargs):
        raise RuntimeError("'1688 产品不存在'")
    monkeypatch.setattr(bit_db_api, "_request", missing)
    with pytest.raises(KeyError, match="1688 产品不存在"):
        bit_db_api.update_1688_product(7, {"price": 30})

@pytest.mark.parametrize("category_id", [None, 23])
def test_recollection_assigns_selected_management_category(row, category_id):
    connection = ProductConnection(row)
    store.upsert_ai_original_product({
        "source_url": "https://detail.1688.com/offer/907173727091.html",
        "title": "分类商品", "images": ["https://cbu01.alicdn.com/product.jpg"],
        "management_category_id": category_id,
    }, connection_factory=lambda: connection)
    writes = [(sql, params) for sql, params in connection.cursor_object.queries
              if sql.startswith("UPDATE") and "`management_category_id` = %s" in sql]
    assert len(writes) == (0 if category_id is None else 1)
    if writes:
        assert writes[0][1][0] == category_id
    assert connection.committed


def test_list_omits_duplicate_ai_payloads_but_preserves_supplier_detail():
    original = {"title": "供应商产品", "variations": [{"sku_id": "sku-1"}],
                "properties": [{"name": "材质", "value": "钢"}], "description_text": "商品说明"}
    route = load_route("api_1688_products", db_list_mercado_product_items=lambda **kw: {
        "rows": [{"id": 7, "original_1688": original, "ai_status": "completed",
                  "source_snapshot_json": "duplicate", "ai_original": {"large": "listing"},
                  "marketplace_listings": {"large": "listing"},
                  "source_1688_overrides": {"net_proceeds_usd": None}}], "total": 1})
    with Flask(__name__).test_request_context('/api/1688-products'):
        data = route().get_json()['data']
    row = data['rows'][0]
    assert data['total'] == 1
    assert row['original_1688'] == original
    assert row['ai_status'] == 'completed'
    assert row['net_proceeds_usd'] is None
    assert not {'source_snapshot_json', 'ai_original', 'marketplace_listings'} & row.keys()


def test_bulk_source_edit_keeps_raw_snapshot_and_ai_draft(row):
    before = json.loads(row["source_snapshot_json"])
    before["marketplace_listings"] = {
        "amazon": {"status": "completed", "review_status": "approved"},
    }
    row["source_snapshot_json"] = json.dumps(before)
    connection = ProductConnection(row)
    result = store.update_1688_product_items([7, 7], {"weight_g": 650, "package_width_cm": 14},
                                              connection_factory=lambda: connection)
    snapshot = json.loads(connection.cursor_object.row["source_snapshot_json"])
    original = json.loads(row["source_snapshot_json"])
    assert snapshot["original_1688"] == original["original_1688"]
    assert snapshot["ai_original"] == original["ai_original"]
    assert snapshot["source_1688_overrides"] == {"weight_g": 650, "package_width_cm": 14}
    assert snapshot["marketplace_listings"]["amazon"]["review_status"] == "unreviewed"
    assert connection.cursor_object.row["package_length_cm"] == 20
    assert result == {"requested": 1, "changed": 1, "updated_fields": ["weight_g", "package_width_cm"]}
    assert connection.committed and connection.closed


def test_bulk_source_edit_rolls_back_if_later_product_missing(row):
    connection = ProductConnection(row)
    old_fetch = connection.cursor_object.fetchone
    connection.cursor_object.fetchone = lambda: None if connection.cursor_object.queries[-1][1] == (8,) else old_fetch()
    with pytest.raises(KeyError):
        store.update_1688_product_items([7, 8], {"weight_g": 650}, connection_factory=lambda: connection)
    assert connection.rolled_back and not connection.committed and connection.closed


@pytest.mark.parametrize("changes", [{}, {"price": 25}, {"weight_g": 0}, {"weight_g": "NaN"}])
def test_invalid_bulk_changes_never_open_connection(changes):
    with pytest.raises(ValueError):
        store.update_1688_product_items([7], changes, connection_factory=lambda: pytest.fail("connected"))


@pytest.mark.parametrize("name", ["api_update_1688_product_items", "api_db_update_1688_product_items"])
def test_bulk_source_routes_forward_selection_and_handle_missing(name):
    calls = []
    route = load_route(name, db_update_1688_product_items=lambda ids, changes: calls.append((ids, changes)) or {"changed": 2},
                       reject_db_api_client_mode=lambda: None)
    with Flask(__name__).test_request_context('/', method='PATCH', json={"product_item_ids": [7, 8], "changes": {"weight_g": 600}}):
        assert route().get_json()["data"]["changed"] == 2
    assert calls == [([7, 8], {"weight_g": 600})]
    with Flask(__name__).test_request_context('/', method='PATCH', json={"product_item_ids": "7", "changes": {"weight_g": 600}}):
        assert route()[1] == 422


def test_weight_filter_uses_source_override_and_snapshot_before_listing_weight(row):
    connection = ProductConnection(row)
    store.list_product_items(source_type="ai_original", weight_min="100", weight_max="500",
                             connection_factory=lambda: connection)
    count_sql, params = next((sql, params) for sql, params in connection.cursor_object.queries if "COUNT(*)" in sql)
    assert "source_1688_overrides.weight_g" in count_sql
    assert "original_1688.weight_g" in count_sql
    assert Decimal("100") in params and Decimal("500") in params
    with pytest.raises(ValueError, match="最低重量"):
        store.list_product_items(source_type="ai_original", weight_min="500", weight_max="100",
                                 connection_factory=lambda: pytest.fail("connected"))


def test_source_list_route_passes_weight_range():
    calls = []
    route = load_route("api_1688_products", db_list_mercado_product_items=lambda **kwargs: calls.append(kwargs) or {"rows": []})
    with Flask(__name__).test_request_context('/?weight_min=200&weight_max=800'):
        assert route().get_json()["status"] == "success"
    assert calls[0]["weight_min"] == "200" and calls[0]["weight_max"] == "800"


@pytest.mark.parametrize("mode", ["api", "mysql"])
def test_bulk_database_adapter_preserves_source_edit_payload(monkeypatch, mode):
    from bit import bit_db_api
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", mode)
    monkeypatch.setattr(bit_db_api, "_request", lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {})
    monkeypatch.setattr(bit_db_api, "_collection_store_call", lambda *args: calls.append(args) or {})
    bit_db_api.update_1688_product_items([7, 8], {"weight_g": 450, "package_width_cm": 12})
    if mode == "api":
        assert calls == [("PATCH", "/api/db/1688-products/bulk-dimensions", {"timeout": 120, "json": {
            "product_item_ids": [7, 8], "changes": {"weight_g": 450, "package_width_cm": 12}}})]
    else:
        assert calls == [("update_1688_product_items", [7, 8], {"weight_g": 450, "package_width_cm": 12})]
