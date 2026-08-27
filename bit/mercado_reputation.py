"""Mercado Libre 官方卖家声誉接口及控制台响应标准化。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import requests


API_BASE_URL = "https://api.mercadolibre.com"
REPUTATION_PATH = "/global/users/seller_reputation"

SITE_NAMES = {
    "MLM": "墨西哥",
    "MLB": "巴西",
    "MLC": "智利",
    "MCO": "哥伦比亚",
    "MLA": "阿根廷",
    "MLU": "乌拉圭",
}

LEVEL_NAMES = {
    "5_green": "绿色",
    "4_light_green": "浅绿色",
    "3_yellow": "黄色",
    "2_orange": "橙色",
    "1_red": "红色",
}


class MercadoReputationError(RuntimeError):
    """官方声誉接口无法返回有效数据。"""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _http_client(http: requests.Session | None = None) -> requests.Session:
    if http is not None:
        return http
    client = requests.Session()
    client.trust_env = False
    return client


def _response_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return str(response.text or "")[:500]
    if not isinstance(payload, Mapping):
        return str(payload)[:500]
    message = payload.get("message") or payload.get("error") or "请求失败"
    cause = payload.get("cause")
    return f"{message}; cause={cause}"[:1000] if cause else str(message)[:1000]


def fetch_reputation_payload(
    access_token: str,
    *,
    http: requests.Session | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """调用 Global Selling 官方声誉接口并返回原始 JSON 对象。"""

    token = str(access_token or "").strip()
    if not token:
        raise MercadoReputationError("该店铺没有可用的 Access Token")

    client = _http_client(http)
    for attempt in range(3):
        try:
            response = client.get(
                f"{API_BASE_URL}{REPUTATION_PATH}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                timeout=timeout,
            )
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise MercadoReputationError(f"无法连接 Mercado Libre 声誉接口：{exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            if attempt < 2:
                retry_after = response.headers.get("Retry-After", 2**attempt)
                try:
                    delay = min(max(float(retry_after), 0), 15)
                except (TypeError, ValueError):
                    delay = float(2**attempt)
                time.sleep(delay)
                continue

        if not response.ok:
            raise MercadoReputationError(
                f"读取美客多声誉失败（HTTP {response.status_code}）："
                f"{_response_message(response)}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MercadoReputationError("美客多声誉接口返回了无法识别的数据") from exc
        if not isinstance(payload, dict):
            raise MercadoReputationError("美客多声誉接口返回格式错误")
        return payload

    raise MercadoReputationError("美客多声誉接口多次重试后仍不可用")


def _number(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _percent(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    return round(float(number) * 100, 4)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def normalize_reputation_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把官方多站点嵌套响应整理为控制台表格行。"""

    raw_rows = payload.get("seller_reputation") or []
    if not isinstance(raw_rows, list):
        raise MercadoReputationError("美客多声誉接口缺少 seller_reputation 列表")

    rows = []
    for entry in raw_rows:
        if not isinstance(entry, Mapping):
            continue
        reputation = _mapping(entry.get("seller_reputation"))
        transactions = _mapping(reputation.get("transactions"))
        ratings = _mapping(transactions.get("ratings"))
        metrics = _mapping(reputation.get("metrics"))
        sales = _mapping(metrics.get("sales"))
        claims = _mapping(metrics.get("claims"))
        delayed = _mapping(metrics.get("delayed_handling_time"))
        cancellations = _mapping(metrics.get("cancellations"))
        site_id = str(entry.get("site_id") or "").strip().upper()
        level_id = str(reputation.get("level_id") or "").strip()
        real_level = str(reputation.get("real_level") or "").strip()

        rows.append(
            {
                "user_id": str(entry.get("user_id") or ""),
                "site_id": site_id,
                "site_name": SITE_NAMES.get(site_id, site_id or "未知站点"),
                "logistic_type": str(entry.get("logistic_type") or ""),
                "level_id": level_id or None,
                "level_name": LEVEL_NAMES.get(level_id, level_id or "暂无等级"),
                "power_seller_status": reputation.get("power_seller_status"),
                "real_level": real_level or None,
                "protection_end_date": reputation.get("protection_end_date"),
                "transaction_period": transactions.get("period"),
                "transaction_total": _number(transactions.get("total")),
                "transaction_completed": _number(transactions.get("completed")),
                "transaction_canceled": _number(transactions.get("canceled")),
                "rating_positive_percent": _percent(ratings.get("positive")),
                "rating_neutral_percent": _percent(ratings.get("neutral")),
                "rating_negative_percent": _percent(ratings.get("negative")),
                "sales_period": sales.get("period"),
                "sales_completed": _number(sales.get("completed")),
                "claims_period": claims.get("period"),
                "claims_rate_percent": _percent(claims.get("rate")),
                "claims_value": _number(claims.get("value")),
                "delayed_handling_period": delayed.get("period"),
                "delayed_handling_rate_percent": _percent(delayed.get("rate")),
                "delayed_handling_value": _number(delayed.get("value")),
                "cancellations_period": cancellations.get("period"),
                "cancellations_rate_percent": _percent(cancellations.get("rate")),
                "cancellations_value": _number(cancellations.get("value")),
            }
        )

    return {
        "user_id": str(payload.get("user_id") or ""),
        "site_id": str(payload.get("site_id") or ""),
        "total": len(rows),
        "rows": rows,
    }


def fetch_store_reputation(
    token_id: int,
    *,
    get_token: Callable[[int], Mapping[str, Any] | None],
    refresh_token: Callable[[int], Any] | None = None,
    record_error: Callable[[int, str], Any] | None = None,
    http: requests.Session | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """按数据库授权记录读取声誉；401 时自动刷新一次 token 后重试。"""

    identifier = int(token_id)
    token = get_token(identifier)
    if not token:
        raise KeyError("店铺授权不存在")

    try:
        try:
            payload = fetch_reputation_payload(
                str(token.get("access_token") or ""), http=http, timeout=timeout
            )
        except MercadoReputationError as exc:
            if exc.status_code != 401 or refresh_token is None:
                raise
            refresh_token(identifier)
            token = get_token(identifier)
            if not token:
                raise KeyError("刷新 Token 后店铺授权不存在")
            payload = fetch_reputation_payload(
                str(token.get("access_token") or ""), http=http, timeout=timeout
            )

        result = normalize_reputation_payload(payload)
        result.update(
            {
                "token_id": identifier,
                "display_name": str(token.get("display_name") or ""),
                "nickname": str(token.get("nickname") or ""),
                "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(
                    timespec="seconds"
                ),
            }
        )
        return result
    except Exception as exc:
        if record_error is not None:
            try:
                record_error(identifier, str(exc))
            except Exception:
                pass
        raise
