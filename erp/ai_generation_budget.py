"""Fail-closed, per-product accounting for text-only official DeepSeek Flash and Qwen3.7-Flash.

Prices were verified against the Chinese (CNY) pricing page on 2026-10-03.
Reservations use peak cache-miss rates and a conservative UTF-8 byte bound,
not an average characters/token estimate.  This cap covers these text calls,
not image services or any API calls made outside this wrapper.  Price changes
require reviewing the pinned tariff below before continuing to promise a cap.
The caller must serialize generation for a product and durably save on_update.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Iterable
from zoneinfo import ZoneInfo


MODEL = "deepseek-flash"
QWEN_MODEL = "qwen3.7-flash"
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_PRICING_SOURCE = "https://help.aliyun.com/zh/model-studio/qwen3-7-flash"

def qwen_rates(prompt_tokens):
    """Conservative billing: cache discounts omitted; tiers use total input."""
    if prompt_tokens <= 32_000:
        return (200, 200, 800)
    if prompt_tokens <= 256_000:
        return (600, 600, 2400)
    return (1200, 1200, 4800)

PRICING_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
PRICING_VERIFIED_ON = "2026-10-03"
BEIJING = ZoneInfo("Asia/Shanghai")
NANO_PER_YUAN = 1_000_000_000
# Integer nanoyuan per token; tariff includes cache reads, misses and output.
OFFPEAK_RATES = (20, 1000, 4000)
PEAK_RATES = (40, 2000, 8000)
DEFAULT_CAP = 30_000_000
MAX_CACHE_ENTRIES = 12


class GenerationBudgetError(RuntimeError):
    """A product must stop automatic paid calls until the issue is resolved."""

    def __init__(self, message: str, *, kind: str = "budget_exhausted"):
        super().__init__(message)
        self.kind = kind


def _beijing(now: datetime | None = None) -> datetime:
    now = now or datetime.now(BEIJING)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING)
    return now.astimezone(BEIJING)


def is_safe_offpeak(now: datetime | None = None) -> bool:
    """Conservative schedule: no holiday calendar; weekday holidays wait too."""
    now = _beijing(now)
    return now.weekday() >= 5 or not (9 <= now.hour < 12 or 14 <= now.hour < 18)


def next_safe_offpeak(now: datetime | None = None) -> datetime:
    now = _beijing(now)
    if is_safe_offpeak(now):
        return now
    return now.replace(hour=12 if now.hour < 12 else 18, minute=0, second=0, microsecond=0)


def _interval_offpeak(start: datetime, end: datetime) -> bool:
    """Use peak if any part of a request might have been processed at peak."""
    if end < start or not is_safe_offpeak(start) or not is_safe_offpeak(end):
        return False
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        if day.weekday() < 5:
            for left, right in ((9, 12), (14, 18)):
                if start < day.replace(hour=right) and end >= day.replace(hour=left):
                    return False
        day += timedelta(days=1)
    return True


def _nano(value) -> int:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid budget amount")
    return int(amount * NANO_PER_YUAN)


def _yuan(value: int) -> float:
    return float(Decimal(value) / NANO_PER_YUAN)


def prompt_token_bound(messages: list[dict], response_format: dict | None = None) -> int:
    """Text UTF-8 bytes plus ample chat-template/JSON-mode framing allowance.

    Reject images/tools/structured content: their token charge cannot be
    bounded by the bytes of their URL or request JSON.
    """
    for message in messages:
        if (not isinstance(message, dict)
                or message.get("role") not in {"system", "user", "assistant"}
                or not isinstance(message.get("content"), str)
                or set(message) - {"role", "content", "name"}):
            raise GenerationBudgetError("当前商品生成模式仅支持纯文本消息。", kind="unsupported_input")
    payload = json.dumps({"messages": messages, "response_format": response_format},
                         ensure_ascii=False, separators=(",", ":"))
    return len(payload.encode("utf-8")) + 256 + 64 * len(messages)


def _usage_counts(usage: dict) -> tuple[int, int, int]:
    if not isinstance(usage, dict):
        raise ValueError("missing usage")

    def count(name, default=None):
        value = usage.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid {name}")
        return value

    prompt = count("prompt_tokens")
    completion = count("completion_tokens")
    # Cache details are optional. Missing details are conservatively charged
    # as full price; inconsistent details cannot be trusted for accounting.
    hit = count("prompt_cache_hit_tokens", 0)
    miss = count("prompt_cache_miss_tokens", prompt - hit)
    if hit + miss != prompt:
        raise ValueError("cache token sum differs from prompt tokens")
    if "total_tokens" in usage and count("total_tokens") != prompt + completion:
        raise ValueError("total token sum differs")
    return hit, miss, completion


class BudgetedChat:
    """A chat_deepseek-compatible callable sharing one durable product ledger.

    ``transport`` returns ``{content, usage, ...}`` like
    ``chat_deepseek_with_usage``. ``on_update`` must raise if durable storage
    fails. ``before_request`` may wait for an offpeak slot; no funds have been
    reserved when it runs. Capped runs block on pending/unknown calls because an interrupted request
    may already have been billed. Uncapped retries retain those reservations
    for accounting while allowing new calls.
    """

    def __init__(self, ledger: dict | None = None, *, on_update: Callable | None = None,
                 transport: Callable | None = None, now: Callable | None = None,
                 before_request: Callable | None = None, cap_yuan="0.03",
                 offpeak_only: bool = True, min_output_tokens: int = 128,
                 enforce_cap: bool = True, model: str = MODEL):
        if model not in {MODEL, QWEN_MODEL}:
            raise ValueError("Unsupported accounting model")
        self.model = model
        self.is_qwen = model == QWEN_MODEL
        self._lock = threading.RLock()
        self._on_update = on_update
        self._transport = transport
        self._now = now or (lambda: datetime.now(BEIJING))
        self._before_request = before_request
        self.offpeak_only = offpeak_only and not self.is_qwen
        self.enforce_cap = enforce_cap
        self.min_output_tokens = max(1, int(min_output_tokens))
        requested_cap = min(DEFAULT_CAP, _nano(cap_yuan))
        self._ledger = copy.deepcopy(ledger) if ledger else {
            "version": 1, "revision": 0, "cap_nano": requested_cap, "calls": [],
            "cache": {}, "cache_hits": 0, "blocked_reason": "", "blocked_kind": "",
        }
        try:
            if self._ledger.get("version") != 1:
                raise ValueError("unsupported ledger version")
            self._ledger["revision"] = self._int(self._ledger.get("revision", 0))
            self._ledger["cap_nano"] = min(requested_cap, self._int(self._ledger["cap_nano"]))
            for entry in self._ledger["calls"]:
                entry.setdefault("model", self._ledger.get("model", MODEL))
                self._int(entry["reserved_nano"])
                if entry["state"] == "complete":
                    self._int(entry["cost_nano"])
                elif entry["state"] in {"pending", "unknown"}:
                    # Unknown charges only block capped runs. Never replace a
                    # storage/provider safety failure with a retryable reason.
                    if self.enforce_cap and self._ledger.get("blocked_kind", "") in {
                        "", "budget_exhausted", "usage_unknown",
                    }:
                        self._ledger["blocked_reason"] = "上次调用费用尚未确认，已保留预算；请核对模型供应商账单后人工处理。"
                        self._ledger["blocked_kind"] = "usage_unknown"
                else:
                    raise ValueError("invalid call state")
            if not isinstance(self._ledger.get("cache"), dict):
                raise ValueError("invalid cache")
        except (KeyError, TypeError, ValueError) as exc:
            raise GenerationBudgetError("商品费用记录损坏，已停止付费调用。", kind="invalid_ledger") from exc

        # Uncapped retries retain unknown charges for accounting, but a past
        # interrupted request must not permanently prevent new generation.
        if not self.enforce_cap and self._ledger.get("blocked_kind") in {"budget_exhausted", "usage_unknown"}:
            self._ledger.update(blocked_reason="", blocked_kind="")

    @staticmethod
    def _int(value) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid ledger amount")
        return value

    def _totals(self) -> tuple[int, int]:
        spent = sum(e["cost_nano"] for e in self._ledger["calls"] if e["state"] == "complete")
        reserved = sum(e["reserved_nano"] for e in self._ledger["calls"] if e["state"] != "complete")
        return spent, reserved

    @property
    def remaining_yuan(self) -> float:
        spent, reserved = self._totals()
        return _yuan(max(0, self._ledger["cap_nano"] - spent - reserved))

    def snapshot(self) -> dict:
        snapshot = copy.deepcopy(self._ledger)
        spent, reserved = self._totals()
        snapshot.update({
            "currency": "CNY", "model": self.model, "cap_yuan": _yuan(snapshot["cap_nano"]),
            "spent_yuan": _yuan(spent), "reserved_yuan": _yuan(reserved),
            "remaining_yuan": self.remaining_yuan if self.enforce_cap else None,
            "budget_limit_enabled": self.enforce_cap,
            "has_unconfirmed_usage": any(e["state"] in {"pending", "unknown"} for e in snapshot["calls"]),
            "total_tokens": sum(e.get("prompt_tokens", 0) + e.get("completion_tokens", 0)
                                for e in snapshot["calls"] if e["state"] == "complete"),
            "status": "blocked" if snapshot.get("blocked_reason") else "ready",
            "pricing_verified_on": "2026-10-06" if self.is_qwen else PRICING_VERIFIED_ON,
            "pricing_source": QWEN_PRICING_SOURCE if self.is_qwen else PRICING_SOURCE,
        })
        for entry in snapshot["calls"]:
            entry["reserved_yuan"] = _yuan(entry["reserved_nano"])
            if "cost_nano" in entry:
                entry["cost_yuan"] = _yuan(entry["cost_nano"])
        return snapshot

    def reject_response(self, response):
        """Evict rejected content, preserving every billed attempt."""
        with self._lock:
            keys = [key for key, value in self._ledger["cache"].items() if value == response]
            if keys:
                for key in keys:
                    del self._ledger["cache"][key]
                self._save()

    def _save(self):
        self._ledger["revision"] += 1
        if self._on_update:
            try:
                self._on_update(self.snapshot())
            except Exception as exc:
                self._ledger["blocked_reason"] = "费用记录保存失败，已停止后续付费调用。"
                self._ledger["blocked_kind"] = "ledger_save_failed"
                raise GenerationBudgetError(self._ledger["blocked_reason"], kind="ledger_save_failed") from exc

    def _block(self, message: str, kind: str):
        self._ledger["blocked_reason"] = message
        self._ledger["blocked_kind"] = kind
        self._save()
        raise GenerationBudgetError(message, kind=kind)

    def __call__(self, messages: Iterable[dict], model=None, temperature=None,
                 max_tokens=None, response_format=None, api_key=None, base_url=None,
                 thinking=None, timeout=None, max_retries=None) -> str:
        with self._lock:
            return self._call(messages, temperature, max_tokens, response_format,
                              api_key, base_url, timeout)

    def _call(self, messages, temperature, max_tokens, response_format, api_key, base_url, timeout):
        # Do not let custom gateways apply unverified tariffs. Always provide
        # the validated URL explicitly so environment defaults cannot override.
        expected_url = QWEN_BASE_URL if self.is_qwen else "https://api.deepseek.com"
        base_url = str(base_url or expected_url).strip().rstrip("/")
        allowed = {expected_url} if self.is_qwen else {expected_url, expected_url + "/v1"}
        if base_url not in allowed:
            self._block("当前商品生成模式仅支持对应模型官方接口。", "unsupported_provider")
        if self.is_qwen and not str(api_key or "").strip():
            raise ValueError("缺少百炼 API Key，请在集成与凭证设置中配置。")
        messages = list(messages)
        bound = prompt_token_bound(messages, response_format)
        requested_output = min(131_072 if self.is_qwen else 384_000, int(max_tokens if max_tokens is not None else 2000))
        if requested_output < 1:
            raise ValueError("max_tokens must be positive")
        fingerprint = hashlib.sha256(json.dumps({
            "messages": messages, "temperature": temperature, "max_tokens": requested_output,
            "response_format": response_format, "model": self.model,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        cached = self._ledger["cache"].get(fingerprint)
        if isinstance(cached, str):
            self._ledger["cache_hits"] += 1
            self._save()
            return cached
        if self._ledger.get("blocked_reason"):
            raise GenerationBudgetError(self._ledger["blocked_reason"],
                                        kind=self._ledger.get("blocked_kind") or "budget_exhausted")
        if self._before_request:
            self._before_request()
        started = _beijing(self._now())
        if self.offpeak_only and not is_safe_offpeak(started):
            # Scheduling is a temporary condition, not a permanently blocked
            # ledger. The caller can queue the same product for the next slot.
            raise GenerationBudgetError(
                f"当前为保守高峰时段，将于北京时间 {next_safe_offpeak(started):%m-%d %H:%M} 后生成。",
                kind="wait_offpeak")
        spent, reserved = self._totals()
        remaining = self._ledger["cap_nano"] - spent - reserved
        reserve_rates = qwen_rates(bound) if self.is_qwen else PEAK_RATES
        input_reserve = bound * reserve_rates[1]
        output_limit = (min(requested_output, (remaining - input_reserve) // reserve_rates[2])
                        if self.enforce_cap else requested_output)
        if output_limit < min(self.min_output_tokens, requested_output):
            self._block("本商品剩余预算不足，已停止自动生成；请人工补齐内容。", "budget_exhausted")
        entry = {
            "state": "pending", "started_at": started.isoformat(),
            "model": self.model, "pricing_source": QWEN_PRICING_SOURCE if self.is_qwen else PRICING_SOURCE,
            "request_hash": fingerprint, "prompt_token_bound": bound,
            "max_tokens": output_limit,
            "reserved_nano": input_reserve + output_limit * reserve_rates[2],
        }
        self._ledger["calls"].append(entry)
        self._save()  # MUST succeed before even creating the paid request.
        try:
            transport = self._transport
            if transport is None:
                from AI_Agent.deepseek import chat_deepseek_with_usage
                transport = chat_deepseek_with_usage
            result = transport(messages=messages, model=self.model, temperature=temperature,
                               max_tokens=output_limit, response_format=response_format,
                               api_key=api_key, base_url=base_url, thinking=False,
                               timeout=timeout if timeout is not None else 120,
                               max_retries=0)
        except Exception as exc:
            entry["state"] = "unknown"
            # Provider exception text may contain credentials or request data.
            entry["error_type"] = type(exc).__name__
            from erp.ai_original_products import original_model_account_error, OriginalModelAccountError
            account_error = original_model_account_error(exc)
            if account_error:
                self._save()
                raise OriginalModelAccountError(account_error) from exc
            message = ("模型调用中断，本次费用待确认；可重新生成继续，历史预留费用已保留。"
                       if not self.enforce_cap else
                       "模型调用未取得可确认用量，已保留本次预算并停止重试；请核对账单后人工处理。")
            self._block(message, "usage_unknown")
        ended = _beijing(self._now())
        try:
            if not isinstance(result, dict) or not isinstance(result.get("content"), str):
                raise ValueError("invalid response")
            hit, miss, completion = _usage_counts(result.get("usage"))
            returned_model = str(result.get("model") or self.model).lower()
            if ((self.is_qwen and returned_model not in {QWEN_MODEL, "qwen3.7-flash-2026-07-15"})
                    or (not self.is_qwen and ("flash" not in returned_model or "pro" in returned_model))):
                raise ValueError("unexpected billed model")
        except (TypeError, ValueError) as exc:
            entry["state"] = "unknown"
            entry["error_type"] = type(exc).__name__
            message = ("模型未返回可靠用量，本次费用待确认；可重新生成继续，历史预留费用已保留。"
                       if not self.enforce_cap else
                       "模型未返回可靠 Token 用量，已保留本次预算并停止重试；请核对账单后人工处理。")
            self._block(message, "usage_unknown")
        offpeak = _interval_offpeak(started, ended)
        rates = qwen_rates(hit + miss) if self.is_qwen else (OFFPEAK_RATES if offpeak else PEAK_RATES)
        cost = hit * rates[0] + miss * rates[1] + completion * rates[2]
        entry.update({"state": "complete", "ended_at": ended.isoformat(),
                      "prompt_tokens": hit + miss, "completion_tokens": completion,
                      "cache_hit_tokens": hit, "cache_miss_tokens": miss,
                      "cost_nano": cost, "rate_period": "standard" if self.is_qwen else ("offpeak" if offpeak else "peak"),
                      "finish_reason": result.get("finish_reason")})
        if hit + miss > bound or completion > output_limit or cost > entry["reserved_nano"]:
            self._block("接口报告用量超过预留上限，已记录费用并停止后续调用；请人工核对。", "usage_bound_exceeded")
        # Truncated output should not be replayed as a successful result.
        if result.get("finish_reason") in (None, "stop"):
            self._ledger["cache"][fingerprint] = result["content"]
            while len(self._ledger["cache"]) > MAX_CACHE_ENTRIES:
                del self._ledger["cache"][next(iter(self._ledger["cache"]))]
        self._save()
        return result["content"]
