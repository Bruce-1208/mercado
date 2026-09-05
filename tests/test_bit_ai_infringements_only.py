from bit import bit_appeal_ai
from bit_playwright import bit_infractions_info


def test_infraction_pages_use_extended_wait_limits():
    assert bit_infractions_info.INFRACTIONS_ELEMENT_TIMEOUT_MS >= 60_000
    assert bit_infractions_info.INFRACTIONS_PAGE_READY_TIMEOUT_MS >= 90_000
    assert bit_infractions_info.INFRACTIONS_NAVIGATION_TIMEOUT_MS >= 120_000


def test_new_infraction_page_receives_extended_default_timeouts():
    calls = []

    class FakePage:
        def set_default_timeout(self, timeout):
            calls.append(("element", timeout))

        def set_default_navigation_timeout(self, timeout):
            calls.append(("navigation", timeout))

    page = FakePage()

    class FakeContext:
        def new_page(self):
            return page

    class FakeBrowser:
        contexts = [FakeContext()]

    class FakeChromium:
        def connect_over_cdp(self, endpoint):
            assert endpoint == "http://127.0.0.1:9222"
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    _browser, connected_page = bit_infractions_info._connect_bitbrowser_with_playwright(
        FakePlaywright(),
        {"data": {"http": "127.0.0.1:9222"}},
    )

    assert connected_page is page
    assert calls == [
        ("element", bit_infractions_info.INFRACTIONS_ELEMENT_TIMEOUT_MS),
        ("navigation", bit_infractions_info.INFRACTIONS_NAVIGATION_TIMEOUT_MS),
    ]


def test_ai_infraction_orders_requests_infringements_only(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        bit_appeal_ai,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
                    "id": 7,
                    "display_name": "店铺",
                    "nickname": "shop",
                    "enabled": True,
                    "site_settings": [
                        {"site_id": "MLM", "appeal_enabled": True},
                    ],
                }
            ]
        },
    )

    def collect(targets, **kwargs):
        captured["targets"] = targets
        captured["kwargs"] = kwargs
        return {
            "data": [
                {"店铺名": "店铺", "站点": "MLM", "编号": "INF-1", "类型": "侵权"},
                {
                    "店铺名": "店铺",
                    "站点": "MLM",
                    "编号": "INF-GENERIC",
                    "类型": "侵权",
                    "侵权原因": "The product's brand is not generic.",
                },
                {
                    "店铺名": "店铺",
                    "站点": "MLM",
                    "编号": "INF-PROHIBITED",
                    "类型": "侵权",
                    "侵权原因": "The product is prohibited.",
                },
                {"店铺名": "店铺", "站点": "MLM", "编号": "REPORT-1", "类型": "权利人"},
                {"店铺名": "店铺", "站点": "MLB", "编号": "INF-2", "类型": "侵权"},
            ]
        }

    monkeypatch.setattr(
        bit_appeal_ai.mercado_infraction_sync,
        "collect_live_detection_infractions",
        collect,
    )
    result = bit_appeal_ai.get_infraction_orders("window", "店铺", "墨西哥")

    assert result == ["INF-1"]
    assert captured["targets"] == [
        {
            "token_id": 7,
            "name": "店铺",
            "aliases": ["店铺", "shop"],
            "site_ids": ["MLM"],
        }
    ]
    assert captured["kwargs"] == {"recent_days": 100, "max_workers": 1}


def test_prohibited_appeal_reads_current_prohibited_list(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_appeal_ai,
        "_find_infraction_api_target",
        lambda *_args: {"token_id": 7, "site_ids": ["MLM"]},
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "list_mercado_prohibited_listings",
        lambda **kwargs: calls.append(kwargs) or {
            "rows": [
                {"site_id": "MLM", "item_id": "MLM-1"},
                {"site_id": "MLB", "item_id": "MLB-1"},
                {"site_id": "MLM", "item_id": "MLM-1"},
            ],
            "pages": 1,
        },
    )

    result = bit_appeal_ai.get_prohibited_listing_ids("店铺", "墨西哥")

    assert result == ["MLM-1"]
    assert calls == [
        {"token_id": 7, "risk_type": "prohibited", "page": 1, "page_size": 500}
    ]


def test_prohibited_appeal_uses_its_own_default_phrase(monkeypatch):
    sent = []
    monkeypatch.setattr(bit_appeal_ai, "open_ai_contact_window", lambda *args: None)
    monkeypatch.setattr(bit_appeal_ai, "get_current_appeal_phrase", lambda: "")
    monkeypatch.setattr(bit_appeal_ai, "get_appeal_log_records", lambda: [])
    monkeypatch.setattr(bit_appeal_ai, "_filter_group_log_records", lambda *args, **kwargs: [])
    monkeypatch.setattr(bit_appeal_ai, "save_ai_appeal_group_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bit_appeal_ai,
        "send_infraction_message_with_retry",
        lambda _driver, message, *_args, **_kwargs: sent.append(message),
    )

    bit_appeal_ai.handle_prohibited(
        "window",
        object(),
        "店铺",
        "墨西哥",
        "",
        "Bruce",
        prohibited_ids=["MLM-1"],
    )

    assert sent == [
        "MLM-1亲爱客服，这个产品不是禁限售产品，他被系统误判了，麻烦你帮我恢复"
    ]


def test_ai_random_infraction_orders_requests_infringements_only(monkeypatch):
    monkeypatch.setattr(
        bit_appeal_ai,
        "get_infraction_orders",
        lambda *args: ["INF-1"],
    )

    result = bit_appeal_ai.get_infraction_orders_random("window", "店铺", "巴西", 10)

    assert "INF-1" in result


def test_infraction_appeal_uses_api_ids_and_sends_at_most_ten(monkeypatch):
    sent_groups = []

    monkeypatch.setattr(
        bit_appeal_ai,
        "get_infraction_orders",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("已有 API 编号时不应重新读取")
        ),
    )
    monkeypatch.setattr(bit_appeal_ai, "open_ai_contact_window", lambda *args: None)
    monkeypatch.setattr(bit_appeal_ai, "get_current_appeal_phrase", lambda: "")
    monkeypatch.setattr(bit_appeal_ai, "get_appeal_log_records", lambda: [])
    monkeypatch.setattr(bit_appeal_ai, "_filter_group_log_records", lambda *args: [])
    monkeypatch.setattr(bit_appeal_ai, "save_ai_appeal_group_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(bit_appeal_ai.time, "sleep", lambda _seconds: None)

    def send(_driver, _message, ids, *_args):
        sent_groups.append(ids.split("、"))

    monkeypatch.setattr(bit_appeal_ai, "send_infraction_message_with_retry", send)

    bit_appeal_ai.handle_infraction(
        "window",
        object(),
        "店铺",
        "墨西哥",
        "",
        "Bruce",
        infraction_ids=[f"MLM-{index}" for index in range(1, 22)],
    )

    assert [len(group) for group in sent_groups] == [10, 10, 1]
    assert all(len(group) <= 10 for group in sent_groups)


def test_infraction_never_calls_deepseek_or_follows_up_on_final_reply(monkeypatch):
    sent = []
    logs = []

    monkeypatch.setattr(bit_appeal_ai, "safe_get_agent_messages", lambda _driver: [])
    monkeypatch.setattr(
        bit_appeal_ai,
        "send_ai_chat_message",
        lambda _driver, message: sent.append(message),
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "wait_for_ai_agent_reply",
        lambda *args, **kwargs: ("客服已经完成核查", ["客服已经完成核查"]),
    )
    from AI_Agent import deepseek
    monkeypatch.setattr(deepseek, "chat_deepseek", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("申诉不得调用外部模型")))
    monkeypatch.setattr(
        bit_appeal_ai,
        "append_chat_log",
        lambda *args, **kwargs: logs.append((args, kwargs)),
    )

    result = bit_appeal_ai.send_infraction_message_with_retry(
        object(),
        "MLM-1 请核查",
        "MLM-1",
        "店铺",
        "墨西哥",
        1,
        1,
    )

    assert sent == ["MLM-1 请核查"]
    assert result["status"] == "replied"
    assert result["reply_received"] is True


def test_collector_skips_reports_tab_when_disabled(monkeypatch):
    collected_types = []

    monkeypatch.setattr(bit_infractions_info, "_safe_goto_infractions", lambda *args: None)
    monkeypatch.setattr(bit_infractions_info.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(bit_infractions_info, "_validate_infractions_page", lambda page: None)
    monkeypatch.setattr(bit_infractions_info, "_switch_site_if_needed", lambda *args: None)
    monkeypatch.setattr(bit_infractions_info, "_current_infraction_type", lambda page: "侵权")

    def collect(page, name, site, infraction_type, already_collected):
        collected_types.append(infraction_type)
        already_collected.add(infraction_type)
        return [[name, site, "INF-1", "title", "", "", "", infraction_type]]

    monkeypatch.setattr(bit_infractions_info, "_collect_type_once", collect)
    monkeypatch.setattr(
        bit_infractions_info,
        "_infraction_type_total",
        lambda page, infraction_type: (_ for _ in ()).throw(
            AssertionError(f"不应读取 {infraction_type} 统计")
        ),
    )

    result = bit_infractions_info._collect_site_infractions(
        object(),
        "店铺",
        "墨西哥",
        switch_site=False,
        include_rights_holder=False,
    )

    assert collected_types == ["侵权"]
    assert result[0][2] == "INF-1"
