from bit import bit_appeal_ai


def test_ai_customer_service_uses_extended_wait_limits():
    assert bit_appeal_ai.AI_BACKEND_SETTLE_SECONDS >= 12
    assert bit_appeal_ai.AI_CHAT_READY_TIMEOUT_SECONDS >= 45
    assert bit_appeal_ai.AI_CHAT_ENTRY_TIMEOUT_SECONDS >= 30
    assert bit_appeal_ai.AI_CHAT_INPUT_TIMEOUT_SECONDS >= 45
    assert bit_appeal_ai.AI_AGENT_REPLY_TIMEOUT_SECONDS >= 300


class FakeMessageElement:
    def __init__(self, text, displayed=True):
        self.text = text
        self._displayed = displayed

    def is_displayed(self):
        return self._displayed


def test_get_agent_messages_supports_new_maxwell_assistant_nodes(monkeypatch):
    selectors = []

    class FakeDriver:
        def find_elements(self, by, selector):
            selectors.append(selector)
            if selector == ".message-item--assistant .chat-message__content":
                return [
                    FakeMessageElement("第一条完整回复"),
                    FakeMessageElement("第二条完整回复"),
                ]
            return []

    monkeypatch.setattr(
        bit_appeal_ai,
        "activate_ai_chat_context",
        lambda driver, require_input=False: bit_appeal_ai.AI_CHAT_MODE_IFRAME,
    )

    messages = bit_appeal_ai.get_agent_messages(FakeDriver())

    assert messages == ["第一条完整回复", "第二条完整回复"]
    assert selectors[0] == ".message-item--assistant .chat-message__content"


def test_handle_delay_waits_for_and_records_each_group_reply(monkeypatch):
    monkeypatch.setattr(bit_appeal_ai, "save_ai_appeal_group_record", lambda *args, **kwargs: None)
    sent_messages = []
    records = []
    wait_calls = []

    monkeypatch.setattr(
        bit_appeal_ai,
        "get_delay_orders_download_list",
        lambda window_id, name, site: ["1001", "1002"],
    )
    monkeypatch.setattr(bit_appeal_ai, "open_ai_contact_window", lambda *args: None)
    monkeypatch.setattr(
        bit_appeal_ai,
        "safe_get_agent_messages",
        lambda driver: ["历史回复"],
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "send_ai_chat_message",
        lambda driver, message: sent_messages.append(message),
    )

    def fake_wait(driver, previous_messages, timeout, poll_interval):
        wait_calls.append((previous_messages, timeout, poll_interval))
        return "本组处理完成", ["历史回复", "本组处理完成"]

    monkeypatch.setattr(bit_appeal_ai, "wait_for_ai_agent_reply", fake_wait)
    monkeypatch.setattr(
        bit_appeal_ai,
        "append_chat_log",
        lambda name, site, event, **kwargs: records.append((event, kwargs)),
    )

    bit_appeal_ai.handle_delay(
        "window-id",
        object(),
        "测试店铺",
        "墨西哥",
        "请复核",
        "Bruce",
    )

    assert sent_messages == ["1001、1002请复核"]
    assert wait_calls == [
        (
            ["历史回复"],
            bit_appeal_ai.AI_AGENT_REPLY_TIMEOUT_SECONDS,
            bit_appeal_ai.AI_AGENT_REPLY_POLL_SECONDS,
        )
    ]
    assert records[-2][0] == "delay_agent_reply"
    assert records[-2][1]["response"] == "本组处理完成"
    assert records[-1][0] == "group_result"
    assert records[-1][1]["extra"]["result"]["status"] == "sent"
    assert records[-1][1]["extra"]["result"]["reply_status"] == "replied"


def test_classify_ai_chat_variant_prefers_visible_shadow_iframe():
    state = {
        "inline_shell": True,
        "shadow_ai_frame_count": 1,
        "visible_ai_frame_count": 1,
        "legacy_frame_count": 1,
    }

    assert (
        bit_appeal_ai.classify_ai_chat_variant(state)
        == bit_appeal_ai.AI_CHAT_MODE_IFRAME
    )


def test_find_frames_including_shadow_dom_uses_deep_dom_script():
    shadow_frame = object()

    class FakeDriver:
        def execute_script(self, script):
            assert "el.shadowRoot" in script
            assert "tagName" in script
            return [shadow_frame]

    assert bit_appeal_ai.find_frames_including_shadow_dom(FakeDriver()) == [
        shadow_frame
    ]


def test_switch_to_ai_chat_frame_accepts_shadow_dom_frame(monkeypatch):
    shadow_frame = object()

    class SwitchTo:
        def __init__(self):
            self.frames = []

        def default_content(self):
            return None

        def parent_frame(self):
            return None

        def frame(self, frame):
            self.frames.append(frame)

    class FakeDriver:
        def __init__(self):
            self.switch_to = SwitchTo()

    driver = FakeDriver()
    monkeypatch.setattr(bit_appeal_ai, "reset_expired_ai_iframe", lambda *args: False)
    monkeypatch.setattr(
        bit_appeal_ai,
        "find_frames_including_shadow_dom",
        lambda driver: [shadow_frame],
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "get_frame_info",
        lambda driver, frame: {
            "src": "https://global-selling.mercadolibre.com/maxwell/new-chat",
            "title": "Meli AI Chat",
            "visible": True,
            "top": 150,
            "right": 1200,
            "bottom": 580,
        },
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "find_chat_input",
        lambda driver, timeout, allow_default_content: object(),
    )

    assert bit_appeal_ai.switch_to_ai_chat_frame(driver, require_input=True)
    assert driver.switch_to.frames == [shadow_frame]


def test_click_inline_entry_recognizes_ready_shadow_iframe_without_click(monkeypatch):
    class SwitchTo:
        def default_content(self):
            return None

    class FakeDriver:
        def __init__(self):
            self.switch_to = SwitchTo()

        def execute_script(self, script):
            raise AssertionError("ready iframe must not click the opener")

    driver = FakeDriver()
    monkeypatch.setattr(
        bit_appeal_ai,
        "switch_to_ai_chat_frame",
        lambda driver, require_input=False: True,
    )

    mode = bit_appeal_ai.click_inline_ai_assistant_entry(driver)

    assert mode == bit_appeal_ai.AI_CHAT_MODE_IFRAME
    assert driver._mercado_ai_chat_mode == bit_appeal_ai.AI_CHAT_MODE_IFRAME


def test_open_ai_contact_window_does_not_toggle_visible_panel(monkeypatch):
    calls = {"ready": 0, "entry": 0}

    class SwitchTo:
        def default_content(self):
            return None

    class FakeDriver:
        def __init__(self):
            self.switch_to = SwitchTo()

    driver = FakeDriver()
    monkeypatch.setattr(
        bit_appeal_ai,
        "open_mercado_backend_page",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "_abort_ai_appeal_after_backend_recovery",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "detect_ai_chat_variant",
        lambda driver: bit_appeal_ai.AI_CHAT_MODE_IFRAME,
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "get_ai_chat_dom_state",
        lambda driver: {"visible_ai_frame_count": 1, "legacy_frame_count": 1},
    )

    def fake_wait(driver, timeout, require_input=False):
        calls["ready"] += 1
        return "" if calls["ready"] == 1 else bit_appeal_ai.AI_CHAT_MODE_IFRAME

    monkeypatch.setattr(bit_appeal_ai, "wait_for_ai_chat_ready", fake_wait)
    monkeypatch.setattr(
        bit_appeal_ai,
        "click_ai_assistant_entry",
        lambda *args: calls.__setitem__("entry", calls["entry"] + 1),
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "click_ai_entry_fallback",
        lambda *args: calls.__setitem__("entry", calls["entry"] + 1),
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "switch_to_ai_chat_frame",
        lambda driver, require_input=False: True,
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "recover_expired_ai_conversation",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "find_chat_input",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(bit_appeal_ai.time, "sleep", lambda seconds: None)

    mode = bit_appeal_ai.open_ai_contact_window(
        driver,
        "测试店铺",
        "墨西哥",
        "window-id",
    )

    assert mode == bit_appeal_ai.AI_CHAT_MODE_IFRAME
    assert calls["entry"] == 0
