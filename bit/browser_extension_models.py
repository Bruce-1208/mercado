"""Encrypted, account-bound model API credentials for the Zeshun console."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent
    / "runtime_locks"
    / "browser_extension_models.json"
)
PROVIDERS = ("deepseek", "dashscope", "openai", "seedance", "wan")
PUBLIC_FIELDS = ("wan_workspace_id",)
_lock = threading.RLock()


def _path(path=None) -> Path:
    configured = str(
        os.environ.get("BIT_BROWSER_EXTENSION_MODEL_CONFIG") or ""
    ).strip()
    return Path(path or configured or DEFAULT_CONFIG_PATH).expanduser().resolve()


def _fernet(secret_key):
    # Model credential storage is served by the workbench, not executed by a
    # Local Agent.  Delay the optional dependency so appeal workers can import
    # the shared interface with their smaller packaged runtime.
    from cryptography.fernet import Fernet

    raw = str(secret_key or "").encode("utf-8")
    key = hashlib.sha256(b"zeshun-browser-extension-models\0" + raw).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _load(path=None) -> dict:
    config_path = _path(path)
    if not config_path.is_file():
        return {"version": 1, "users": {}}
    with _lock:
        try:
            value = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "users": {}}
    if not isinstance(value, dict) or not isinstance(value.get("users"), dict):
        return {"version": 1, "users": {}}
    return value


def _write(value: dict, path=None) -> None:
    config_path = _path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(".json.tmp")
    with _lock:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, config_path)


def _decrypt(record: dict, provider: str, secret_key) -> str:
    from cryptography.fernet import InvalidToken

    encrypted = str(record.get(f"{provider}_api_key_encrypted") or "")
    if not encrypted:
        return ""
    try:
        return _fernet(secret_key).decrypt(encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"{provider} API Key 无法解密，请在控制台“集成与凭证设置”中重新保存") from exc


def get_public_settings(user_id, secret_key, path=None) -> dict:
    record = dict(_load(path).get("users", {}).get(str(int(user_id)), {}) or {})
    configured = {
        provider: bool(record.get(f"{provider}_api_key_encrypted"))
        for provider in PROVIDERS
    }
    return {
        **{
            f"{provider}_configured": configured[provider]
            for provider in PROVIDERS
        },
        "wan_workspace_id": str(record.get("wan_workspace_id") or ""),
        "updated_at": record.get("updated_at"),
    }


def save_settings(user_id, payload, secret_key, path=None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("模型 API 配置格式无效")
    allowed = {f"{provider}_api_key" for provider in PROVIDERS} | set(PUBLIC_FIELDS)
    if set(payload) - allowed:
        raise ValueError("模型 API 配置包含未知字段")
    value = _load(path)
    user_key = str(int(user_id))
    record = dict(value["users"].get(user_key, {}) or {})
    changed = False
    for provider in PROVIDERS:
        field = f"{provider}_api_key"
        if field not in payload:
            continue
        api_key = str(payload.get(field) or "").strip()
        # Empty password inputs intentionally preserve the saved credential.
        if not api_key:
            continue
        if len(api_key) > 8192:
            raise ValueError(f"{provider} API Key 长度无效")
        record[f"{provider}_api_key_encrypted"] = _fernet(secret_key).encrypt(
            api_key.encode("utf-8")
        ).decode("ascii")
        changed = True
    for field in PUBLIC_FIELDS:
        if field not in payload:
            continue
        value_text = str(payload.get(field) or "").strip()
        if len(value_text) > 200:
            raise ValueError(f"{field} 长度无效")
        record[field] = value_text
        changed = True
    if not changed:
        raise ValueError("请至少填写一个新的模型 API Key 或工作空间 ID")
    record["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    value["users"][user_key] = record
    _write(value, path)
    return get_public_settings(user_id, secret_key, path)


def get_api_key(user_id, provider, secret_key, path=None) -> str:
    if provider not in PROVIDERS:
        raise ValueError("不支持的模型 API")
    record = dict(_load(path).get("users", {}).get(str(int(user_id)), {}) or {})
    return _decrypt(record, provider, secret_key)


def get_private_settings(user_id, secret_key, path=None) -> dict:
    record = dict(_load(path).get("users", {}).get(str(int(user_id)), {}) or {})
    return {
        **{
            f"{provider}_api_key": _decrypt(record, provider, secret_key)
            for provider in PROVIDERS
        },
        **{field: str(record.get(field) or "") for field in PUBLIC_FIELDS},
    }
