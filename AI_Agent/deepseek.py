import os
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from openai import APITimeoutError, OpenAI

from erp.ai_usage_billing import record_usage


def _load_local_env():
    """Load project-local .env files without requiring python-dotenv."""
    current_dir = Path(__file__).resolve().parent
    candidates = [
        current_dir.parent / ".env",
        current_dir / ".env",
    ]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_local_env()

# Legacy constant/function names remain import-compatible for existing callers.
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_MODEL = "qwen3.7-flash"
DEEPSEEK_BASE_URL = QWEN_BASE_URL
DEEPSEEK_MODEL = QWEN_MODEL


def _read_key_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip().strip('"').strip("'")


def _get_deepseek_api_key() -> str:
    """Compatibility name: only read the current provider's credentials."""
    return os.getenv("DASHSCOPE_API_KEY", "").strip()


def _get_client(
    api_key: str | None = None,
    base_url: str | None = None,
) -> OpenAI:
    api_key = str(api_key or "").strip() or _get_deepseek_api_key()
    if not api_key:
        raise RuntimeError(
            "缺少阿里云百炼 API Key。请在集成与凭证设置中填写百炼 Key，"
            "或在 .env 配置 DASHSCOPE_API_KEY。"
        )

    return OpenAI(
        api_key=api_key,
        base_url=str(base_url or "").strip() or DEEPSEEK_BASE_URL,
    )


def chat_deepseek_with_usage(
    messages: Iterable[dict],
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    thinking: bool | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    thinking_budget: int | None = None,
) -> dict:
    """Return content and provider usage for callers that account for API costs.

    Unchanged callers can keep using ``chat_deepseek`` below.  Usage is left
    absent when the provider omits it; callers must not interpret that as zero.
    """
    endpoint = urlsplit(base_url or QWEN_BASE_URL)
    if (str(model or "").lower().startswith("deepseek")
            or endpoint.hostname == "api.deepseek.com"):
        raise ValueError("DeepSeek 生成已停用，请刷新页面使用 qwen3.7-flash 和百炼凭证。")
    if endpoint.hostname == "dashscope.aliyuncs.com" and thinking is None:
        thinking = False
    kwargs = {
        "model": model or DEEPSEEK_MODEL,
        "messages": list(messages),
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if response_format is not None:
        kwargs["response_format"] = response_format
    if thinking is not None:
        if urlsplit(base_url or DEEPSEEK_BASE_URL).hostname == "dashscope.aliyuncs.com":
            kwargs["extra_body"] = {"enable_thinking": thinking}
            if thinking and thinking_budget is not None:
                kwargs["extra_body"]["thinking_budget"] = thinking_budget
        else:
            kwargs["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}

    client = _get_client(
        api_key=api_key,
        base_url=base_url,
    )
    options = {}
    if timeout is not None:
        options["timeout"] = timeout
    if max_retries is not None:
        options["max_retries"] = max_retries
    if options:
        client = client.with_options(**options)
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as exc:
        # A gateway may reject the final result after reporting billable usage.
        # Never infer consumed tokens from prompts or failed network requests.
        payload = getattr(exc, "body", None)
        if isinstance(payload, dict) and payload.get("usage"):
            record_usage(payload, model=kwargs["model"],
                         base_url=base_url or DEEPSEEK_BASE_URL,
                         status="failed")
        elif isinstance(exc, (TimeoutError, APITimeoutError)):
            # The request may have reached the provider. Preserve a pending
            # audit row without inventing a token count or monetary amount.
            record_usage(usage=None, model=kwargs["model"],
                         base_url=base_url or DEEPSEEK_BASE_URL,
                         request_id=getattr(exc, "request_id", ""),
                         status="usage_unknown")
        raise
    # Persist immediately, before interpreting choices or structured content.
    # Malformed/truncated results can still have consumed paid model tokens.
    record_usage(response, model=getattr(response, "model", None) or kwargs["model"],
                 base_url=base_url or DEEPSEEK_BASE_URL,
                 request_id=getattr(response, "id", ""))
    usage = getattr(response, "usage", None)
    if usage is not None and hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    elif usage is not None and not isinstance(usage, dict):
        usage = vars(usage)
    choice = response.choices[0]
    return {
        "content": choice.message.content or "",
        "usage": usage,
        "model": getattr(response, "model", kwargs["model"]),
        "id": getattr(response, "id", ""),
        "created": getattr(response, "created", None),
        "finish_reason": getattr(choice, "finish_reason", None),
    }


def chat_deepseek(
    messages: Iterable[dict],
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    thinking: bool | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    thinking_budget: int | None = None,
) -> str:
    return chat_deepseek_with_usage(
        messages=messages, model=model, temperature=temperature,
        max_tokens=max_tokens, response_format=response_format,
        api_key=api_key, base_url=base_url, thinking=thinking,
        timeout=timeout, max_retries=max_retries, thinking_budget=thinking_budget,
    )["content"]


def get_ai_response(message: str) -> str:
    return chat_deepseek([{"role": "user", "content": message}])


if __name__ == "__main__":
    print(get_ai_response("请用中文回复：千问接入测试成功。"))
