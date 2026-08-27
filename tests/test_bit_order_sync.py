from datetime import datetime, timedelta, timezone

import pytest

from bit import bit_mysql, bit_order_sync


@pytest.fixture(autouse=True)
def reset_order_sync_state(monkeypatch):
    monkeypatch.setattr(bit_order_sync, "get_lock_owner", lambda _key: None)
    with bit_order_sync._state_guard:
        bit_order_sync._sync_state.update(
            running=False,
            task_id="",
            mode="idle",
            status="idle",
            message="等待订单同步",
            fetched_count=0,
            inserted_count=0,
            updated_count=0,
            processed_stores=0,
            total_stores=0,
            results=[],
            logs=[],
            recent_window_hours=bit_order_sync.RECENT_ORDER_WINDOW_HOURS,
            daily_status_last_run_date="",
            next_daily_status_at="",
        )
    yield


def test_manual_date_range_includes_entire_end_day():
    _start, _end, start_at, end_at = bit_order_sync._date_range(
        "2026-08-01", "2026-08-23"
    )

    assert bit_order_sync._iso_millis(start_at) == "2026-08-01T00:00:00.000Z"
    assert bit_order_sync._iso_millis(end_at) == "2026-08-24T00:00:00.000Z"


def test_manual_date_range_rejects_reverse_dates():
    with pytest.raises(ValueError, match="截止日期"):
        bit_order_sync._date_range("2026-08-23", "2026-08-01")


def test_sync_store_fetches_every_order_and_upserts(monkeypatch):
    filters_seen = {}

    class FakeClient:
        def iter_order_ids(self, seller_id, **filters):
            filters_seen.update(filters)
            assert seller_id == "seller-7"
            return iter(["101", "102"])

        def get_order(self, order_id):
            return {"id": order_id, "status": "paid", "order_items": []}

    token = {
        "id": 7,
        "display_name": "泽顺墨西哥",
        "meli_user_id": "seller-7",
        "access_token": "secret",
    }
    batches = []
    monkeypatch.setattr(
        bit_order_sync,
        "_client_and_token",
        lambda record: (FakeClient(), record),
    )
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "upsert_mercado_synced_orders",
        lambda record, orders: batches.append((record, list(orders))) or {
            "inserted": 2,
            "updated": 0,
        },
    )

    result = bit_order_sync._sync_store(token, {"order.date_created.from": "from"})

    assert result == {
        "store": "泽顺墨西哥",
        "status": "success",
        "fetched": 2,
        "inserted": 2,
        "updated": 0,
    }
    assert filters_seen["order.date_created.from"] == "from"
    assert [order["id"] for order in batches[0][1]] == ["101", "102"]


def test_sync_enriches_order_sku_image_from_marketplace_item():
    orders = [
        {
            "id": "101",
            "order_items": [{"item": {"id": "MLM-1", "title": "商品一"}}],
        }
    ]

    class Client:
        def get_marketplace_item(self, item_id):
            assert item_id == "MLM-1"
            return {"pictures": [{"secure_url": "https://img.example/sku.jpg"}]}

    bit_order_sync._enrich_order_images(Client(), orders)

    assert orders[0]["order_items"][0]["item"]["secure_thumbnail"] == "https://img.example/sku.jpg"


def test_automatic_sync_refreshes_recent_72_hours(monkeypatch):
    token = {
        "id": 3,
        "display_name": "泽顺巴西",
        "meli_user_id": "seller-3",
        "access_token": "secret",
    }
    filters_seen = {}
    options_seen = {}
    monkeypatch.setattr(bit_order_sync, "_token_records", lambda _ids: [token])

    def fake_sync(_record, filters, **options):
        filters_seen.update(filters)
        options_seen.update(options)
        return {"store": "泽顺巴西", "status": "success", "fetched": 0, "inserted": 0, "updated": 0}

    monkeypatch.setattr(bit_order_sync, "_sync_store", fake_sync)

    state = bit_order_sync.run_order_sync(mode="automatic")

    assert state["status"] == "completed"
    assert filters_seen["sort"] == "date_asc"
    start_at = datetime.fromisoformat(
        filters_seen["order.date_created.from"].replace("Z", "+00:00")
    )
    end_at = datetime.fromisoformat(
        filters_seen["order.date_created.to"].replace("Z", "+00:00")
    )
    assert end_at - start_at == timedelta(hours=72)
    assert "last_updated.from" not in filters_seen
    assert options_seen == {"enrich_images": True}


def test_daily_status_sync_uses_local_old_order_ids(monkeypatch):
    token = {
        "id": 3,
        "display_name": "泽顺巴西",
        "meli_user_id": "seller-3",
        "access_token": "secret",
    }
    cutoff_seen = {}
    monkeypatch.setattr(bit_order_sync, "_token_records", lambda _ids: [token])

    def fake_sync(record, cutoff):
        cutoff_seen["record"] = record
        cutoff_seen["cutoff"] = cutoff
        return {
            "store": "泽顺巴西",
            "status": "success",
            "fetched": 4,
            "inserted": 0,
            "updated": 4,
            "failed": 0,
        }

    monkeypatch.setattr(bit_order_sync, "_sync_old_store_statuses", fake_sync)

    before = datetime.now(timezone.utc) - timedelta(hours=72, seconds=1)
    state = bit_order_sync.run_order_sync(mode=bit_order_sync.DAILY_STATUS_MODE)
    after = datetime.now(timezone.utc) - timedelta(hours=72) + timedelta(seconds=1)

    assert state["status"] == "completed"
    assert state["mode"] == bit_order_sync.DAILY_STATUS_MODE
    assert cutoff_seen["record"] == token
    assert before <= cutoff_seen["cutoff"] <= after


def test_old_status_sync_fetches_only_local_old_orders_and_skips_images(monkeypatch):
    token = {
        "id": 8,
        "display_name": "泽顺墨西哥",
        "meli_user_id": "seller-8",
        "access_token": "secret",
    }
    cutoff = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    requested = []
    batches = []

    class FakeClient:
        def get_order(self, order_id):
            requested.append(order_id)
            return {"id": order_id, "status": "delivered", "order_items": []}

    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "list_mercado_order_ids_before",
        lambda token_id, cutoff_value: ["101", "102"]
        if token_id == 8 and cutoff_value == cutoff
        else [],
    )
    monkeypatch.setattr(
        bit_order_sync,
        "_client_and_token",
        lambda record: (FakeClient(), record),
    )
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "upsert_mercado_synced_orders",
        lambda record, orders: batches.append((record, list(orders)))
        or {"inserted": 0, "updated": len(orders)},
    )
    monkeypatch.setattr(
        bit_order_sync,
        "_enrich_order_images",
        lambda *_args: pytest.fail("老订单状态刷新不应读取 SKU 图片"),
    )

    result = bit_order_sync._sync_old_store_statuses(token, cutoff)

    assert requested == ["101", "102"]
    assert [order["id"] for order in batches[0][1]] == ["101", "102"]
    assert result == {
        "store": "泽顺墨西哥",
        "status": "success",
        "fetched": 2,
        "inserted": 0,
        "updated": 2,
        "failed": 0,
    }


def test_mysql_upsert_maps_token_order_origin_fields(monkeypatch):
    captured_rows = []
    upsert_sql = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, _params=None):
            self.sql = sql

        def executemany(self, sql, rows):
            upsert_sql.append(sql)
            captured_rows.extend(rows)

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()

        def cursor(self):
            return self.cursor_instance

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_kwargs: Connection())
    token = {
        "id": 9,
        "display_name": "泽顺墨西哥",
        "meli_user_id": "seller-9",
        "site_id": "CBT",
    }
    order = {
        "id": 200001,
        "status": "delivered",
        "date_created": "2026-08-23T01:02:03.000Z",
        "last_updated": "2026-08-23T05:06:07.000Z",
        "currency_id": "USD",
        "total_amount": 18.5,
        "context": {"site": "MLM"},
        "buyer": {"id": 5, "nickname": "buyer"},
        "shipping": {"id": 77},
        "order_items": [
            {
                "quantity": 2,
                "unit_price": 9.25,
                "sale_fee": 1.2,
                "item": {"id": "MLM-1", "title": "商品一"},
            }
        ],
    }

    result = bit_mysql.upsert_mercado_synced_orders(token, [order])

    assert result == {"total": 1, "inserted": 1, "updated": 0}
    row = captured_rows[0]
    assert row[0] == "200001"
    assert row[5] == "墨西哥"
    assert row[7] == "交付"
    assert row[19] == "商品一"
    assert row[-1] == "MXN"
    assert "status_label" in upsert_sql[0]
    assert "amount_currency_id" in upsert_sql[0]
    assert "workflow_status" not in upsert_sql[0]


def test_mysql_bulk_update_changes_only_authorized_store_orders(monkeypatch):
    calls = []

    class Cursor:
        rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            self.sql = sql
            calls.append((sql, params))
            if sql.lstrip().startswith("UPDATE"):
                self.rowcount = 2

        def fetchone(self):
            if "SHOW COLUMNS" in self.sql:
                return {"Field": "exists"}
            return None

        def fetchall(self):
            if "SELECT synced.`order_id`" in self.sql:
                return [
                    {
                        "order_id": "20001", "workflow_status": "找货",
                        "purchase_order": None, "purchase_tracking": None,
                        "logistics_company": None, "purchase_cost": None,
                        "purchase_remark": None,
                    },
                    {
                        "order_id": "20002", "workflow_status": "找货",
                        "purchase_order": "CG-OLD", "purchase_tracking": "YT000",
                        "logistics_company": "yuantong", "purchase_cost": "70.00",
                        "purchase_remark": "旧备注",
                    },
                ]
            return []

        def executemany(self, sql, params):
            calls.append((sql, params))

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_kwargs: Connection())

    result = bit_mysql.bulk_update_mercado_orders(
        ["20001", "20002"],
        workflow_status="配货",
        purchase_order="CG-01",
        purchase_tracking="SF123",
        logistics_company="shunfeng",
        purchase_cost="88.5",
        purchase_remark="采购备注",
        operator_id=7,
        operator_name="采购员甲",
    )

    assert result == {"matched": 2, "changed": 2}
    update_sql, update_params = next(
        (sql, params) for sql, params in calls if sql.lstrip().startswith("UPDATE")
    )
    assert "INNER JOIN `mercado_store_tokens`" in update_sql
    assert "workflow_status" in update_sql
    assert "purchase_order" in update_sql
    assert "purchase_tracking" in update_sql
    assert "purchase_cost" in update_sql
    assert "purchase_remark" in update_sql
    assert update_params[0:2] == ["配货", "CG-01"]
    assert update_params[2:4] == ["SF123", "shunfeng"]
    assert str(update_params[4]) == "88.5"
    assert update_params[5] == "采购备注"
    assert update_params[-2:] == ["20001", "20002"]
    log_sql, log_params = next(
        (sql, params) for sql, params in calls if "INSERT INTO `mercado_order_operation_logs`" in sql
    )
    assert "changes_json" in log_sql
    assert len(log_params) == 2
    assert log_params[0][1:5] == ("purchase_created", "新增采购单", 7, "采购员甲")
    assert log_params[1][1:5] == ("purchase_updated", "修改采购单", 7, "采购员甲")
