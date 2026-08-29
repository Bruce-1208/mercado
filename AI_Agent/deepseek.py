import os
import sys
from getpass import getpass
from pathlib import Path
from typing import Iterable

from openai import OpenAI


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

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")


def _read_key_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip().strip('"').strip("'")


def _get_deepseek_api_key() -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if api_key:
        return api_key

    current_dir = Path(__file__).resolve().parent
    candidates = [
        current_dir / "deepseek_key.txt",
        current_dir.parent / "deepseek_key.txt",
    ]
    for key_path in candidates:
        api_key = _read_key_file(key_path)
        if api_key:
            os.environ["DEEPSEEK_API_KEY"] = api_key
            return api_key

    if sys.stdin.isatty():
        api_key = getpass("请输入 DeepSeek API Key：").strip()
        if api_key:
            os.environ["DEEPSEEK_API_KEY"] = api_key
            return api_key

    return ""


def _get_client() -> OpenAI:
    api_key = _get_deepseek_api_key()
    if not api_key:
        raise RuntimeError(
            "缺少 DeepSeek API Key。请任选一种方式配置："
            "1）在项目根目录创建 deepseek_key.txt；"
            "2）在 AI_Agent/deepseek_key.txt 写入 key；"
            "3）在 .env 写入 DEEPSEEK_API_KEY=你的key。"
        )

    return OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)


def chat_deepseek(
    messages: Iterable[dict],
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
) -> str:
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

    response = _get_client().chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""


def get_ai_response(message: str) -> str:
    return chat_deepseek([{"role": "user", "content": message}])


if __name__ == "__main__":
    print(get_ai_response("请用中文回复：DeepSeek 接入测试成功。"))
