import json
import re
from decimal import Decimal
from unittest.mock import patch

import pytest

from erp import mercadolibre_collection_store as store


class _FakeCursor:
    def __init__(self, *, update_rowcount=0):
        self.queries = []
        self.rowcount = 0
        self.update_rowcount = update_rowcount
        self.lastrowid = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(str(query).split())
        self.queries.append((normalized, params))
        self.rowcount = self.update_rowcount if normalized.startswith("UPDATE") else 0
        if normalized.startswith(f"INSERT INTO `{store.PUBLISH_RECORD_TABLE}`"):
            self.lastrowid += 1

    def executemany(self, query, params):
        normalized = " ".join(str(query).split())
        rows = list(params)
        self.queries.append((normalized, rows))
        self.rowcount = len(rows)

    def fetchone(self):
        # Pretend all migration columns already exist.
        return {"Field": "existing"}

    def fetchall(self):
        return []


class _FakeConnection:
    def __init__(self, *, update_rowcount=0):
        self.fake_cursor = _FakeCursor(update_rowcount=update_rowcount)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.fake_cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class _IndexCursor:
    def __init__(self, columns):
        self.columns = list(columns)
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((" ".join(str(query).split()), params))

    def fetchall(self):
        return [
            {"Column_name": column, "Seq_in_index": index}
            for index, column in enumerate(self.columns, start=1)
        ]


def test_create_task_rejects_non_url_before_database_connection():
    with pytest.raises(ValueError, match="有效"):
        store.create_collection_task("not-a-url", 10, connection_factory=lambda: None)


def test_create_task_persists_worker_count_for_task_summary():
    connection = _FakeConnection()

    store.create_collection_task(
        "https://listado.mercadolibre.com.mx/cardgame",
        200,
        "tester",
        worker_count=10,
        connection_factory=lambda: connection,
    )

    insert_sql, params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"INSERT INTO `{store.TASK_TABLE}`")
    )
    assert "`worker_count`" in insert_sql
    assert params[1:3] == (200, 10)


def test_add_products_rejects_invalid_or_empty_ids_before_database_connection():
    with pytest.raises(ValueError, match="至少勾选"):
        store.add_collection_items_to_products([], connection_factory=lambda: None)
    with pytest.raises(ValueError, match="编号无效"):
        store.add_collection_items_to_products(["bad"], connection_factory=lambda: None)


def test_zying_detail_snapshot_is_upserted_as_third_product_source():
    connection = _FakeConnection()
    snapshot = {
        "source_url": "https://meli.zying.net/#/product",
        "main_image_url": "https://example.test/image.jpg",
        "title": "Zying product",
        "price": 36.55,
        "currency_id": "USD",
        "category_id": "CBT430974",
        "weight_g": 1000,
        "package_length_cm": 23,
        "package_width_cm": 22,
        "package_height_cm": 13,
        "source": {
            "id": "CBT795184904",
            "title": "Zying product",
            "category_id": "CBT430974",
            "attributes": [{"id": "BRAND", "value_name": "Generic"}],
            "pictures": [{"source": "https://example.test/image.jpg"}],
        },
        "description": {"plain_text": "description"},
    }

    result = store.upsert_zying_products_to_products(
        [
            {
                "product_id": "795184904",
                "title": "Zying product",
                "sale_price": "USD 36.55",
                "net_income": "USD 22",
                "product_category_id": "CBT430974",
                "product_category": "Home / Test",
                "product_developer_id": "121658",
                "product_developer_name": "张三",
                "listing_snapshot": snapshot,
            }
        ],
        connection_factory=lambda: connection,
    )

    upsert_sql, rows = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"INSERT INTO `{store.PRODUCT_TABLE}`")
    )
    row = rows[0]
    assert result == {"count": 1, "skipped": 0}
    assert row[1] == "zying"
    assert row[3] == "795184904"
    assert row[10] == Decimal("1000")
    assert row[19] == Decimal("22")
    stored_snapshot = json.loads(row[22])
    assert stored_snapshot["source"]["attributes"][0]["id"] == "BRAND"
    assert stored_snapshot["zying_net_proceeds_usd"] == "22"
    assert "IF(`source_type` = 'zying'" in upsert_sql
    assert "zying" in store.PRODUCT_SOURCE_TYPES
    assert row[24:26] == ("121658", "张三")
    assert row[-4:] == ("wuhan-zeshun", None, "", "enterprise")
    assert "`product_developer_name`" in upsert_sql


def test_add_products_keeps_missing_weight_rows_in_collection_list():
    rows = [
        {
            "id": 1,
            "source_item_id": "MLM1",
            "source_url": "https://example/MLM1",
            "title": "Complete",
            "weight_g": 200,
            "infringement_risk_level": 1,
            "infringement_keywords": "BrandX",
            "infringement_reason": "需要人工复核",
            "infringement_checked_at": "2026-09-15 10:00:00",
        },
        {
            "id": 2,
            "source_item_id": "MLM2",
            "source_url": "https://example/MLM2",
            "title": "Missing weight",
            "weight_g": None,
        },
    ]

    class Cursor(_FakeCursor):
        def fetchall(self):
            query = self.queries[-1][0] if self.queries else ""
            if query.startswith(f"SELECT * FROM `{store.COLLECTION_TABLE}` WHERE"):
                return rows
            return []

    class Connection(_FakeConnection):
        def __init__(self):
            super().__init__(update_rowcount=1)
            self.fake_cursor = Cursor(update_rowcount=1)

    connection = Connection()
    with patch(
        "erp.mercadolibre_source_store.upsert_source_snapshot"
    ) as upsert_source_snapshot:
        result = store.add_collection_items_to_products(
            [1, 2], connection_factory=lambda: connection
        )

    product_inserts = [
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"INSERT INTO `{store.PRODUCT_TABLE}`")
    ]
    collection_updates = [
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"UPDATE `{store.COLLECTION_TABLE}` SET `added_to_products`")
    ]
    assert result["count"] == 1
    assert result["skipped_incomplete"] == 1
    assert result["skipped_incomplete_item_ids"] == ["MLM2"]
    assert len(product_inserts) == 1
    assert "`infringement_risk_level`" in product_inserts[0][0]
    assert any(
        query.startswith("INSERT INTO `infringement_risk_checks`")
        and "SELECT 'product_list'" in query
        for query, _params in connection.fake_cursor.queries
    )
    assert any(
        query.startswith("DELETE FROM `infringement_risk_checks`")
        and "`source_type` = 'collection_list'" in query
        for query, _params in connection.fake_cursor.queries
    )
    assert collection_updates[-2][1] == (1,)
    assert collection_updates[-1][1] == (2,)
    upsert_source_snapshot.assert_called_once()


def test_delete_and_publish_selection_reject_empty_ids_before_database_connection():
    with pytest.raises(ValueError, match="至少勾选"):
        store.delete_collection_items([], connection_factory=lambda: None)
    with pytest.raises(ValueError, match="至少勾选"):
        store.delete_product_items([], connection_factory=lambda: None)
    with pytest.raises(ValueError, match="至少勾选"):
        store.get_product_items_by_ids([], connection_factory=lambda: None)
    with pytest.raises(ValueError, match="至少勾选"):
        store.move_product_items_to_collection([], connection_factory=lambda: None)


def test_weight_dimension_completeness_uses_current_values_not_scrape_status():
    complete = {
        "scrape_status": "partial",
        "weight_g": "300.0 g",
        "package_length_cm": Decimal("10"),
        "package_width_cm": "20",
        "package_height_cm": 5,
    }

    assert store.has_complete_weight_dimensions(complete) is True
    assert store._json_safe_row(complete)["weight_dimensions_complete"] is True
    assert store.has_complete_weight_dimensions({**complete, "weight_g": 0}) is False


def test_profitability_updates_exact_collection_duplicate_and_only_newest_product():
    connection = _FakeConnection(update_rowcount=1)
    snapshot = {
        "id": 1880,
        "task_id": 77,
        "listing_type_id": "gold_special",
        "net_proceeds_usd": 12.34,
    }

    store.update_item_profitability(
        "mlm2990352733",
        snapshot,
        connection_factory=lambda: connection,
    )

    updates = [
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith("UPDATE")
    ]
    collection_sql, collection_params = next(
        item for item in updates if item[0].startswith(f"UPDATE `{store.COLLECTION_TABLE}`")
    )
    product_sql, product_params = next(
        item for item in updates if item[0].startswith(f"UPDATE `{store.PRODUCT_TABLE}`")
    )
    assert "WHERE `id` = %s AND `source_item_id` = %s" in collection_sql
    assert collection_params[-2:] == (1880, "MLM2990352733")
    assert "newer.`id` > %s" in product_sql
    assert product_params[-4:] == (
        "MLM2990352733", 1880, "MLM2990352733", 1880,
    )
    assert "`collection_item_id` = %s" in product_sql
    assert "`source_type` = 'collected'" in product_sql


@pytest.mark.parametrize("source_type", ["collected", "pulled"])
def test_product_profitability_updates_do_not_overwrite_collection_history(source_type):
    connection = _FakeConnection(update_rowcount=1)
    store.update_item_profitability("MLM1", {
        "id": 15, "source_type": source_type, "shipping_fee_usd": 6.4,
    }, connection_factory=lambda: connection)
    updates = [(sql, params) for sql, params in connection.fake_cursor.queries
               if sql.startswith("UPDATE")]
    assert len(updates) == 1
    sql, params = updates[0]
    assert sql.startswith(f"UPDATE `{store.PRODUCT_TABLE}`")
    assert "WHERE `id` = %s AND `source_item_id` = %s" in sql
    assert params[-2:] == (15, "MLM1")


@pytest.mark.parametrize("source_type", ["zying", "ai_original"])
def test_profitability_estimator_never_overwrites_source_owned_net_proceeds(source_type):
    connection = _FakeConnection(update_rowcount=1)

    applied = store.update_item_profitability("860217541", {
        "id": 15,
        "source_type": source_type,
        "net_proceeds_usd": None,
        "profitability_error": "无法识别商品所属国家站点",
    }, connection_factory=lambda: connection)

    assert applied is False
    assert not [sql for sql, _params in connection.fake_cursor.queries
                if sql.startswith("UPDATE")]


def test_profitability_update_uses_input_cas_and_stops_mirror_on_conflict():
    connection = _FakeConnection(update_rowcount=0)
    expected = {
        "updated_at": "2026-09-12 12:00:00",
        "price": 299,
        "currency_id": "MXN",
        "weight_g": 420,
        "weight_basis": "plugin_actual",
        "category_id": None,
        "title": "Producto",
        "source_json": "{}",
        "description_json": "{}",
        "page_snapshot_json": "{}",
    }

    applied = store.update_item_profitability(
        "MLM1",
        {
            "id": 88,
            "task_id": 7,
            "shipping_fee_usd": 6.4,
            "_expected_profitability_inputs": expected,
        },
        connection_factory=lambda: connection,
    )

    updates = [
        (sql, params) for sql, params in connection.fake_cursor.queries
        if sql.startswith("UPDATE")
    ]
    assert applied is False
    assert len(updates) == 1
    sql, params = updates[0]
    assert sql.startswith(f"UPDATE `{store.COLLECTION_TABLE}`")
    for column in (
        "updated_at", "price", "currency_id", "weight_g", "weight_basis",
        "category_id", "title", "source_json", "description_json",
        "page_snapshot_json",
    ):
        assert f"`{column}` <=> %s" in sql
    assert params[-10:] == tuple(expected.values())


def test_profitability_queue_excludes_source_owned_rows_and_retries_incomplete_rows():
    connection = _FakeConnection()
    batches = iter([
        [{"id": 1, "source_type": "pulled"}],
        [],
        [{"id": 3, "task_id": 10}],
        [],
    ])
    connection.fake_cursor.fetchall = lambda: next(batches)
    with patch.object(store, "ensure_collection_tables"):
        rows = store.list_stale_profitability_items(
            stale_before="2026-09-06 12:00:00", retry_before="2026-09-07 11:55:00",
            limit=10, connection_factory=lambda: connection,
        )
    assert [row["id"] for row in rows] == [1, 3]
    product_pending_sql, product_pending_params = connection.fake_cursor.queries[0]
    product_stale_sql, product_stale_params = connection.fake_cursor.queries[1]
    collection_pending_sql, collection_pending_params = connection.fake_cursor.queries[2]
    collection_stale_sql, collection_stale_params = connection.fake_cursor.queries[3]
    assert store.PRODUCT_TABLE in product_pending_sql
    assert store.COLLECTION_TABLE in collection_pending_sql
    assert product_pending_params == (5,)
    assert collection_pending_params == (9,)
    assert product_stale_params == (
        "2026-09-06 12:00:00", "2026-09-07 11:55:00", 4,
    )
    assert collection_stale_params == (
        "2026-09-06 12:00:00", "2026-09-07 11:55:00", 8,
    )
    for sql in (product_pending_sql, collection_pending_sql):
        assert "ORDER BY `id` DESC" in sql
        assert "`profitability_updated_at` IS NULL" in sql
    assert "`source_type` NOT IN ('zying', 'ai_original')" in product_pending_sql
    assert "`source_type` NOT IN ('zying', 'ai_original')" in product_stale_sql
    assert "`source_type`" not in collection_pending_sql
    assert "`source_type`" not in collection_stale_sql
    assert "fixed_commission_15_pct%%" in collection_stale_sql
    # Mirror PyMySQL's percent formatting so a raw SQL LIKE wildcard cannot
    # silently break the real profitability worker while fake cursors pass.
    collection_stale_sql % tuple(map(repr, collection_stale_params))
    for sql in (product_stale_sql, collection_stale_sql):
        assert "`weight_g` > 0" in sql
        assert "`commission_amount_usd` IS NULL" in sql
        assert "`shipping_fee_usd` IS NULL" in sql
        assert "`net_proceeds_usd` IS NULL" in sql
        assert "max_gross_or_volumetric" not in sql
        assert "plugin_volumetric_fallback" in sql
        assert "CASE WHEN `profitability_updated_at`" not in sql


def test_reference_refresh_clears_only_shipping_and_net_for_safe_recalculation():
    class Cursor(_FakeCursor):
        def __init__(self):
            super().__init__(update_rowcount=2)
            self.select_calls = 0

        def fetchall(self):
            query = self.queries[-1][0] if self.queries else ""
            if query.startswith("SELECT `id` FROM"):
                self.select_calls += 1
                return [{"id": self.select_calls}] if self.select_calls in (1, 3) else []
            return []

    class Connection(_FakeConnection):
        def __init__(self):
            super().__init__(update_rowcount=2)
            self.fake_cursor = Cursor()
            self.commit_count = 0

        def commit(self):
            self.committed = True
            self.commit_count += 1

    connection = Connection()
    with patch.object(store, "ensure_collection_tables"):
        assert store.mark_all_profitability_stale(
            site_ids=["MLM", "MLB"],
            batch_size=100,
            connection_factory=lambda: connection,
        ) == 4
    update_queries = [
        (sql, params)
        for sql, params in connection.fake_cursor.queries
        if sql.startswith("UPDATE")
    ]
    assert connection.commit_count == 2
    for sql, params in update_queries:
        assert "`profitability_updated_at` = NULL" in sql
        assert "`commission_amount_usd` = NULL" not in sql
        assert "`shipping_fee_usd` = NULL" in sql
        assert "`shipping_weight_rule` = NULL" in sql
        assert "`net_proceeds_usd` = NULL" in sql
        assert "WHERE `id` IN (%s)" in sql
    select_queries = [
        (sql, params)
        for sql, params in connection.fake_cursor.queries
        if sql.startswith("SELECT `id` FROM")
    ]
    assert all("LEFT(`source_item_id`, 3) IN (%s, %s)" in sql for sql, _ in select_queries)
    assert all(params[-3:-1] == ("MLB", "MLM") for _, params in select_queries)
    collection_select, product_select = select_queries[0], select_queries[2]
    assert "`source_type`" not in collection_select[0]
    assert "`source_type` NOT IN ('zying', 'ai_original')" in product_select[0]


def test_add_products_retries_transient_lock_timeout(monkeypatch):
    calls = []

    def add_once(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) < 3:
            raise RuntimeError(1205, "Lock wait timeout exceeded")
        return {"count": 1}

    monkeypatch.setattr(store, "_add_collection_items_to_products_once", add_once)
    monkeypatch.setattr(store.time, "sleep", lambda _seconds: None)

    assert store.add_collection_items_to_products([7]) == {"count": 1}
    assert len(calls) == 3


def test_existing_schema_still_installs_refresh_indexes_once(monkeypatch):
    indexes = []
    monkeypatch.setattr(store, "_schema_ready", False)
    monkeypatch.setattr(store, "_collection_schema_is_current", lambda cursor: True)
    monkeypatch.setattr(store, "_ensure_index", lambda cursor, table, name, definition:
                        indexes.append((table, definition)))
    store.ensure_collection_tables(_FakeCursor())
    store.ensure_collection_tables(_FakeCursor())
    assert indexes == [
        (store.COLLECTION_TABLE, "(`profitability_updated_at`, `id`)"),
        (store.PRODUCT_TABLE, "(`profitability_updated_at`, `id`)"),
        (store.PRODUCT_TABLE, "(`source_item_id`, `organization_key`)"),
        (store.PRODUCT_TABLE, "(`last_publish_token_id`)"),
    ]


@pytest.mark.parametrize("missing_column", [
    (store.MANAGEMENT_CATEGORY_TABLE, "listing_ratio_percent"),
    *[(table, column)
      for table in (store.COLLECTION_TABLE, store.PRODUCT_TABLE)
      for column, _definition in store.INFRINGEMENT_RESULT_COLUMN_DEFINITIONS],
])
def test_missing_required_column_triggers_schema_migration(monkeypatch, missing_column):
    class SchemaCursor(_FakeCursor):
        columns = set()

        def execute(self, query, params=None):
            super().execute(query, params)
            create = re.search(r"CREATE TABLE IF NOT EXISTS `([^`]+)`", query)
            if create:
                self.columns.update(
                    (create[1], column)
                    for column in re.findall(r"^\s*`([^`]+)`", query, re.MULTILINE)
                )
            alter = re.search(r"ALTER TABLE `([^`]+)` ADD COLUMN `([^`]+)`", query)
            if alter:
                self.columns.add((alter[1], alter[2]))

        def fetchone(self):
            return None

        def fetchall(self):
            return [{"TABLE_NAME": table, "COLUMN_NAME": column}
                    for table, column in self.columns]

    cursor = SchemaCursor()
    store._migrate_collection_tables(cursor)
    assert store._collection_schema_is_current(cursor)
    cursor.columns.remove(missing_column)
    assert not store._collection_schema_is_current(cursor)
    monkeypatch.setattr(store, "_schema_ready", False)
    store.ensure_collection_tables(cursor)
    assert store._collection_schema_is_current(cursor)


@pytest.mark.parametrize(("weight_basis", "expected_scrape_status"), [
    ("official_api", "ok"),
    ("plugin_volumetric_fallback", "partial"),
])
def test_move_pulled_product_preserves_weight_basis_before_deleting_product(
    weight_basis, expected_scrape_status,
):
    product_row = {
        "id": 21,
        "collection_item_id": 0,
        "source_type": "pulled",
        "source_item_id": "MLM21",
        "source_url": "https://example/MLM21",
        "main_image_url": "https://example/image.jpg",
        "title": "Pulled product",
        "price": 100,
        "currency_id": "MXN",
        "weight_g": 300,
        "package_length_cm": 10,
        "package_width_cm": 20,
        "package_height_cm": 5,
        "weight_basis": weight_basis,
        "review_status": "risk",
        "last_publish_status": "failed",
        "source_snapshot_json": json.dumps({
            "source": {"id": "MLM21"},
            "description": {"plain_text": "description"},
        }),
    }

    class Cursor(_FakeCursor):
        def execute(self, query, params=None):
            super().execute(query, params)
            normalized = " ".join(str(query).split())
            if normalized.startswith(f"DELETE FROM `{store.PRODUCT_TABLE}`"):
                self.rowcount = 1

        def fetchall(self):
            query = self.queries[-1][0] if self.queries else ""
            if query.startswith(f"SELECT * FROM `{store.PRODUCT_TABLE}` WHERE"):
                return [product_row]
            return []

        def fetchone(self):
            query = self.queries[-1][0] if self.queries else ""
            if query.startswith(f"SELECT `id` FROM `{store.COLLECTION_TABLE}`"):
                return None
            return {"Field": "existing"}

    class Connection(_FakeConnection):
        def __init__(self):
            super().__init__(update_rowcount=1)
            self.fake_cursor = Cursor(update_rowcount=1)

    connection = Connection()
    result = store.move_product_items_to_collection(
        [21],
        reason="审核状态未通过 1 件",
        connection_factory=lambda: connection,
    )

    insert_sql, insert_params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"INSERT INTO `{store.COLLECTION_TABLE}`")
    )
    assert result == {
        "requested": 1,
        "moved": 1,
        "created_collection_rows": 1,
        "deleted": 1,
    }
    assert insert_params[0:3] == (0, "MLM21", "https://example/MLM21")
    assert "`review_status`, `last_publish_status`" in insert_sql
    assert "risk" in insert_params
    assert "failed" in insert_params
    assert weight_basis in insert_params
    assert expected_scrape_status in insert_params
    assert "产品列表自动移回：审核状态未通过 1 件" in insert_params
    assert connection.committed is True


def test_dimension_only_store_link_sync_does_not_relabel_weight_as_actual():
    dimensions_connection = _FakeConnection(update_rowcount=1)

    changed = store.sync_pulled_product_fields_from_store_links(
        [18],
        ["package_length_cm"],
        connection_factory=lambda: dimensions_connection,
    )

    dimensions_sql = next(
        query for query, _params in dimensions_connection.fake_cursor.queries
        if query.startswith(f"UPDATE `{store.PRODUCT_TABLE}` AS products")
    )
    assert changed == 1
    assert "products.`package_length_cm` = links.`package_length_cm`" in dimensions_sql
    assert "products.`volumetric_weight_kg` = links.`volumetric_weight_kg`" in dimensions_sql
    assert "products.`weight_basis`" not in dimensions_sql
    assert "products.`profitability_updated_at` = NULL" not in dimensions_sql

    weight_connection = _FakeConnection(update_rowcount=1)
    store.sync_pulled_product_fields_from_store_links(
        [18],
        ["weight_g"],
        connection_factory=lambda: weight_connection,
    )
    weight_sql = next(
        query for query, _params in weight_connection.fake_cursor.queries
        if query.startswith(f"UPDATE `{store.PRODUCT_TABLE}` AS products")
    )
    assert "products.`weight_basis` = 'mercado_remote_update'" in weight_sql
    assert "products.`shipping_fee_usd` = NULL" in weight_sql
    guard = "LOWER(TRIM(COALESCE(products.`profitability_source`, ''))) <> 'purchase_cost_ratio'"
    assert f"products.`net_proceeds_usd` = IF({guard}, NULL, products.`net_proceeds_usd`)" in weight_sql
    assert f"products.`profitability_source` = IF({guard}, 'mercado_remote_edit_pending', products.`profitability_source`)" in weight_sql


def test_product_review_status_validates_and_updates_selected_rows():
    connection = _FakeConnection(update_rowcount=2)

    result = store.update_product_review_status(
        [9, 3, 9],
        "approved",
        connection_factory=lambda: connection,
    )

    update_sql, params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"UPDATE `{store.PRODUCT_TABLE}` SET `review_status`")
    )
    assert result == {"requested": 2, "changed": 2}
    assert "WHERE `id` IN (%s, %s)" in update_sql
    assert params == ("approved", 3, 9)
    assert connection.committed is True

    with pytest.raises(ValueError, match="不支持的审核状态"):
        store.update_product_review_status([1], "published", connection_factory=lambda: None)


def test_product_content_update_validates_persists_and_invalidates_profitability():
    connection = _FakeConnection(update_rowcount=1)

    result = store.update_product_item(
        9,
        {
            "title": "Nuevo título completo",
            "description_text": "Nueva descripción",
            "main_image_url": "https://http2.mlstatic.com/new.jpg",
            "category_id": "MLM123",
            "price": "1299.90",
            "weight_g": 420,
            "package_length_cm": 30,
            "package_width_cm": 20,
            "package_height_cm": 10,
        },
        connection_factory=lambda: connection,
    )

    product_sql, params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"UPDATE `{store.PRODUCT_TABLE}` SET")
    )
    collection_sql, collection_params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"UPDATE `{store.COLLECTION_TABLE}` AS c")
    )
    assert result == {
        "product_item_id": 9,
        "changed": 1,
        "profitability_refresh_pending": True,
    }
    assert "`description_text` = %s" in product_sql
    assert "`volumetric_weight_kg` = CASE" in product_sql
    guard = "LOWER(TRIM(COALESCE(`profitability_source`, ''))) <> 'purchase_cost_ratio'"
    assert f"`net_proceeds_usd` = IF({guard}, IF(`source_type` = 'zying', `net_proceeds_usd`, NULL), `net_proceeds_usd`)" in product_sql
    assert params[-1] == 9
    assert "c.`price` = p.`price`" in collection_sql
    assert "c.`profitability_updated_at` = NULL" in collection_sql
    assert collection_params == (9,)
    assert connection.committed is True

    with pytest.raises(ValueError, match="原价必须大于 0"):
        store.update_product_item(9, {"price": 0}, connection_factory=lambda: None)
    with pytest.raises(ValueError, match="主图链接必须"):
        store.update_product_item(
            9, {"main_image_url": "javascript:bad"}, connection_factory=lambda: None
        )


def test_dimension_only_product_edit_keeps_profitability_snapshot_current():
    connection = _FakeConnection(update_rowcount=1)

    result = store.update_product_item(
        9,
        {
            "package_length_cm": 30,
            "package_width_cm": 20,
            "package_height_cm": 10,
        },
        connection_factory=lambda: connection,
    )

    updates = [
        sql for sql, _params in connection.fake_cursor.queries
        if sql.startswith("UPDATE")
    ]
    assert result["profitability_refresh_pending"] is False
    assert any("`volumetric_weight_kg` = CASE" in sql for sql in updates)
    assert all("`shipping_fee_usd` = NULL" not in sql for sql in updates)
    assert all("`profitability_updated_at` = NULL" not in sql for sql in updates)


def test_bulk_product_content_update_uses_one_transaction_and_selected_fields_only():
    connection = _FakeConnection(update_rowcount=3)

    result = store.update_product_items(
        [12, 7, 12, 9],
        {"weight_g": 560, "category_id": "MLM999"},
        connection_factory=lambda: connection,
    )

    product_sql, params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"UPDATE `{store.PRODUCT_TABLE}` SET")
    )
    collection_sql, collection_params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"UPDATE `{store.COLLECTION_TABLE}` AS c")
    )
    assert result == {
        "requested": 3,
        "changed": 3,
        "updated_fields": ["category_id", "weight_g"],
        "profitability_refresh_pending": True,
    }
    assert "`weight_g` = %s" in product_sql
    assert "`category_id` = %s" in product_sql
    assert "WHERE `id` IN (%s, %s, %s)" in product_sql
    assert params[-3:] == (7, 9, 12)
    assert "c.`weight_g` = p.`weight_g`" in collection_sql
    assert "c.`category_id` = p.`category_id`" in collection_sql
    assert collection_params == (7, 9, 12)
    assert connection.committed is True


def test_bulk_collection_measurement_update_mirrors_products_atomically():
    connection = _FakeConnection(update_rowcount=2)

    result = store.update_collection_items(
        [8, 7, 8],
        {
            "weight_g": 480,
            "package_length_cm": 30,
            "package_width_cm": 20,
            "package_height_cm": 10,
        },
        connection_factory=lambda: connection,
    )

    collection_sql, params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"UPDATE `{store.COLLECTION_TABLE}` SET")
    )
    product_sql, product_params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"UPDATE `{store.PRODUCT_TABLE}` AS p")
    )
    assert result == {
        "requested": 2,
        "changed": 2,
        "updated_fields": [
            "weight_g", "package_length_cm", "package_width_cm",
            "package_height_cm",
        ],
        "profitability_refresh_pending": True,
    }
    assert "`weight_basis` = 'manual_edit'" in collection_sql
    assert "`volumetric_weight_kg` = CASE" in collection_sql
    guard = "LOWER(TRIM(COALESCE(`profitability_source`, ''))) <> 'purchase_cost_ratio'"
    assert f"`profitability_updated_at` = IF({guard}, NULL, `profitability_updated_at`)" in collection_sql
    assert params[-2:] == (7, 8)
    assert "p.`weight_g` = c.`weight_g`" in product_sql
    assert "p.`volumetric_weight_kg` = c.`volumetric_weight_kg`" in product_sql
    product_guard = "LOWER(TRIM(COALESCE(p.`profitability_source`, ''))) <> 'purchase_cost_ratio'"
    assert f"p.`profitability_updated_at` = IF({product_guard}, NULL, p.`profitability_updated_at`)" in product_sql
    assert product_params == (7, 8)
    assert connection.committed is True

    with pytest.raises(ValueError, match="只支持批量修改"):
        store.update_collection_items(
            [7], {"title": "not allowed"}, connection_factory=lambda: None
        )


@patch.object(store, "ensure_collection_tables", lambda cursor: None)
def test_pulled_store_link_is_mirrored_as_publish_ready_unreviewed_product():
    connection = _FakeConnection()
    item = {
        "id": "MLM1234567890",
        "site_id": "MLM",
        "title": "Official API product",
        "permalink": "https://articulo.mercadolibre.com.mx/MLM-1234567890",
        "pictures": [{"secure_url": "https://http2.mlstatic.com/image.jpg"}],
        "price": 399.9,
        "currency_id": "MXN",
        "category_id": "MLM123",
        "listing_type_id": "gold_special",
        "attributes": [
            {"id": "PACKAGE_WEIGHT", "value_name": "500 g"},
            {"id": "PACKAGE_LENGTH", "value_name": "20 cm"},
            {"id": "PACKAGE_WIDTH", "value_name": "10 cm"},
            {"id": "PACKAGE_HEIGHT", "value_name": "5 cm"},
        ],
        "net_proceeds": {"amount": 18.25, "currency_id": "USD"},
    }

    result = store.upsert_pulled_store_links_to_products(
        {"id": 7, "display_name": "MX Store", "site_id": "MLM"},
        [item],
        connection_factory=lambda: connection,
    )

    upsert_sql, rows = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if "ON DUPLICATE KEY UPDATE" in query
    )
    values = rows[0]
    snapshot = json.loads(values[-6])
    assert result == {"count": 1, "skipped": 0}
    assert values[1:4] == ("pulled", "unreviewed", "MLM1234567890")
    assert values[5:9] == (
        "https://http2.mlstatic.com/image.jpg",
        "Official API product",
        399.9,
        "MXN",
    )
    assert snapshot["source"] == item
    assert snapshot["plugin_snapshot"]["source_type"] == "pulled"
    assert "IF(`source_type` = 'pulled'" in upsert_sql
    assert "`review_status`" not in upsert_sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
    assert connection.committed is True


def test_failed_refresh_cannot_overwrite_an_existing_complete_item():
    connection = _FakeConnection()
    store.upsert_collection_items(
        12,
        [
            {
                "source_item_id": "MLM3016972321",
                "source_url": "https://articulo.mercadolibre.com.mx/MLM-3016972321",
                "scrape_status": "failed",
                "error_message": "browser closed",
            }
        ],
        connection_factory=lambda: connection,
    )

    upsert_sql = next(
        query
        for query, _params in connection.fake_cursor.queries
        if "ON DUPLICATE KEY UPDATE" in query
    )
    assert "`scrape_status` = 'ok' AND VALUES(`scrape_status`) <> 'ok'" in upsert_sql
    assert "`weight_g` = IF(" in upsert_sql
    assert "`weight_g`, CASE" in upsert_sql
    assert "ELSE `weight_g` END" in upsert_sql
    assert "`scrape_status` = IF(" in upsert_sql
    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.closed is True


def test_collection_save_calculates_usd_price_from_fixed_database_rate():
    connection = _FakeConnection()
    original_fetchall = connection.fake_cursor.fetchall

    def fetchall():
        last_query = connection.fake_cursor.queries[-1][0]
        if f"FROM `{store.EXCHANGE_RATE_TABLE}`" in last_query:
            return [{
                "from_currency_id": "MXN",
                "rate": Decimal("0.05000000"),
                "source_created_at": "2026-09-12T00:00:00.000+00:00",
                "refreshed_at": "2026-09-12 08:00:00",
            }]
        return original_fetchall()

    connection.fake_cursor.fetchall = fetchall
    store.upsert_collection_items(
        12,
        [{
            "source_item_id": "MLM3016972321",
            "price": 350,
            "currency_id": "MXN",
            "_replace_profitability_snapshot": True,
        }],
        connection_factory=lambda: connection,
    )

    _sql, params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if "ON DUPLICATE KEY UPDATE" in query
    )
    assert params[14] == Decimal("17.50")
    assert params[15] == Decimal("0.05000000")
    assert params[16] == "2026-09-12T00:00:00.000+00:00"


@pytest.mark.parametrize("weight, expected_net", [(600, 7.71), (None, None)])
def test_plugin_collection_saves_official_freight_and_net_atomically(weight, expected_net):
    from erp.mercadolibre_shipping_rate_cards import OFFICIAL_SHIPPING_RATE_CARD_TABLE

    connection = _FakeConnection()
    original_fetchall = connection.fake_cursor.fetchall

    def fetchall():
        if f"FROM `{OFFICIAL_SHIPPING_RATE_CARD_TABLE}`" in connection.fake_cursor.queries[-1][0]:
            return [{
                "site_id": "MLM", "rate_kind": "above_threshold",
                "weight_min_g": 500, "weight_max_g": 600,
                "shipping_amount_usd": 7.16, "exchange_rate_to_usd": .05,
                "refreshed_at": "2026-10-08 08:00:00",
            }]
        return original_fetchall()

    connection.fake_cursor.fetchall = fetchall
    store.upsert_collection_items(12, [{
        "source_item_id": "MLM3016972321", "title": "Plugin product",
        "price": 350, "currency_id": "MXN", "weight_g": weight,
        "weight_basis": "server_actual", "volumetric_weight_kg": 9,
        # Reference values supplied by the plugin must not become saved net.
        "net_proceeds_usd": 100, "commission_amount_usd": 80,
    }], connection_factory=lambda: connection)
    sql, params = next(
        (query, params) for query, params in connection.fake_cursor.queries
        if query.startswith(f"INSERT INTO `{store.COLLECTION_TABLE}`")
    )
    columns = re.findall(r"`([^`]+)`", sql.split(" VALUES ", 1)[0])[1:]
    saved = dict(zip(columns, params))
    assert saved["net_proceeds_usd"] == expected_net
    assert saved["shipping_fee_usd"] == (7.16 if weight else None)
    assert saved["commission_amount_usd"] == (2.63 if weight else None)
    assert saved["billable_weight_g"] == weight
    assert "`net_proceeds_usd`, VALUES(`net_proceeds_usd`))" in sql
    assert connection.committed


def test_recollection_replaces_stale_profitability_and_weight_basis_atomically():
    connection = _FakeConnection()
    store.upsert_collection_items(
        12,
        [{
            "source_item_id": "MLM3016972321",
            "source_url": "https://articulo.mercadolibre.com.mx/MLM-3016972321",
            "weight_g": 600,
            "weight_basis": "plugin_actual",
            "scrape_status": "ok",
            "_replace_profitability_snapshot": True,
            "profitability_updated_at": None,
        }],
        connection_factory=lambda: connection,
    )

    upsert_sql = next(
        query for query, _params in connection.fake_cursor.queries
        if "ON DUPLICATE KEY UPDATE" in query
    )
    assert "`profitability_updated_at` = IF(" in upsert_sql
    assert "`profitability_updated_at`, VALUES(`profitability_updated_at`))" in upsert_sql
    assert "WHEN VALUES(`weight_g`) IS NOT NULL AND VALUES(`weight_g`) > 0" in upsert_sql
    assert "THEN COALESCE(NULLIF(VALUES(`weight_basis`), ''), `weight_basis`)" in upsert_sql


def test_volumetric_fallback_upsert_clears_fake_actual_weight():
    connection = _FakeConnection()
    store.upsert_collection_items(
        12,
        [{
            "source_item_id": "MLM3016972321",
            "weight_g": 1900,
            "weight_basis": "plugin_volumetric_fallback",
            "scrape_status": "ok",
        }],
        connection_factory=lambda: connection,
    )

    upsert_sql, params = next(
        (query, params) for query, params in connection.fake_cursor.queries
        if "ON DUPLICATE KEY UPDATE" in query
    )
    assert "'plugin_volumetric_fallback' ) THEN NULL" in upsert_sql
    assert params[-11] == "partial"


def test_collection_unique_index_is_scoped_to_each_task():
    legacy = _IndexCursor(["source_item_id"])
    assert store._ensure_collection_task_unique_index(legacy) is True
    migration_sql = [query for query, _params in legacy.queries]
    assert any("DROP INDEX `uniq_erp_meli_collection_item`" in query for query in migration_sql)
    assert any("(`organization_key`, `task_id`, `source_item_id`)" in query for query in migration_sql)

    current = _IndexCursor(["organization_key", "task_id", "source_item_id"])
    assert store._ensure_collection_task_unique_index(current) is False
    assert not any("ALTER TABLE" in query for query, _params in current.queries)


def test_collection_list_can_hide_items_already_added_to_products():
    connection = _FakeConnection()
    store.list_collection_items(
        exclude_added=True,
        connection_factory=lambda: connection,
    )

    count_sql, _params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"SELECT /*+ MAX_EXECUTION_TIME(8000) */ COUNT(*) AS total FROM `{store.COLLECTION_TABLE}`")
    )
    assert "`added_to_products` = 0" in count_sql


def test_lists_filter_by_management_category_and_include_category_name():
    categorized = _FakeConnection()
    store.list_collection_items(
        management_category_id="12",
        connection_factory=lambda: categorized,
    )
    count_sql, count_params = next(
        (query, params)
        for query, params in categorized.fake_cursor.queries
        if query.startswith(f"SELECT /*+ MAX_EXECUTION_TIME(8000) */ COUNT(*) AS total FROM `{store.COLLECTION_TABLE}`")
    )
    row_sql, _row_params = next(
        (query, params)
        for query, params in categorized.fake_cursor.queries
        if query.startswith(f"SELECT /*+ MAX_EXECUTION_TIME(8000) */ `{store.COLLECTION_TABLE}`.*")
    )
    assert "`management_category_id` = %s" in count_sql
    assert count_params == (12,)
    assert store.MANAGEMENT_CATEGORY_TABLE in row_sql
    assert "`management_category_name`" in row_sql

    uncategorized = _FakeConnection()
    store.list_product_items(
        management_category_id="uncategorized",
        connection_factory=lambda: uncategorized,
    )
    product_count_sql, product_params = next(
        (query, params)
        for query, params in uncategorized.fake_cursor.queries
        if query.startswith(f"SELECT /*+ MAX_EXECUTION_TIME(8000) */ COUNT(*) AS total FROM `{store.PRODUCT_TABLE}`")
    )
    assert "`management_category_id` IS NULL" in product_count_sql
    assert product_params == ()


def test_assign_management_category_updates_selected_list_and_mirrors_source_items():
    connection = _FakeConnection(update_rowcount=2)

    result = store.assign_management_category(
        "collection",
        [8, 7],
        3,
        connection_factory=lambda: connection,
    )

    updates = [
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith("UPDATE")
    ]
    assert result == {
        "item_type": "collection",
        "requested": 2,
        "changed": 2,
        "category_id": 3,
    }
    assert updates[0][0].startswith(f"UPDATE `{store.COLLECTION_TABLE}`")
    assert updates[0][1] == (3, 7, 8)
    assert f"INNER JOIN `{store.COLLECTION_TABLE}`" in updates[1][0]
    assert updates[1][1] == (3, 7, 8)
    assert connection.committed is True


def test_management_category_name_validation_happens_before_connecting():
    with pytest.raises(ValueError, match="请输入分类名称"):
        store.create_management_category("   ", connection_factory=lambda: None)
    with pytest.raises(ValueError, match="不能超过 64"):
        store.update_management_category(1, "x" * 65, connection_factory=lambda: None)


def test_collection_list_applies_weight_profit_and_collection_time_filters():
    connection = _FakeConnection()
    store.list_collection_items(
        review_status="risk",
        publish_status="failed",
        weight_min="100",
        weight_max="500",
        price_min="20",
        price_max="80",
        net_proceeds_min="1",
        net_proceeds_max="40",
        date_from="2026-08-25",
        date_to="2026-08-30",
        exclude_added=True,
        connection_factory=lambda: connection,
    )

    count_sql, params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"SELECT /*+ MAX_EXECUTION_TIME(8000) */ COUNT(*) AS total FROM `{store.COLLECTION_TABLE}`")
    )
    for clause in (
        "`review_status` = %s",
        "`last_publish_status` = %s",
        "`weight_g` >= %s",
        "`weight_g` <= %s",
        "`price` >= %s",
        "`price` <= %s",
        "`net_proceeds_usd` >= %s",
        "`net_proceeds_usd` <= %s",
        "`collected_at` >= %s",
        "`collected_at` < %s",
    ):
        assert clause in count_sql
    assert "`added_to_products` = 0" in count_sql
    assert params[:2] == ("risk", "failed")
    assert params[-2:] == ("2026-08-25 00:00:00", "2026-08-31 00:00:00")


@pytest.mark.parametrize(
    ("weight_status", "expected_clause"),
    [
        ("available", "(`weight_g` IS NOT NULL AND `weight_g` > 0"),
        ("missing", "NOT (`weight_g` IS NOT NULL AND `weight_g` > 0"),
    ],
)
def test_collection_list_filters_by_actual_weight_status(
    weight_status, expected_clause
):
    connection = _FakeConnection()
    store.list_collection_items(
        weight_status=weight_status,
        connection_factory=lambda: connection,
    )

    count_sql, params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"SELECT /*+ MAX_EXECUTION_TIME(8000) */ COUNT(*) AS total FROM `{store.COLLECTION_TABLE}`")
    )
    assert expected_clause in count_sql
    assert "'calculated_volumetric', 'legacy_unknown', 'plugin_volumetric_fallback'" in count_sql
    assert params == ()


def test_product_list_applies_status_range_and_date_filters_in_database():
    connection = _FakeConnection()
    store.list_product_items(
        search="cosplay",
        source_type="collected",
        review_status="approved",
        publish_status="failed",
        mercado_category="Costumes",
        weight_min="100",
        weight_max="500",
        price_min="200",
        price_max="900",
        net_proceeds_min="-5",
        net_proceeds_max="40",
        date_from="2026-08-01",
        date_to="2026-08-25",
        connection_factory=lambda: connection,
    )

    count_sql, params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"SELECT /*+ MAX_EXECUTION_TIME(8000) */ COUNT(*) AS total FROM `{store.PRODUCT_TABLE}`")
    )
    for clause in (
        "`review_status` = %s",
        "`last_publish_status` = %s",
        "(`category_id` = %s OR `category_name` LIKE %s)",
        "`weight_g` >= %s",
        "`weight_g` <= %s",
        "`price` >= %s",
        "`price` <= %s",
        "`net_proceeds_usd` >= %s",
        "`net_proceeds_usd` <= %s",
        "`added_at` >= %s",
        "`added_at` < %s",
    ):
        assert clause in count_sql
    assert params == (
        "%cosplay%", "%cosplay%", "Costumes", "%Costumes%",
        "collected", "approved", "failed",
        Decimal("100"), Decimal("500"), Decimal("200"), Decimal("900"),
        Decimal("-5"), Decimal("40"),
        "2026-08-01 00:00:00", "2026-08-26 00:00:00",
    )


def test_product_list_filters_zying_category_and_product_developer():
    connection = _FakeConnection()
    store.list_product_items(
        zying_category="圆佑同步/家电类",
        product_developer_id="121658",
        connection_factory=lambda: connection,
    )

    count_sql, params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"SELECT /*+ MAX_EXECUTION_TIME(8000) */ COUNT(*) AS total FROM `{store.PRODUCT_TABLE}`")
    )
    assert "plugin_snapshot.zying_category" in count_sql
    assert "plugin_snapshot.zying_category_id" in count_sql
    assert "TRIM(COALESCE(`product_developer_id`, '')) = %s" in count_sql
    assert params == (
        "圆佑同步/家电类", "%圆佑同步/家电类%", "圆佑同步/家电类",
        "121658", "121658",
    )


def test_product_list_applies_minute_datetime_range_in_database():
    connection = _FakeConnection()
    store.list_product_items(
        date_from="2026-08-27T09:15",
        date_to="2026-08-27T10:30",
        connection_factory=lambda: connection,
    )

    count_sql, params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"SELECT /*+ MAX_EXECUTION_TIME(8000) */ COUNT(*) AS total FROM `{store.PRODUCT_TABLE}`")
    )
    assert "`added_at` >= %s" in count_sql
    assert "`added_at` < %s" in count_sql
    assert params == ("2026-08-27 09:15:00", "2026-08-27 10:31:00")


def test_product_list_rejects_invalid_filter_ranges_before_connecting():
    with pytest.raises(ValueError, match="最低重量不能大于最高重量"):
        store.list_product_items(
            weight_min=501,
            weight_max=500,
            connection_factory=lambda: None,
        )
    with pytest.raises(ValueError, match="不支持的上架状态"):
        store.list_product_items(
            publish_status="unknown",
            connection_factory=lambda: None,
        )
    with pytest.raises(ValueError, match="不支持的实重状态"):
        store.list_product_items(
            weight_status="unknown",
            connection_factory=lambda: None,
        )
    with pytest.raises(ValueError, match="开始时间不能晚于结束时间"):
        store.list_product_items(
            date_from="2026-08-26",
            date_to="2026-08-25",
            connection_factory=lambda: None,
        )


def test_exchange_price_backfill_updates_collection_and_product_tables():
    connection = _FakeConnection(update_rowcount=3)

    result = store.backfill_item_exchange_prices(
        {
            "MXN": {
                "ratio": "0.05894593",
                "creation_date": "2026-08-25T00:00:00Z",
            }
        },
        connection_factory=lambda: connection,
    )

    updates = [
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith("UPDATE `erp_mercadolibre_")
        and "`sale_price_usd` = ROUND(`price` * %s, 2)" in query
    ]
    assert len(updates) == 4
    assert {params[3] for _query, params in updates} == {"USD", "MXN"}
    assert any(params[:2] == (Decimal("0.05894593"), Decimal("0.05894593")) for _query, params in updates)
    assert result == {"updated": 12, "currencies": ["MXN", "USD"]}
    assert connection.committed is True


def test_recover_interrupted_tasks_marks_only_prestartup_rows():
    connection = _FakeConnection(update_rowcount=3)
    recovered = store.recover_interrupted_collection_tasks(
        cutoff="2026-08-24 02:00:00",
        connection_factory=lambda: connection,
    )

    update_sql, params = next(
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith("UPDATE")
    )
    assert recovered == 3
    assert "`status` IN ('pending', 'starting', 'running')" in update_sql
    assert "`updated_at` < %s" in update_sql
    assert params[1:] == ("2026-08-24 02:00:00", "2026-08-24 02:00:00")
    assert connection.committed is True


def test_publish_attempt_records_copy_product_details_and_save_failure_reason():
    connection = _FakeConnection(update_rowcount=1)
    record_ids = store.create_product_publish_records(
        [
            {
                "id": 11,
                "source_item_id": "mlm111",
                "source_url": "https://example/MLM111",
                "main_image_url": "https://example/image.jpg",
                "title": "测试产品",
            },
            {"id": 12, "source_item_id": "MLM222", "title": "第二个产品"},
        ],
        batch_id="batch-001",
        token_id=7,
        store_name="测试店铺",
        site_id="mlb",
        site_name="巴西",
        quantity=3,
        created_by="测试用户",
        connection_factory=lambda: connection,
    )

    insert_queries = [
        (query, params)
        for query, params in connection.fake_cursor.queries
        if query.startswith(f"INSERT INTO `{store.PUBLISH_RECORD_TABLE}`")
    ]
    assert record_ids == {11: 1, 12: 2}
    assert len(insert_queries) == 2
    assert insert_queries[0][1][0:3] == ("batch-001", 11, "MLM111")
    assert insert_queries[0][1][6:11] == (7, "测试店铺", "MLB", "巴西", 3)

    store.update_product_publish_record(
        1,
        status="failed",
        failure_reason="category rejected",
        finished=True,
        connection_factory=lambda: connection,
    )
    update_query, update_params = next(
        (query, params)
        for query, params in reversed(connection.fake_cursor.queries)
        if query.startswith(f"UPDATE `{store.PUBLISH_RECORD_TABLE}`")
    )
    assert "`failure_reason` = %s" in update_query
    assert update_params[0:2] == ("failed", "category rejected")
    assert update_params[-1] == 1


def test_published_product_ids_are_scoped_to_account_and_site():
    connection = _FakeConnection()
    connection.fake_cursor.fetchall = lambda: [
        {"product_item_id": 11},
        {"product_item_id": 13},
    ]

    with patch.object(store, "ensure_collection_tables"):
        result = store.get_published_product_item_ids(
            [11, 12, 13],
            token_id=7,
            site_id="mlm",
            connection_factory=lambda: connection,
        )

    query, params = connection.fake_cursor.queries[-1]
    assert result == [11, 13]
    assert "`status` = 'published'" in query
    assert params == (11, 12, 13, 7, "MLM")


def test_published_product_account_ids_keep_latest_successful_owner():
    connection = _FakeConnection()
    connection.fake_cursor.fetchall = lambda: [
        {"id": 12, "product_item_id": 11, "token_id": 8},
        {"id": 10, "product_item_id": 11, "token_id": 7},
        {"id": 9, "product_item_id": 13, "token_id": 7},
    ]

    with patch.object(store, "ensure_collection_tables"):
        result = store.get_published_product_account_ids(
            [11, 12, 13], connection_factory=lambda: connection
        )

    query, params = connection.fake_cursor.queries[-1]
    assert result == {11: 8, 13: 7}
    assert "`status` = 'published'" in query
    assert "ORDER BY `id` DESC" in query
    assert params == (11, 12, 13)


def test_existing_user_product_ids_are_reused_across_sites_for_same_account():
    connection = _FakeConnection()
    connection.fake_cursor.fetchall = lambda: [
        {"id": 9, "product_item_id": 11, "published_item_id": "CBTU123"},
        {"id": 8, "product_item_id": 11, "published_item_id": "CBTU122"},
        {"id": 7, "product_item_id": 13, "published_item_id": "U456"},
    ]

    with patch.object(store, "ensure_collection_tables"):
        result = store.get_existing_user_product_ids(
            [11, 12, 13], token_id=7, connection_factory=lambda: connection
        )

    query, params = connection.fake_cursor.queries[-1]
    assert result == {11: "CBTU123", 13: "U456"}
    assert "`site_id`" not in query
    assert "`status` = 'published'" in query
    assert "LIKE 'CBTU%%'" in query
    assert params == (11, 12, 13, 7)


def test_publish_records_can_be_loaded_by_selected_ids_for_retry():
    connection = _FakeConnection()
    connection.fake_cursor.fetchall = lambda: [
        {"id": 12, "status": "failed", "quantity": 5},
        {"id": 9, "status": "publishing", "quantity": 3},
    ]

    with patch.object(store, "ensure_collection_tables"):
        rows = store.get_product_publish_records_by_ids(
            [12, 9, 12],
            connection_factory=lambda: connection,
        )

    query, params = connection.fake_cursor.queries[-1]
    assert [row["id"] for row in rows] == [12, 9]
    assert f"FROM `{store.PUBLISH_RECORD_TABLE}`" in query
    assert "WHERE `id` IN (%s, %s) ORDER BY `id` DESC" in query
    assert params == (12, 9)
    assert connection.committed is True

    with pytest.raises(ValueError, match="至少勾选"):
        store.get_product_publish_records_by_ids(
            [], connection_factory=lambda: None
        )


def test_publish_record_list_supports_time_and_store_group_filters():
    connection = _FakeConnection()

    with patch.object(store, "ensure_collection_tables"):
        result = store.list_product_publish_records(
            group_name="精品组",
            start_date="2026-09-12T00:00",
            end_date="2026-09-12T23:59",
            connection_factory=lambda: connection,
        )

    assert result == {
        "total": 0,
        "counts": {
            **{status: 0 for status in store.PRODUCT_PUBLISH_RECORD_STATUSES},
            "all": 0,
        },
        "rows": [],
    }
    count_query, count_params = connection.fake_cursor.queries[-3]
    total_query, total_params = connection.fake_cursor.queries[-2]
    rows_query, rows_params = connection.fake_cursor.queries[-1]
    for query in (count_query, total_query, rows_query):
        assert "mercado_store_site_settings" in query
        assert "settings.`group_name` = %s" in query
        assert "records.`created_at` >= %s" in query
        assert "records.`created_at` < %s" in query
    assert count_params == (
        "精品组", "2026-09-12 00:00:00", "2026-09-13 00:00:00"
    )
    assert total_params == count_params
    assert rows_params == count_params + (500, 0)


@pytest.mark.parametrize("salesperson", ["张三", "__unassigned__"])
def test_publish_record_salesperson_filters_counts_and_rows_with_store_scope(salesperson):
    connection = _FakeConnection()
    with patch.object(store, "ensure_collection_tables"):
        store.list_product_publish_records(
            salesperson=salesperson, token_ids=[7, 9], filter_token_ids=[9],
            group_name="精品组", site_id="MLM", status="failed",
            connection_factory=lambda: connection,
        )
    count, total, rows = connection.fake_cursor.queries[-3:]
    expected = (7, 9, 9, "MLM", "精品组")
    for sql, params in (count, total, rows):
        assert "records.`token_id` IN (%s, %s)" in sql
        assert "records.`token_id` IN (%s)" in sql
        assert "settings.`site_id` = records.`site_id`" in sql
        if salesperson == "__unassigned__":
            assert "COALESCE(TRIM(settings.`salesperson`), '') = ''" in sql
        else:
            assert "TRIM(settings.`salesperson`) = %s" in sql
            assert salesperson not in sql
    if salesperson != "__unassigned__":
        expected += (salesperson,)
    assert count[1] == expected
    assert total[1] == expected + ("failed",)
    assert rows[1] == expected + ("failed", 500, 0)


def test_publish_record_salesperson_rejects_oversized_name_before_querying():
    with pytest.raises(ValueError, match="业务员名称"):
        store.list_product_publish_records(salesperson="a" * 101, connection_factory=lambda: None)


def test_1688_recollection_clears_unverified_old_weight_and_package():
    connection = _FakeConnection()
    with patch.object(store, 'ensure_collection_tables'):
        store.upsert_ai_original_product({
            'source_url': 'https://detail.1688.com/offer/907173727091.html',
            'title': '正确标题', 'price': 12.5, 'weight_g': None,
            'images': ['https://cbu01.alicdn.com/product.jpg'],
            'variations': [{'sku_id': '11', 'price': 12.5, 'weight_g': 250}],
        }, connection_factory=lambda: connection)
    query, params = next((q, p) for q, p in connection.fake_cursor.queries if q.startswith('INSERT INTO'))
    assert params[6:10] == (None, None, None, None)
    for field in ('weight_g', 'package_length_cm', 'package_width_cm', 'package_height_cm'):
        assert f'`{field}` = IF(`source_type` = \'ai_original\', VALUES(`{field}`), `{field}`)' in query
    snapshot = json.loads(params[11])
    assert snapshot['original_1688']['variations'][0]['weight_g'] == 250
    assert connection.committed


@pytest.mark.parametrize("field, path", [
    ("collector_salesperson", "$.plugin_snapshot.created_by"),
    ("generator_salesperson", "$.ai_original.generated_by"),
])
def test_product_owner_filter_applies_before_pagination(field, path):
    connection = _FakeConnection()
    with patch.object(store, "ensure_collection_tables"):
        store.list_product_items(
            source_type="ai_original", limit=50, offset=50,
            connection_factory=lambda: connection, **{field: "张三"},
        )
    queries = connection.fake_cursor.queries
    assert len(queries) == 2
    for sql, params in queries:
        assert path in sql
        assert ("张三" if field == "generator_salesperson" else "%张三%") in params
    assert queries[-1][1][-2:] == (50, 50)


def test_product_list_limits_before_join_and_records_query_stages(caplog):
    connection = _FakeConnection()
    with caplog.at_level("INFO"):
        store.list_product_items(search="shirt", limit=100, offset=200,
                                 connection_factory=lambda: connection)
    sql, params = next((q, p) for q, p in connection.fake_cursor.queries
                       if "LEFT JOIN" in q and "LIMIT %s OFFSET %s" in q)
    assert sql.index("LIMIT %s OFFSET %s") < sql.index("LEFT JOIN")
    assert params == ("%shirt%", "%shirt%", 100, 200)
    assert "stages_ms=" in caplog.text
    assert "'count':" in caplog.text and "'page':" in caplog.text
    assert connection.closed


def test_product_list_closes_connection_on_query_timeout(caplog):
    connection = _FakeConnection()
    def timeout(sql, params=None):
        raise TimeoutError("query timed out")
    connection.fake_cursor.execute = timeout
    with patch.object(store, "ensure_collection_tables"), pytest.raises(TimeoutError):
        store.list_product_items(connection_factory=lambda: connection)
    assert connection.closed
    assert "'count':" in caplog.text


@pytest.mark.parametrize('filters,pinned', [({}, True), ({'source_type': 'zying'}, False), ({'token_ids': [7]}, False), ({'token_ids': []}, False)])
def test_product_total_pins_narrow_index_only_without_filters(monkeypatch, filters, pinned):
    connection = _FakeConnection()
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    store.list_product_items(connection_factory=lambda: connection, **filters)
    count_sql, params = next((sql, params) for sql, params in connection.fake_cursor.queries if 'COUNT(*) AS total' in sql)
    assert ('FORCE INDEX (`idx_erp_meli_product_added`)' in count_sql) is pinned
    if filters.get('token_ids'):
        assert 'own_records' in count_sql and 'own_links' in count_sql
        assert params == (7, 7, 7, 500, 0)
        assert len(connection.fake_cursor.queries) == 1
    if filters.get('token_ids') == []:
        assert '1 = 0' in count_sql


@pytest.mark.parametrize('table,listing', [
    (store.PRODUCT_TABLE, store.list_product_items),
    (store.COLLECTION_TABLE, store.list_collection_items),
])
def test_list_materializes_only_ids_before_loading_full_page(table, listing):
    connection = _FakeConnection()
    with patch.object(store, 'ensure_collection_tables'):
        listing(search='sample', limit=100, offset=200,
                connection_factory=lambda: connection)
    sql, params = connection.fake_cursor.queries[-1]
    assert f'(SELECT `id` FROM `{table}` WHERE' in sql
    assert 'ORDER BY `id` DESC LIMIT %s OFFSET %s) AS page_ids' in sql
    assert f'INNER JOIN `{table}` ON `{table}`.`id` = page_ids.`id`' in sql
    assert f'SELECT * FROM `{table}`' not in sql
    assert params == ('%sample%', '%sample%', 100, 200)


def test_pending_review_publish_records_are_saved_and_filterable():
    connection = _FakeConnection(update_rowcount=1)
    with patch.object(store, "ensure_collection_tables"):
        store.update_product_publish_state(9, status="pending_review", store_name="Shop", token_id=7,
                                           connection_factory=lambda: connection)
        store.update_product_publish_record(12, status="pending_review", failure_reason="侵权待审核",
                                            connection_factory=lambda: connection)
        store.list_product_publish_records(status="pending_review", connection_factory=lambda: connection)
    queries = connection.fake_cursor.queries
    assert any("last_publish_status" in sql and "pending_review" in params for sql, params in queries)
    assert any("UPDATE" in sql and "侵权待审核" in params for sql, params in queries)
    assert any("records.`status` = %s" in sql and "pending_review" in params for sql, params in queries)
    assert "pending_review" not in store.PRODUCT_PUBLISH_RETRYABLE_STATUSES


@pytest.mark.parametrize('tokens,source,expected', [
    ([7], '', 3), ([8], '', 1), ([7, 8, 7], '', 4),
    ([], '', 0), ([7], 'zying', 2), ([999], '', 0),
])
def test_product_scope_union_preserves_all_ownership_paths(monkeypatch, tokens, source, expected):
    """Execute the generated predicate, including overlaps and foreign stores."""
    import sqlite3
    connection = _FakeConnection()
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    store.list_product_items(token_ids=tokens, source_type=source,
                             connection_factory=lambda: connection)
    sql, params = next((sql, params) for sql, params in connection.fake_cursor.queries
                       if 'COUNT(*) AS total' in sql)
    combined = sql.startswith('WITH authorized_products AS (')
    if combined:
        union = sql[len('WITH authorized_products AS ('):sql.index(') SELECT /*+')]
        count_sql = f'SELECT COUNT(*) AS total FROM ({union}) AS authorized_products'
        count_params = params[:-2]
    else:
        count_sql, count_params = sql, params
    with sqlite3.connect(':memory:') as db:
        db.executescript('''
            CREATE TABLE erp_mercadolibre_products
                (id INTEGER, source_item_id TEXT, last_publish_token_id INTEGER, source_type TEXT);
            CREATE TABLE erp_mercadolibre_publish_records (product_item_id INTEGER, token_id INTEGER);
            CREATE TABLE erp_mercadolibre_store_links (item_id TEXT, token_id INTEGER);
            INSERT INTO erp_mercadolibre_products VALUES
                (1,'A',7,'zying'),(2,'B',NULL,'zying'),(3,'C',NULL,'pulled'),
                (4,'D',8,'zying'),(5,'E',NULL,'zying');
            INSERT INTO erp_mercadolibre_publish_records VALUES (1,7),(2,7),(2,7),(99,7);
            INSERT INTO erp_mercadolibre_store_links VALUES ('A',7),('C',7),('C',7),('D',8);
        ''')
        assert db.execute(count_sql.replace('%s', '?'), count_params).fetchone()[0] == expected
    if tokens:
        assert 'EXISTS' not in sql
        assert sql.count(' UNION ') == 2
        page_sql, page_params = next((query, values) for query, values in connection.fake_cursor.queries
                                     if 'AS page_ids' in query)
        if combined:
            assert len(connection.fake_cursor.queries) == 1
            assert 'SELECT `id` FROM authorized_products ORDER BY `id` DESC LIMIT %s OFFSET %s' in page_sql
        else:
            assert sql.split(' WHERE ', 1)[1] in page_sql
        assert page_params[:-2] == count_params


def test_collection_salesperson_filter_is_exact_and_applies_before_pagination():
    connection = _FakeConnection()
    store.list_collection_items(
        salesperson="  业务甲_%'  ", exclude_added=True,
        include_salesperson_options=True, limit=20, offset=40,
        connection_factory=lambda: connection,
    )
    count_sql, count_params = next(
        (sql, params) for sql, params in connection.fake_cursor.queries
        if sql.startswith("SELECT /*+ MAX_EXECUTION_TIME(8000) */ COUNT(*)")
    )
    assert "EXISTS (SELECT 1 FROM" in count_sql
    assert "collection_task.`created_by`" in count_sql
    assert "'浏览器插件：'" in count_sql
    assert "= %s" in count_sql
    assert count_params == ("业务甲_%'",)
    row_sql, row_params = next(
        (sql, params) for sql, params in connection.fake_cursor.queries
        if sql.startswith("SELECT /*+ MAX_EXECUTION_TIME(8000) */") and "COUNT(*)" not in sql
    )
    assert "EXISTS (SELECT 1 FROM" in row_sql
    assert row_params == ("业务甲_%'", 20, 40)
    directory_sql, directory_params = next(
        (sql, params) for sql, params in connection.fake_cursor.queries
        if sql.startswith("SELECT DISTINCT") and "AS salesperson" in sql
    )
    assert "item.`added_to_products` = 0" in directory_sql
    assert "LIMIT" not in directory_sql and "= %s" not in directory_sql
    assert directory_params is None
    assert "`workbench_users` AS collection_person" in directory_sql
    assert "collection_person.`is_active` = 1" in directory_sql
    assert "collection_person.`organization_key` = item.`organization_key`" in directory_sql
    assert "TRIM(collection_person.`username`)" in directory_sql
    assert "TRIM(collection_person.`display_name`)" in directory_sql
    assert "ELSE '未分配' END AS `salesperson`" in row_sql


def test_collection_unassigned_filter_includes_non_personnel_creators():
    connection = _FakeConnection()
    with patch.object(store, 'ensure_collection_tables'):
        store.list_collection_items(salesperson='未分配', include_salesperson_options=True,
                                    connection_factory=lambda: connection)
    directory_sql = next(sql for sql, _ in connection.fake_cursor.queries if sql.startswith('SELECT DISTINCT'))
    assert "ELSE '未分配' END AS salesperson" in directory_sql
    count_sql = next(sql for sql, _ in connection.fake_cursor.queries if 'COUNT(*) AS total' in sql)
    assert 'NOT EXISTS' in count_sql
    assert 'collection_person.`is_active` = 1' in count_sql


def test_collection_personnel_directory_and_unassigned_rows_with_saved_creators():
    import sqlite3
    database = sqlite3.connect(':memory:')
    database.row_factory = sqlite3.Row
    database.execute('CREATE TABLE workbench_users (username TEXT, display_name TEXT, organization_key TEXT, is_active INTEGER)')
    database.execute(f'CREATE TABLE {store.TASK_TABLE} (id INTEGER, created_by TEXT)')
    database.execute(f'CREATE TABLE {store.COLLECTION_TABLE} (id INTEGER, task_id INTEGER, organization_key TEXT, added_to_products INTEGER, management_category_id INTEGER)')
    database.execute(f'CREATE TABLE {store.MANAGEMENT_CATEGORY_TABLE} (id INTEGER, name TEXT, listing_ratio_percent REAL)')
    database.executemany('INSERT INTO workbench_users VALUES (?, ?, ?, ?)', [
        ('sales-a', '业务甲', 'a', 1), ('inactive', '停用人员', 'a', 0),
    ])
    database.executemany(f'INSERT INTO {store.TASK_TABLE} VALUES (?, ?)', [
        (1, '浏览器插件：业务甲'), (2, 'script-test'), (3, '停用人员'), (4, '业务甲'), (5, 'sales-a'),
    ])
    database.executemany(f'INSERT INTO {store.COLLECTION_TABLE} VALUES (?, ?, ?, 0, NULL)', [
        (1, 1, 'a'), (2, 2, 'a'), (3, 3, 'a'), (4, 4, 'b'), (5, 5, 'a'),
    ])
    connection = _FakeConnection()
    with patch.object(store, 'ensure_collection_tables'):
        store.list_collection_items(include_salesperson_options=True, connection_factory=lambda: connection)
    directory_sql, directory_params = next((sql, params) for sql, params in connection.fake_cursor.queries if sql.startswith('SELECT DISTINCT'))
    names = [row['salesperson'] for row in database.execute(directory_sql, directory_params or ())]
    assert set(names) == {'业务甲', 'sales-a', '未分配'}
    page_sql, page_params = connection.fake_cursor.queries[-1]
    people = {row['id']: row['salesperson'] for row in database.execute(page_sql.replace('%s', '?'), page_params)}
    assert people == {1: '业务甲', 2: '未分配', 3: '未分配', 4: '未分配', 5: 'sales-a'}
    connection = _FakeConnection()
    with patch.object(store, 'ensure_collection_tables'):
        store.list_collection_items(salesperson='未分配', connection_factory=lambda: connection)
    page_sql, page_params = connection.fake_cursor.queries[-1]
    assert {row['id'] for row in database.execute(page_sql.replace('%s', '?'), page_params)} == {2, 3, 4}
    database.close()


@pytest.mark.parametrize('row_ids,has_more', [([5, 4, 3], True), ([5], False), ([], False)])
def test_product_count_deadline_still_returns_filtered_page(monkeypatch, row_ids, has_more):
    import pymysql
    connection = _FakeConnection()
    cursor = connection.fake_cursor
    original_execute = cursor.execute
    def execute(sql, params=None):
        original_execute(sql, params)
        if 'COUNT(*) AS total' in sql:
            raise pymysql.err.OperationalError(3024, 'maximum statement execution time exceeded')
    cursor.execute = execute
    cursor.fetchall = lambda: [{'id': value} for value in row_ids]
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    with store.product_access.scope({'access_version': 1, 'role_key': 'super_admin',
                                     'organization_key': 'test-enterprise'}):
        result = store.list_product_items(source_type='zying', limit=2, offset=4,
                                          connection_factory=lambda: connection)
    assert result['total'] is None
    assert result['total_exact'] is False
    assert result['has_more'] is has_more
    assert [row['id'] for row in result['rows']] == row_ids[:2]
    assert len(cursor.queries) == 2
    page_sql, page_params = cursor.queries[-1]
    assert '`organization_key` = %s' in page_sql and '`source_type` = %s' in page_sql
    assert page_params == ('test-enterprise', 'zying', 3, 4)
    assert connection.closed and connection.committed


def test_product_count_connection_loss_is_not_hidden(monkeypatch):
    import pymysql
    connection = _FakeConnection()
    def execute(sql, params=None):
        raise pymysql.err.OperationalError(2013, 'Lost connection')
    connection.fake_cursor.execute = execute
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    with pytest.raises(pymysql.err.OperationalError):
        store.list_product_items(connection_factory=lambda: connection)
    assert connection.closed


def test_product_category_options_are_permission_scoped_and_not_paginated(monkeypatch):
    connection = _FakeConnection()
    monkeypatch.setattr(store.product_access, 'actor', lambda: None)
    monkeypatch.setattr(store.product_access, 'predicate', lambda table: ('`organization_key` = %s', ['enterprise-a']))
    monkeypatch.setattr(store, '_product_token_scope', lambda ids: ('', '`token_id` = %s', [7]))
    store.list_product_mercado_categories(token_ids=[7], connection_factory=lambda: connection)
    sql, params = next((sql, params) for sql, params in connection.fake_cursor.queries if 'AS product_count' in sql)
    assert 'GROUP BY `category_id`' in sql and 'LIMIT' not in sql
    assert '`organization_key` = %s' in sql and '`token_id` = %s' in sql
    assert params == (7, 'enterprise-a')
    assert connection.closed


def test_product_category_options_empty_scope_never_reads_database(monkeypatch):
    monkeypatch.setattr(store.product_access, 'actor', lambda: None)
    assert store.list_product_mercado_categories(token_ids=[], connection_factory=lambda: pytest.fail('empty scope')) == {'rows': []}


def test_product_parent_category_filter_uses_complete_exact_ids(monkeypatch):
    from erp import mercadolibre_category_tree as tree
    ids = [f'CBT{i}' for i in range(1, 160)]
    monkeypatch.setattr(tree, 'category_filter_ids', lambda value: ids)
    connection = _FakeConnection()
    store.list_product_items(mercado_category='tree:CBT1', connection_factory=lambda: connection)
    sql, params = next((sql, params) for sql, params in connection.fake_cursor.queries if 'COUNT(*) AS total' in sql and store.PRODUCT_TABLE in sql)
    assert '`category_id` IN (' in sql and '`category_name` LIKE' not in sql
    assert list(params) == ids


def test_product_salesperson_filter_matches_display_and_directory_respects_scope():
    connection = _FakeConnection()
    result = store.list_product_items(
        salesperson="  业务甲_%'  ", include_salesperson_options=True,
        source_type='zying', token_ids=[7], limit=20, offset=40,
        connection_factory=lambda: connection,
    )
    count_sql, count_params = next((sql, params) for sql, params in connection.fake_cursor.queries
                                 if 'COUNT(*) AS total' in sql)
    expression = store._product_salesperson_expression()
    assert '`owner_name` = %s' in count_sql
    assert count_params == ("业务甲_%'", 7, 7, 7, 'zying')
    assert 'source_snapshot_json' not in expression
    assert 'collection_task' not in expression
    directory_sql, directory_params = next((sql, params) for sql, params in connection.fake_cursor.queries
                                          if 'GROUP BY `organization_key`, `owner_name`' in sql)
    assert "MAX_EXECUTION_TIME(2000)" in directory_sql
    assert "idx_product_salesperson" in directory_sql
    assert 'authorized_products' in directory_sql
    assert directory_params == (7, 7, 7)
    assert "`source_type` = %s" not in directory_sql
    assert result['salespeople'] == []


def test_slow_legacy_salesperson_directory_does_not_block_product_rows(monkeypatch):
    import pymysql
    connection = _FakeConnection()
    cursor = connection.fake_cursor
    original = cursor.execute
    def execute(sql, params=None):
        original(sql, params)
        if 'GROUP BY `organization_key`, `owner_name`' in sql:
            raise pymysql.err.OperationalError(3024, 'directory exceeded deadline')
    cursor.execute = execute
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    result = store.list_product_items(include_salesperson_options=True, connection_factory=lambda: connection)
    assert result['rows'] == [] and result['total'] == 0
    assert 'salespeople' not in result
    assert any('ORDER BY' in sql and 'page_ids' in sql for sql, _ in cursor.queries)
    assert connection.closed


@patch.object(store, "ensure_collection_tables", lambda cursor: None)
def test_pulled_products_use_each_site_salesperson_instead_of_sync_operator():
    connection = _FakeConnection()
    token = {'id': 7, 'display_name': 'Store', 'organization_key': 'company-a',
             'owner_name': '授权人', 'owner_user_id': 42,
             'site_settings': [{'site_id': 'MLM', 'salesperson': ' 甲 '},
                               {'site_id': 'MLB', 'salesperson': '乙'}]}
    items = [dict(id=site + '123', site_id=site, title='Product',
                  permalink='https://example.com/' + site, thumbnail='https://example.com/p.jpg',
                  price=10, currency_id='USD', category_id=site + '1')
             for site in ['MLM', 'MLB', 'MLC']]
    result = store.upsert_pulled_store_links_to_products(token, items, connection_factory=lambda: connection)
    batches = [(sql, rows) for sql, rows in connection.fake_cursor.queries
               if sql.startswith('INSERT INTO') and 'ON DUPLICATE KEY UPDATE' in sql]
    assert result == {'count': 3, 'skipped': 0}
    assert [rows[0][-4:] for _, rows in batches] == [
        ('company-a', None, '甲', 'enterprise'),
        ('company-a', None, '乙', 'enterprise'),
        ('company-a', None, '', 'enterprise')]
    for sql, rows in batches:
        assert "`owner_name` = IF(`source_type` = 'pulled', VALUES(`owner_name`), `owner_name`)" in sql
        assert "`owner_user_id` = IF(`source_type` = 'pulled', VALUES(`owner_user_id`), `owner_user_id`)" in sql
        assert json.loads(rows[0][-6])['plugin_snapshot']['salesperson'] == rows[0][-2]


@patch.object(store, 'ensure_collection_tables', lambda cursor: None)
def test_pulled_sync_loads_site_settings_for_secret_token_records():
    connection = _FakeConnection()
    connection.fake_cursor.fetchall = lambda: [{'site_id': 'MLM', 'salesperson': '店铺负责人'}]
    store.upsert_pulled_store_links_to_products(
        {'id': 7, 'organization_key': 'company-a'},
        [dict(id='MLM123', site_id='MLM', title='Product', permalink='https://example.com/p',
              thumbnail='https://example.com/p.jpg', price=10, currency_id='USD', category_id='MLM1')],
        connection_factory=lambda: connection)
    queries = connection.fake_cursor.queries
    assert queries[0][1] == (7,)
    assert 'mercado_store_site_settings' in queries[0][0]
    rows = next(rows for sql, rows in queries if sql.startswith('INSERT INTO'))
    assert rows[0][-2] == '店铺负责人'
