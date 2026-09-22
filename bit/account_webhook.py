"""Encrypted, account-bound webhook settings and delivery helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken


DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent / "runtime_locks" / "account_webhooks.json"
)
_lock = threading.RLock()


def _path(path=None) -> Path:
    configured = str(os.environ.get("BIT_ACCOUNT_WEBHOOK_CONFIG") or "").strip()
    return Path(path or configured or DEFAULT_CONFIG_PATH).expanduser().resolve()


def _fernet(secret_key) -> Fernet:
    digest = hashlib.sha256(
        b"zeshun-account-webhook\0" + str(secret_key or "").encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _load(path=None) -> dict:
    target = _path(path)
    if not target.is_file():
        return {"version": 1, "users": {}}
    with _lock:
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "users": {}}
    if not isinstance(value, dict) or not isinstance(value.get("users"), dict):
        return {"version": 1, "users": {}}
    return value


def _write(value: dict, path=None) -> None:
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    with _lock:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)


def _validate_url(value) -> str:
    webhook_url = str(value or "").strip()
    if len(webhook_url) > 2000:
        raise ValueError("Webhook 地址不能超过 2000 个字符")
    parsed = urlsplit(webhook_url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Webhook 地址格式无效")
    is_local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_local):
        raise ValueError("Webhook 必须使用 HTTPS；本机 localhost 可使用 HTTP")
    return webhook_url


def get_public_settings(user_id, secret_key, path=None) -> dict:
    record = dict(_load(path).get("users", {}).get(str(int(user_id)), {}) or {})
    return {
        "enabled": bool(record.get("enabled")),
        "url": str(record.get("url") or ""),
        "secret_configured": bool(record.get("secret_encrypted")),
        "configured": bool(record.get("url")),
        "updated_at": record.get("updated_at"),
    }


def save_settings(user_id, payload, secret_key, path=None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Webhook 配置格式无效")
    if set(payload) - {"enabled", "url", "secret"}:
        raise ValueError("Webhook 配置包含未知字段")
    value = _load(path)
    user_key = str(int(user_id))
    existing = dict(value["users"].get(user_key, {}) or {})
    webhook_url = _validate_url(payload.get("url"))
    secret = str(payload.get("secret") or "")
    if len(secret) > 512:
        raise ValueError("Webhook 签名密钥长度无效")
    record = {
        "enabled": payload.get("enabled") is True,
        "url": webhook_url,
        "secret_encrypted": existing.get("secret_encrypted") or "",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if secret:
        record["secret_encrypted"] = _fernet(secret_key).encrypt(
            secret.encode("utf-8")
        ).decode("ascii")
    value["users"][user_key] = record
    _write(value, path)
    return get_public_settings(user_id, secret_key, path)


def _private_settings(user_id, secret_key, path=None) -> dict:
    record = dict(_load(path).get("users", {}).get(str(int(user_id)), {}) or {})
    if not record.get("url"):
        raise ValueError("Webhook 尚未完成配置")
    secret = ""
    if record.get("secret_encrypted"):
        try:
            secret = _fernet(secret_key).decrypt(
                str(record["secret_encrypted"]).encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Webhook 签名密钥无法解密，请重新保存") from exc
    return {**record, "secret": secret}


def _is_public_destination(hostname: str) -> bool:
    """Reject private network targets except explicit localhost development URLs."""
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise ValueError("Webhook 域名无法解析") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def send_event(user_id, event: dict, secret_key, path=None, urlopen_impl=urlopen) -> dict:
    if not isinstance(event, dict):
        raise ValueError("Webhook 事件格式无效")
    config = _private_settings(user_id, secret_key, path)
    if not config.get("enabled"):
        return {"sent": False, "disabled": True}
    parsed = urlsplit(config["url"])
    if not _is_public_destination(str(parsed.hostname or "")):
        raise ValueError("Webhook 不允许访问内网或保留地址")
    event_type = str(event.get("event_type") or "notification").strip()[:80]
    timestamp = str(int(time.time()))
    body = json.dumps(
        {
            "event": event_type,
            "timestamp": timestamp,
            "data": event,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = ""
    if config.get("secret"):
        digest = hmac.new(
            config["secret"].encode("utf-8"),
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        signature = f"sha256={digest}"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "Zeshun-Webhook/1.0",
        "X-Zeshun-Event": event_type,
        "X-Zeshun-Timestamp": timestamp,
    }
    if signature:
        headers["X-Zeshun-Signature"] = signature
    response = urlopen_impl(
        Request(config["url"], data=body, headers=headers, method="POST"), timeout=15
    )
    status = int(getattr(response, "status", 200) or 200)
    if status < 200 or status >= 300:
        raise ValueError(f"Webhook 返回 HTTP {status}")
    return {"sent": True, "status_code": status, "event_type": event_type}


def send_test(user_id, secret_key, path=None, **kwargs) -> dict:
    return send_event(
        user_id,
        {
            "event_type": "test",
            "source": "泽顺控制台",
            "message": "Webhook 测试成功",
        },
        secret_key,
        path,
        **kwargs,
    )
