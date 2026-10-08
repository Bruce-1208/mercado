from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from erp.ai_generation_budget import (
    BEIJING, BudgetedChat, GenerationBudgetError, is_safe_offpeak,
    next_safe_offpeak, prompt_token_bound,
)


OFFPEAK = datetime(2026, 10, 3, 10, 0, tzinfo=BEIJING)
MESSAGES = [{"role": "user", "content": "生成水瓶文案"}]


def response(prompt=100, completion=100, **usage):
    return {"content": '{"title":"Botella"}', "model": "deepseek-flash",
            "finish_reason": "stop", "usage": {
                "prompt_tokens": prompt, "completion_tokens": completion, **usage}}


def make_chat(transport, **kwargs):
    kwargs.setdefault("now", lambda: OFFPEAK)
    return BudgetedChat(transport=transport, **kwargs)


def test_reservation_is_durable_before_request_and_locks_safe_parameters():
    snapshots = []
    def transport(**kwargs):
        assert snapshots[-1]["calls"][-1]["state"] == "pending"
        assert snapshots[-1]["reserved_yuan"] > 0
        assert kwargs["model"] == "deepseek-flash"
        assert kwargs["thinking"] is False
        assert kwargs["max_retries"] == 0
        assert kwargs["base_url"] == "https://api.deepseek.com"
        assert kwargs["max_tokens"] < 8000
        return response()
    chat = make_chat(transport, on_update=snapshots.append)
    assert chat(MESSAGES, model="deepseek-v4-pro", thinking=True, max_retries=8,
                api_key="not-saved-secret", max_tokens=8000) == '{"title":"Botella"}'
    assert chat.snapshot()["spent_yuan"] == 0.0005
    assert chat.snapshot()["reserved_yuan"] == 0
    assert "not-saved-secret" not in str(snapshots)


def test_exact_cap_allowed_and_one_token_above_clipped_without_rounding():
    bound = prompt_token_bound(MESSAGES)
    cap_nano = bound * 2000 + 128 * 8000
    calls = []
    def transport(**kwargs):
        calls.append(kwargs)
        return response(prompt=bound, completion=128)
    chat = make_chat(transport, cap_yuan=Decimal(cap_nano) / 1_000_000_000,
                     offpeak_only=False,
                     now=lambda: datetime(2026, 10, 5, 10, tzinfo=BEIJING))
    chat(MESSAGES, max_tokens=129)
    assert calls[0]["max_tokens"] == 128
    assert chat.remaining_yuan == 0
    with pytest.raises(GenerationBudgetError, match="剩余预算不足"):
        chat([{"role": "user", "content": "different request"}], max_tokens=128)
    assert len(calls) == 1


def test_one_nanoyuan_below_threshold_blocks_before_call():
    bound = prompt_token_bound(MESSAGES)
    cap_nano = bound * 2000 + 128 * 8000 - 1
    chat = make_chat(lambda **kw: pytest.fail("must not make a paid call"),
                     cap_yuan=Decimal(cap_nano) / 1_000_000_000)
    with pytest.raises(GenerationBudgetError):
        chat(MESSAGES, max_tokens=128)


def test_cache_usage_is_charged_and_exact_response_reused_after_restore():
    calls = []
    def transport(**kwargs):
        calls.append(kwargs)
        return response(prompt=100, completion=100,
                        prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20)
    first = make_chat(transport)
    answer = first(MESSAGES, max_tokens=500)
    snapshot = first.snapshot()
    assert snapshot["spent_yuan"] == 0.0004216
    restored = make_chat(transport, ledger=snapshot)
    assert restored(MESSAGES, max_tokens=500) == answer
    assert len(calls) == 1
    assert restored.snapshot()["cache_hits"] == 1
    restored([{"role": "user", "content": "different"}], max_tokens=500)
    assert restored.snapshot()["spent_yuan"] == 0.0008432
    assert len(calls) == 2


def test_revisions_advance_and_cached_result_remains_free_after_block():
    snapshots = []
    def transport(**kwargs):
        return response()
    chat = make_chat(transport, on_update=snapshots.append)
    answer = chat(MESSAGES)
    assert [item["revision"] for item in snapshots] == [1, 2]
    with pytest.raises(GenerationBudgetError):
        chat([{"role": "user", "content": "large" * 10000}])
    assert chat.snapshot()["revision"] == 3
    restored = make_chat(lambda **kw: pytest.fail("only cache can be used"), ledger=chat.snapshot())
    assert restored(MESSAGES) == answer
    assert restored.snapshot()["revision"] == 4
    with pytest.raises(GenerationBudgetError):
        restored([{"role": "user", "content": "new request"}])


def test_cache_size_is_bounded_and_restoring_cannot_raise_cap():
    chat = make_chat(lambda **kwargs: response(), cap_yuan="0.02")
    for index in range(13):
        chat([{"role": "user", "content": str(index)}], max_tokens=200)
    assert len(chat.snapshot()["cache"]) == 12
    restored = make_chat(lambda **kwargs: response(), ledger=chat.snapshot(), cap_yuan="1.00")
    assert restored.snapshot()["cap_yuan"] == 0.02
    assert restored.snapshot()["spent_yuan"] == 0.0065


@pytest.mark.parametrize("result", [
    {"content": "answer", "usage": None},
    response(prompt=-1),
    response(prompt=100, prompt_cache_hit_tokens=90, prompt_cache_miss_tokens=40),
    response(total_tokens=999),
])
def test_unreliable_usage_keeps_reservation_and_halts_after_restore(result):
    calls = []
    def transport(**kwargs):
        calls.append(kwargs)
        return result
    chat = make_chat(transport)
    with pytest.raises(GenerationBudgetError) as caught:
        chat(MESSAGES)
    assert caught.value.kind == "usage_unknown"
    saved = chat.snapshot()
    assert saved["reserved_yuan"] > 0
    assert saved["spent_yuan"] == 0
    restored = make_chat(transport, ledger=saved)
    with pytest.raises(GenerationBudgetError):
        restored(MESSAGES)
    assert len(calls) == 1


def test_timeout_retains_funds_and_does_not_implicitly_retry():
    calls = []
    def transport(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("credentials must never be persisted")
    chat = make_chat(transport)
    with pytest.raises(GenerationBudgetError):
        chat(MESSAGES)
    with pytest.raises(GenerationBudgetError):
        chat(MESSAGES)
    saved = chat.snapshot()
    assert len(calls) == 1
    assert saved["calls"][0]["state"] == "unknown"
    assert saved["calls"][0]["error_type"] == "TimeoutError"
    assert "credentials" not in str(saved)


def test_crash_after_reservation_blocks_restored_product():
    snapshots = []
    class ProcessStopped(BaseException):
        pass
    def transport(**kwargs):
        raise ProcessStopped()
    chat = make_chat(transport, on_update=snapshots.append)
    with pytest.raises(ProcessStopped):
        chat(MESSAGES)
    assert snapshots[-1]["calls"][0]["state"] == "pending"
    restored = make_chat(lambda **kw: pytest.fail("crash must be reconciled"), ledger=snapshots[-1])
    with pytest.raises(GenerationBudgetError):
        restored(MESSAGES)


def test_save_failure_prevents_dispatch():
    def save(snapshot):
        raise OSError("disk unavailable")
    chat = make_chat(lambda **kw: pytest.fail("no dispatch without durable reserve"), on_update=save)
    with pytest.raises(GenerationBudgetError) as caught:
        chat(MESSAGES)
    assert caught.value.kind == "ledger_save_failed"


@pytest.mark.parametrize("url", ["https://proxy.example.com", "http://api.deepseek.com",
                                "https://api.deepseek.com@evil.example/v1",
                                "https://api.deepseek.com/other", "https://api.deepseek.com?x=1"])
def test_custom_provider_is_rejected(url):
    chat = make_chat(lambda **kw: pytest.fail("unverified prices"))
    with pytest.raises(GenerationBudgetError) as caught:
        chat(MESSAGES, base_url=url)
    assert caught.value.kind == "unsupported_provider"


def test_text_bound_accounts_for_utf8_and_rejects_images():
    assert prompt_token_bound(MESSAGES) > len("生成水瓶文案".encode("utf-8"))
    with pytest.raises(GenerationBudgetError):
        prompt_token_bound([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}])


def test_peak_window_boundaries_and_weekend():
    for hour, allowed in [(8, True), (9, False), (11, False), (12, True),
                          (13, True), (14, False), (17, False), (18, True)]:
        when = datetime(2026, 10, 5, hour, tzinfo=BEIJING)
        assert is_safe_offpeak(when) is allowed
        assert is_safe_offpeak(next_safe_offpeak(when))
    assert is_safe_offpeak(OFFPEAK)
    peak = datetime(2026, 10, 5, 10, 15, tzinfo=BEIJING)
    assert next_safe_offpeak(peak) == peak.replace(hour=12, minute=0)


def test_wait_for_offpeak_is_temporary_and_does_not_reserve():
    clock = [datetime(2026, 10, 5, 10, tzinfo=BEIJING)]
    chat = make_chat(lambda **kwargs: response(), now=lambda: clock[0])
    with pytest.raises(GenerationBudgetError) as caught:
        chat(MESSAGES)
    assert caught.value.kind == "wait_offpeak"
    assert chat.snapshot()["calls"] == []
    clock[0] = clock[0].replace(hour=12)
    chat(MESSAGES)
    assert chat.snapshot()["spent_yuan"] == 0.0005


def test_crossing_into_peak_uses_peak_tariff():
    times = iter([datetime(2026, 10, 5, 8, 59, 59, tzinfo=BEIJING),
                  datetime(2026, 10, 5, 9, 0, 1, tzinfo=BEIJING)])
    chat = make_chat(lambda **kwargs: response(), now=lambda: next(times))
    chat(MESSAGES)
    assert chat.snapshot()["spent_yuan"] == 0.001
    assert chat.snapshot()["calls"][0]["rate_period"] == "peak"


def test_provider_exceeding_bound_records_cost_and_halts():
    chat = make_chat(lambda **kwargs: response(completion=kwargs["max_tokens"] + 1))
    with pytest.raises(GenerationBudgetError) as caught:
        chat(MESSAGES, max_tokens=500)
    assert caught.value.kind == "usage_bound_exceeded"
    assert chat.snapshot()["spent_yuan"] > 0
    assert chat.snapshot()["cache"] == {}


def test_corrupt_ledger_cannot_reset_spending():
    with pytest.raises(GenerationBudgetError) as caught:
        make_chat(lambda **kwargs: response(), ledger={"version": 2})
    assert caught.value.kind == "invalid_ledger"


def test_deepseek_content_api_remains_compatible_and_usage_is_exposed(monkeypatch):
    from AI_Agent import deepseek
    options = []
    provider = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hello"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
        model="deepseek-flash", id="response-id", created=1)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: provider)))
    def with_options(**kwargs):
        options.append(kwargs)
        return client
    client.with_options = with_options
    monkeypatch.setattr(deepseek, "_get_client", lambda **kwargs: client)
    assert deepseek.chat_deepseek(MESSAGES) == "hello"
    result = deepseek.chat_deepseek_with_usage(MESSAGES, thinking=False, max_retries=0)
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 20}
    assert options[-1]["max_retries"] == 0


def test_uncapped_restores_exhausted_ledger_and_preserves_spend_and_output():
    old = make_chat(lambda **kw: response())
    old(MESSAGES)
    ledger = old.snapshot()
    ledger.update(blocked_kind='budget_exhausted', blocked_reason='limit reached')
    calls = []
    chat = make_chat(lambda **kw: calls.append(kw) or response(), ledger=ledger, enforce_cap=False)
    chat([{'role': 'user', 'content': 'x' * 20000}], max_tokens=8000)
    assert calls[0]['max_tokens'] == 8000
    assert chat.snapshot()['spent_yuan'] == 0.001
    assert chat.snapshot()['total_tokens'] == 400
    assert chat.snapshot()['remaining_yuan'] is None
    assert chat.snapshot()['budget_limit_enabled'] is False


def test_uncapped_keeps_unknown_usage_blocked():
    ledger = make_chat(lambda **kw: response()).snapshot()
    ledger.update(blocked_kind='usage_unknown', blocked_reason='unknown charge')
    chat = make_chat(lambda **kw: pytest.fail('must not call'), ledger=ledger, enforce_cap=False)
    with pytest.raises(GenerationBudgetError, match='unknown charge'):
        chat(MESSAGES)


@pytest.mark.parametrize('prompt,expected', [(32000, .00648), (32001, .0194406), (256001, .3076812)])
def test_qwen_tiers_peak_hours_and_preserved_legacy_spend(prompt, expected):
    from erp.ai_generation_budget import QWEN_MODEL, QWEN_BASE_URL
    legacy = make_chat(lambda **kw: response())
    legacy(MESSAGES)
    calls = []
    def transport(**kwargs):
        calls.append(kwargs)
        return {**response(prompt=prompt, completion=100), 'model': QWEN_MODEL}
    chat = BudgetedChat(model=QWEN_MODEL, ledger=legacy.snapshot(), transport=transport,
                        enforce_cap=False, now=lambda: datetime(2026, 10, 5, 10, tzinfo=BEIJING))
    chat([{'role': 'user', 'content': 'x' * prompt}], api_key='test-qwen')
    assert calls[0]['base_url'] == QWEN_BASE_URL
    assert calls[0]['model'] == QWEN_MODEL
    assert calls[0]['thinking'] is False
    ledger = chat.snapshot()
    assert ledger['calls'][0]['model'] == 'deepseek-flash'
    assert ledger['calls'][0]['cost_yuan'] == .0005
    assert ledger['calls'][-1]['cost_yuan'] == expected
    assert ledger['calls'][-1]['rate_period'] == 'standard'
    assert ledger['spent_yuan'] == pytest.approx(.0005 + expected)


def test_qwen_adapter_uses_enable_thinking(monkeypatch):
    from AI_Agent import deepseek
    from erp.ai_generation_budget import QWEN_MODEL, QWEN_BASE_URL
    calls = []
    provider = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'), finish_reason='stop')],
                               usage={'prompt_tokens': 10, 'completion_tokens': 2}, model=QWEN_MODEL)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: calls.append(kw) or provider)))
    monkeypatch.setattr(deepseek, '_get_client', lambda **kw: client)
    deepseek.chat_deepseek_with_usage(MESSAGES, model=QWEN_MODEL, base_url=QWEN_BASE_URL,
                                      api_key='test', thinking=False)
    assert calls[0]['extra_body'] == {'enable_thinking': False}


def test_qwen_requires_own_key_before_any_request():
    from erp.ai_generation_budget import QWEN_MODEL
    chat = BudgetedChat(model=QWEN_MODEL, transport=lambda **kw: pytest.fail('must not send DeepSeek key'))
    with pytest.raises(ValueError, match='百炼 API Key'):
        chat(MESSAGES)
