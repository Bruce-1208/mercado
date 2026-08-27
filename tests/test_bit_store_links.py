from decimal import Decimal
from unittest.mock import patch

import pytest

from bit import bit_store_link_sync
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
            if f"FROM `{store.STORE_LINK_TABLE}`" in self.sql and "LIMIT %s OFFSET %s" in self.sql:
                return [{"id": 1, "site_id": "MLM", "sold_quantity": 20, "is_current": 1}]
            if "FROM `mercado_store_tokens` AS tokens" in self.sql:
                return [
                    {"token_id": 1, "store_name": "店铺一", "link_count": 2},
                    {"token_id": 2, "store_name": "新授权店铺", "link_count": 0},
                ]
            if "GROUP BY `site_id`" in self.sql:
                return [{"site_id": "MLM", "link_count": 2}]
            return []

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def close(self):
            pass

    result = store.list_store_links(site_id="mlm", connection_factory=Connection)

    list_sql, params = next(
        (sql, params)
        for sql, params in calls
        if f"FROM `{store.STORE_LINK_TABLE}`" in sql and "LIMIT %s OFFSET %s" in sql
    )
    assert "`site_id` = %s" in list_sql
    assert "`remote_json`" not in list_sql
    assert "COALESCE(`sold_quantity`, 0) DESC" in list_sql
    assert params[0] == "MLM"
    assert result["page_size"] == 1000
    assert result["stores"][1] == {
        "token_id": 2,
        "store_name": "新授权店铺",
        "link_count": 0,
        "is_current": False,
    }
    assert result["sites"] == [{"site_id": "MLM", "link_count": 2, "is_current": False}]


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
    assert b'id="store-link-site-filter"' in response.data
    assert b'id="store-link-sales-sort"' in response.data
    assert b'id="store-link-sync-log"' in response.data
    assert "同步所有店铺链接".encode("utf-8") in response.data
    assert "同步当前店铺".encode("utf-8") in response.data
    assert "销量从高到低".encode("utf-8") in response.data
    assert "净收益(USD)".encode("utf-8") in response.data
    assert "任务执行日志".encode("utf-8") in response.data
    assert "每页最多 1,000 条".encode("utf-8") in response.data

    listing_data = {
        "rows": [{"id": 1, "item_id": "MLM1"}],
        "stores": [],
        "sites": [{"site_id": "MLM", "link_count": 1}],
        "summary": {},
        "total": 1,
        "page": 1,
        "pages": 1,
        "page_size": 1000,
    }
    with patch.object(workbench.bit_db_api, "list_mercado_store_links", return_value=listing_data) as listing:
        response = client.get("/api/store-links?search=MLM1&site_id=MLM&sales_sort=asc")
    assert response.status_code == 200
    assert response.get_json()["data"]["rows"][0]["item_id"] == "MLM1"
    assert response.get_json()["data"]["sites"][0]["site_id"] == "MLM"
    assert listing.call_args.kwargs["site_id"] == "MLM"
    assert listing.call_args.kwargs["sales_sort"] == "asc"
    assert listing.call_args.kwargs["page_size"] == 1000

    with patch.object(
        workbench.bit_db_api,
        "bulk_update_mercado_store_links",
        return_value={"matched": 2, "changed": 2},
    ) as update:
        response = client.post(
            "/api/store-links/bulk-update",
            json={"link_ids": [1, 2], "price": 9.9, "weight_g": 500},
        )
    assert response.status_code == 200
    update.assert_called_once_with([1, 2], price=9.9, weight_g=500)


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
