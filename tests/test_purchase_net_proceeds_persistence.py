"""Offline persistence checks for authoritative purchase-cost targets.

Execute the actual store statements with an in-memory database. The small
dialect adapter substitutes binding/upsert syntax; source ownership and its
conditions are evaluated by SQL, rather than by a mock of the implementation.
"""
import json
import re
import sqlite3
from decimal import Decimal

import pytest

from erp import mercadolibre_collection_store as store


TARGET = 21.375
SAVED_AT = "2026-10-07 12:00:00"
PURCHASE_METADATA = {"source": "purchase_cost_ratio", "ratio": 150,
                     "net_proceeds_usd": "21.3750", "exchange_rate": {"cny_per_usd": "7"}}


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.db.cursor()
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        query = str(query)
        self.connection.queries.append((query, params))
        if self.connection.before_execute:
            self.connection.before_execute(query)
        query = query.replace("%s", "?").replace("%%", "%").replace("<=>", "IS")
        query = query.replace(" FOR UPDATE", "")
        if query.startswith("SHOW TABLES LIKE"):
            query = "SELECT 1 WHERE 0"
        if "ON DUPLICATE KEY UPDATE" in query:
            query = query.replace("ON DUPLICATE KEY UPDATE", "ON CONFLICT(source_item_id) DO UPDATE SET")
            query = re.sub(r"VALUES\(`(\w+)`\)", r"excluded.`\1`", query)
        joined = re.search(
            r"UPDATE (`\w+`) AS (\w+)\s+INNER JOIN (`\w+`) AS (\w+)\s+ON (.*?)\s+SET (.*?)\s+WHERE (.*)",
            query, re.S,
        )
        if joined:
            table, alias, other_table, other_alias, on, assignments, where = joined.groups()
            assignments = re.sub(rf"{alias}\.(?=`\w+` = )", "", assignments)
            query = f"UPDATE {table} AS {alias} SET {assignments} FROM {other_table} AS {other_alias} WHERE {on} AND {where}"
        values = tuple(float(value) if isinstance(value, Decimal) else value for value in (params or ()))
        self.cursor.execute(query, values)
        self.rowcount = self.cursor.rowcount
        self.lastrowid = self.cursor.lastrowid

    def executemany(self, query, rows):
        for row in rows:
            self.execute(query, row)

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None


class _Connection:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.create_function("IF", 3, lambda condition, yes, no: yes if condition else no)
        self.db.create_function("JSON_UNQUOTE", 1, lambda value: value)
        self.db.create_function("LEFT", 2, lambda value, count: value[:count])
        self.queries = []
        self.before_execute = None
        fields = set(store.PROFITABILITY_COLUMNS) | {
            "source_type", "collection_item_id", "task_id", "updated_at", "title", "source_url",
            "main_image_url", "description_text", "price", "currency_id", "weight_g", "weight_basis",
            "package_length_cm", "package_width_cm", "package_height_cm", "volumetric_weight_kg",
            "source_snapshot_json", "source_json", "description_json", "page_snapshot_json",
            "plugin_snapshot_json", "review_status", "added_at", "product_developer_id",
            "product_developer_name", "management_category_id", "ai_weight_price_json",
            "added_to_products", "organization_key", "owner_user_id", "owner_name", "visibility",
        }
        fields.update(column for column, _definition in store.INFRINGEMENT_RESULT_COLUMN_DEFINITIONS)
        definitions = ", ".join(f"`{field}`" for field in sorted(fields))
        for table in (store.PRODUCT_TABLE, store.COLLECTION_TABLE):
            self.db.execute(f"CREATE TABLE `{table}` (`id` INTEGER PRIMARY KEY, `source_item_id` UNIQUE, {definitions})")
        self.db.execute("CREATE TABLE mercado_store_site_settings (token_id, site_id, salesperson)")
        self.db.execute("CREATE TABLE erp_mercadolibre_product_sku_members (product_id INTEGER PRIMARY KEY,parent_id,original_snapshot_json)")
        self.db.execute("CREATE TABLE erp_mercadolibre_product_sku_groups (product_id INTEGER PRIMARY KEY,group_key,group_title,sku_label)")
        self.db.execute("CREATE TABLE erp_mercadolibre_store_links (id INTEGER, item_id, price, weight_g, net_proceeds_usd)")

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        # Keep the in-memory result available for assertions after store cleanup.
        pass

    def insert(self, table, **values):
        fields = ", ".join(f"`{field}`" for field in values)
        placeholders = ", ".join("?" for _ in values)
        self.db.execute(f"INSERT INTO `{table}` ({fields}) VALUES ({placeholders})", tuple(values.values()))

    def row(self, table, row_id=1):
        return dict(self.db.execute(f"SELECT * FROM `{table}` WHERE id = ?", (row_id,)).fetchone())


@pytest.fixture
def database(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    yield connection
    connection.db.close()


def _row(*, row_id=1, item_id="MLM1", source_type="collected", purchased=True):
    snapshot = {"source": {"title": "Saved source"}, "purchase_net_proceeds": PURCHASE_METADATA,
                "zying_net_proceeds_usd": TARGET,
                "source_1688_overrides": {"net_proceeds_usd": TARGET, "price": 14},
                "ai_original": {"status": "pending"}}
    return dict(id=row_id, source_item_id=item_id, source_type=source_type, collection_item_id=1, organization_key="default",
                title="Product", price=100, currency_id="USD", weight_g=500, weight_basis="plugin_actual",
                category_id="MLM1", source_url="https://example.test/1", updated_at=SAVED_AT,
                profitability_updated_at=SAVED_AT, profitability_source="purchase_cost_ratio" if purchased else "official",
                net_proceeds_usd=TARGET if purchased else 10, shipping_fee_usd=4, commission_amount_usd=15,
                source_snapshot_json=json.dumps(snapshot), source_json="{}", description_json="{}",
                page_snapshot_json="{}", description_text="Saved description")


def _assert_purchase(row):
    assert row["net_proceeds_usd"] == TARGET
    assert row["profitability_source"] == "purchase_cost_ratio"
    assert row["profitability_updated_at"] == SAVED_AT
    assert json.loads(row["source_snapshot_json"])["purchase_net_proceeds"] == PURCHASE_METADATA


@pytest.mark.parametrize("pending", [True, False])
def test_purchase_target_is_excluded_from_pending_and_stale_queue(database, pending):
    for table in (store.PRODUCT_TABLE, store.COLLECTION_TABLE):
        saved = _row()
        saved["profitability_updated_at"] = None if pending else SAVED_AT
        database.insert(table, **saved)
        database.insert(table, **{**_row(row_id=2, item_id="MLM2", purchased=False),
                                  "profitability_updated_at": None if pending else SAVED_AT})
    rows = store.list_stale_profitability_items(stale_before="2026-10-08 00:00:00", limit=10,
                                               connection_factory=lambda: database)
    assert [row["id"] for row in rows] == [2, 2]


@pytest.mark.parametrize("source_type", ["collected", "pulled"])
@pytest.mark.parametrize("with_expected_inputs", [True, False])
def test_inflight_product_refresh_cannot_replace_purchase_target(database, source_type, with_expected_inputs):
    saved = _row(source_type=source_type)
    database.insert(store.PRODUCT_TABLE, **saved)
    stale = {"id": 1, "source_type": source_type, "net_proceeds_usd": 2,
             "profitability_source": "official", "shipping_fee_usd": None}
    if with_expected_inputs:
        stale["_expected_profitability_inputs"] = dict(saved)
    assert store.update_item_profitability("MLM1", stale, connection_factory=lambda: database) is False
    row = database.row(store.PRODUCT_TABLE)
    _assert_purchase(row)
    assert row["shipping_fee_usd"] == 4


def test_collection_refresh_updates_capture_without_overwriting_purchase_product(database):
    database.insert(store.PRODUCT_TABLE, **_row())
    database.insert(store.COLLECTION_TABLE, **_row(purchased=False))
    assert store.update_item_profitability("MLM1", {"id": 1, "task_id": 8,
        "net_proceeds_usd": 3, "profitability_source": "official"}, connection_factory=lambda: database)
    assert database.row(store.COLLECTION_TABLE)["net_proceeds_usd"] == 3
    _assert_purchase(database.row(store.PRODUCT_TABLE))


@pytest.mark.parametrize("source_type", ["collected", "zying", "ai_original"])
def test_generic_refresh_respects_persisted_owner_and_updates_ordinary_rows(database, source_type):
    database.insert(store.PRODUCT_TABLE, **_row(source_type=source_type))
    database.insert(store.COLLECTION_TABLE, **_row(purchased=False))
    assert store.update_item_profitability("MLM1", {"net_proceeds_usd": 3,
        "profitability_source": "official"}, connection_factory=lambda: database)
    _assert_purchase(database.row(store.PRODUCT_TABLE))
    assert database.row(store.COLLECTION_TABLE)["net_proceeds_usd"] == 3


@pytest.mark.parametrize("source_type", ["zying", "ai_original"])
def test_generic_refresh_does_not_overwrite_source_owned_rows_without_incoming_type(database, source_type):
    database.insert(store.PRODUCT_TABLE, **_row(source_type=source_type, purchased=False))
    assert store.update_item_profitability("MLM1", {"net_proceeds_usd": 3,
        "profitability_source": "official"}, connection_factory=lambda: database) is False
    assert database.row(store.PRODUCT_TABLE)["net_proceeds_usd"] == 10


def test_daily_invalidation_preserves_purchase_but_invalidates_estimated_costs(database):
    for table in (store.PRODUCT_TABLE, store.COLLECTION_TABLE):
        database.insert(table, **_row())
        database.insert(table, **_row(row_id=2, item_id="MLM2", purchased=False))
    assert store.mark_all_profitability_stale(connection_factory=lambda: database) == 2
    for table in (store.PRODUCT_TABLE, store.COLLECTION_TABLE):
        _assert_purchase(database.row(table))
        estimated = database.row(table, 2)
        assert estimated["net_proceeds_usd"] is None
        assert estimated["shipping_fee_usd"] is None
        assert estimated["commission_amount_usd"] == 15


def test_purchase_saved_between_invalidation_selection_and_write_is_preserved(database):
    database.insert(store.PRODUCT_TABLE, **_row(purchased=False))
    def save_during_invalidation(query):
        if query.lstrip().startswith(f"UPDATE `{store.PRODUCT_TABLE}`"):
            database.db.execute(f"UPDATE `{store.PRODUCT_TABLE}` SET profitability_source = ?, net_proceeds_usd = ?",
                                ("purchase_cost_ratio", TARGET))
    database.before_execute = save_during_invalidation
    assert store.mark_all_profitability_stale(connection_factory=lambda: database) == 0
    _assert_purchase(database.row(store.PRODUCT_TABLE))


@pytest.mark.parametrize("purchased", [True, False])
def test_zying_recollection_refreshes_source_without_erasing_purchase_target(database, purchased):
    database.insert(store.PRODUCT_TABLE, **_row(item_id="ZY1", source_type="zying", purchased=purchased))
    store.upsert_zying_products_to_products([{"product_id": "ZY1", "title": "Fresh source",
        "sale_price": 200, "net_income": 9, "listing_snapshot": {"title": "Fresh source", "new_fact": True}}],
        connection_factory=lambda: database)
    row = database.row(store.PRODUCT_TABLE)
    assert row["title"] == "Fresh source"
    snapshot = json.loads(row["source_snapshot_json"])
    assert snapshot["new_fact"] is True
    if purchased:
        _assert_purchase(row)
        assert snapshot["zying_net_proceeds_usd"] == TARGET
    else:
        assert row["net_proceeds_usd"] == 9
        assert row["profitability_source"] == "zying_collection"


def test_pulled_listing_import_keeps_purchase_value_and_metadata(database):
    database.insert(store.PRODUCT_TABLE, **_row(source_type="pulled"))
    store.upsert_pulled_store_links_to_products({"id": 11, "store_name": "Store"}, [
        {"id": "MLM1", "title": "Fresh listing", "price": 200, "currency_id": "USD",
         "permalink": "https://example.test/1", "category_id": "MLM1",
         "pictures": [{"secure_url": "https://example.test/new.jpg"}],
         "net_proceeds": {"amount": 9, "currency_id": "USD"}}
    ], connection_factory=lambda: database)
    row = database.row(store.PRODUCT_TABLE)
    _assert_purchase(row)
    assert row["title"] == "Fresh listing"
    assert json.loads(row["source_snapshot_json"])["source"]["price"] == 200


def test_ai_recollection_and_inflight_worker_keep_purchase_metadata_and_override(database):
    database.insert(store.PRODUCT_TABLE, **_row(item_id="1688:907173727091", source_type="ai_original"))
    store.upsert_ai_original_product({"source_url": "https://detail.1688.com/offer/907173727091.html",
        "title": "Fresh 1688", "price": 15, "images": ["https://example.test/new.jpg"]},
        connection_factory=lambda: database)
    row = database.row(store.PRODUCT_TABLE)
    _assert_purchase(row)
    assert json.loads(row["source_snapshot_json"])["source_1688_overrides"]["net_proceeds_usd"] == TARGET
    store.update_ai_original_product(1, {"source_snapshot_json": {"ai_original": {"status": "completed"}}},
                                     connection_factory=lambda: database)
    _assert_purchase(database.row(store.PRODUCT_TABLE))


@pytest.mark.parametrize("fields", [["price"], ["weight_g"], ["net_proceeds_usd"]])
def test_remote_listing_field_sync_keeps_purchase_target(database, fields):
    database.insert(store.PRODUCT_TABLE, **_row(source_type="pulled"))
    database.insert("erp_mercadolibre_store_links", id=11, item_id="MLM1", price=200, weight_g=800, net_proceeds_usd=9)
    store.sync_pulled_product_fields_from_store_links([11], fields, connection_factory=lambda: database)
    _assert_purchase(database.row(store.PRODUCT_TABLE))
    if fields == ["price"]:
        assert database.row(store.PRODUCT_TABLE)["price"] == 200
    if fields == ["weight_g"]:
        assert database.row(store.PRODUCT_TABLE)["weight_g"] == 800


@pytest.mark.parametrize("purchased", [True, False])
def test_content_input_edits_preserve_purchase_target_and_invalidate_estimate(database, purchased):
    database.insert(store.PRODUCT_TABLE, **_row(purchased=purchased))
    assignments, values, stale = store._product_content_update_plan({"price": Decimal(200)}, preserve_zying_net_proceeds=True)
    assert stale
    with database.cursor() as cursor:
        cursor.execute(f"UPDATE `{store.PRODUCT_TABLE}` SET {', '.join(assignments)} WHERE id = 1", values)
    row = database.row(store.PRODUCT_TABLE)
    assert row["price"] == 200
    assert row["shipping_fee_usd"] is None
    if purchased:
        _assert_purchase(row)
    else:
        assert row["net_proceeds_usd"] is None
        assert row["profitability_updated_at"] is None
        assert row["profitability_source"] == "manual_edit_pending"


def test_collection_measurement_edit_keeps_matching_product_purchase_target(database):
    database.insert(store.PRODUCT_TABLE, **_row())
    database.insert(store.COLLECTION_TABLE, **_row(purchased=False))
    store.update_collection_items([1], {"weight_g": 800}, connection_factory=lambda: database)
    _assert_purchase(database.row(store.PRODUCT_TABLE))
    assert database.row(store.PRODUCT_TABLE)["weight_g"] == 800
    assert database.row(store.COLLECTION_TABLE)["net_proceeds_usd"] is None


def test_readding_collection_capture_keeps_product_purchase_and_refreshes_other_source_data(database):
    database.insert(store.PRODUCT_TABLE, **_row())
    database.insert(store.COLLECTION_TABLE, **{**_row(purchased=False), "title": "New capture"})
    store._add_collection_items_to_products_once([1], connection_factory=lambda: database)
    row = database.row(store.PRODUCT_TABLE)
    _assert_purchase(row)
    assert row["title"] == "New capture"
    assert json.loads(row["source_snapshot_json"])["source"] == {}
