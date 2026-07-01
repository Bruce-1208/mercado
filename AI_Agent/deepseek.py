import os
from typing import Iterable

from openai import OpenAI


DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")


def _get_client() -> OpenAI:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "缺少 DeepSeek API Key，请先设置环境变量 DEEPSEEK_API_KEY。"
        )

    return OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)


def chat_deepseek(
    messages: Iterable[dict],
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    kwargs = {
        "model": model or DEEPSEEK_MODEL,
        "messages": list(messages),
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    response = _get_client().chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""


def get_ai_response(message: str) -> str:
    return chat_deepseek([{"role": "user", "content": message}])


if __name__ == "__main__":
    print(get_ai_response("请用中文回复：DeepSeek 接入测试成功。"))
