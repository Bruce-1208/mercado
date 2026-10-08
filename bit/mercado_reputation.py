"""Mercado Libre 官方卖家声誉接口及控制台响应标准化。"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Callable, Mapping

import requests


API_BASE_URL = "https://api.mercadolibre.com"
REPUTATION_PATH = "/global/users/seller_reputation"
ORDERS_SEARCH_PATH = "/marketplace/orders/search"
RIGHTS_HOLDER_CASES_PATH = "/moderations/pppi/cases"
LISTING_SAMPLE_SIZE = 50
OFFICIAL_INFRACTION_DAYS = 100
SEVEN_DAY_RATE_DISCLAIMER = (
    "七天变化率按单站点由官方订单 API 自算；数值可能因取消单、退款、时区和"
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
    site_id: str,
    date_from: datetime,
    date_to: datetime,
    *,
    http: requests.Session | None,
    timeout: int,
) -> int:
    normalized_site_id = str(site_id or "").strip().upper()
    if not normalized_site_id:
        raise MercadoReputationError("订单变化率缺少站点编号")
    payload = _fetch_json(
        access_token,
        ORDERS_SEARCH_PATH,
        params={
            "seller.id": str(seller_id),
            "site": normalized_site_id,
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
    raw_total = _mapping(payload.get("paging")).get("total", payload.get("total"))
    total = _number(raw_total)
    if isinstance(raw_total, bool) or not isinstance(total, int) or total < 0:
        raise MercadoReputationError("美客多订单接口未返回有效订单总数")
    return total


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
    status_codes = []
    raw_codes = []
    for value in (sell.get("codes"), listing.get("codes")):
        if isinstance(value, (list, tuple, set)):
            raw_codes.extend(value)
        elif value not in (None, ""):
            raw_codes.append(value)
    for value in raw_codes:
        if isinstance(value, Mapping):
            text = str(value.get("code") or value.get("id") or value.get("message") or "").strip()
        else:
            text = str(value or "").strip()
        if text and text not in status_codes:
            status_codes.append(text)
    required_action = str(status.get("required_action") or "").strip()
    if required_action and required_action not in status_codes:
        status_codes.append(required_action)
    lowered = raw_status.casefold()
    if sell_allowed is False or lowered in ("blocked", "disabled", "suspended"):
        account_status = "restricted"
        display = "已封禁（站点销售暂停，API 未提供封禁期限）"
    elif sell_allowed is True:
        account_status = "normal"
        display = "未封禁" + ("（限制刊登）" if list_allowed is False else "")
    elif list_allowed is False:
        account_status = "unknown"
        display = "待确认（限制刊登，销售权限未提供）"
    elif lowered in ("active", "enabled", "ok"):
        account_status = "unknown"
        display = "待确认（API 仅返回公开 active）"
    elif raw_status:
        account_status = "unknown"
        display = f"待确认（API 状态：{raw_status}）"
    else:
        account_status = "unknown"
        display = "待确认（API 未提供销售权限）"
    return {
        "site_status": raw_status or None,
        "site_status_display": display,
        "sell_allowed": sell_allowed,
        "list_allowed": list_allowed,
        "status_codes": status_codes,
        "required_action": required_action or None,
        "account_status": account_status,
        "is_banned": {"restricted": True, "normal": False}.get(account_status),
        "suspension_until": "",
        "suspension_days": None,
        "suspension_remaining_days": None,
        "suspension_total_days": None,
        "site_status_source": "official_users_api",
    }


def _merge_site_status(local_status: Mapping[str, Any], global_status: Mapping[str, Any] | None):
    """主账号状态没有站点作用范围，不能覆盖站点结论。"""
    local = dict(local_status or {})
    global_value = dict(global_status or {})
    local["global_site_status"] = global_value.get("site_status")
    return local


def _sample_site_listing_status(
    access_token: str,
    seller_id: str,
    *,
    http: requests.Session | None,
    timeout: int,
    sample_size: int = LISTING_SAMPLE_SIZE,
) -> dict[str, Any]:
    """Inspect the latest site listings, never a random or status-filtered page."""
    seller = str(seller_id or "").strip()
    size = min(100, max(1, int(sample_size)))
    if not seller:
        raise MercadoReputationError("缺少站点 seller_id，无法读取刊登")
    sampled_ids = []
    expected_count = None
    search_error = ""
    for offset in range(0, size, 50):
        try:
            payload = _fetch_json(
                access_token, f"/marketplace/users/{seller}/items/search",
                params={"limit": min(50, size - offset), "offset": offset,
                        "orders": "start_time_desc"},
                http=http, timeout=timeout, label="站点最新刊登接口",
            )
        except MercadoReputationError as exc:
            if exc.status_code == 401:
                raise
            search_error = str(exc)
            break
        if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
            search_error = "站点最新刊登接口返回格式错误"
            break
        ids = [str(value).strip() for value in payload["results"] if str(value or "").strip()]
        if expected_count is None:
            total = _mapping(payload.get("paging")).get("total")
            try:
                expected_count = min(size, max(0, int(total))) if total is not None else size
            except (TypeError, ValueError) as exc:
                raise MercadoReputationError("站点最新刊登总数无效") from exc
        sampled_ids.extend(value for value in ids if value not in sampled_ids)
        if len(sampled_ids) >= expected_count:
            break
        if not ids:
            break
    sampled_ids = sampled_ids[:size]

    sample = {
        "listing_sample_source": "official_listing_api",
        "listing_sample_expected_count": expected_count,
        "listing_sample_requested_count": len(sampled_ids),
        "listing_sample_count": 0,
        "listing_sample_active_count": 0,
        "listing_sample_inactive_count": 0,
        "listing_sample_status": None,
    }
    if search_error:
        sample["listing_sample_error"] = search_error
    if not sampled_ids:
        if not search_error:
            sample["listing_sample_status"] = "inactive"
        return sample

    detail_rows = []
    for offset in range(0, len(sampled_ids), 20):
        try:
            details = _fetch_json(
                access_token, "/items",
                params={"ids": ",".join(sampled_ids[offset:offset + 20])},
                http=http, timeout=timeout, label="站点刊登状态接口",
            )
        except MercadoReputationError as exc:
            if exc.status_code == 401:
                raise
            # Other batches may already prove an active listing. Missing data
            # can never prove a ban, but must not erase positive evidence.
            sample["listing_sample_error"] = str(exc)
            continue
        if isinstance(details, Mapping):
            details = details.get("items") or details.get("results") or []
        if not isinstance(details, list):
            sample["listing_sample_error"] = "站点刊登状态接口返回格式错误"
            continue
        detail_rows.extend(details)

    sampled_statuses: dict[str, str] = {}
    for entry in detail_rows:
        if not isinstance(entry, Mapping):
            continue
        try:
            response_code = int(entry.get("status_code", entry.get("code", 200)))
        except (TypeError, ValueError):
            continue
        if not 200 <= response_code < 300:
            continue
        body = _mapping(entry.get("body")) or entry
        item_id = str(body.get("id") or entry.get("id") or "").strip()
        status = str(body.get("status") or "").strip().casefold()
        if item_id in sampled_ids and status:
            sampled_statuses[item_id] = status

    active_count = sum(status == "active" for status in sampled_statuses.values())
    inactive_count = sum(status != "active" for status in sampled_statuses.values())
    sample.update({
        "listing_sample_count": len(sampled_statuses),
        "listing_sample_active_count": active_count,
        "listing_sample_inactive_count": inactive_count,
    })
    if active_count:
        sample["listing_sample_status"] = "active"
    elif len(sampled_statuses) == len(sampled_ids) == expected_count:
        sample["listing_sample_status"] = "inactive"
    return sample


def _apply_listing_sample_status(
    row: dict[str, Any], sample: Mapping[str, Any]
) -> None:
    """Combine account permissions with listing evidence without inferring a ban."""
    row.update(dict(sample))
    row.setdefault("site_status_official_display", row.get("site_status_display"))
    row.setdefault("site_status_official_account_status", row.get("account_status"))
    official_status = row.get("site_status_official_account_status")
    row["site_status_source"] = "official_status_evidence"
    # Current explicit permissions/restrictions take precedence over item state.
    if official_status in {"restricted", "normal"}:
        row["account_status"] = official_status
        row["site_status_display"] = row["site_status_official_display"]
        return
    if row.get("list_allowed") is False:
        # 刊登限制本身不证明整站封禁，也不能被商品抽样替换掉。
        return

    row.update({"suspension_until": "", "suspension_days": None,
                "suspension_remaining_days": None, "suspension_total_days": None,
                "account_status": "unknown"})
    if sample.get("listing_sample_status") == "active":
        display = "有在售商品（账号权限待确认）"
    elif sample.get("listing_sample_error"):
        display = "待确认（刊登读取异常）"
    elif sample.get("listing_sample_status") == "inactive":
        display = ("待确认（暂无商品）"
                   if sample.get("listing_sample_expected_count") == 0
                   else "待确认（抽样未见在售商品）")
    else:
        display = "待确认（数据不足）"
    row["site_status_display"] = display


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


def _notice_text(value: Any) -> str:
    """把官方 reason/remedy 的 HTML 转成可显示的完整正文。"""
    text = str(value or "")
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", "", text, flags=re.I | re.S)
    text = re.sub(r"<\s*(?:br\s*/?|/p|/div|/li)\s*>", "\n", text, flags=re.I)
    return unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _latest_site_warning(access_token, seller_id, site_id, *, http=None, timeout=30):
    """读取站点 API 可提供的最新违规告警，不限近 100 天、不限商品类型。

    官方接口按发生时间倒序；跨站点记录不进入候选。接口只提供部分
    违规管理记录，不能将空结果解释为后台历史告警全部为空。
    """
    if not seller_id or site_id not in SITE_NAMES:
        raise MercadoReputationError("缺少有效站点卖家 ID 或站点 ID")
    offset = 0
    for _page in range(100):
        payload = _fetch_json(
            access_token, f"/marketplace/moderations/infractions/{seller_id}",
            params={"sort": "date_created_desc", "limit": 20, "offset": offset},
            http=http, timeout=timeout, label="美客多站点最新告警接口",
        )
        if not isinstance(payload, Mapping) or "infractions" not in payload:
            raise MercadoReputationError("站点告警接口缺少 infractions 字段")
        values = payload["infractions"]
        paging = _mapping(payload.get("paging"))
        total = _number(paging.get("total"))
        if total is None or total < 0 or int(total) != total:
            raise MercadoReputationError("站点告警接口缺少有效分页总数")
        if values is None and total == 0:
            values = []
        if not isinstance(values, list) or any(not isinstance(v, Mapping) for v in values):
            raise MercadoReputationError("站点告警接口返回格式错误")
        if payload.get("sorting_type", "date_created_desc") != "date_created_desc":
            raise MercadoReputationError("站点告警接口未确认按发生时间倒序")
        candidates = []
        for notice in values:
            notice_site = str(notice.get("site_id") or "").strip().upper()
            if not notice_site:
                item_id = str(notice.get("related_item_id") or notice.get("element_id") or "").upper()
                notice_site = item_id[:3] if item_id[:3] in SITE_NAMES else ""
            if not notice_site:
                raise MercadoReputationError("告警记录缺少站点归属，无法确认最新告警")
            if notice_site != site_id:
                continue
            if notice.get("user_id") is not None and str(notice["user_id"]) != seller_id:
                raise MercadoReputationError("告警记录卖家与站点卖家不一致")
            date = str(notice.get("date_created") or "").strip()
            try:
                occurred = datetime.fromisoformat(date.replace("Z", "+00:00"))
                if occurred.tzinfo is None:
                    raise ValueError("missing timezone")
            except ValueError as exc:
                raise MercadoReputationError("告警发生时间无效，无法确认最新一条") from exc
            reason = _notice_text(notice.get("reason"))
            remedy = _notice_text(notice.get("remedy"))
            if not reason:
                raise MercadoReputationError("站点告警缺少完整原因")
            candidates.append((occurred, notice, date, reason, remedy))
        if candidates:
            _date, latest, date, reason, remedy = max(candidates, key=lambda v: v[0])
            return {
                "system_warning": "\n".join(v for v in (date, reason, remedy) if v),
                "system_warning_id": str(latest.get("id") or ""),
                "system_warning_occurred_at": date,
                "system_warning_source": "official_site_infractions_api",
                "system_warning_status": "success",
            }
        if offset + len(values) >= total:
            return {
                "system_warning": "暂无 API 告警（仅限接口可获取记录）",
                "system_warning_source": "official_site_infractions_api",
                "system_warning_status": "empty",
            }
        if not values:
            raise MercadoReputationError("站点告警分页不完整")
        offset += len(values)
    raise MercadoReputationError("站点告警分页超出读取上限，未确认最新告警")


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
    global_user_id: str | int | None = None,
    now: datetime | None = None,
    http: requests.Session | None = None,
    timeout: int = 30,
    include_listing_sample: bool = False,
) -> list[dict[str, Any]]:
    """补齐站点封禁、最新 API 告警、订单变化及近 100 天违规统计。"""

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
        row["site_status_checked_at"] = current_time.isoformat()

        try:
            if not seller_id or site_id not in SITE_NAMES:
                raise MercadoReputationError("缺少有效站点卖家 ID 或站点 ID")
            profile = _fetch_json(
                access_token,
                f"/users/{seller_id}",
                http=http,
                timeout=timeout,
                label="美客多站点状态接口",
            )
            if not isinstance(profile, Mapping):
                raise MercadoReputationError("美客多站点状态接口返回格式错误")
            if not seller_id:
                raise MercadoReputationError("缺少站点卖家 ID")
            if profile.get("id") is not None and str(profile["id"]) != seller_id:
                raise MercadoReputationError("账号接口卖家与站点卖家不一致")
            if profile.get("site_id") and str(profile["site_id"]).upper() != site_id:
                raise MercadoReputationError("账号接口返回了其他站点")
            row.update(_site_status(profile))
        except MercadoReputationError as exc:
            if exc.status_code == 401:
                raise
            row.update({
                "site_status": None,
                "site_status_display": "待确认（站点状态 API 读取失败）",
                "account_status": "unknown",
                "is_banned": None,
                "site_status_source": "official_users_api",
            })
            errors.append(f"站点状态：{exc}")

        if include_listing_sample:
            try:
                listing_sample = _sample_site_listing_status(
                    access_token,
                    seller_id,
                    http=http,
                    timeout=timeout,
                )
                _apply_listing_sample_status(row, listing_sample)
                if listing_sample.get("listing_sample_error"):
                    errors.append(f"站点最新刊登：{listing_sample['listing_sample_error']}")
            except MercadoReputationError as exc:
                if exc.status_code == 401:
                    raise
                _apply_listing_sample_status(row, {})
                errors.append(f"站点最新刊登：{exc}")

        order_counts = {}
        for key, date_from, date_to, label in (
            ("orders_previous_7d", previous_start, current_start, "前七天单量"),
            ("orders_current_7d", current_start, current_time, "七天单量"),
        ):
            try:
                order_counts[key] = _order_total(
                    access_token, seller_id, site_id, date_from, date_to,
                    http=http, timeout=timeout,
                )
            except MercadoReputationError as exc:
                if exc.status_code == 401:
                    raise
                order_counts[key] = None
                errors.append(f"{label}：{exc}")
        row.update(order_counts)
        row.update({
            "gradient_source": "official_orders_api",
            "gradient_disclaimer": SEVEN_DAY_RATE_DISCLAIMER,
        })
        previous_orders = order_counts["orders_previous_7d"]
        current_orders = order_counts["orders_current_7d"]
        if previous_orders is not None and current_orders is not None:
            direction, rate_text, rate = _format_change_rate(
                current_orders, previous_orders
            )
            row.update({
                "direction": direction,
                "gradient_rate": rate_text,
                "gradient_rate_percent": rate,
            })
        else:
            row.update({
                "direction": "获取失败",
                "gradient_rate": "-",
                "gradient_rate_percent": None,
            })

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

        row["system_warning_checked_at"] = current_time.isoformat()
        try:
            row.update(_latest_site_warning(
                access_token, seller_id, site_id, http=http, timeout=timeout,
            ))
        except MercadoReputationError as exc:
            if exc.status_code == 401:
                raise
            row.update({"system_warning": None, "system_warning_status": "failed",
                        "system_warning_source": "official_site_infractions_api",
                        "system_warning_error": str(exc)})
            errors.append(f"系统告警：{exc}")

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
            enrich_reputation_with_official_data(
                loaded["rows"],
                str(current_token.get("access_token") or ""),
                http=http,
                timeout=timeout,
                include_listing_sample=False,
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
