"""Mercado Libre OAuth and centrally managed shop-token operations.

OAuth credentials stay on the database server.  Browser-facing routes only
receive non-sensitive token metadata; access and refresh tokens never leave the
server-side data layer.
"""

from __future__ import annotations

import os
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from bit.bit_runtime_lock import InterProcessLock


API_BASE_URL = "https://api.mercadolibre.com"
DEFAULT_AUTHORIZATION_URL = "https://global-selling.mercadolibre.com/authorization"
DEFAULT_REDIRECT_URI = "https://zeshun.nat100.top/zs"
TOKEN_AUTO_REFRESH_BEFORE_MINUTES = max(
    5, int(os.environ.get("MERCADO_TOKEN_AUTO_REFRESH_BEFORE_MINUTES", "60"))
)
TOKEN_AUTO_REFRESH_RETRY_MINUTES = max(
    1, int(os.environ.get("MERCADO_TOKEN_AUTO_REFRESH_RETRY_MINUTES", "15"))
)
TOKEN_AUTO_REFRESH_CHECK_SECONDS = max(
    60, int(os.environ.get("MERCADO_TOKEN_AUTO_REFRESH_CHECK_SECONDS", "300"))
)
TOKEN_AUTO_REFRESH_SCAN_LOCK_KEY = "mercado_token_auto_refresh_scan"

_token_refresh_locks_guard = threading.Lock()
_token_refresh_locks: dict[int, Any] = {}
_token_refresh_scheduler_guard = threading.Lock()
_token_refresh_scheduler_thread: threading.Thread | None = None


class MercadoTokenError(RuntimeError):
    """A Mercado Libre OAuth or identity request failed."""


def _mercado_http_client(http: requests.Session | None = None) -> requests.Session:
    """Use a direct client so a stale Windows system proxy cannot break OAuth."""
    if http is not None:
        return http
    client = requests.Session()
    client.trust_env = False
    return client


def _legacy_oauth_credentials() -> dict[str, str]:
    """Reuse the project's existing Mercado application during migration."""
    try:
        from mercado_interface import mercado_interface as legacy
    except Exception:
        return {}
    return {
        "client_id": str(getattr(legacy, "APP_ID", "") or "").strip(),
        "client_secret": str(getattr(legacy, "CLIENT_SECRET", "") or "").strip(),
        "redirect_uri": str(getattr(legacy, "REDIRECT_URL", "") or "").strip(),
    }


def _oauth_settings(require_secret: bool = False) -> dict[str, str]:
    legacy = _legacy_oauth_credentials()
    settings = {
        "client_id": (
            os.environ.get("MELI_CLIENT_ID")
            or os.environ.get("MERCADO_CLIENT_ID")
            or legacy.get("client_id")
            or ""
        ).strip(),
        "client_secret": (
            os.environ.get("MELI_CLIENT_SECRET")
            or os.environ.get("MERCADO_CLIENT_SECRET")
            or legacy.get("client_secret")
            or ""
        ).strip(),
        "redirect_uri": (
            os.environ.get("MELI_REDIRECT_URI")
            or os.environ.get("MERCADO_REDIRECT_URI")
            or legacy.get("redirect_uri")
            or DEFAULT_REDIRECT_URI
        ).strip(),
        "authorization_base_url": (
            os.environ.get("MELI_AUTHORIZATION_URL")
            or DEFAULT_AUTHORIZATION_URL
        ).strip(),
    }
    missing = []
    if not settings["client_id"]:
        missing.append("MELI_CLIENT_ID")
    if require_secret and not settings["client_secret"]:
        missing.append("MELI_CLIENT_SECRET")
    if missing:
        raise MercadoTokenError(f"服务端未配置：{', '.join(missing)}")
    return settings


def authorization_info() -> dict[str, Any]:
    """Return a safe authorization link without exposing the client secret."""
    try:
        settings = _oauth_settings(require_secret=False)
    except MercadoTokenError as exc:
        return {
            "configured": False,
            "authorization_url": "",
            "redirect_uri": DEFAULT_REDIRECT_URI,
            "message": str(exc),
        }

    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings["client_id"],
            "redirect_uri": settings["redirect_uri"],
        }
    )
    separator = "&" if "?" in settings["authorization_base_url"] else "?"
    return {
        "configured": True,
        "authorization_url": f"{settings['authorization_base_url']}{separator}{query}",
        "redirect_uri": settings["redirect_uri"],
        "message": "",
    }


def extract_authorization_code(callback_or_code: str) -> str:
    """Extract a one-time TG code from a raw code or full callback URL."""
    raw = str(callback_or_code or "").strip()
    if raw.startswith("TG-") and len(raw) > 3:
        return raw
    values = parse_qs(urlparse(raw).query).get("code", [])
    if values and str(values[0]).startswith("TG-") and len(str(values[0])) > 3:
        return str(values[0])
    raise ValueError("请输入有效的 TG Code，或包含 TG Code 的完整回调链接")


def _response_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:500]
    if not isinstance(payload, dict):
        return str(payload)[:500]
    message = payload.get("message") or payload.get("error") or "请求失败"
    cause = payload.get("cause")
    return f"{message}; cause={cause}"[:1000] if cause else str(message)[:1000]


def _request_token(
    form_data: Mapping[str, Any],
    *,
    http: requests.Session | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    client = _mercado_http_client(http)
    try:
        response = client.post(
            f"{API_BASE_URL}/oauth/token",
            headers={"Accept": "application/json"},
            data=dict(form_data),
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        raise MercadoTokenError(
            "无法连接 Mercado Libre Token 接口，请检查服务器网络后重试"
        ) from exc
    if not response.ok:
        raise MercadoTokenError(
            f"Token 请求失败（HTTP {response.status_code}）：{_response_message(response)}"
        )
    try:
        token_data = response.json()
    except ValueError as exc:
        raise MercadoTokenError("Token 接口返回了无法识别的数据") from exc
    if not isinstance(token_data, dict) or not token_data.get("access_token"):
        raise MercadoTokenError("Token 请求成功，但响应中没有 Access Token")
    return token_data


def _seller_profile(
    access_token: str,
    *,
    http: requests.Session | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    client = _mercado_http_client(http)
    try:
        response = client.get(
            f"{API_BASE_URL}/users/me",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        raise MercadoTokenError(
            "无法连接 Mercado Libre 店铺身份接口，请检查服务器网络后重试"
        ) from exc
    if not response.ok:
        raise MercadoTokenError(
            f"读取授权店铺失败（HTTP {response.status_code}）：{_response_message(response)}"
        )
    try:
        profile = response.json()
    except ValueError as exc:
        raise MercadoTokenError("店铺身份接口返回了无法识别的数据") from exc
    if not isinstance(profile, dict):
        raise MercadoTokenError("店铺身份接口返回格式错误")
    return profile


def _normalize_display_name(display_name: str) -> str:
    value = str(display_name or "").strip()
    if not value:
        raise ValueError("请输入自定义店铺名称")
    if len(value) > 100:
        raise ValueError("自定义店铺名称不能超过 100 个字符")
    return value


def _token_record(
    display_name: str,
    token_data: Mapping[str, Any],
    profile: Mapping[str, Any] | None,
    *,
    client_id: str,
) -> dict[str, Any]:
    profile = dict(profile or {})
    issued_at = datetime.now()
    try:
        expires_in = max(0, int(token_data.get("expires_in") or 0))
    except (TypeError, ValueError):
        expires_in = 0
    user_id = token_data.get("user_id") or profile.get("id")
    return {
        "display_name": _normalize_display_name(display_name),
        "meli_user_id": str(user_id).strip() if user_id not in (None, "") else None,
        "nickname": str(profile.get("nickname") or "").strip(),
        "site_id": str(profile.get("site_id") or "").strip(),
        "client_id": client_id,
        "access_token": str(token_data["access_token"]),
        "refresh_token": str(token_data.get("refresh_token") or ""),
        "token_type": str(token_data.get("token_type") or "Bearer"),
        "scope": str(token_data.get("scope") or ""),
        "expires_at": issued_at + timedelta(seconds=expires_in) if expires_in else None,
        "last_verified_at": issued_at if profile else None,
        "last_refreshed_at": issued_at,
        "last_error": "",
    }


def exchange_and_save(
    display_name: str,
    callback_or_code: str,
    *,
    upsert,
    http: requests.Session | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Exchange a TG code, identify its seller, and save the rotating tokens."""
    name = _normalize_display_name(display_name)
    code = extract_authorization_code(callback_or_code)
    settings = _oauth_settings(require_secret=True)
    token_data = _request_token(
        {
            "grant_type": "authorization_code",
            "client_id": settings["client_id"],
            "client_secret": settings["client_secret"],
            "code": code,
            "redirect_uri": settings["redirect_uri"],
        },
        http=http,
        timeout=timeout,
    )

    # The TG code is one-time.  If identity lookup is temporarily unavailable,
    # persist the successfully exchanged tokens instead of losing the grant.
    profile: dict[str, Any] = {}
    profile_error = ""
    try:
        profile = _seller_profile(
            str(token_data["access_token"]), http=http, timeout=timeout
        )
    except MercadoTokenError as exc:
        profile_error = str(exc)

    record = _token_record(
        name,
        token_data,
        profile,
        client_id=settings["client_id"],
    )
    if profile_error:
        record["last_error"] = profile_error
    result = upsert(record)
    return {
        **dict(result or {}),
        "warning": profile_error,
    }


def _refresh_and_save_unlocked(
    token_id: int,
    *,
    get_token,
    update_token,
    record_error=None,
    http: requests.Session | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Rotate a store's refresh token and persist the replacement atomically."""
    existing = get_token(token_id)
    if not existing:
        raise KeyError("店铺授权不存在")
    refresh_token = str(existing.get("refresh_token") or "").strip()
    if not refresh_token:
        raise MercadoTokenError("该店铺没有 Refresh Token，请重新授权")
    settings = _oauth_settings(require_secret=True)
    try:
        token_data = _request_token(
            {
                "grant_type": "refresh_token",
                "client_id": settings["client_id"],
                "client_secret": settings["client_secret"],
                "refresh_token": refresh_token,
            },
            http=http,
            timeout=timeout,
        )
    except Exception as exc:
        if record_error is not None:
            try:
                record_error(token_id, str(exc))
            except Exception:
                pass
        raise

    if not token_data.get("refresh_token"):
        token_data["refresh_token"] = refresh_token

    # Refresh tokens rotate at the token endpoint.  Once that succeeds, save
    # the replacement even when the optional identity check is temporarily
    # unavailable; otherwise the old refresh token may already be unusable.
    profile = {
        "id": existing.get("meli_user_id"),
        "nickname": existing.get("nickname"),
        "site_id": existing.get("site_id"),
    }
    profile_error = ""
    try:
        profile = _seller_profile(
            str(token_data["access_token"]), http=http, timeout=timeout
        )
    except MercadoTokenError as exc:
        profile_error = str(exc)

    record = _token_record(
        str(existing.get("display_name") or ""),
        token_data,
        profile,
        client_id=settings["client_id"],
    )
    if profile_error:
        record["last_error"] = profile_error
    try:
        result = dict(update_token(token_id, record) or {})
    except Exception as exc:
        if record_error is not None:
            try:
                record_error(token_id, str(exc))
            except Exception:
                pass
        raise
    result["warning"] = profile_error
    return result


def _token_refresh_lock(token_id: int):
    normalized_id = int(token_id)
    with _token_refresh_locks_guard:
        return _token_refresh_locks.setdefault(normalized_id, threading.Lock())


def refresh_and_save(
    token_id: int,
    *,
    get_token,
    update_token,
    record_error=None,
    http: requests.Session | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Refresh one token safely even when manual and automatic jobs overlap."""

    token_id = int(token_id)
    with _token_refresh_lock(token_id):
        process_lock = InterProcessLock(
            f"mercado_store_token_refresh_{token_id}",
            owner="mercado_token_refresh",
            metadata={"token_id": token_id},
        )
        if not process_lock.acquire(timeout=max(5, int(timeout) + 5)):
            raise MercadoTokenError("该店铺 Token 正在其他任务中刷新，请稍后重试")
        try:
            return _refresh_and_save_unlocked(
                token_id,
                get_token=get_token,
                update_token=update_token,
                record_error=record_error,
                http=http,
                timeout=timeout,
            )
        finally:
            process_lock.release()


def _token_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _comparable_now(now: datetime, target: datetime) -> datetime:
    if target.tzinfo is not None and now.tzinfo is None:
        return now.replace(tzinfo=target.tzinfo)
    if target.tzinfo is None and now.tzinfo is not None:
        return now.replace(tzinfo=None)
    return now


def auto_refresh_due_store_tokens(
    *,
    list_tokens=None,
    refresh_token=None,
    now: datetime | None = None,
    refresh_before_minutes: int = TOKEN_AUTO_REFRESH_BEFORE_MINUTES,
    retry_minutes: int = TOKEN_AUTO_REFRESH_RETRY_MINUTES,
) -> dict[str, Any]:
    """Refresh every renewable store token approaching expiry."""

    if list_tokens is None or refresh_token is None:
        from bit import bit_db_api

        list_tokens = list_tokens or bit_db_api.list_mercado_store_tokens
        refresh_token = refresh_token or bit_db_api.refresh_mercado_store_token

    current = now or datetime.now()
    rows = list((list_tokens() or {}).get("rows") or [])
    result: dict[str, Any] = {
        "checked": len(rows),
        "due": 0,
        "refreshed": 0,
        "failed": 0,
        "retry_deferred": 0,
        "failures": [],
    }
    for row in rows:
        token_id = int(row.get("id") or 0)
        expires_at = _token_datetime(row.get("expires_at"))
        if not token_id or not row.get("has_refresh_token") or expires_at is None:
            continue
        row_now = _comparable_now(current, expires_at)
        if expires_at > row_now + timedelta(minutes=max(5, int(refresh_before_minutes))):
            continue
        result["due"] += 1

        updated_at = _token_datetime(row.get("updated_at"))
        if row.get("last_error") and updated_at is not None:
            updated_now = _comparable_now(current, updated_at)
            if updated_at > updated_now - timedelta(minutes=max(1, int(retry_minutes))):
                result["retry_deferred"] += 1
                continue
        try:
            refresh_token(token_id)
            result["refreshed"] += 1
        except Exception as exc:
            result["failed"] += 1
            result["failures"].append({
                "token_id": token_id,
                "store_name": str(row.get("display_name") or row.get("nickname") or token_id),
                "error": str(exc),
            })
    return result


def _token_auto_refresh_loop() -> None:
    while True:
        scan_lock = InterProcessLock(
            TOKEN_AUTO_REFRESH_SCAN_LOCK_KEY,
            owner="mercado_token_auto_refresh_scheduler",
        )
        if scan_lock.acquire(timeout=0):
            try:
                result = auto_refresh_due_store_tokens()
                if result["refreshed"] or result["failed"]:
                    logging.info(
                        "店铺 Token 自动刷新完成：检查 %s，需刷新 %s，成功 %s，失败 %s",
                        result["checked"],
                        result["due"],
                        result["refreshed"],
                        result["failed"],
                    )
            except Exception:
                logging.exception("店铺 Token 自动刷新检查失败")
            finally:
                scan_lock.release()
        threading.Event().wait(TOKEN_AUTO_REFRESH_CHECK_SECONDS)


def start_token_auto_refresh_scheduler() -> bool:
    """Start one background scheduler for proactive token renewal."""

    global _token_refresh_scheduler_thread
    with _token_refresh_scheduler_guard:
        if _token_refresh_scheduler_thread and _token_refresh_scheduler_thread.is_alive():
            return False
        _token_refresh_scheduler_thread = threading.Thread(
            target=_token_auto_refresh_loop,
            name="mercado-token-auto-refresh",
            daemon=True,
        )
        _token_refresh_scheduler_thread.start()
        return True
