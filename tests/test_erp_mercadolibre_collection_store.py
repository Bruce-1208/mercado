import json

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


def test_add_products_rejects_invalid_or_empty_ids_before_database_connection():
    with pytest.raises(ValueError, match="至少勾选"):
        store.add_collection_items_to_products([], connection_factory=lambda: None)
    with pytest.raises(ValueError, match="编号无效"):
        store.add_collection_items_to_products(["bad"], connection_factory=lambda: None)


def test_delete_and_publish_selection_reject_empty_ids_before_database_connection():
    with pytest.raises(ValueError, match="至少勾选"):
        store.delete_collection_items([], connection_factory=lambda: None)
    with pytest.raises(ValueError, match="至少勾选"):
        store.delete_product_items([], connection_factory=lambda: None)
    with pytest.raises(ValueError, match="至少勾选"):
        store.get_product_items_by_ids([], connection_factory=lambda: None)


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
