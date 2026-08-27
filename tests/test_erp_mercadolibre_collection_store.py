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
    assert json.loads(row[22])["source"]["attributes"][0]["id"] == "BRAND"
    assert "IF(`source_type` = 'zying'" in upsert_sql
    assert "zying" in store.PRODUCT_SOURCE_TYPES


def test_add_products_keeps_missing_weight_rows_in_collection_list():
    rows = [
        {
            "id": 1,
            "source_item_id": "MLM1",
            "source_url": "https://example/MLM1",
            "title": "Complete",
            "weight_g": 200,
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


def test_move_pulled_product_creates_collection_row_before_deleting_product():
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
    assert "产品列表自动移回：审核状态未通过 1 件" in insert_params
    assert connection.committed is True


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
    assert "`net_proceeds_usd` = NULL" in product_sql
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
    assert "`weight_g` = COALESCE(VALUES(`weight_g`), `weight_g`)" in upsert_sql
    assert "`scrape_status` = IF(" in upsert_sql
    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.closed is True


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


def test_product_list_applies_status_range_and_date_filters_in_database():
    connection = _FakeConnection()
    store.list_product_items(
        search="cosplay",
        source_type="collected",
        review_status="approved",
        publish_status="failed",
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
        "%cosplay%", "%cosplay%", "collected", "approved", "failed",
        Decimal("100"), Decimal("500"), Decimal("200"), Decimal("900"),
        Decimal("-5"), Decimal("40"),
        "2026-08-01 00:00:00", "2026-08-26 00:00:00",
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
