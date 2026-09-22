import json
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
    assert row[24:] == ("121658", "张三")
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
    ]


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
    assert "products.`net_proceeds_usd` = NULL" in weight_sql
    assert "products.`profitability_source` = 'mercado_remote_edit_pending'" in weight_sql


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
    assert "`net_proceeds_usd` = IF(`source_type` = 'zying'" in product_sql
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
    assert "`profitability_updated_at` = NULL" in collection_sql
    assert params[-2:] == (7, 8)
    assert "p.`weight_g` = c.`weight_g`" in product_sql
    assert "p.`volumetric_weight_kg` = c.`volumetric_weight_kg`" in product_sql
    assert "p.`profitability_updated_at` = NULL" in product_sql
    assert product_params == (7, 8)
    assert connection.committed is True

    with pytest.raises(ValueError, match="只支持批量修改"):
        store.update_collection_items(
            [7], {"title": "not allowed"}, connection_factory=lambda: None
        )


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
    snapshot = json.loads(values[-2])
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
    assert params[-7] == "partial"


def test_collection_unique_index_is_scoped_to_each_task():
    legacy = _IndexCursor(["source_item_id"])
    assert store._ensure_collection_task_unique_index(legacy) is True
    migration_sql = [query for query, _params in legacy.queries]
    assert any("DROP INDEX `uniq_erp_meli_collection_item`" in query for query in migration_sql)
    assert any("(`task_id`, `source_item_id`)" in query for query in migration_sql)

    current = _IndexCursor(["task_id", "source_item_id"])
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
        if query.startswith(f"SELECT COUNT(*) AS total FROM `{store.COLLECTION_TABLE}`")
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
        if query.startswith(f"SELECT COUNT(*) AS total FROM `{store.COLLECTION_TABLE}`")
    )
    row_sql, _row_params = next(
        (query, params)
        for query, params in categorized.fake_cursor.queries
        if query.startswith(f"SELECT `{store.COLLECTION_TABLE}`.*")
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
        if query.startswith(f"SELECT COUNT(*) AS total FROM `{store.PRODUCT_TABLE}`")
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
        if query.startswith(f"SELECT COUNT(*) AS total FROM `{store.COLLECTION_TABLE}`")
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
        if query.startswith(f"SELECT COUNT(*) AS total FROM `{store.COLLECTION_TABLE}`")
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
        if query.startswith(f"SELECT COUNT(*) AS total FROM `{store.PRODUCT_TABLE}`")
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
        if query.startswith(f"SELECT COUNT(*) AS total FROM `{store.PRODUCT_TABLE}`")
    )
    assert "plugin_snapshot.zying_category" in count_sql
    assert "plugin_snapshot.zying_category_id" in count_sql
    assert "`product_developer_id` = %s" in count_sql
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
        if query.startswith(f"SELECT COUNT(*) AS total FROM `{store.PRODUCT_TABLE}`")
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
