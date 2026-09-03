"""Mercado Libre 官方卖家声誉接口及控制台响应标准化。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

import requests


API_BASE_URL = "https://api.mercadolibre.com"
REPUTATION_PATH = "/global/users/seller_reputation"
ORDERS_SEARCH_PATH = "/marketplace/orders/search"
RIGHTS_HOLDER_CASES_PATH = "/moderations/pppi/cases"
OFFICIAL_INFRACTION_DAYS = 100
SEVEN_DAY_RATE_DISCLAIMER = (
    "七天变化率由官方订单 API 自算；数值可能因取消单、退款、时区和"
    "平台内部统计口径而与后台略有差异。"
)

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


def _fetch_json(
    access_token: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    http: requests.Session | None = None,
    timeout: int = 30,
    label: str = "美客多接口",
) -> Any:
    """读取一个 Mercado Libre 官方 JSON 接口，统一处理重试和错误。"""

    token = str(access_token or "").strip()
    if not token:
        raise MercadoReputationError("该店铺没有可用的 Access Token")

    client = _http_client(http)
    for attempt in range(3):
        try:
            response = client.get(
                f"{API_BASE_URL}{path}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                params=dict(params or {}),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise MercadoReputationError(f"无法连接 {label}：{exc}") from exc

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
                f"读取{label}失败（HTTP {response.status_code}）："
                f"{_response_message(response)}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MercadoReputationError(f"{label}返回了无法识别的数据") from exc
        if not isinstance(payload, (dict, list)):
            raise MercadoReputationError(f"{label}返回格式错误")
        return payload

    raise MercadoReputationError(f"{label}多次重试后仍不可用")


def fetch_reputation_payload(
    access_token: str,
    *,
    http: requests.Session | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """调用 Global Selling 官方声誉接口并返回原始 JSON 对象。"""

    payload = _fetch_json(
        access_token,
        REPUTATION_PATH,
        http=http,
        timeout=timeout,
        label="美客多声誉接口",
    )
    if not isinstance(payload, dict):
        raise MercadoReputationError("美客多声誉接口返回格式错误")
    return payload


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


def _paging_total(payload: Mapping[str, Any]) -> int:
    paging = _mapping(payload.get("paging"))
    for value in (paging.get("total"), payload.get("total")):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    results = payload.get("results")
    return len(results) if isinstance(results, list) else 0


def _api_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _order_total(
    access_token: str,
    seller_id: str,
    date_from: datetime,
    date_to: datetime,
    *,
    http: requests.Session | None,
    timeout: int,
) -> int:
    payload = _fetch_json(
        access_token,
        ORDERS_SEARCH_PATH,
        params={
            "seller": str(seller_id),
            "date_created.from": _api_datetime(date_from),
            "date_created.to": _api_datetime(date_to),
            "offset": 0,
            "limit": 1,
        },
        http=http,
        timeout=timeout,
        label="美客多订单接口",
    )
    if not isinstance(payload, Mapping):
        raise MercadoReputationError("美客多订单接口返回格式错误")
    return _paging_total(payload)


def _format_change_rate(current: int, previous: int) -> tuple[str, str, float]:
    if current > previous:
        direction = "增长"
    elif current < previous:
        direction = "下滑"
    else:
        direction = "持平"
    if previous:
        rate = round((current - previous) * 100 / previous, 2)
    elif current:
        rate = 100.0
    else:
        rate = 0.0
    rate_text = f"{rate:.2f}".rstrip("0").rstrip(".") + "%"
    return direction, rate_text, rate


def _site_status(profile: Mapping[str, Any]) -> dict[str, Any]:
    status = _mapping(profile.get("status"))
    sell = _mapping(status.get("sell"))
    listing = _mapping(status.get("list"))
    raw_status = str(
        profile.get("site_status") or status.get("site_status") or ""
    ).strip()
    sell_allowed = sell.get("allow")
    list_allowed = listing.get("allow")
    lowered = raw_status.casefold()
    if sell_allowed is False:
        display = "暂停销售"
    elif list_allowed is False:
        display = "限制刊登"
    elif lowered in ("active", "enabled", "ok"):
        display = "正常"
    elif raw_status:
        display = raw_status
    elif sell_allowed is True:
        display = "正常"
    else:
        display = "未知"
    return {
        "site_status": raw_status or None,
        "site_status_display": display,
        "sell_allowed": sell_allowed,
        "list_allowed": list_allowed,
    }


def _infraction_total(
    access_token: str,
    seller_id: str,
    cutoff: datetime,
    now: datetime,
    *,
    http: requests.Session | None,
    timeout: int,
) -> int:
    payload = _fetch_json(
        access_token,
        f"/marketplace/moderations/infractions/{seller_id}",
        params={
            "element_type": "ITM",
            "date_created_since": cutoff.date().isoformat(),
            "date_created_to": now.date().isoformat(),
            "sort": "date_created_desc",
            "offset": 0,
            "limit": 1,
        },
        http=http,
        timeout=timeout,
        label="美客多违规接口",
    )
    if not isinstance(payload, Mapping):
        raise MercadoReputationError("美客多违规接口返回格式错误")
    return _paging_total(payload)


def _case_rows(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(payload, Mapping):
        values = payload.get("cases") or payload.get("results") or []
        rows = [dict(value) for value in values if isinstance(value, Mapping)]
        return rows, dict(_mapping(payload.get("paging")))
    if not isinstance(payload, list):
        return [], {}
    rows = []
    paging: dict[str, Any] = {}
    for value in payload:
        if not isinstance(value, Mapping):
            continue
        if value.get("case_id") or value.get("item_id"):
            rows.append(dict(value))
        elif any(key in value for key in ("total", "offset", "limit")):
            paging.update(dict(value))
    return rows, paging


def _rights_holder_counts(
    access_token: str,
    cutoff: datetime,
    *,
    http: requests.Session | None,
    timeout: int,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    seen: set[str] = set()
    offset = 0
    for _page in range(100):
        payload = _fetch_json(
            access_token,
            RIGHTS_HOLDER_CASES_PATH,
            params={
                "offset": offset,
                "limit": 50,
                "date_created": cutoff.date().isoformat(),
                "status": "",
            },
            http=http,
            timeout=timeout,
            label="美客多权利人案件接口",
        )
        rows, paging = _case_rows(payload)
        for row in rows:
            case_id = str(row.get("case_id") or "").strip()
            item_id = str(row.get("item_id") or "").strip().upper()
            key = case_id or f"{item_id}|{row.get('date_created')}"
            if not item_id or key in seen:
                continue
            seen.add(key)
            site_id = item_id[:3]
            counts[site_id] = counts.get(site_id, 0) + 1
        try:
            payload_total = payload.get("total") if isinstance(payload, Mapping) else None
            total = int(paging.get("total") or payload_total or len(rows))
        except (TypeError, ValueError):
            total = offset + len(rows)
        try:
            limit = max(1, int(paging.get("limit") or 50))
        except (TypeError, ValueError):
            limit = 50
        if not rows or offset + len(rows) >= total or len(rows) < limit:
            break
        offset += limit
    return counts


def enrich_reputation_with_official_data(
    rows: list[dict[str, Any]],
    access_token: str,
    *,
    now: datetime | None = None,
    http: requests.Session | None = None,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """补齐站点状态、订单变化率及近 100 天官方违规统计。"""

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_time = current_time.astimezone(timezone.utc)
    current_start = current_time - timedelta(days=7)
    previous_start = current_time - timedelta(days=14)
    cutoff = current_time - timedelta(days=OFFICIAL_INFRACTION_DAYS)

    try:
        rights_counts = _rights_holder_counts(
            access_token,
            cutoff,
            http=http,
            timeout=timeout,
        )
        rights_error = ""
    except MercadoReputationError as exc:
        if exc.status_code == 401:
            raise
        rights_counts = {}
        rights_error = str(exc)

    for row in rows:
        seller_id = str(row.get("user_id") or "").strip()
        site_id = str(row.get("site_id") or "").strip().upper()
        errors: list[str] = []

        try:
            profile = _fetch_json(
                access_token,
                f"/users/{seller_id}",
                http=http,
                timeout=timeout,
                label="美客多站点状态接口",
            )
            if not isinstance(profile, Mapping):
                raise MercadoReputationError("美客多站点状态接口返回格式错误")
            row.update(_site_status(profile))
        except MercadoReputationError as exc:
            if exc.status_code == 401:
                raise
            row.update({"site_status": None, "site_status_display": "获取失败"})
            errors.append(f"站点状态：{exc}")

        try:
            previous_orders = _order_total(
                access_token,
                seller_id,
                previous_start,
                current_start,
                http=http,
                timeout=timeout,
            )
            current_orders = _order_total(
                access_token,
                seller_id,
                current_start,
                current_time,
                http=http,
                timeout=timeout,
            )
            direction, rate_text, rate = _format_change_rate(
                current_orders, previous_orders
            )
            row.update({
                "orders_current_7d": current_orders,
                "orders_previous_7d": previous_orders,
                "direction": direction,
                "gradient_rate": rate_text,
                "gradient_rate_percent": rate,
                "gradient_source": "official_orders_api",
                "gradient_disclaimer": SEVEN_DAY_RATE_DISCLAIMER,
            })
        except MercadoReputationError as exc:
            if exc.status_code == 401:
                raise
            row.update({
                "orders_current_7d": None,
                "orders_previous_7d": None,
                "direction": "获取失败",
                "gradient_rate": "-",
                "gradient_rate_percent": None,
                "gradient_source": "official_orders_api",
                "gradient_disclaimer": SEVEN_DAY_RATE_DISCLAIMER,
            })
            errors.append(f"七天变化率：{exc}")

        try:
            row["infraction_count"] = _infraction_total(
                access_token,
                seller_id,
                cutoff,
                current_time,
                http=http,
                timeout=timeout,
            )
        except MercadoReputationError as exc:
            if exc.status_code == 401:
                raise
            row["infraction_count"] = None
            errors.append(f"侵权数量：{exc}")

        row["rights_holder_count"] = (
            rights_counts.get(site_id, 0) if not rights_error else None
        )
        row["infraction_recent_days"] = OFFICIAL_INFRACTION_DAYS
        row["infraction_source"] = "official_api"
        row["rights_holder_source"] = "official_api"
        if rights_error:
            errors.append(f"权利人数量：{rights_error}")
        row["official_api_errors"] = errors
    return rows


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
        def load(current_token: Mapping[str, Any]) -> dict[str, Any]:
            payload = fetch_reputation_payload(
                str(current_token.get("access_token") or ""),
                http=http,
                timeout=timeout,
            )
            loaded = normalize_reputation_payload(payload)
            # 旧测试数据和历史授权可能没有 meli_user_id；这种情况下仍返回
            # 主声誉数据，待授权资料补全后再启用官方附加指标。
            if str(current_token.get("meli_user_id") or "").strip():
                enrich_reputation_with_official_data(
                    loaded["rows"],
                    str(current_token.get("access_token") or ""),
                    http=http,
                    timeout=timeout,
                )
            return loaded

        try:
            result = load(token)
        except MercadoReputationError as exc:
            if exc.status_code != 401 or refresh_token is None:
                raise
            refresh_token(identifier)
            token = get_token(identifier)
            if not token:
                raise KeyError("刷新 Token 后店铺授权不存在")
            result = load(token)
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
