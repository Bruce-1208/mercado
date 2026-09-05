from bit import bit_appeal_ai


def test_appeal_result_uses_latest_reply_without_calling_deepseek(monkeypatch):
    def unexpected_deepseek_call(*args, **kwargs):
        raise AssertionError("申诉结果整理不应调用 DeepSeek")

    from AI_Agent import deepseek
    monkeypatch.setattr(deepseek, "chat_deepseek", unexpected_deepseek_call)

    result = bit_appeal_ai.summarize_ai_appeal_result(
        "侵权-第1/1组",
        ["MLB123"],
        "请重新核查",
        ["正在核查", "已转交人工团队，请等待处理结果。"],
        force=True,
    )

    assert result == {
        "status": "待确认",
        "summary": "客服最后回复：已转交人工团队，请等待处理结果。",
        "success_ids": [],
        "failed_ids": [],
        "error": "",
    }


def test_appeal_result_without_reply_remains_pending():
    result = bit_appeal_ai.summarize_ai_appeal_result(
        "投诉",
        ["200001"],
        "请重新核查",
        [],
    )

    assert result["status"] == "待确认"
    assert result["summary"] == "未读取到 AI 客服回复，无法判断申诉结果。"
    assert result["success_ids"] == []
    assert result["failed_ids"] == []
    assert result["error"] == ""
