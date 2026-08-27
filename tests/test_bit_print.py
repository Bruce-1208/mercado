from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bit import bit_print


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


def test_shop_job_downloads_only_candidate_orders_and_records_success(monkeypatch):
    candidates = [
        _context("20001", "30001"),
        _context("20002", "30001"),
        _context("20003", "30002"),
    ]
    seen = {"candidate_args": None, "recorded": []}
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
        lambda order_ids: seen["recorded"].extend(order_ids) or len(order_ids),
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
    assert len(documents) == 2


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
