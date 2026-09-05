from datetime import datetime, timedelta, timezone
import threading

import pytest

from bit import bit_mysql, bit_order_sync


@pytest.fixture(autouse=True)
def reset_order_sync_state(monkeypatch):
    monkeypatch.setattr(bit_order_sync, "get_lock_owner", lambda _key: None)
    bit_order_sync._recent_sync_due_event.clear()
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
            daily_status_run_date="",
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


def test_manual_datetime_range_uses_selected_china_times():
    start_text, end_text, start_at, end_at = bit_order_sync._date_range(
        "2026-08-29T08:30", "2026-08-29T09:45"
    )

    assert start_text == "2026-08-29T08:30"
    assert end_text == "2026-08-29T09:45"
    assert bit_order_sync._iso_millis(start_at) == "2026-08-29T00:30:00.000Z"
    assert bit_order_sync._iso_millis(end_at) == "2026-08-29T01:46:00.000Z"


def test_manual_sync_passes_selected_datetime_range_to_mercado(monkeypatch):
    token = {
        "id": 3,
        "display_name": "泽顺巴西",
        "meli_user_id": "seller-3",
        "access_token": "secret",
    }
    filters_seen = {}
    monkeypatch.setattr(bit_order_sync, "_token_records", lambda _ids: [token])

    def fake_sync(_record, filters, **_options):
        filters_seen.update(filters)
        return {
            "store": "泽顺巴西",
            "status": "success",
            "fetched": 0,
            "inserted": 0,
            "updated": 0,
        }

    monkeypatch.setattr(bit_order_sync, "_sync_store", fake_sync)

    state = bit_order_sync.run_order_sync(
        "2026-08-29T08:30",
        "2026-08-29T09:45",
        token_ids=[3],
        mode="manual",
    )

    assert filters_seen == {
        "sort": "date_asc",
        "order.date_created.from": "2026-08-29T00:30:00.000Z",
        "order.date_created.to": "2026-08-29T01:46:00.000Z",
    }
    assert state["start_date"] == "2026-08-29T08:30"
    assert state["end_date"] == "2026-08-29T09:45"
    assert any("08:30 至 2026-08-29T09:45" in row for row in state["logs"])


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


def test_sync_uses_picture_assigned_to_purchased_variation():
    orders = [
        {
            "id": "101",
            "order_items": [
                {
                    "item": {
                        "id": "MLB-1",
                        "title": "鞋子",
                        "variation_id": "202",
                    }
                }
            ],
        }
    ]

    class Client:
        def get_marketplace_item(self, item_id):
            assert item_id == "MLB-1"
            return {
                "pictures": [
                    {"id": "main", "secure_url": "https://img.example/main-O.jpg"},
                    {"id": "white-33", "secure_url": "https://img.example/white-33-O.jpg"},
                ],
                "variations": [
                    {"id": 201, "picture_ids": ["main"]},
                    {"id": 202, "picture_ids": ["white-33"]},
                ],
            }

    bit_order_sync._enrich_order_images(Client(), orders)

    product = orders[0]["order_items"][0]["item"]
    assert product["sku_image_url"] == "https://img.example/white-33-O.jpg"
    assert product["secure_thumbnail"] == "https://img.example/white-33-O.jpg"


def test_mysql_builds_sku_items_with_variation_and_sku_image():
    items = bit_mysql._mercado_order_sku_items(
        {
            "order_items": [
                {
                    "quantity": 2,
                    "item": {
                        "id": "MLB-1",
                        "title": "鞋子",
                        "seller_sku": "SHOE-WHITE-33",
                        "variation_id": 202,
                        "sku_image_url": "https://img.example/white-33-O.jpg",
                        "variation_attributes": [
                            {"name": "颜色", "value_name": "白色"},
                            {"name": "尺码", "value_name": "33"},
                        ],
                    },
                }
            ]
        }
    )

    assert items == [
        {
            "product_id": "MLB-1",
            "seller_sku": "SHOE-WHITE-33",
            "title": "鞋子",
            "variation_id": "202",
            "variation": "颜色: 白色 · 尺码: 33",
            "quantity": 2,
            "image_url": "https://img.example/white-33-O.jpg",
            "product_url": "https://produto.mercadolivre.com.br/MLB-1-_JM",
        }
    ]


def test_mysql_uses_store_link_assets_when_order_image_is_missing():
    items = bit_mysql._mercado_order_sku_items(
        {
            "order_items": [
                {
                    "quantity": 1,
                    "item": {"id": "MLM123456", "title": "测试商品"},
                }
            ]
        },
        product_assets={
            "MLM123456": {
                "site_id": "MLM",
                "thumbnail_url": "http://http2.mlstatic.com/test.jpg",
                "permalink": "http://articulo.mercadolibre.com.mx/MLM-123456-test-_JM",
            }
        },
    )

    assert items[0]["image_url"] == "https://http2.mlstatic.com/test.jpg"
    assert items[0]["product_url"] == (
        "https://articulo.mercadolibre.com.mx/MLM-123456-test-_JM"
    )


def test_order_weight_quote_sums_sku_quantities_and_matches_official_rate():
    matched = {}

    def rate_matcher(**quote):
        matched.update(quote)
        return {
            "shipping_amount_usd": "12.34",
            "rate_kind": "above_threshold",
            "price_label": "MXN 299 及以上",
            "weight_label": "1.0 - 1.5 kg",
            "source_url": "https://global-selling.mercadolibre.com/help/41817",
            "refreshed_at": datetime(2026, 8, 31, 10, 0),
        }

    result = bit_mysql._build_mercado_order_weight_quote(
        [
            {
                "order_id": "101",
                "token_id": 7,
                "site_id": "MLM",
                "total_amount": "200",
                "raw_json": {
                    "order_items": [{
                        "item": {"id": "MLM-A", "seller_sku": "SKU-A", "title": "A"},
                        "quantity": 2,
                    }],
                },
            },
            {
                "order_id": "102",
                "token_id": 7,
                "site_id": "MLM",
                "total_amount": "150",
                "raw_json": {
                    "order_items": [{
                        "item": {"id": "MLM-B", "seller_sku": "SKU-B", "title": "B"},
                        "quantity": 1,
                    }],
                },
            },
        ],
        {
            ("7", "MLM-A"): {"weight_g": "300", "volumetric_weight_kg": "0.4"},
            ("7", "MLM-B"): {"weight_g": "500", "volumetric_weight_kg": "0.2"},
        },
        rate_matcher=rate_matcher,
    )

    assert result["weight_complete"] is True
    assert result["actual_weight_g"] == 1100
    assert result["volumetric_weight_g"] == 1000
    assert result["billable_weight_g"] == 1100
    assert result["shipping_amount_usd"] == 12.34
    assert result["rate_weight_label"] == "1.0 - 1.5 kg"
    assert matched == {
        "site_id": "MLM",
        "price_local": 350.0,
        "billable_weight_g": 1100.0,
        "free_shipping": True,
    }


def test_order_weight_quote_does_not_estimate_when_a_sku_weight_is_missing():
    called = []
    result = bit_mysql._build_mercado_order_weight_quote(
        [{
            "order_id": "101",
            "token_id": 7,
            "site_id": "MLM",
            "total_amount": "200",
            "raw_json": {
                "order_items": [{
                    "item": {"id": "MLM-A", "seller_sku": "SKU-A"},
                    "quantity": 1,
                }],
            },
        }],
        {},
        rate_matcher=lambda **quote: called.append(quote),
    )

    assert result["weight_complete"] is False
    assert result["billable_weight_g"] is None
    assert result["shipping_amount_usd"] is None
    assert result["missing_skus"] == ["SKU-A"]
    assert called == []


def test_preloaded_official_rate_rows_match_actual_weight_band():
    rows = [
        {
            "site_id": "MLM", "rate_kind": "above_threshold",
            "price_min_local": 299, "price_max_local": None,
            "weight_min_g": 0, "weight_max_g": 300,
            "shipping_amount_usd": 4.6,
        },
        {
            "site_id": "MLM", "rate_kind": "above_threshold",
            "price_min_local": 299, "price_max_local": None,
            "weight_min_g": 300, "weight_max_g": 500,
            "shipping_amount_usd": 5.8,
        },
        {
            "site_id": "MLM", "rate_kind": "below_threshold",
            "price_min_local": 0, "price_max_local": 299,
            "weight_min_g": 0, "weight_max_g": 300,
            "shipping_amount_usd": 6.1,
        },
    ]

    matched = bit_mysql._match_mercado_official_rate_rows(
        rows,
        site_id="MLM",
        price_local=350,
        billable_weight_g=250,
    )

    assert matched["shipping_amount_usd"] == 4.6


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


def test_daily_status_sync_uses_incremental_window(monkeypatch):
    token = {
        "id": 3,
        "display_name": "泽顺巴西",
        "meli_user_id": "seller-3",
        "access_token": "secret",
    }
    cutoff_seen = {}
    monkeypatch.setattr(bit_order_sync, "_token_records", lambda _ids: [token])
    bootstrap_from = datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        bit_order_sync,
        "_daily_status_bootstrap_from",
        lambda _now: bootstrap_from,
    )

    def fake_sync(record, cutoff, **context):
        cutoff_seen["record"] = record
        cutoff_seen["cutoff"] = cutoff
        cutoff_seen["context"] = context
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
    assert cutoff_seen["context"]["default_from"] == bootstrap_from
    assert cutoff_seen["context"]["window_to"].tzinfo == timezone.utc
    assert cutoff_seen["context"]["run_date"]


def test_daily_status_yields_before_store_when_recent_sync_is_due(monkeypatch):
    token = {
        "id": 3,
        "display_name": "泽顺巴西",
        "meli_user_id": "seller-3",
        "access_token": "secret",
    }
    monkeypatch.setattr(bit_order_sync, "_token_records", lambda _ids: [token])
    monkeypatch.setattr(
        bit_order_sync,
        "_daily_status_bootstrap_from",
        lambda now: now - timedelta(days=1),
    )
    monkeypatch.setattr(
        bit_order_sync,
        "_sync_old_store_statuses",
        lambda *_args, **_kwargs: pytest.fail("最近任务到期后不应再启动下一家店"),
    )
    bit_order_sync._recent_sync_due_event.set()

    state = bit_order_sync.run_order_sync(mode=bit_order_sync.DAILY_STATUS_MODE)

    assert state["status"] == "paused"
    assert state["running"] is False
    assert state["processed_stores"] == 0
    assert "十五分钟任务" in state["message"]


def test_scheduler_requests_daily_task_to_yield_when_recent_sync_is_due(monkeypatch):
    class StopAfterOneTick:
        @staticmethod
        def is_set():
            return False

        @staticmethod
        def wait(_seconds):
            return True

    clock = iter([100.0, 161.0, 161.0])
    today = datetime.now(bit_order_sync.WORKBENCH_LOCAL_TIMEZONE).date().isoformat()
    monkeypatch.setattr(bit_order_sync, "_scheduler_stop_event", StopAfterOneTick())
    monkeypatch.setattr(bit_order_sync.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "get_mercado_order_sync_schedule_value",
        lambda _key: today,
    )
    monkeypatch.setattr(
        bit_order_sync,
        "get_lock_owner",
        lambda _key: {"metadata": {"mode": bit_order_sync.DAILY_STATUS_MODE}},
    )

    bit_order_sync._scheduler_loop(60)

    assert bit_order_sync._recent_sync_due_event.is_set()


def test_daily_background_records_completed_run_from_shared_state(monkeypatch):
    saved = []

    class FakeLock:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def acquire(timeout=0):
            assert timeout == 0
            return True

        @staticmethod
        def release():
            pass

    def fake_run(*_args, **_kwargs):
        with bit_order_sync._state_guard:
            bit_order_sync._sync_state.update(
                status="completed",
                daily_status_run_date="2026-08-31",
            )
        # The old implementation trusted this return value.  In production it
        # can still say "running" because the inter-process lock is held here.
        return {"status": "running"}

    monkeypatch.setattr(bit_order_sync, "InterProcessLock", FakeLock)
    monkeypatch.setattr(bit_order_sync, "run_order_sync", fake_run)
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "set_mercado_order_sync_schedule_value",
        lambda key, value: saved.append((key, value)),
    )

    bit_order_sync._run_background(
        "", "", None, bit_order_sync.DAILY_STATUS_MODE
    )

    assert saved == [
        (bit_order_sync.DAILY_STATUS_STATE_KEY, "2026-08-31")
    ]
    assert bit_order_sync._sync_state["daily_status_last_run_date"] == "2026-08-31"


def test_old_status_sync_uses_last_updated_checkpoint_and_parallel_details(monkeypatch):
    token = {
        "id": 8,
        "display_name": "泽顺墨西哥",
        "meli_user_id": "seller-8",
        "access_token": "secret",
    }
    cutoff = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    requested = []
    batches = []
    filters_seen = {}
    checkpoints = []
    completed = []
    barrier = threading.Barrier(2)

    class FakeClient:
        def search_order_ids_page(self, seller_id, *, offset, limit, **filters):
            assert seller_id == "seller-8"
            assert offset == 0
            assert limit == 50
            filters_seen.update(filters)
            return {
                "order_ids": ["101", "102"],
                "result_count": 2,
                "next_offset": 2,
                "total": 2,
            }

        def get_order(self, order_id):
            requested.append(order_id)
            barrier.wait(timeout=2)
            return {"id": order_id, "status": "delivered", "order_items": []}

    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "begin_mercado_order_status_window",
        lambda token_id, run_date, default_from, window_to: {
            "token_id": token_id,
            "run_date": run_date,
            "completed_for_run": 0,
            "window_from": datetime(2026, 8, 29, 0, 0),
            "window_to": datetime(2026, 8, 30, 0, 0),
            "next_offset": 0,
            "checked_count": 0,
            "updated_count": 0,
            "failed_count": 0,
        },
    )
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "checkpoint_mercado_order_status_window",
        lambda *args: checkpoints.append(args),
    )
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "complete_mercado_order_status_window",
        lambda token_id: completed.append(token_id),
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
    monkeypatch.setattr(
        bit_order_sync,
        "_sync_order_financials",
        lambda *_args, **_kwargs: pytest.fail("状态刷新不应重复计算运费"),
    )

    result = bit_order_sync._sync_old_store_statuses(
        token,
        cutoff,
        run_date="2026-08-30",
        default_from=datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc),
        window_to=datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc),
    )

    assert sorted(requested) == ["101", "102"]
    assert [order["id"] for order in batches[0][1]] == ["101", "102"]
    assert filters_seen["last_updated.from"].startswith("2026-08-29T00:00:00")
    assert filters_seen["last_updated.to"].startswith("2026-08-30T00:00:00")
    assert filters_seen["date_created.to"].startswith("2026-08-24T08:00:00")
    assert checkpoints[-1][0:2] == (8, 2)
    assert completed == [8]
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
    assert str(row[22]) == "2.4"
    assert row[31] == "MXN"
    assert row[32] == ""
    assert "status_label" in upsert_sql[0]
    assert "amount_currency_id" in upsert_sql[0]
    assert "workflow_status" not in upsert_sql[0]


def test_mysql_upsert_uses_top_level_shipping_cost(monkeypatch):
    captured_rows = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql, _params=None):
            pass

        def executemany(self, _sql, rows):
            captured_rows.extend(rows)

        def fetchall(self):
            return []

        def fetchone(self):
            return {"Field": "exists"}

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

    bit_mysql.upsert_mercado_synced_orders(
        {"id": 4, "display_name": "巴西店", "meli_user_id": "seller-4"},
        [
            {
                "id": "40001",
                "site_id": "MLB",
                "currency_id": "USD",
                "total_amount": 58.32,
                "paid_amount": 10.86,
                "shipping_cost": 31.99,
                "shipping": {"id": "shipment-4", "cost": 999},
                "order_items": [],
            }
        ],
    )

    row = captured_rows[0]
    assert str(row[24]) == "31.99"


def test_shipment_sender_cost_sums_all_seller_parts():
    assert bit_order_sync._shipment_sender_cost(
        {"senders": [{"cost": 6.71}, {"cost": "1.29"}]}
    ) == bit_order_sync.Decimal("8.00")


def test_sync_order_financials_reads_official_shipment_cost(monkeypatch):
    saved_entries = []

    class Client:
        def get_shipment_costs(self, shipment_id):
            assert shipment_id == "shipment-9"
            return {
                "currency_id": "USD",
                "senders": [{"cost": 6.71}],
                "receivers": [{"cost": 0}],
            }

    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "list_mercado_shipment_cost_cache",
        lambda _ids: {},
    )
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "save_mercado_shipment_costs",
        lambda token_id, entries: saved_entries.extend(entries)
        or {"shipments": len(entries), "orders": 1},
    )

    result = bit_order_sync._sync_order_financials(
        Client(),
        {"id": 9},
        [{"shipping": {"id": "shipment-9"}}],
    )

    assert result == {"shipments": 1, "orders": 1, "failed": 0}
    assert saved_entries[0]["currency_id"] == "USD"
    assert saved_entries[0]["seller_cost"] == bit_order_sync.Decimal("6.71")


def test_daily_financial_sync_bypasses_fresh_shipment_cache(monkeypatch):
    saved_entries = []
    calls = []

    class Client:
        def get_shipment_costs(self, shipment_id):
            calls.append(shipment_id)
            return {
                "currency_id": "USD",
                "senders": [{"cost": 8.25}],
            }

    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "list_mercado_shipment_cost_cache",
        lambda _ids: {
            "shipment-9": {
                "shipping_id": "shipment-9",
                "seller_cost": 6.71,
                "currency_id": "USD",
                "payload_json": "{}",
                "checked_at": datetime.now(),
                "last_error": "",
            }
        },
    )
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "save_mercado_shipment_costs",
        lambda token_id, entries: saved_entries.extend(entries)
        or {"shipments": len(entries), "orders": 1},
    )

    result = bit_order_sync._sync_order_financials(
        Client(),
        {"id": 9},
        [{"shipping": {"id": "shipment-9"}}],
        force_refresh=True,
    )

    assert result["failed"] == 0
    assert calls == ["shipment-9"]
    assert saved_entries[0]["seller_cost"] == bit_order_sync.Decimal("8.25")


def test_historical_financial_backfill_propagates_interpreter_shutdown(monkeypatch):
    shutdown_error = RuntimeError(
        "cannot schedule new futures after interpreter shutdown"
    )
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "list_mercado_pending_shipment_cost_rows",
        lambda limit=200: [{"token_id": 25, "shipping_id": "shipment-25"}],
    )
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "get_mercado_store_token",
        lambda token_id: {"id": token_id, "access_token": "secret"},
    )
    monkeypatch.setattr(
        bit_order_sync,
        "_client_and_token",
        lambda record: (object(), record),
    )
    monkeypatch.setattr(
        bit_order_sync,
        "_sync_order_financials",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(shutdown_error),
    )

    with pytest.raises(RuntimeError, match="interpreter shutdown"):
        bit_order_sync.backfill_order_financials()


def test_financial_backfill_loop_stops_during_interpreter_shutdown(monkeypatch):
    shutdown_error = RuntimeError(
        "cannot schedule new futures after interpreter shutdown"
    )

    class Lock:
        released = False

        def acquire(self, timeout=0):
            return True

        def release(self):
            self.released = True

    lock = Lock()
    monkeypatch.setattr(bit_order_sync, "InterProcessLock", lambda *_a, **_k: lock)
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "backfill_mercado_order_sale_fees",
        lambda: {"updated": 0},
    )
    monkeypatch.setattr(
        bit_order_sync,
        "backfill_order_financials",
        lambda limit=200: (_ for _ in ()).throw(shutdown_error),
    )
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "refresh_mercado_order_quoted_freight",
        lambda limit=200: pytest.fail("退出期间不应继续补算标价运费"),
    )
    monkeypatch.setattr(bit_order_sync, "_financial_backfill_stop_event", threading.Event())

    bit_order_sync._financial_backfill_loop()

    assert lock.released is True


def test_historical_image_backfill_saves_purchased_variation(monkeypatch):
    saved_entries = []
    raw_order = {
        "id": "101",
        "order_items": [{
            "item": {"id": "MLM-1", "variation_id": "202"},
            "quantity": 1,
        }],
    }

    class Client:
        def get_marketplace_item(self, item_id):
            assert item_id == "MLM-1"
            return {
                "pictures": [
                    {"id": "main", "secure_url": "https://img.example/main-O.jpg"},
                    {"id": "blue", "secure_url": "https://img.example/blue-O.jpg"},
                ],
                "variations": [{"id": 202, "picture_ids": ["blue"]}],
            }

    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "list_mercado_pending_order_image_rows",
        lambda limit=50: [{
            "order_id": "101", "token_id": 9,
            "product_id": "MLM-1", "raw_json": raw_order,
        }],
    )
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "get_mercado_store_token",
        lambda token_id: {"id": token_id, "access_token": "secret"},
    )
    monkeypatch.setattr(
        bit_order_sync,
        "_client_and_token",
        lambda record: (Client(), record),
    )
    monkeypatch.setattr(
        bit_order_sync.bit_mysql,
        "save_mercado_order_image_results",
        lambda entries: saved_entries.extend(entries)
        or {"checked": 1, "updated": 1, "failed": 0},
    )

    result = bit_order_sync.backfill_order_sku_images(limit=50)

    assert result == {"requested": 1, "checked": 1, "updated": 1, "failed": 0}
    assert saved_entries[0]["image_url"] == "https://img.example/blue-O.jpg"
    product = saved_entries[0]["raw_order"]["order_items"][0]["item"]
    assert product["sku_image_url"] == "https://img.example/blue-O.jpg"


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
