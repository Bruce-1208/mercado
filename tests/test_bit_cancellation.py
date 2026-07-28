import pytest

from bit import bit_appeal_ai
from bit import bit_reputation_info


def test_normalize_cancellation_order_ids_preserves_order_and_deduplicates():
    assert bit_reputation_info._normalize_cancellation_order_ids(
        ["#2000017402628934", "2000017402628934", "订单 1234567890", "20260722", None]
    ) == ["2000017402628934", "1234567890"]


def test_get_cancellation_orders_collects_every_page(monkeypatch):
    calls = []

    class SwitchTo:
        def window(self, handle):
            calls.append(("window", handle))

    class FakeDriver:
        window_handles = ["main"]
        current_url = "https://global-selling.mercadolibre.com/metrics#cancellations"
        switch_to = SwitchTo()

    states = iter(
        [
            {
                "ids": ["2000017402628934", "2000017401538762"],
                "fingerprint": "page-1",
                "height": 1000,
            },
            {
                "ids": ["2000017401538762", "2000017387373690"],
                "fingerprint": "page-2",
                "height": 1000,
            },
        ]
    )
    actions = iter(["clicked_next", "done"])

    monkeypatch.setattr(
        bit_reputation_info,
        "_open_reputation_page_with_validation",
        lambda driver, name, site: calls.append(("open", name, site)),
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "_select_country",
        lambda driver, site, name: calls.append(("site", name, site)),
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "_click_cancellation_review_in_metrics",
        lambda driver: {"clicked": True, "has_metric": True},
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "_extract_visible_cancellation_order_ids",
        lambda driver: next(states),
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "_advance_cancellation_orders_page",
        lambda driver: next(actions),
    )
    monkeypatch.setattr(bit_reputation_info, "_raise_if_mercado_unavailable", lambda **kwargs: {})
    monkeypatch.setattr(bit_reputation_info.time, "sleep", lambda seconds: None)

    result = bit_reputation_info.get_cancellation_orders(
        FakeDriver(),
        "测试店铺",
        "墨西哥",
    )

    assert result == ["2000017402628934", "2000017401538762", "2000017387373690"]
    assert calls[:2] == [("open", "测试店铺", "墨西哥"), ("site", "测试店铺", "墨西哥")]


def test_get_cancellation_orders_raises_when_metric_card_is_missing(monkeypatch):
    class FakeDriver:
        pass

    monkeypatch.setattr(bit_reputation_info, "_open_reputation_page_with_validation", lambda *args: True)
    monkeypatch.setattr(bit_reputation_info, "_select_country", lambda *args: True)
    monkeypatch.setattr(
        bit_reputation_info,
        "_click_cancellation_review_in_metrics",
        lambda driver: {"clicked": False, "has_metric": False, "reason": "missing"},
    )

    with pytest.raises(bit_reputation_info.MercadoPageStructureError, match="取消率指标卡片"):
        bit_reputation_info.get_cancellation_orders(FakeDriver(), "测试店铺", "巴西")


def test_ai_cancellation_uses_infraction_grouping_rules(monkeypatch):
    sent_groups = []
    saved_groups = []

    class FakeDriver:
        def __init__(self):
            self.urls = []

        def get(self, url):
            self.urls.append(url)

    driver = FakeDriver()
    orders = [str(2000017400000000 + index) for index in range(12)]
    monkeypatch.setattr(bit_appeal_ai, "get_cancellation_orders", lambda *args: orders)
    monkeypatch.setattr(
        bit_appeal_ai,
        "open_help_page_with_daily_validation",
        lambda driver, *args, **kwargs: driver.get(bit_appeal_ai.HELP_URL) or True,
    )
    monkeypatch.setattr(bit_appeal_ai, "select_site", lambda *args: True)
    monkeypatch.setattr(bit_appeal_ai, "open_ai_contact_window", lambda *args: True)
    monkeypatch.setattr(bit_appeal_ai.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(bit_appeal_ai, "get_appeal_log_records", lambda: [])
    monkeypatch.setattr(
        bit_appeal_ai,
        "send_infraction_message_with_retry",
        lambda driver, message, identifiers, name, site, group_index, total_groups, appeal_kind="侵权": sent_groups.append(
            {
                "identifiers": identifiers.split("、"),
                "group_index": group_index,
                "total_groups": total_groups,
                "appeal_kind": appeal_kind,
            }
        ),
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "save_ai_appeal_group_record",
        lambda *args, **kwargs: saved_groups.append(kwargs),
    )

    bit_appeal_ai.handle_cancellation(
        "window-id",
        driver,
        "测试店铺",
        "墨西哥",
        "",
        "Bruce",
    )

    assert [len(group["identifiers"]) for group in sent_groups] == [10, 2]
    assert [group["group_index"] for group in sent_groups] == [1, 2]
    assert all(group["total_groups"] == 2 for group in sent_groups)
    assert all(group["appeal_kind"] == "取消率" for group in sent_groups)
    assert all(group["appeal_kind"] == "取消率" for group in saved_groups)
    assert driver.urls == [bit_appeal_ai.HELP_URL]


def test_ai_appeal_record_collects_cancellation_ids():
    fields = bit_appeal_ai._collect_appeal_record_fields(
        [
            {
                "message": "请复核取消订单",
                "extra": {"cancellation_ids": ["2000017402628934", "2000017401538762"]},
            }
        ]
    )

    assert fields["identifiers"] == ["2000017402628934", "2000017401538762"]
