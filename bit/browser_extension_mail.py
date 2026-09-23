"""Encrypted SMTP settings and deduplicated alert mail for the browser extension."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import smtplib
import ssl
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

try:
    import certifi
except ImportError:  # pragma: no cover - certifi is installed with the server dependencies
    certifi = None


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "runtime_locks" / "browser_extension_mail.json"
EVENT_LABELS = {
    "rate_limit": "访问限频",
    "slider_verification": "滑块或人机验证",
    "buyer_login": "买家账号需要登录",
    "purchase_login": "采购账号需要登录",
    "task_blocked": "任务等待人工处理",
}
SMTP_PRESETS = {
    "qq.com": ("smtp.qq.com", 465, "ssl"),
    "163.com": ("smtp.163.com", 465, "ssl"),
    "126.com": ("smtp.126.com", 465, "ssl"),
    "gmail.com": ("smtp.gmail.com", 465, "ssl"),
    "outlook.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "hotmail.com": ("smtp-mail.outlook.com", 587, "starttls"),
}
_lock = threading.RLock()
_recent: dict[str, float] = {}


def _path(path=None) -> Path:
    configured = str(os.environ.get("BIT_BROWSER_EXTENSION_MAIL_CONFIG") or "").strip()
    return Path(path or configured or DEFAULT_CONFIG_PATH).expanduser().resolve()


def _fernet(secret_key):
    # This integration is server-only.  Local Agent workers still import the
    # shared workbench module, so importing it must not require cryptography.
    from cryptography.fernet import Fernet

    raw = str(secret_key or "").encode("utf-8")
    key = hashlib.sha256(b"zeshun-browser-extension-mail\0" + raw).digest()
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
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, config_path)


def _valid_email(value, label: str) -> str:
    text = str(value or "").strip().lower()
    parsed = parseaddr(text)[1]
    if parsed != text or len(text) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", text):
        raise ValueError(f"{label}格式无效")
    return text


def _normalize(payload: dict, existing: dict | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("邮件通知配置格式无效")
    existing = existing or {}
    sender = _valid_email(payload.get("sender_email"), "发件邮箱")
    receiver = _valid_email(payload.get("receiver_email"), "收件邮箱")
    domain = sender.rsplit("@", 1)[-1]
    preset = SMTP_PRESETS.get(domain, ("", 465, "ssl"))
    host = str(payload.get("smtp_host") or preset[0]).strip().lower()
    if not host or len(host) > 253 or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError("SMTP 服务器地址无效")
    try:
        port = int(payload.get("smtp_port") or preset[1])
    except (TypeError, ValueError) as exc:
        raise ValueError("SMTP 端口无效") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SMTP 端口无效")
    security = str(payload.get("smtp_security") or preset[2]).strip().lower()
    if security not in {"ssl", "starttls"}:
        raise ValueError("SMTP 加密方式无效")
    password = str(payload.get("smtp_password") or "")
    password_token = str(existing.get("password_token") or "")
    if password:
        if len(password) > 512:
            raise ValueError("邮箱授权码长度无效")
        password_token = password
    if not password_token:
        raise ValueError("请填写发件邮箱的 SMTP 授权码")
    return {
        "enabled": payload.get("enabled") is True,
        "sender_email": sender,
        "receiver_email": receiver,
        "smtp_host": host,
        "smtp_port": port,
        "smtp_security": security,
        "password_token": password_token,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def get_public_settings(user_id, secret_key, path=None) -> dict:
    record = dict(_load(path).get("users", {}).get(str(int(user_id)), {}) or {})
    return {
        "enabled": bool(record.get("enabled")),
        "sender_email": str(record.get("sender_email") or ""),
        "receiver_email": str(record.get("receiver_email") or ""),
        "smtp_host": str(record.get("smtp_host") or ""),
        "smtp_port": int(record.get("smtp_port") or 465),
        "smtp_security": str(record.get("smtp_security") or "ssl"),
        "password_configured": bool(record.get("password_encrypted")),
        "configured": bool(record.get("sender_email") and record.get("receiver_email") and record.get("password_encrypted")),
        "updated_at": record.get("updated_at"),
    }


def save_settings(user_id, payload, secret_key, path=None) -> dict:
    from cryptography.fernet import InvalidToken

    value = _load(path)
    key = str(int(user_id))
    existing = dict(value["users"].get(key, {}) or {})
    existing_password = ""
    if existing.get("password_encrypted"):
        try:
            existing_password = _fernet(secret_key).decrypt(
                str(existing["password_encrypted"]).encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError):
            existing_password = ""
    normalized = _normalize(payload, {"password_token": existing_password})
    password = normalized.pop("password_token")
    normalized["password_encrypted"] = _fernet(secret_key).encrypt(password.encode("utf-8")).decode("ascii")
    value["users"][key] = normalized
    _write(value, path)
    return get_public_settings(user_id, secret_key, path)


def _private_settings(user_id, secret_key, path=None) -> dict:
    from cryptography.fernet import InvalidToken

    value = _load(path).get("users", {}).get(str(int(user_id)), {})
    if not value or not value.get("password_encrypted"):
        raise ValueError("邮件通知尚未完成配置")
    try:
        password = _fernet(secret_key).decrypt(
            str(value["password_encrypted"]).encode("ascii")
        ).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("邮件授权码无法解密，请重新保存邮件通知配置") from exc
    return {**value, "smtp_password": password}


def get_private_settings(user_id, secret_key, path=None) -> dict:
    """Return decrypted mail settings for an explicit, authenticated view."""
    return _private_settings(user_id, secret_key, path)


def _smtp_ssl_context() -> ssl.SSLContext:
    """Build a verified SMTP TLS context with a usable CA bundle.

    Some Windows Python installations do not ship an OpenSSL CA bundle, so
    ``ssl.create_default_context()`` can otherwise fail against normal public
    SMTP certificates with ``unable to get local issuer certificate``.
    ``SSL_CERT_FILE`` remains an explicit override for environments that use a
    corporate or otherwise private CA.
    """
    configured_cafile = str(os.environ.get("SSL_CERT_FILE") or "").strip()
    if configured_cafile:
        return ssl.create_default_context(cafile=configured_cafile)

    context = ssl.create_default_context()
    if context.get_ca_certs() or certifi is None:
        return context
    return ssl.create_default_context(cafile=certifi.where())


def _send(config: dict, subject: str, body: str, smtp_ssl=smtplib.SMTP_SSL, smtp=smtplib.SMTP) -> None:
    message = EmailMessage()
    message["From"] = config["sender_email"]
    message["To"] = config["receiver_email"]
    message["Subject"] = subject[:180]
    message.set_content(body, charset="utf-8")
    context = _smtp_ssl_context()
    if config["smtp_security"] == "ssl":
        with smtp_ssl(config["smtp_host"], int(config["smtp_port"]), timeout=25, context=context) as server:
            server.login(config["sender_email"], config["smtp_password"])
            server.send_message(message)
    else:
        with smtp(config["smtp_host"], int(config["smtp_port"]), timeout=25) as server:
            server.starttls(context=context)
            server.login(config["sender_email"], config["smtp_password"])
            server.send_message(message)


def send_test(user_id, secret_key, path=None, **smtp_factories) -> dict:
    config = _private_settings(user_id, secret_key, path)
    _send(
        config,
        "[泽顺插件] 邮件通知测试成功",
        "这是一封泽顺插件测试邮件。\n\n收到此邮件表示发件邮箱、授权码和收件邮箱配置正确。",
        **smtp_factories,
    )
    return {"sent": True, "receiver_email": config["receiver_email"]}


def send_alert(user_id, event: dict, secret_key, path=None, dedupe_seconds=1800, **smtp_factories) -> dict:
    if not isinstance(event, dict):
        raise ValueError("通知事件格式无效")
    event_type = str(event.get("event_type") or "").strip()
    if event_type not in EVENT_LABELS:
        raise ValueError("通知事件类型无效")
    public = get_public_settings(user_id, secret_key, path)
    if not public.get("configured"):
        return {"sent": False, "unconfigured": True}
    config = _private_settings(user_id, secret_key, path)
    if not config.get("enabled"):
        return {"sent": False, "disabled": True}
    message = " ".join(str(event.get("message") or "").split())[:1000]
    source = " ".join(str(event.get("source") or "泽顺插件").split())[:120]
    fingerprint = hashlib.sha256(
        f"{int(user_id)}\0{event_type}\0{source}\0{message}".encode("utf-8")
    ).hexdigest()
    now = time.time()
    with _lock:
        expired = [key for key, sent_at in _recent.items() if now - sent_at >= dedupe_seconds]
        for key in expired:
            _recent.pop(key, None)
        if fingerprint in _recent:
            return {"sent": False, "deduplicated": True}
    label = EVENT_LABELS[event_type]
    body = (
        f"泽顺插件检测到需要人工处理的问题。\n\n"
        f"问题类型：{label}\n"
        f"来源：{source}\n"
        f"时间：{datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        f"详情：{message or '未提供'}\n\n"
        "请打开对应浏览器页面完成登录、滑块或验证后，再继续原任务。"
    )
    _send(config, f"[泽顺插件告警] {label}", body, **smtp_factories)
    with _lock:
        _recent[fingerprint] = now
    return {"sent": True, "event_type": event_type, "receiver_email": config["receiver_email"]}
