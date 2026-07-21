from bit import bit_appeal_ai


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
    assert records[-1][0] == "delay_agent_reply"
    assert records[-1][1]["response"] == "本组处理完成"
