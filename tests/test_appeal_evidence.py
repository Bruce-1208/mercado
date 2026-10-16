"""Offline package verification and screenshot lifecycle contracts; no real submissions."""
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest
from bit import appeal_evidence as evidence
from bit import bit_appeal_ai as ai
from bit import bit_daily_task as daily
from bit import bit_interface
from test_browser_extension_appeal import service, params, browser_params, browser_only  # noqa: F401

SALE = "2000000000000001"
PACK = "2000000000000099"


@pytest.mark.parametrize("getter", ["get_cancellation_orders", "get_complaint_orders"])
def test_metrics_package_mode_combines_explicit_packs_and_verified_sales(monkeypatch, getter):
    from bit import bit_reputation_info as metrics, bit_db_api
    monkeypatch.setattr(metrics, "_open_reputation_page_with_validation", lambda *a: None)
    monkeypatch.setattr(metrics, "_select_country", lambda *a: None)
    monkeypatch.setattr(metrics, "_raise_if_mercado_unavailable", lambda **kw: None)
    monkeypatch.setattr(metrics.time, "sleep", lambda *a: None)
    for click in ("_click_cancellation_review_in_metrics", "_click_complaint_review_in_metrics"):
        monkeypatch.setattr(metrics, click, lambda *a: {"has_metric": True, "clicked": True})
    monkeypatch.setattr(metrics, "_extract_visible_cancellation_order_ids",
                        lambda *a: {"ids": [SALE], "pack_ids": [PACK], "fingerprint": "page1"})
    monkeypatch.setattr(metrics, "_advance_cancellation_orders_page", lambda *a: "done")
    resolver = Mock(return_value=[PACK, "2000000000000088"])
    monkeypatch.setattr(bit_db_api, "get_appeal_package_ids", resolver)
    assert getattr(metrics, getter)(object(), "测试店铺", "墨西哥", package_ids=True) == [PACK, "2000000000000088"]
    resolver.assert_called_once_with("测试店铺", "墨西哥", [SALE])


@pytest.mark.parametrize("order, error", [
    ({"id": SALE, "seller": {"id": "101"}, "shipping": {"id": PACK}}, "缺少包裹号"),
    ({"id": SALE, "seller": {"id": "202"}, "pack_id": PACK}, "店铺/站点"),
    ({"id": "2000000000000002", "seller": {"id": "101"}, "pack_id": PACK}, "店铺/站点"),
    ({"id": SALE, "seller": {"id": "101"}, "pack_id": PACK, "site_id": "MLB"}, "站点"),
    ({"id": SALE, "seller": {"id": "101"}, "pack_id": float(PACK)}, "编号无效"),
])
def test_package_resolution_never_falls_back_to_sale_or_shipping(monkeypatch, order, error):
    from bit import mercado_infraction_sync as sync
    client = SimpleNamespace(request=Mock(return_value=order))
    monkeypatch.setattr(sync, "_client_and_token", lambda token, **kwargs: (client, token))
    with pytest.raises(ValueError, match=error):
        evidence.resolve_with_token({"meli_user_id": "100"}, "MX", [SALE], accounts=[{"user_id": "101", "site_id": "MLM"}])
    client.request.assert_called_once_with("GET", f"/marketplace/orders/{SALE}", max_attempts=1)


def test_explicit_package_id_is_deduplicated_and_sale_is_excluded(monkeypatch):
    from bit import mercado_infraction_sync as sync
    client = SimpleNamespace(request=lambda *a, **kw: {"id": SALE, "seller": {"id": "101"}, "pack_id": PACK})
    monkeypatch.setattr(sync, "_client_and_token", lambda token, **kw: (client, token))
    assert evidence.resolve_with_token({}, "MLM", [SALE, SALE], accounts=[{"user_id": "101", "site_id": "MLM"}]) == [PACK]
    with pytest.raises(ValueError, match="未核实"):
        evidence.validate_package_message(f"销售单号 {SALE} 请复核", [PACK])
    assert evidence.validate_package_message(f"订单编号 {PACK} 请复核", [PACK]) == f"包裹号 {PACK} 请复核"


@pytest.mark.parametrize("mode", ["AI客服", "人工客服"])
@pytest.mark.parametrize("form", ["延误", "取消率", "投诉"])
def test_all_modes_custom_order_appeals_send_packages(service, mode, form):
    service.packages_loader = lambda identity, site, ids: [PACK]
    plan = service.plan(service.user, params(mode=mode, forms=[form], message="请按实际记录复核", screenshot_mode=True))
    assert plan["params"]["screenshot_mode"] is True
    assert plan["groups"] == []
    plan = service.plan(service.user, {"task_id": plan["task_id"], "reputation_identifiers": {"MLM": {form: [SALE]}}})
    assert plan["groups"][0]["identifiers"] == [PACK]
    assert PACK in plan["groups"][0]["message"]
    assert SALE not in plan["groups"][0]["message"]


def test_browser_explicit_packs_do_not_read_oauth(service):
    browser_only(service)
    service.packages_loader = lambda *a: pytest.fail("explicit package must not be remapped as a sale")
    plan = service.plan(service.user, browser_params(forms=["投诉"], screenshot_mode=True))
    plan = service.plan(service.user, {"task_id": plan["task_id"], "browser_identifiers": {"MLM": {"投诉": {"pack_ids": [PACK], "order_ids": []}}}})
    assert plan["groups"][0]["identifiers"] == [PACK]


@pytest.mark.parametrize("mode", ["once", "loop"])
@pytest.mark.parametrize("strategy", ["normal", "algorithm"])
def test_daily_screenshot_flag_survives_api_and_execution(monkeypatch, tmp_path, mode, strategy):
    run = Mock(return_value=[])
    monkeypatch.setattr(daily, "loop_ai_appeal" if mode == "loop" else "run_ai_appeal_once", run)
    config = bit_interface.build_daily_task_params({"mode": mode, "task_strategy": strategy, "screenshot_mode": True})
    bit_interface.execute_daily_task(config, object(), threading.Event(), "offline", tmp_path / "log", {})
    assert run.call_args.kwargs["screenshot_mode"] is True
    assert bit_interface.build_daily_task_params({"screenshot_mode": "false"})["screenshot_mode"] is False


@pytest.mark.parametrize("failed", [False, True])
def test_agent_preserves_chat_and_browser_even_on_error(monkeypatch, failed):
    events = []
    lease = SimpleNamespace(acquired=True, acquire=lambda **kw: True, release=lambda: events.append("release"))
    driver = SimpleNamespace(service=SimpleNamespace(stop=lambda: events.append("stop_driver")),
                             switch_to=SimpleNamespace(new_window=lambda kind: events.append("new_tab")))
    monkeypatch.setattr(ai, "current_thread_window_lease", lambda w: None)
    monkeypatch.setattr(ai, "create_window_lease", lambda *a, **kw: lease)
    monkeypatch.setattr(ai, "connect_bit_browser", lambda w: (driver, {}))
    monkeypatch.setattr(ai, "releaseBrowserLease", lambda w: events.append("manual_handoff"))
    monkeypatch.setattr(ai, "open_help_page_with_daily_validation", lambda *a, **kw: None)
    monkeypatch.setattr(ai, "select_site", lambda *a: None)
    monkeypatch.setattr(ai, "save_ai_appeal_record", lambda *a, **kw: None)
    def handle(*args, **kwargs):
        assert driver._bit_screenshot_mode
        if failed:
            raise RuntimeError("offline failure")
    monkeypatch.setattr(ai, "handle_infraction", handle)
    monkeypatch.setattr(ai, "close_current_tab_keep_browser", lambda *a: pytest.fail("chat must remain visible"))
    monkeypatch.setattr(ai, "closeBrowser", lambda *a, **kw: pytest.fail("browser must remain open"))
    result = ai.shensu("测试店铺", "MX", "侵权", "请复核", window_id="offline", screenshot_mode=True)
    assert result["status"] == ("failed" if failed else "no_data")
    assert events[-2:] == ["stop_driver", "release"]
    assert "manual_handoff" in events


def test_screenshot_failure_does_not_resend_or_reset_chat(monkeypatch):
    driver = SimpleNamespace(_bit_screenshot_mode=True)
    sends = []
    monkeypatch.setattr(ai, "_prepare_group_conversation", lambda *a: None)
    monkeypatch.setattr(ai, "safe_get_agent_messages", lambda *a: [])
    monkeypatch.setattr(ai, "send_ai_chat_message", lambda d, text: sends.append(text) or {})
    monkeypatch.setattr(ai, "wait_for_ai_agent_reply", lambda *a, **kw: ("正在复核", []))
    monkeypatch.setattr(ai, "append_chat_log", lambda *a, **kw: None)
    monkeypatch.setattr(evidence, "save_chat_screenshot", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    result = ai.send_infraction_message_with_retry(driver, f"{PACK}请核查", PACK, "店铺", "MX", 1, 1, appeal_kind="投诉")
    assert result["acknowledged"] and result["status"] == "sent"
    assert result["screenshot_error"] == "disk full"
    assert len(sends) == 1


def test_daily_screenshot_error_retains_window_without_retry(monkeypatch):
    events = []
    lease = SimpleNamespace(acquired=True, acquire=lambda **kw: True, release=lambda: events.append("release"))
    monkeypatch.setattr(daily, "create_window_lease", lambda *a, **kw: lease)
    monkeypatch.setattr(ai, "releaseBrowserLease", lambda w: events.append("manual"))
    monkeypatch.setattr(daily, "_close_ai_appeal_browser", lambda *a: pytest.fail("must not close screenshot window"))
    run = Mock(return_value={"status": "failed", "execution_status": "failed", "retryable": True})
    monkeypatch.setattr(ai, "shensu", run)
    owned = {}
    daily.appeal_one_shop({"name": "店", "window_id": "offline", "total": 1, "screenshot_mode": True,
                          "sites": [{"site_code": "MX", "count": 1}]}, site_pause=0, owned_window_ids=owned)
    assert run.call_count == 1 and run.call_args.kwargs["screenshot_mode"] is True
    assert owned == {} and events == ["manual", "release"]
