from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import time
from unittest import mock

from bit import bit_print
from mercado_api.client import MercadoAPIError


def _token(token_id, name):
    return {
        "id": token_id,
        "display_name": name,
        "site_settings": [
            {"site_id": "MLM", "site_name": "墨西哥"},
            {"site_id": "MLB", "site_name": "巴西"},
        ],
    }


def _context(order_id="20001", shipment_id="30001", site_id="MLM"):
    return {
        "order_id": order_id,
        "shipping_id": shipment_id,
        "token_id": 7,
        "shop_name": "店铺甲",
        "site_id": site_id,
        "access_token": "token-value",
        "refresh_token": "refresh-value",
    }


def test_build_print_jobs_uses_api_tokens_and_supports_multiple_stores():
    jobs = bit_print.build_print_jobs(
        [_token(7, "店铺甲"), _token(8, "店铺乙")],
        selected_shops=["店铺甲", "店铺乙"],
        selected_sites=["巴西"],
    )

    assert jobs == [
        {"token_id": 7, "shop_name": "店铺甲", "sites": ["巴西"]},
        {"token_id": 8, "shop_name": "店铺乙", "sites": ["巴西"]},
    ]


def test_build_print_jobs_supports_exact_store_site_targets():
    jobs = bit_print.build_print_jobs(
        [_token(7, "店铺甲"), _token(8, "店铺乙")],
        selected_targets=[
            {"shop_name": "店铺甲", "site": "墨西哥"},
            {"shop_name": "店铺乙", "site": "巴西"},
        ],
    )

    assert jobs == [
        {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥"]},
        {"token_id": 8, "shop_name": "店铺乙", "sites": ["巴西"]},
    ]


def test_build_print_jobs_can_select_automatic_sync_token_ids():
    jobs = bit_print.build_print_jobs(
        [_token(7, "店铺甲"), _token(8, "店铺乙")],
        selected_token_ids=[8],
    )

    assert jobs == [
        {"token_id": 8, "shop_name": "店铺乙", "sites": ["墨西哥", "巴西"]}
    ]


def test_first_scan_falls_back_to_last_72_hours_and_saves_tracking_state(monkeypatch):
    calls = {}

    class Client:
        def iter_order_ids(self, seller_id, **filters):
            calls["seller_id"] = seller_id
            calls["filters"] = filters
            return iter(["20001"])

        def get_order(self, order_id):
            return {"id": order_id}

    record = {
        "id": 7,
        "display_name": "店铺甲",
        "meli_user_id": "seller-7",
        "access_token": "token",
    }
    monkeypatch.setattr(bit_print.bit_mysql, "get_mercado_store_token", lambda _id: record)
    monkeypatch.setattr(bit_print.bit_mysql, "get_mercado_order_print_state", lambda _id: None)
    monkeypatch.setattr(bit_print, "_client_and_record", lambda row: (Client(), row))
    monkeypatch.setattr(
        bit_print.bit_mysql,
        "upsert_mercado_synced_orders",
        lambda _record, orders: {"inserted": len(orders), "updated": 0},
    )
    monkeypatch.setattr(
        bit_print.bit_mysql,
        "save_mercado_order_print_state",
        lambda token_id, tracking_since, last_scan_at: calls.update(
            token_id=token_id,
            tracking_since=tracking_since,
            last_scan_at=last_scan_at,
        ),
    )

    result = bit_print._scan_store_orders(
        {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥"]},
        fallback_hours=72,
        logger=lambda _message: None,
    )

    assert result["first_run"] is True
    assert calls["seller_id"] == "seller-7"
    assert "order.date_created.from" in calls["filters"]
    assert "last_updated.from" not in calls["filters"]
    assert 71.9 <= (
        datetime.now(timezone.utc) - calls["tracking_since"]
    ).total_seconds() / 3600 <= 72.1
    assert calls["token_id"] == 7


def test_scan_skips_one_451_order_and_continues_same_store(monkeypatch):
    calls = {"saved_orders": [], "log": []}

    class Client:
        def iter_order_ids(self, _seller_id, **_filters):
            return iter(["blocked-order", "good-order"])

        def get_order(self, order_id):
            if order_id == "blocked-order":
                raise MercadoAPIError(
                    'GET /marketplace/orders/blocked-order 失败 (451): '
                    '{"message":"user not available for legal reasons","status":451}'
                )
            return {"id": order_id}

    record = {
        "id": 7,
        "display_name": "店铺甲",
        "meli_user_id": "seller-7",
        "access_token": "token",
    }
    monkeypatch.setattr(bit_print.bit_mysql, "get_mercado_store_token", lambda _id: record)
    monkeypatch.setattr(bit_print.bit_mysql, "get_mercado_order_print_state", lambda _id: None)
    monkeypatch.setattr(bit_print, "_client_and_record", lambda row: (Client(), row))
    monkeypatch.setattr(
        bit_print.bit_mysql,
        "upsert_mercado_synced_orders",
        lambda _record, orders: calls["saved_orders"].extend(orders)
        or {"inserted": len(orders), "updated": 0},
    )
    monkeypatch.setattr(
        bit_print.bit_mysql,
        "save_mercado_order_print_state",
        lambda *_args: None,
    )

    result = bit_print._scan_store_orders(
        {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥", "巴西"]},
        fallback_hours=72,
        logger=calls["log"].append,
    )

    assert calls["saved_orders"] == [{"id": "good-order"}]
    assert result["fetched"] == 1
    assert result["legally_unavailable_order_ids"] == ["blocked-order"]
    assert any("已跳过并继续处理" in line for line in calls["log"])


def test_scan_skips_wrapped_451_error_and_continues_same_store(monkeypatch):
    saved = []

    class Client:
        def iter_order_ids(self, _seller_id, **_filters):
            return iter(["blocked-order", "good-order"])

        def get_order(self, order_id):
            if order_id == "blocked-order":
                raise RuntimeError(
                    '代理调用失败：GET /marketplace/orders/blocked-order (451) '
                    '{"message":"Unavailable For Legal Reasons"}'
                )
            return {"id": order_id}

    record = {
        "id": 7,
        "display_name": "店铺甲",
        "meli_user_id": "seller-7",
        "access_token": "token",
    }
    monkeypatch.setattr(bit_print.bit_mysql, "get_mercado_store_token", lambda _id: record)
    monkeypatch.setattr(bit_print.bit_mysql, "get_mercado_order_print_state", lambda _id: None)
    monkeypatch.setattr(bit_print, "_client_and_record", lambda row: (Client(), row))
    monkeypatch.setattr(
        bit_print.bit_mysql,
        "upsert_mercado_synced_orders",
        lambda _record, orders: saved.extend(orders)
        or {"inserted": len(orders), "updated": 0},
    )
    monkeypatch.setattr(
        bit_print.bit_mysql,
        "save_mercado_order_print_state",
        lambda *_args: None,
    )

    result = bit_print._scan_store_orders(
        {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥"]},
        fallback_hours=72,
        logger=lambda _message: None,
    )

    assert saved == [{"id": "good-order"}]
    assert result["legally_unavailable_order_ids"] == ["blocked-order"]


def test_scan_does_not_swallow_non_451_api_errors(monkeypatch):
    class Client:
        def iter_order_ids(self, _seller_id, **_filters):
            return iter(["broken-order"])

        def get_order(self, _order_id):
            raise MercadoAPIError("GET /marketplace/orders/broken-order 失败 (500)")

    record = {
        "id": 7,
        "display_name": "店铺甲",
        "meli_user_id": "seller-7",
        "access_token": "token",
    }
    monkeypatch.setattr(bit_print.bit_mysql, "get_mercado_store_token", lambda _id: record)
    monkeypatch.setattr(bit_print.bit_mysql, "get_mercado_order_print_state", lambda _id: None)
    monkeypatch.setattr(bit_print, "_client_and_record", lambda row: (Client(), row))

    with mock.patch.object(bit_print.bit_mysql, "save_mercado_order_print_state"):
        try:
            bit_print._scan_store_orders(
                {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥"]},
                fallback_hours=72,
                logger=lambda _message: None,
            )
        except MercadoAPIError as exc:
            assert "(500)" in str(exc)
        else:
            raise AssertionError("非 451 API 错误不应被跳过")


def test_subsequent_scan_uses_incremental_api_window(monkeypatch):
    last_scan = datetime.now(timezone.utc) - timedelta(hours=4)
    tracking_since = last_scan - timedelta(days=2)
    calls = {}

    class Client:
        def iter_order_ids(self, _seller_id, **filters):
            calls["filters"] = filters
            return iter(())

    record = {
        "id": 7,
        "display_name": "店铺甲",
        "meli_user_id": "seller-7",
        "access_token": "token",
    }
    monkeypatch.setattr(bit_print.bit_mysql, "get_mercado_store_token", lambda _id: record)
    monkeypatch.setattr(
        bit_print.bit_mysql,
        "get_mercado_order_print_state",
        lambda _id: {"tracking_since": tracking_since, "last_scan_at": last_scan},
    )
    monkeypatch.setattr(bit_print, "_client_and_record", lambda row: (Client(), row))
    monkeypatch.setattr(bit_print.bit_mysql, "save_mercado_order_print_state", mock.Mock())

    result = bit_print._scan_store_orders(
        {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥"]},
        fallback_hours=72,
        logger=lambda _message: None,
    )

    assert result["first_run"] is False
    assert "last_updated.from" in calls["filters"]
    assert "order.date_created.from" not in calls["filters"]


def test_selected_time_range_uses_created_window_and_upper_bound(monkeypatch):
    now = datetime.now(timezone.utc)
    start_at = now - timedelta(hours=8)
    end_at = now - timedelta(hours=2)
    calls = {}

    class Client:
        def iter_order_ids(self, _seller_id, **filters):
            calls["filters"] = filters
            return iter(())

    record = {
        "id": 7,
        "display_name": "店铺甲",
        "meli_user_id": "seller-7",
        "access_token": "token",
    }
    monkeypatch.setattr(bit_print.bit_mysql, "get_mercado_store_token", lambda _id: record)
    monkeypatch.setattr(
        bit_print.bit_mysql,
        "get_mercado_order_print_state",
        lambda _id: {
            "tracking_since": now - timedelta(days=2),
            "last_scan_at": now - timedelta(minutes=10),
        },
    )
    monkeypatch.setattr(bit_print, "_client_and_record", lambda row: (Client(), row))
    monkeypatch.setattr(
        bit_print.bit_mysql,
        "save_mercado_order_print_state",
        lambda _token_id, tracking_since, last_scan_at: calls.update(
            tracking_since=tracking_since,
            last_scan_at=last_scan_at,
        ),
    )

    result = bit_print._scan_store_orders(
        {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥"]},
        fallback_hours=72,
        start_at=start_at.isoformat(),
        end_at=end_at.isoformat(),
        logger=lambda _message: None,
    )

    assert calls["filters"]["order.date_created.from"] == bit_print._iso_millis(start_at)
    assert calls["filters"]["order.date_created.to"] == bit_print._iso_millis(end_at)
    assert "last_updated.from" not in calls["filters"]
    assert result["tracking_since"] == start_at
    assert result["end_at"] == end_at
    assert calls["last_scan_at"] == end_at


def test_shop_job_downloads_only_candidate_orders_and_records_success(monkeypatch):
    candidates = [
        _context("20001", "30001"),
        _context("20002", "30001"),
        _context("20003", "30002"),
    ]
    seen = {"candidate_args": None, "recorded": [], "operator_name": ""}
    monkeypatch.setattr(
        bit_print,
        "_scan_store_orders",
        lambda *_args, **_kwargs: {
            "first_run": False,
            "tracking_since": datetime.now(timezone.utc) - timedelta(days=1),
        },
    )

    def list_candidates(token_id, **kwargs):
        seen["candidate_args"] = (token_id, kwargs)
        return candidates

    monkeypatch.setattr(bit_print.bit_mysql, "list_mercado_order_print_candidates", list_candidates)
    monkeypatch.setattr(
        bit_print,
        "_download_label",
        lambda context, **_kwargs: (
            context["shipping_id"],
            b"%PDF-1.4\nlabel\n%%EOF",
            1,
        ),
    )
    monkeypatch.setattr(
        bit_print,
        "_record_printed_orders",
        lambda order_ids, operator_name="": (
            seen["recorded"].extend(order_ids),
            seen.update(operator_name=operator_name),
            len(order_ids),
        )[-1],
    )
    documents = []
    printed_orders = []

    rows = bit_print._run_shop_job(
        {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥"]},
        logger=lambda _message: None,
        document_sink=documents,
        printed_order_sink=printed_orders,
    )

    assert rows[0]["status"] == "printed"
    assert rows[0]["selected_count"] == 3
    assert rows[0]["shipment_count"] == 2
    assert seen["candidate_args"][1]["include_previously_printed"] is False
    assert seen["recorded"] == ["20001", "20002", "20003"]
    assert seen["operator_name"] == "订单打印/API"
    assert len(documents) == 2


def test_automatic_shop_job_uses_activation_floor_and_system_operator(monkeypatch):
    scan_start = datetime(2026, 9, 5, 8, 30, tzinfo=timezone.utc)
    activation_start = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)
    seen = {}
    monkeypatch.setattr(
        bit_print,
        "_scan_store_orders",
        lambda *_args, **_kwargs: {
            "first_run": True,
            "tracking_since": scan_start,
            "end_at": datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
        },
    )

    def list_candidates(_token_id, **kwargs):
        seen["candidate_args"] = kwargs
        return [_context("20001", "30001")]

    monkeypatch.setattr(
        bit_print.bit_mysql,
        "list_mercado_order_print_candidates",
        list_candidates,
    )
    monkeypatch.setattr(
        bit_print,
        "_download_label",
        lambda context, **_kwargs: (
            context["shipping_id"],
            b"%PDF-1.4\nlabel\n%%EOF",
            1,
        ),
    )
    monkeypatch.setattr(
        bit_print,
        "_record_printed_orders",
        lambda order_ids, operator_name="": seen.update(
            order_ids=list(order_ids),
            operator_name=operator_name,
        ) or len(order_ids),
    )

    rows = bit_print._run_shop_job(
        {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥"]},
        candidate_start_at=activation_start,
        include_first_run_backlog=False,
        operator_name="系统自动打印",
    )

    assert rows[0]["status"] == "printed"
    assert seen["candidate_args"]["tracking_since"] == activation_start
    assert seen["candidate_args"]["include_previously_printed"] is False
    assert seen["order_ids"] == ["20001"]
    assert seen["operator_name"] == "系统自动打印"
    assert "系统自动打印" in bit_print._task_record(
        rows[0], operator_name="系统自动打印"
    )[3]


def test_shop_job_does_not_expand_skipped_451_order_into_site_failures(monkeypatch):
    monkeypatch.setattr(
        bit_print,
        "_scan_store_orders",
        lambda *_args, **_kwargs: {
            "first_run": False,
            "tracking_since": datetime.now(timezone.utc) - timedelta(days=1),
            "end_at": datetime.now(timezone.utc),
            "legally_unavailable_order_ids": ["blocked-order"],
        },
    )
    monkeypatch.setattr(
        bit_print.bit_mysql,
        "list_mercado_order_print_candidates",
        lambda *_args, **_kwargs: [],
    )

    rows = bit_print._run_shop_job(
        {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥", "巴西"]},
        logger=lambda _message: None,
    )

    assert [row["status"] for row in rows] == ["no_orders", "no_orders"]
    assert all("451 受限订单" in row["message"] for row in rows)
    assert not any(row["status"] == "failed" for row in rows)


def test_shop_job_skips_terminal_shipment_without_retrying_forever(monkeypatch):
    candidate = _context("20001", "30001")
    monkeypatch.setattr(
        bit_print,
        "_scan_store_orders",
        lambda *_args, **_kwargs: {
            "first_run": False,
            "tracking_since": datetime.now(timezone.utc) - timedelta(days=1),
            "end_at": datetime.now(timezone.utc),
        },
    )
    monkeypatch.setattr(
        bit_print.bit_mysql,
        "list_mercado_order_print_candidates",
        lambda *_args, **_kwargs: [candidate],
    )
    attempts = []

    def unavailable(_context, **_kwargs):
        attempts.append(1)
        raise bit_print.bit_order_labels.MercadoLabelUnavailable(
            "运单已取消",
            shipment_status="cancelled",
            permanent=True,
        )

    recorded = []
    monkeypatch.setattr(bit_print, "_download_label", unavailable)
    monkeypatch.setattr(
        bit_print,
        "_record_unavailable_orders",
        lambda order_ids, _exc: recorded.extend(order_ids),
    )

    rows = bit_print._run_shop_job(
        {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥"]},
        max_retries=3,
        logger=lambda _message: None,
    )

    assert attempts == [1]
    assert recorded == ["20001"]
    assert rows[0]["status"] == "skipped"
    assert rows[0]["failed_count"] == 0


def test_shop_job_marks_mixed_success_and_failure_as_partial(monkeypatch):
    candidates = [_context("20001", "30001"), _context("20002", "30002")]
    monkeypatch.setattr(
        bit_print,
        "_scan_store_orders",
        lambda *_args, **_kwargs: {
            "first_run": False,
            "tracking_since": datetime.now(timezone.utc) - timedelta(days=1),
            "end_at": datetime.now(timezone.utc),
        },
    )
    monkeypatch.setattr(
        bit_print.bit_mysql,
        "list_mercado_order_print_candidates",
        lambda *_args, **_kwargs: candidates,
    )

    def download(context, **_kwargs):
        if context["shipping_id"] == "30002":
            raise bit_print.bit_order_labels.MercadoLabelError("临时下载失败")
        return context["shipping_id"], b"%PDF-1.4\nlabel\n%%EOF", 1

    monkeypatch.setattr(bit_print, "_download_label", download)
    monkeypatch.setattr(
        bit_print,
        "_record_printed_orders",
        lambda order_ids, **_kwargs: len(order_ids),
    )

    rows = bit_print._run_shop_job(
        {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥"]},
        logger=lambda _message: None,
    )

    assert rows[0]["status"] == "partial"
    assert rows[0]["shipment_count"] == 1
    assert rows[0]["failed_count"] == 1


def test_print_round_combines_multiple_selected_stores(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bit_print,
        "build_print_jobs",
        lambda **_kwargs: [
            {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥"]},
            {"token_id": 8, "shop_name": "店铺乙", "sites": ["巴西"]},
        ],
    )

    def run_store(job, document_sink, printed_order_sink, **_kwargs):
        document_sink.append(b"%PDF-1.4\nlabel\n%%EOF")
        printed_order_sink.append(f"order-{job['token_id']}")
        return [
            bit_print._result_row(
                job["shop_name"], job["sites"][0], "printed", "API 已生成", shipment_count=1
            )
        ]

    monkeypatch.setattr(bit_print, "_run_shop_job", run_store)
    monkeypatch.setattr(
        bit_print,
        "_write_output",
        lambda documents, **_kwargs: (tmp_path / "labels.pdf", "labels.pdf"),
    )
    monkeypatch.setattr(bit_print, "insert_task_record", mock.Mock())

    summary = bit_print.print_orders_all(
        selected_shops=["店铺甲", "店铺乙"],
        logger=lambda _message: None,
    )

    assert summary["printed"] == 2
    assert summary["printed_order_count"] == 2
    assert summary["shipment_count"] == 2
    assert summary["download_name"] == "labels.pdf"


def test_print_round_runs_stores_in_parallel_with_isolated_outputs(monkeypatch, tmp_path):
    jobs = [
        {"token_id": index, "shop_name": f"店铺{index}", "sites": ["墨西哥"]}
        for index in range(1, 5)
    ]
    monkeypatch.setattr(bit_print, "build_print_jobs", lambda **_kwargs: jobs)
    monkeypatch.setattr(bit_print, "insert_task_record", mock.Mock())
    monkeypatch.setattr(
        bit_print,
        "_write_output",
        lambda documents, **_kwargs: (tmp_path / "labels.pdf", "labels.pdf"),
    )
    guard = threading.Lock()
    active = 0
    max_active = 0

    def run_store(job, document_sink, printed_order_sink, **_kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        document_sink.append(f"pdf-{job['token_id']}".encode())
        printed_order_sink.append(f"order-{job['token_id']}")
        with guard:
            active -= 1
        return [
            bit_print._result_row(
                job["shop_name"],
                "墨西哥",
                "printed",
                "API 已生成",
                shipment_count=1,
            )
        ]

    monkeypatch.setattr(bit_print, "_run_shop_job", run_store)

    summary = bit_print.print_orders_all(
        store_workers=2,
        logger=lambda _message: None,
    )

    assert max_active == 2
    assert summary["store_worker_count"] == 2
    assert summary["printed"] == 4
    assert summary["printed_order_count"] == 4
    assert summary["shipment_count"] == 4


def test_store_worker_count_is_bounded(monkeypatch):
    monkeypatch.setenv(bit_print.STORE_WORKERS_ENV, "999")

    assert bit_print._store_worker_count(None, 100) == bit_print.MAX_STORE_WORKERS
    assert bit_print._store_worker_count(8, 3) == 3
    assert bit_print._store_worker_count("invalid", 100) == bit_print.DEFAULT_STORE_WORKERS


def test_automatic_no_order_round_does_not_flood_task_history(monkeypatch):
    monkeypatch.setattr(
        bit_print,
        "build_print_jobs",
        lambda **_kwargs: [
            {"token_id": 7, "shop_name": "店铺甲", "sites": ["墨西哥"]}
        ],
    )
    monkeypatch.setattr(
        bit_print,
        "_run_shop_job",
        lambda job, **_kwargs: [
            bit_print._result_row(
                job["shop_name"],
                "墨西哥",
                "no_orders",
                "没有未打印订单",
            )
        ],
    )
    monkeypatch.setattr(bit_print, "_write_output", lambda *_args, **_kwargs: (None, None))
    insert_record = mock.Mock()
    monkeypatch.setattr(bit_print, "insert_task_record", insert_record)

    summary = bit_print.print_orders_all(operator_name="系统自动打印")

    assert summary["no_orders"] == 1
    insert_record.assert_not_called()


def test_order_print_source_has_no_browser_automation_dependency():
    source = Path(bit_print.__file__).read_text(encoding="utf-8")

    assert "from selenium" not in source
    assert "openBrowser" not in source
    assert "BitBrowser" in source  # only the migration explanation remains


def test_order_print_page_describes_api_unprinted_and_72_hour_fallback():
    template = (
        Path(bit_print.__file__).resolve().parent / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert "/api/order-print/options" in template
    assert "/api/order-print/download" in template
    assert "只处理未打印订单" in template
    assert "最近 72 小时" in template
    assert "API 授权店铺（可多选）" in template
    assert 'id="order-print-date-from" type="datetime-local"' in template
    assert 'id="order-print-date-to" type="datetime-local"' in template
