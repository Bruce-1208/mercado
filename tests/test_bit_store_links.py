from decimal import Decimal
import threading
import time
from unittest.mock import patch

import pytest

from bit import bit_store_link_remote_update, bit_store_link_sync
import bit.bit_interface as workbench
from erp import mercadolibre_store_link_store as store


def _client():
    workbench.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    client = workbench.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {
            "id": 1,
            "username": "tester",
            "display_name": "测试用户",
        }
    return client


@pytest.fixture(autouse=True)
def reset_sync_state():
    with bit_store_link_sync._state_guard:
        bit_store_link_sync._sync_state.update(
            running=False,
            task_id="",
            status="idle",
            message="等待同步店铺链接",
            total_stores=0,
            processed_stores=0,
            active_stores=[],
            discovered_count=0,
            inserted_count=0,
            updated_count=0,
            detail_count=0,
            detail_failed_count=0,
            product_count=0,
            failed_count=0,
            results=[],
            logs=[],
        )
    with bit_store_link_remote_update._state_guard:
        bit_store_link_remote_update._update_state.update(
            running=False,
            task_id="",
            status="idle",
            message="等待修改美客多后台链接",
            total_links=0,
            processed_links=0,
            success_count=0,
            partial_count=0,
            failed_count=0,
            results=[],
            logs=[],
        )
    yield


def test_listing_record_extracts_link_weight_dimensions_and_sku():
    record = store.listing_record(
        {
            "id": 7,
            "display_name": "泽顺墨西哥",
            "meli_user_id": "seller-7",
            "site_id": "CBT",
        },
        {
            "id": "MLM123",
            "site_id": "MLM",
            "title": "测试商品",
            "permalink": "https://articulo.mercadolibre.com.mx/MLM123",
            "price": 19.99,
            "currency_id": "USD",
            "net_proceeds": {
                "amount": 14.25,
                "currency_id": "USD",
                "additional_concepts": [
                    {"id": "sale_fee", "amount": 2.49},
                ],
            },
            "attributes": [
                {"id": "PACKAGE_WEIGHT", "value_struct": {"number": 0.8, "unit": "kg"}},
                {"id": "PACKAGE_LENGTH", "value_struct": {"number": 30, "unit": "cm"}},
                {"id": "PACKAGE_WIDTH", "value_struct": {"number": 20, "unit": "cm"}},
                {"id": "PACKAGE_HEIGHT", "value_struct": {"number": 10, "unit": "cm"}},
                {"id": "SELLER_SKU", "value_name": "SKU-01"},
            ],
        },
        "2026-08-24 10:00:00",
    )

    assert record["item_id"] == "MLM123"
    assert record["store_name"] == "泽顺墨西哥"
    assert record["weight_g"] == Decimal("800.0")
    assert record["package_length_cm"] == Decimal("30")
    assert record["volumetric_weight_kg"] == Decimal("1.0000")
    assert record["seller_sku"] == "SKU-01"
    assert record["net_proceeds_usd"] == Decimal("14.25")
    assert record["permalink"].startswith("https://")


def test_remote_update_pushes_price_package_and_net_proceeds_then_updates_local(monkeypatch):
    api_calls = []
    local_calls = []

    class FakeClient:
        def __init__(self, token):
            assert token == "token-value"

        def update_global_item(self, item_id, payload):
            api_calls.append((item_id, payload))
            return {}

        def get_marketplace_item(self, item_id, *, attributes=None):
            assert item_id == "MLM3308393921"
            assert "net_proceeds" in attributes
            return {
                "id": item_id,
                "price": 12.5,
                "currency_id": "USD",
                "net_proceeds": {"amount": 10, "currency_id": "USD"},
                "attributes": [
                    {"id": "PACKAGE_WEIGHT", "value_name": "300 g"},
                    {"id": "PACKAGE_LENGTH", "value_name": "30 cm"},
                    {"id": "PACKAGE_WIDTH", "value_name": "20 cm"},
                    {"id": "PACKAGE_HEIGHT", "value_name": "20 cm"},
                ],
            }

    monkeypatch.setattr(bit_store_link_remote_update, "MercadoLibreClient", FakeClient)
    monkeypatch.setattr(
        bit_store_link_remote_update,
        "bulk_update_store_links",
        lambda ids, changes: local_calls.append(("links", ids, dict(changes))) or {"changed": 1},
    )
    monkeypatch.setattr(
        bit_store_link_remote_update,
        "sync_pulled_product_fields_from_store_links",
        lambda ids, fields: local_calls.append(("products", ids, list(fields))) or 1,
    )
    row = {
        "id": 18,
        "token_id": 74,
        "store_name": "测试店铺",
        "item_id": "MLM3308393921",
        "weight_g": 250,
        "package_length_cm": 20,
        "package_width_cm": 20,
        "package_height_cm": 20,
    }
    changes = bit_store_link_remote_update._normalize_changes({
        "price": 12.5,
        "weight_g": 300,
        "package_length_cm": 30,
        "net_proceeds_usd": 10,
    })

    result = bit_store_link_remote_update._update_one_link(
        row, {"id": 74, "access_token": "token-value"}, changes
    )

    assert result["status"] == "success"
    assert api_calls[0] == ("MLM3308393921", {"price": 12.5})
    assert api_calls[2] == ("MLM3308393921", {"net_proceeds": 10})
    package_attributes = api_calls[1][1]["attributes"]
    assert {row["id"] for row in package_attributes} == {
        "PACKAGE_WEIGHT", "PACKAGE_LENGTH", "PACKAGE_WIDTH", "PACKAGE_HEIGHT",
    }
    assert next(row for row in package_attributes if row["id"] == "PACKAGE_WEIGHT")["value_name"] == "300 g"
    assert next(row for row in package_attributes if row["id"] == "PACKAGE_LENGTH")["value_name"] == "30 cm"
    assert local_calls[0][0] == "links"
    assert local_calls[0][2]["net_proceeds_usd"] == Decimal("10")
    assert local_calls[1][0] == "products"


def test_sync_run_records_three_day_clock_for_completed_store(monkeypatch):
    events = []
    token = {"id": 8, "display_name": "自动同步店铺"}
    monkeypatch.setattr(bit_store_link_sync, "get_lock_owner", lambda _key: None)
    monkeypatch.setattr(bit_store_link_sync, "_token_records", lambda _ids: [token])
    monkeypatch.setattr(
        bit_store_link_sync,
        "_sync_store",
        lambda _record: {
            "store": "自动同步店铺",
            "token_id": 8,
            "status": "success",
            "discovered": 12,
            "stored": 12,
            "inserted": 2,
            "updated": 10,
            "details": 12,
            "failed": 0,
            "products": 12,
        },
    )
    monkeypatch.setattr(
        bit_store_link_sync,
        "mark_store_link_sync_started",
        lambda token_id: events.append(("started", token_id)),
    )
    monkeypatch.setattr(
        bit_store_link_sync,
        "mark_store_link_sync_finished",
        lambda token_id, status, error="": events.append(("finished", token_id, status, error)),
    )

    state = bit_store_link_sync.run_store_link_sync([8])

    assert state["status"] == "completed"
    assert events == [("started", 8), ("finished", 8, "success", "")]


def test_store_sync_processes_multiple_stores_in_parallel(monkeypatch):
    records = [
        {"id": token_id, "display_name": f"并行店铺{token_id}"}
        for token_id in range(1, 5)
    ]
    activity_lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_sync(record):
        nonlocal active, max_active
        with activity_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with activity_lock:
            active -= 1
        return {
            "store": record["display_name"],
            "token_id": record["id"],
            "status": "success",
            "discovered": 0,
            "stored": 0,
            "inserted": 0,
            "updated": 0,
            "details": 0,
            "failed": 0,
            "products": 0,
        }

    monkeypatch.setattr(bit_store_link_sync, "STORE_LINK_STORE_WORKERS", 3)
    monkeypatch.setattr(bit_store_link_sync, "get_lock_owner", lambda _key: None)
    monkeypatch.setattr(bit_store_link_sync, "_token_records", lambda _ids: records)
    monkeypatch.setattr(bit_store_link_sync, "_sync_store", fake_sync)
    monkeypatch.setattr(bit_store_link_sync, "mark_store_link_sync_started", lambda _id: None)
    monkeypatch.setattr(
        bit_store_link_sync,
        "mark_store_link_sync_finished",
        lambda _id, _status, _error="": None,
    )

    state = bit_store_link_sync.run_store_link_sync([])

    assert state["status"] == "completed"
    assert state["processed_stores"] == 4
    assert max_active >= 2
    assert any("3 家店铺并行" in line for line in state["logs"])


def test_due_scheduler_starts_only_returned_store_ids(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_store_link_sync,
        "list_due_store_link_token_ids",
        lambda **kwargs: calls.append(("due", kwargs)) or [3, 9],
    )
    monkeypatch.setattr(
        bit_store_link_sync,
        "start_store_link_sync",
        lambda token_ids: calls.append(("start", token_ids)) or (True, {"running": True}),
    )

    result = bit_store_link_sync.start_due_store_link_sync()

    assert result["started"] is True
    assert result["due_token_ids"] == [3, 9]
    assert calls[0][1]["interval_days"] == 3
    assert calls[1] == ("start", [3, 9])


def test_immediate_sync_request_is_persisted_for_new_store():
    batches = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            self.sql = sql

        def executemany(self, sql, rows):
            batches.append((sql, list(rows)))

        def fetchone(self):
            return {"Field": "exists"} if "SHOW COLUMNS" in self.sql else None

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            raise AssertionError("should not roll back")

        def close(self):
            pass

    queued = store.request_store_link_sync([7, 7, 12], connection_factory=Connection)

    assert queued == 2
    assert [row[0] for row in batches[0][1]] == [7, 12]
    assert "`requested_at` = VALUES(`requested_at`)" in batches[0][0]


def test_full_sync_order_prioritizes_never_synced_then_oldest_completed():
    queries = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            self.sql = sql
            queries.append((sql, params))

        def fetchone(self):
            return {"Field": "exists"} if "SHOW COLUMNS" in self.sql else None

        def fetchall(self):
            return [
                {"token_id": 11, "last_completed_at": "2026-08-28 10:00:00"},
                {"token_id": 12, "last_completed_at": None},
                {"token_id": 13, "last_completed_at": "2026-08-20 10:00:00"},
            ]

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def close(self):
            pass

    ordered = store.order_store_link_token_ids_for_full_sync(
        [11, 12, 13, 14], connection_factory=Connection
    )

    assert ordered == [12, 14, 13, 11]
    assert queries[-1][1] == (11, 12, 13, 14)


def test_token_records_applies_priority_only_when_syncing_all(monkeypatch):
    summaries = [{"id": 1}, {"id": 2}, {"id": 3}]
    calls = []
    monkeypatch.setattr(
        bit_store_link_sync.bit_mysql,
        "list_mercado_store_tokens",
        lambda: {"rows": summaries},
    )
    monkeypatch.setattr(
        bit_store_link_sync.bit_mysql,
        "get_mercado_store_token",
        lambda token_id: {"id": token_id, "display_name": f"店铺{token_id}"},
    )
    monkeypatch.setattr(
        bit_store_link_sync,
        "order_store_link_token_ids_for_full_sync",
        lambda token_ids: calls.append(list(token_ids)) or [3, 1, 2],
    )

    all_records = bit_store_link_sync._token_records([])
    selected_records = bit_store_link_sync._token_records([1, 3])

    assert [row["id"] for row in all_records] == [3, 1, 2]
    assert [row["id"] for row in selected_records] == [1, 3]
    assert calls == [[1, 2, 3]]


def test_replace_store_snapshot_marks_missing_links_and_upserts_current_rows():
    calls = []
    batches = []

    class Cursor:
        rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            self.sql = sql
            calls.append((sql, params))

        def executemany(self, sql, rows):
            batches.extend(rows)
            calls.append((sql, None))

        def fetchall(self):
            if "SELECT `item_id`" in self.sql:
                return [{"item_id": "MLM-OLD"}]
            return []

        def fetchone(self):
            if "SHOW COLUMNS" in self.sql:
                return {"Field": "sync_marker"}
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    token = {"id": 2, "display_name": "店铺二", "meli_user_id": "seller-2"}
    result = store.replace_store_snapshot(
        token,
        [
            {"id": "MLM-OLD", "title": "旧商品", "price": 10},
            {"id": "MLM-NEW", "title": "新商品", "price": 20},
        ],
        connection_factory=Connection,
    )

    assert result == {"total": 2, "inserted": 1, "updated": 1}
    assert len(batches) == 2
    assert any("SET `is_current` = 0" in sql for sql, _params in calls)
    assert any("ON DUPLICATE KEY UPDATE" in sql for sql, _params in calls)


def test_bulk_update_store_links_updates_only_allowed_numeric_fields():
    calls = []

    class Cursor:
        rowcount = 2

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            self.sql = sql
            calls.append((sql, params))

        def fetchone(self):
            return {"total": 2}

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    result = store.bulk_update_store_links(
        [4, 5],
        {"price": "12.50", "package_length_cm": "30", "net_proceeds_usd": "-1.25"},
        connection_factory=Connection,
    )

    assert result == {"matched": 2, "changed": 2}
    update_sql, params = next(
        (sql, params) for sql, params in calls if sql.lstrip().startswith("UPDATE")
    )
    assert "`price` = %s" in update_sql
    assert "`package_length_cm` = %s" in update_sql
    assert "`volumetric_weight_kg`" in update_sql
    assert "`net_proceeds_manual` = 1" in update_sql
    assert params[-2:] == (4, 5)


def test_list_store_links_filters_site_and_defaults_to_sales_descending():
    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            self.sql = sql
            calls.append((sql, params))

        def fetchone(self):
            if "SHOW COLUMNS" in self.sql:
                return {"Field": "sync_marker"}
            if "AS `all_count`" in self.sql:
                return {"all_count": 2, "current_count": 2, "store_count": 1}
            return {"total": 2}

        def fetchall(self):
            if "FROM `mercado_store_site_settings`" in self.sql:
                return [{"token_id": 1, "site_id": "MLM", "group_name": "运营一组"}]
            if f"FROM `{store.STORE_LINK_TABLE}`" in self.sql and "LIMIT %s OFFSET %s" in self.sql:
                return [{
                    "id": 1,
                    "token_id": 1,
                    "site_id": "MLM",
                    "sold_quantity": 20,
                    "is_current": 1,
                }]
            if "FROM `mercado_store_tokens` AS tokens" in self.sql:
                return [
                    {"token_id": 1, "store_name": "店铺一", "link_count": 2},
                    {"token_id": 2, "store_name": "新授权店铺", "link_count": 0},
                ]
            if "GROUP BY links.`site_id`" in self.sql:
                return [{"site_id": "MLM", "link_count": 2}]
            return []

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def close(self):
            pass

    result = store.list_store_links(
        site_id="mlm",
        group_name="运营一组",
        management_category_id="12",
        mercado_category="Toys",
        page_size=25,
        connection_factory=Connection,
    )

    list_sql, params = next(
        (sql, params)
        for sql, params in calls
        if f"FROM `{store.STORE_LINK_TABLE}`" in sql and "LIMIT %s OFFSET %s" in sql
    )
    assert "`site_id` = %s" in list_sql
    assert "`remote_json`" not in list_sql
    assert "INNER JOIN (" in list_sql
    assert "links.`token_id` = %s AND links.`site_id` = %s" in list_sql
    assert "categorized_product.`management_category_id` = %s" in list_sql
    assert "links.`category_id` = %s" in list_sql
    assert "mercado_product.`category_name` LIKE %s" in list_sql
    assert "ORDER BY links.`sold_quantity` DESC" in list_sql
    assert params[0] == "MLM"
    assert params[1:5] == (12, "Toys", "Toys", "%Toys%")
    assert params[5:7] == (1, "MLM")
    assert params[-2:] == (1000, 0)
    assert result["page_size"] == 1000
    assert result["rows"][0]["group_name"] == "运营一组"
    assert result["stores"][1] == {
        "token_id": 2,
        "store_name": "新授权店铺",
        "link_count": 0,
        "is_current": False,
    }
    assert result["sites"] == [{"site_id": "MLM", "link_count": 2, "is_current": False}]
    assert result["groups"] == [
        {"group_name": "运营一组"},
        {"group_name": "__ungrouped__"},
    ]


def test_sync_store_writes_listing_batches_incrementally(monkeypatch):
    class FakeClient:
        def request(self, method, path):
            assert method == "GET"
            assert path == "/marketplace/users/seller-8"
            return {"marketplaces": [{"user_id": "seller-8-mlm", "site_id": "MLM"}]}

        def iter_listing_ids(self, seller_id, **filters):
            assert seller_id == "seller-8-mlm"
            if filters.get("status") == "active":
                return iter(["MLM1", "MLM2", "MLM3", "MLM4", "MLM5"])
            return iter([])

        def get_marketplace_item(self, item_id, *, attributes=None):
            assert "title" in attributes
            assert "pictures" in attributes
            assert "attributes" in attributes
            number = int(str(item_id).removeprefix("MLM"))
            return {
                "id": item_id,
                "site_id": "MLM",
                "title": f"测试商品 {number}",
                "thumbnail": f"https://http2.mlstatic.com/D_{number}.jpg",
                "price": 10 + number,
                "currency_id": "USD",
                "status": "active",
                "attributes": [
                    {"id": "PACKAGE_WEIGHT", "value_name": "500 g"},
                    {"id": "PACKAGE_LENGTH", "value_name": "30 cm"},
                    {"id": "PACKAGE_WIDTH", "value_name": "20 cm"},
                    {"id": "PACKAGE_HEIGHT", "value_name": "10 cm"},
                ],
            }

    token = {
        "id": 8,
        "display_name": "泽顺店铺",
        "meli_user_id": "seller-8",
        "access_token": "secret",
    }
    captured = {"batches": [], "finalized": []}
    monkeypatch.setattr(bit_store_link_sync, "_client_and_token", lambda record: (FakeClient(), record))
    monkeypatch.setattr(bit_store_link_sync, "STORE_LINK_WRITE_BATCH_SIZE", 2)
    monkeypatch.setattr(
        bit_store_link_sync,
        "replace_store_snapshot",
        lambda record, items, current_item_ids, sync_marker, finalize, synced_at: (
            captured["batches"].append(
                {
                    "record": record,
                    "items": list(items),
                    "current_item_ids": list(current_item_ids),
                    "sync_marker": sync_marker,
                    "finalize": finalize,
                    "synced_at": synced_at,
                }
            )
            or {
                "total": len(captured["batches"][-1]["items"]),
                "inserted": len(captured["batches"][-1]["items"]),
                "updated": 0,
            }
        ),
    )
    monkeypatch.setattr(
        bit_store_link_sync,
        "finalize_store_snapshot",
        lambda token_id, marker: captured["finalized"].append((token_id, marker)) or 0,
    )
    monkeypatch.setattr(
        bit_store_link_sync,
        "upsert_pulled_store_links_to_products",
        lambda record, items: {"count": len(list(items)), "skipped": 0},
    )

    result = bit_store_link_sync._sync_store(token)

    assert result["discovered"] == 5
    assert result["inserted"] == 5
    assert result["updated"] == 0
    assert result["details"] == 5
    assert result["failed"] == 0
    assert result["products"] == 5
    assert bit_store_link_sync._sync_state["discovered_count"] == 5
    assert bit_store_link_sync._sync_state["detail_count"] == 5
    assert bit_store_link_sync._sync_state["product_count"] == 5
    assert any("开始扫描 MLM · active" in line for line in bit_store_link_sync._sync_state["logs"])
    assert any("批次完成" in line for line in bit_store_link_sync._sync_state["logs"])
    assert [len(batch["items"]) for batch in captured["batches"]] == [2, 2, 1]
    assert all(batch["finalize"] is False for batch in captured["batches"])
    assert len({batch["sync_marker"] for batch in captured["batches"]}) == 1
    assert captured["finalized"] == [(8, captured["batches"][0]["sync_marker"])]
    assert captured["batches"][0]["items"][0]["permalink"].endswith("/MLM-1-_JM")
    assert captured["batches"][0]["items"][0]["title"] == "测试商品 1"
    assert captured["batches"][0]["items"][0]["price"] == 11
    assert captured["batches"][0]["items"][0]["attributes"][0]["id"] == "PACKAGE_WEIGHT"


def test_workbench_store_link_ui_and_routes():
    client = _client()
    response = client.get("/")
    assert response.status_code == 200
    assert b'data-tab="store-links"' in response.data
    assert b'id="store-link-sync-all-button"' in response.data
    assert b'id="store-link-sync-button"' in response.data
    assert b'id="store-link-group-filter"' in response.data
    assert b'id="store-link-site-filter"' in response.data
    assert b'id="store-link-product-category-filter"' in response.data
    assert b'id="store-link-mercado-category-filter"' in response.data
    assert b'id="store-link-page-buttons"' in response.data
    assert b'id="store-link-sales-sort"' in response.data
    assert b'id="store-link-sync-log"' in response.data
    assert "同步所有店铺链接".encode("utf-8") in response.data
    assert "同步当前店铺".encode("utf-8") in response.data
    assert "销量从高到低".encode("utf-8") in response.data
    assert "产品分类".encode("utf-8") in response.data
    assert "美客多分类".encode("utf-8") in response.data
    assert "净收益(USD)".encode("utf-8") in response.data
    assert "任务执行日志".encode("utf-8") in response.data
    assert "修改美客多后台".encode("utf-8") in response.data
    assert "美客多后台修改日志".encode("utf-8") in response.data
    assert "每 3 天自动同步链接状态".encode("utf-8") in response.data
    assert "每页 1,000 条".encode("utf-8") in response.data

    listing_data = {
        "rows": [{"id": 1, "item_id": "MLM1"}],
        "stores": [],
        "sites": [{"site_id": "MLM", "link_count": 1}],
        "groups": [{"group_name": "运营一组", "link_count": 1}],
        "summary": {},
        "total": 1,
        "page": 1,
        "pages": 1,
        "page_size": 1000,
    }
    with patch.object(workbench.bit_db_api, "list_mercado_store_links", return_value=listing_data) as listing:
        response = client.get(
            "/api/store-links?search=MLM1&site_id=MLM&group_name="
            "%E8%BF%90%E8%90%A5%E4%B8%80%E7%BB%84&management_category_id=12"
            "&mercado_category=MLM123&sales_sort=asc&page_size=10"
        )
    assert response.status_code == 200
    assert response.get_json()["data"]["rows"][0]["item_id"] == "MLM1"
    assert response.get_json()["data"]["sites"][0]["site_id"] == "MLM"
    assert listing.call_args.kwargs["site_id"] == "MLM"
    assert listing.call_args.kwargs["group_name"] == "运营一组"
    assert listing.call_args.kwargs["management_category_id"] == "12"
    assert listing.call_args.kwargs["mercado_category"] == "MLM123"
    assert listing.call_args.kwargs["sales_sort"] == "asc"
    assert listing.call_args.kwargs["page_size"] == 1000

    with patch.object(
        workbench.bit_db_api,
        "bulk_update_mercado_store_links",
        return_value={
            "started": True,
            "state": {"running": True, "task_id": "remote-update-1", "total_links": 2},
        },
    ) as update:
        response = client.post(
            "/api/store-links/bulk-update",
            json={"link_ids": [1, 2], "price": 9.9, "weight_g": 500},
        )
    assert response.status_code == 202
    assert response.get_json()["data"]["running"] is True
    update.assert_called_once_with([1, 2], price=9.9, weight_g=500)

    with patch.object(
        workbench.bit_db_api,
        "get_mercado_store_link_remote_update_status",
        return_value={"running": False, "status": "completed", "success_count": 2},
    ):
        response = client.get("/api/store-links/bulk-update/status")
    assert response.status_code == 200
    assert response.get_json()["data"]["success_count"] == 2


def test_start_store_link_sync_route_starts_background_task():
    client = _client()
    with patch.object(
        workbench.bit_db_api,
        "start_store_link_sync",
        return_value={"started": True, "state": {"running": True, "task_id": "task-1"}},
    ) as start:
        response = client.post("/api/store-links/sync/start", json={"token_ids": [7]})

    assert response.status_code == 202
    assert response.get_json()["data"]["running"] is True
    start.assert_called_once_with([7])


def test_start_all_store_link_sync_ignores_selected_store_ids():
    client = _client()
    with patch.object(
        workbench.bit_db_api,
        "start_store_link_sync",
        return_value={"started": True, "state": {"running": True, "task_id": "task-all"}},
    ) as start:
        response = client.post(
            "/api/store-links/sync/start",
            json={"sync_all": True, "token_ids": [7]},
        )

    assert response.status_code == 202
    start.assert_called_once_with([])
