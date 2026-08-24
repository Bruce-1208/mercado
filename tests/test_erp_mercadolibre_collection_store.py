import pytest

from erp import mercadolibre_collection_store as store


class _FakeCursor:
    def __init__(self, *, update_rowcount=0):
        self.queries = []
        self.rowcount = 0
        self.update_rowcount = update_rowcount

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(str(query).split())
        self.queries.append((normalized, params))
        self.rowcount = self.update_rowcount if normalized.startswith("UPDATE") else 0

    def fetchone(self):
        # Pretend all migration columns already exist.
        return {"Field": "existing"}


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
