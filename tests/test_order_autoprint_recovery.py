from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from bit import bit_order_sync, bit_print, print_wallet


def test_automatic_bootstrap_recovers_recent_backlog(monkeypatch):
    monkeypatch.delenv(bit_order_sync.AUTO_PRINT_LOOKBACK_ENV, raising=False)
    monkeypatch.setattr(bit_order_sync.bit_mysql, "get_mercado_order_sync_schedule_value", lambda _key: "")
    saved = Mock()
    monkeypatch.setattr(bit_order_sync.bit_mysql, "set_mercado_order_sync_schedule_value", saved)
    now = datetime(2026, 10, 7, 8, tzinfo=timezone.utc)
    starts = bit_order_sync._automatic_print_candidate_starts([{"token_id": 7}], now=now)
    assert starts[7] == now - timedelta(hours=72)
    saved.assert_called_once()


def test_automatic_print_disabled_flag_prevents_all_printing(monkeypatch):
    monkeypatch.setenv(bit_order_sync.AUTO_PRINT_DISABLED_ENV, "1")
    build = Mock(side_effect=AssertionError("must not build jobs"))
    monkeypatch.setattr(bit_print, "build_print_jobs", build)
    assert bit_order_sync._run_automatic_order_print()["reason"] == "disabled_by_environment"
    build.assert_not_called()


def test_prepaid_automatic_print_keeps_other_shipments_when_balance_insufficient(monkeypatch):
    contexts = [{"order_id": "1", "shipping_id": "a"}, {"order_id": "2", "shipping_id": "b"}]
    events = []

    def paid(order_ids, request_id, persist_result):
        if order_ids == ["1"]:
            raise print_wallet.WalletInsufficientFunds("余额不足")
        result = {"content": b"%PDF label", "order_ids": order_ids, "shipment_count": 1, "wallet_charge_cents": 300}
        persist_result(result)
        events.append("settled")
        return result

    monkeypatch.setattr(print_wallet, "download_automatic_paid_labels", paid, raising=False)
    monkeypatch.setattr(bit_print, "_write_output", lambda *_args, **_kwargs: events.append("saved") or ("/tmp/label.pdf", "label.pdf"))
    monkeypatch.setattr(bit_print, "_record_printed_orders", lambda *_args, **_kwargs: events.append("logged"))
    row, documents, printed = bit_print._print_prepaid_site(
        contexts, shop_name="shop", site="巴西", stop_event=None, logger=lambda _line: None, output_dir=None,
    )
    assert printed == ["2"]
    assert len(documents) == 1
    assert events == ["saved", "settled", "logged"]
    assert row["status"] == "partial"
    assert row["failed_order_ids"] == ["1"]
    assert row["wallet_charge_cents"] == 300


def test_pdf_storage_failure_cannot_mark_order_printed(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(bit_print, "_scan_store_orders", lambda *_args, **_kwargs: {"first_run": False, "tracking_since": now})
    monkeypatch.setattr(bit_print.bit_mysql, "list_mercado_order_print_candidates", lambda *_args, **_kwargs: [{"order_id": "1", "shipping_id": "a", "site_id": "MLB"}])
    monkeypatch.setattr(bit_print, "_download_label", lambda *_args, **_kwargs: ("a", b"%PDF label", 1))
    monkeypatch.setattr(bit_print, "_write_output", Mock(side_effect=OSError("disk full")))
    logged = Mock()
    monkeypatch.setattr(bit_print, "_record_printed_orders", logged)
    documents, printed = [], []
    rows = bit_print._run_shop_job(
        {"token_id": 7, "shop_name": "shop", "sites": ["巴西"]}, persist_labels=True,
        logger=lambda _line: None, document_sink=documents, printed_order_sink=printed,
    )
    assert rows[0]["status"] == "failed"
    assert documents == printed == []
    logged.assert_not_called()


def test_combined_pdf_failure_preserves_successful_individual_pdf(monkeypatch):
    monkeypatch.setattr(bit_print, "build_print_jobs", lambda **_kwargs: [{"token_id": 7, "shop_name": "shop", "sites": ["巴西"]}])

    def job(_job, document_sink, printed_order_sink, **_kwargs):
        document_sink.append(b"%PDF individual")
        printed_order_sink.append("1")
        return [{**bit_print._result_row("shop", "巴西", "printed", "done", shipment_count=1), "output_files": ["/tmp/individual.pdf"]}]

    monkeypatch.setattr(bit_print, "_run_shop_job", job)
    monkeypatch.setattr(bit_print, "_write_output", Mock(side_effect=ValueError("merge failed")))
    result = bit_print.print_orders_all(persist=False, logger=lambda _line: None)
    assert result["printed_order_ids"] == ["1"]
    assert result["output_files"] == ["/tmp/individual.pdf"]
    assert result["output_error"] == "merge failed"
    assert result["download_path"] == ""


def test_scheduler_recovers_immediately_after_restart(monkeypatch):
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.return_value = True
    monkeypatch.setattr(bit_order_sync, "_scheduler_stop_event", stop)
    monkeypatch.setattr(bit_order_sync.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(bit_order_sync, "get_lock_owner", lambda _key: None)
    monkeypatch.setattr(bit_order_sync, "_sync_state", {"running": False})
    monkeypatch.setattr(bit_order_sync.bit_mysql, "get_mercado_order_sync_schedule_value",
                        lambda _key: datetime.now(bit_order_sync.WORKBENCH_LOCAL_TIMEZONE).date().isoformat())
    start = Mock(return_value=(True, {}))
    monkeypatch.setattr(bit_order_sync, "start_order_sync", start)
    bit_order_sync._scheduler_loop(900)
    start.assert_called_once_with(mode="automatic")


def test_free_automatic_labels_persist_and_audit_without_wallet(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(bit_print, "_scan_store_orders", lambda *_a, **_kw: {"first_run": False, "tracking_since": now})
    monkeypatch.setattr(bit_print.bit_mysql, "list_mercado_order_print_candidates", lambda *_a, **_kw: [{"order_id": "1", "shipping_id": "a", "site_id": "MLB"}])
    monkeypatch.setattr(print_wallet, "download_automatic_paid_labels", Mock(side_effect=AssertionError("free labels cannot charge")))
    monkeypatch.setattr(bit_print, "_download_label", lambda *_a, **_kw: ("a", b"%PDF label", 1))
    events = []
    monkeypatch.setattr(bit_print, "_write_output", lambda *_a, **_kw: events.append("saved") or ("/tmp/label.pdf", "label.pdf"))
    monkeypatch.setattr(bit_print, "_sync_printed_shipment_tracking_number", lambda *_a, **_kw: None)
    monkeypatch.setattr(bit_print, "_record_printed_orders", lambda ids, operator_name: events.append((ids, operator_name)))
    printed = []
    rows = bit_print._run_shop_job(
        {"token_id": 7, "shop_name": "shop", "sites": ["巴西"]}, persist_labels=True,
        require_prepaid=False, operator_name="系统自动打印", printed_order_sink=printed,
        logger=lambda _line: None,
    )
    assert rows[0]["status"] == "printed"
    assert printed == ["1"]
    assert events == ["saved", (["1"], "系统自动打印")]
