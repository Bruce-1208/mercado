"""Offline coverage for collection destination categories in the extension."""

import pytest

from bit import bit_zying_caiji
from erp import mercadolibre_collection_store as store


@pytest.fixture
def workbench(monkeypatch, tmp_path):
    # The workbench otherwise initializes its production MySQL/API service on
    # import. This route suite must never depend on a running production store.
    from erp.ai_weight_price import service

    original_service = service.Service
    monkeypatch.setattr(
        service, "Service",
        lambda *args, **kwargs: original_service(
            tmp_path / "ai-weight-price", storage_backend="sqlite",
            migrate_legacy_state=False,
        ),
    )
    from bit import bit_interface

    bit_interface.app.config.update(TESTING=True)
    monkeypatch.setattr(
        bit_interface, "_browser_extension_user_from_token",
        lambda token: {
            "id": 7, "username": "collector", "display_name": "采集员",
            "permissions": ["*"], "access_version": 1,
            "is_platform_admin": True,
        } if token == "test-token" else None,
    )
    monkeypatch.setattr(
        bit_interface, "db_list_mercado_management_categories",
        lambda: {"total": 1, "rows": [{"id": 23, "name": "收纳用品"}]},
    )
    return bit_interface


HEADERS = {"Authorization": "Bearer test-token"}
PRODUCT = {
    "source_item_id": "MLM3016972321",
    "source_url": "https://articulo.mercadolibre.com.mx/MLM-3016972321",
    "title": "收纳盒", "scrape_status": "partial",
}


def test_destination_categories_use_extension_login_and_standard_rows(workbench):
    client = workbench.app.test_client()
    assert client.get("/api/browser-extension/management-categories").status_code == 401
    response = client.get("/api/browser-extension/management-categories", headers=HEADERS)
    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "total": 1, "rows": [{"id": 23, "name": "收纳用品"}],
    }
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("selection", [23, "23", " 23 "])
@pytest.mark.parametrize("location", ["product", "request"])
def test_mercado_collection_saves_selected_destination(workbench, monkeypatch, selection, location):
    writes = []
    monkeypatch.setattr(workbench, "db_create_mercado_collection_task", lambda *args: 88)
    monkeypatch.setattr(
        workbench, "db_upsert_mercado_collection_items",
        lambda task_id, rows: writes.extend(rows) or len(rows),
    )
    monkeypatch.setattr(workbench, "db_update_mercado_collection_task", lambda *a, **k: None)
    body = {"product": dict(PRODUCT)}
    (body["product"] if location == "product" else body)["management_category_id"] = selection
    response = workbench.app.test_client().post(
        "/api/browser-extension/collect",
        json=body, headers=HEADERS,
    )
    assert response.status_code == 201
    assert writes[0]["management_category_id"] == 23
    assert writes[0]["source_item_id"] == PRODUCT["source_item_id"]


@pytest.mark.parametrize("selection", [True, False, 0, -1, 1.5, "23.0", "wrong", "9223372036854775808", 99])
def test_invalid_destination_rejected_before_creating_collection_task(workbench, monkeypatch, selection):
    monkeypatch.setattr(
        workbench, "db_create_mercado_collection_task",
        lambda *a: pytest.fail("Invalid destination must not create a task"),
    )
    response = workbench.app.test_client().post(
        "/api/browser-extension/collect",
        json={"product": PRODUCT, "management_category_id": selection}, headers=HEADERS,
    )
    assert response.status_code == 400
    assert "分类" in response.get_json()["message"]


def test_empty_destination_does_not_read_categories(workbench, monkeypatch):
    monkeypatch.setattr(
        workbench, "db_list_mercado_management_categories",
        lambda: pytest.fail("Empty destination must preserve existing classification"),
    )
    assert workbench.build_zying_collection_params({"management_category_id": ""})["management_category_id"] is None


def test_zying_start_keeps_source_and_destination_categories_independent(workbench, monkeypatch):
    received = []
    monkeypatch.setattr(workbench, "_remember_browser_extension_zying_categories", lambda data: [])
    monkeypatch.setattr(
        workbench, "_start_zying_collection_task",
        lambda params, **kwargs: (received.append(params) or {}, "", 200),
    )
    response = workbench.app.test_client().post(
        "/api/browser-extension/zying/start",
        json={"credential": "local-credential", "category": "202170568",
              "category_name": "智赢原分类", "management_category_id": "23"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert received[0]["category"] == "202170568"
    assert received[0]["category_name"] == "智赢原分类"
    assert received[0]["management_category_id"] == 23


def test_zying_start_rejects_deleted_destination_before_launch(workbench, monkeypatch):
    monkeypatch.setattr(workbench, "_remember_browser_extension_zying_categories", lambda data: [])
    monkeypatch.setattr(
        workbench, "_start_zying_collection_task",
        lambda *a, **k: pytest.fail("Deleted destination must not launch collection"),
    )
    response = workbench.app.test_client().post(
        "/api/browser-extension/zying/start",
        json={"credential": "local-credential", "management_category_id": 99},
        headers=HEADERS,
    )
    assert response.status_code == 400


class _CategoryConnection:
    def __init__(self, category_exists=True):
        self.queries = []
        self.category_exists = category_exists
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.rowcount = 1

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params))

    def executemany(self, sql, rows):
        self.execute(sql, list(rows))

    def fetchone(self):
        return {"id": 23} if self.category_exists else None

    def fetchall(self):
        return []

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.fixture
def offline_schema(monkeypatch):
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)


def test_mercado_destination_and_existing_product_change_in_one_transaction(offline_schema):
    connection = _CategoryConnection()
    assert store.upsert_collection_items(
        88, [{**PRODUCT, "management_category_id": 23}],
        connection_factory=lambda: connection,
    ) == 1
    assert "FOR UPDATE" in connection.queries[0][0]
    insert_sql, values = next((s, p) for s, p in connection.queries if s.startswith("INSERT"))
    assert "`collected_at`, `management_category_id`" in insert_sql
    assert "`management_category_id` = VALUES(`management_category_id`)" in insert_sql
    assert values[-1] == 23
    assert connection.queries[-1] == (
        f"UPDATE `{store.PRODUCT_TABLE}` SET `management_category_id` = %s WHERE `source_item_id` = %s",
        (23, PRODUCT["source_item_id"]),
    )
    assert connection.committed and connection.closed and not connection.rolled_back


@pytest.mark.parametrize("writer, rows", [
    (store.upsert_collection_items, [{**PRODUCT, "management_category_id": 23}]),
    (store.upsert_zying_products_to_products, [{"product_id": "801623017", "title": "产品", "management_category_id": 23}]),
])
def test_destination_deleted_after_start_rolls_back_before_product_write(offline_schema, writer, rows):
    connection = _CategoryConnection(category_exists=False)
    args = (88, rows) if writer is store.upsert_collection_items else (rows,)
    with pytest.raises(ValueError, match="分类不存在"):
        writer(*args, connection_factory=lambda: connection)
    assert not any(sql.startswith("INSERT") for sql, _ in connection.queries)
    assert connection.rolled_back and connection.closed and not connection.committed


@pytest.mark.parametrize("writer, rows", [
    (store.upsert_collection_items, [PRODUCT]),
    (store.upsert_zying_products_to_products, [{"product_id": "801623017", "title": "产品"}]),
])
def test_no_destination_preserves_existing_category_in_upsert(offline_schema, writer, rows):
    connection = _CategoryConnection()
    args = (88, rows) if writer is store.upsert_collection_items else (rows,)
    writer(*args, connection_factory=lambda: connection)
    insert_sql = next(sql for sql, _ in connection.queries if sql.startswith("INSERT"))
    assert "management_category_id" not in insert_sql
    assert not any("FOR UPDATE" in sql or sql.startswith("UPDATE") for sql, _ in connection.queries)


def test_zying_destination_is_written_only_with_valid_product_rows(offline_schema):
    connection = _CategoryConnection()
    result = store.upsert_zying_products_to_products(
        [{"product_id": "801623017", "title": "产品", "management_category_id": 23},
         {"product_id": "801623018", "title": "", "management_category_id": 99}],
        connection_factory=lambda: connection,
    )
    assert result == {"count": 1, "skipped": 1}
    insert_sql, rows = next((s, p) for s, p in connection.queries if s.startswith("INSERT"))
    assert "`product_developer_name`, `management_category_id`" in insert_sql
    assert rows[0][-1] == 23
    assert "COALESCE(VALUES(`management_category_id`), `management_category_id`)" in insert_sql
    assert connection.queries[0][1] == (23,)


def test_category_deletion_takes_same_lock_before_clearing_products(offline_schema):
    connection = _CategoryConnection()
    store.delete_management_category(23, connection_factory=lambda: connection)
    assert "FOR UPDATE" in connection.queries[0][0]
    assert connection.committed


def test_zying_only_assigns_destination_to_new_matching_products(monkeypatch):
    written = []
    mirrored = []
    monkeypatch.setattr(bit_zying_caiji, "list_zying_product_developers", lambda token: [])
    monkeypatch.setattr(bit_zying_caiji, "_load_zying_category_name_lookup", lambda reader: {})
    monkeypatch.setattr(
        bit_zying_caiji, "_zying_api_post",
        lambda *args: {"list": {"data": [
            {"id": 801623017, "title": "已有产品"},
            {"id": 801623018, "title": "本次保存"},
            {"id": 801623019, "title": "源分类不匹配"},
        ]}},
    )
    def enrich(driver, rows, token=None):
        assert {row["product_id"] for row in rows} == {"801623018", "801623019"}
        for row in rows:
            row["detail_data"] = {
                "sale_id": row["product_id"], "sale_title": row["title"],
                "sale_localid": "10" if row["product_id"] == "801623018" else "11",
            }
    monkeypatch.setattr(bit_zying_caiji, "_enrich_product_records", enrich)
    result = bit_zying_caiji.collect_zying_products_api(
        auth_token="local-token", number=1, category="10", category_name="智赢原分类",
        management_category_id=23,
        existing_product_id_reader=lambda ids: {"801623017"},
        product_writer=lambda rows: written.extend(rows) or len(rows),
        product_mirror_writer=lambda rows: mirrored.extend(rows) or {"count": len(rows)},
        return_summary=True,
    )
    assert result["inserted_count"] == 1
    assert result["skipped_existing_count"] == 1
    assert result["category_mismatch_count"] == 1
    assert [row["product_id"] for row in written] == ["801623018"]
    assert written[0]["management_category_id"] == 23
    assert written[0]["zying_category"] == "智赢原分类"
    assert mirrored == written


def test_zying_wrapper_forwards_destination_to_api(monkeypatch):
    received = {}
    monkeypatch.setattr(
        bit_zying_caiji, "collect_zying_products_api",
        lambda **params: received.update(params) or [],
    )
    bit_zying_caiji.collect_zying_products(auth_token="local-token", management_category_id="23")
    assert received["management_category_id"] == 23
