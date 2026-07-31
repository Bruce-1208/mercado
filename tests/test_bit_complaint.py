import pytest

from bit import bit_appeal_ai
from bit import bit_reputation_info


def test_complaint_review_uses_first_reputation_metric_card(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_reputation_info,
        "_click_reputation_review_in_metrics",
        lambda driver, metric_kind, fallback_index: calls.append(
            (driver, metric_kind, fallback_index)
        ) or {"clicked": True, "has_metric": True},
    )
    driver = object()

    result = bit_reputation_info._click_complaint_review_in_metrics(driver)

    assert result["clicked"] is True
    assert calls == [(driver, "complaints", 0)]


def test_get_complaint_orders_collects_all_metrics_pages(monkeypatch):
    calls = []

    class FakeDriver:
        pass

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
        "_click_complaint_review_in_metrics",
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
    monkeypatch.setattr(
        bit_reputation_info,
        "_raise_if_mercado_unavailable",
        lambda **kwargs: {},
    )
    monkeypatch.setattr(bit_reputation_info.time, "sleep", lambda seconds: None)

    result = bit_reputation_info.get_complaint_orders(
        FakeDriver(),
        "测试店铺",
        "墨西哥",
    )

    assert result == ["2000017402628934", "2000017401538762", "2000017387373690"]
    assert calls == [("open", "测试店铺", "墨西哥"), ("site", "测试店铺", "墨西哥")]


def test_get_complaint_orders_raises_when_complaints_card_is_missing(monkeypatch):
    monkeypatch.setattr(
        bit_reputation_info,
        "_open_reputation_page_with_validation",
        lambda *args: True,
    )
    monkeypatch.setattr(bit_reputation_info, "_select_country", lambda *args: True)
    monkeypatch.setattr(
        bit_reputation_info,
        "_click_complaint_review_in_metrics",
        lambda driver: {"clicked": False, "has_metric": False, "reason": "missing"},
    )

    with pytest.raises(bit_reputation_info.MercadoPageStructureError, match="投诉指标卡片"):
        bit_reputation_info.get_complaint_orders(object(), "测试店铺", "巴西")


def test_ai_complaint_groups_all_orders_and_uses_default_message(monkeypatch):
    sent_groups = []
    saved_groups = []

    class FakeDriver:
        def __init__(self):
            self.urls = []

        def get(self, url):
            self.urls.append(url)

    driver = FakeDriver()
    orders = [str(2000017400000000 + index) for index in range(5)]
    monkeypatch.setattr(bit_appeal_ai, "get_complaint_orders", lambda *args: orders)
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
                "message": message,
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

    bit_appeal_ai.handle_complaint(
        "window-id",
        driver,
        "测试店铺",
        "墨西哥",
        "",
        "Bruce",
    )

    assert [len(group["identifiers"]) for group in sent_groups] == [2, 2, 1]
    assert [group["group_index"] for group in sent_groups] == [1, 2, 3]
    assert all(group["total_groups"] == 3 for group in sent_groups)
    assert len(saved_groups) == 3
    assert all(group["appeal_kind"] == "投诉" for group in sent_groups)
    assert all(group["appeal_kind"] == "投诉" for group in saved_groups)
    assert sent_groups[0]["message"].startswith(
        "销售单号：" + "、".join(sent_groups[0]["identifiers"])
    )
    assert bit_appeal_ai.COMPLAINT_DEFAULT_APPEAL_MESSAGE in sent_groups[0]["message"]
    assert "买家想白嫖" in sent_groups[0]["message"]
    assert driver.urls == [bit_appeal_ai.HELP_URL]


def test_ai_appeal_record_collects_complaint_order_ids():
    fields = bit_appeal_ai._collect_appeal_record_fields(
        [
            {
                "message": "请复核投诉销售单",
                "extra": {
                    "complaint_order_ids": ["2000017402628934", "2000017401538762"]
                },
            }
        ]
    )

    assert fields["identifiers"] == ["2000017402628934", "2000017401538762"]
