"""Official Mercado Libre infringement collection and 12-hour scheduling."""

from __future__ import annotations

import hashlib
import html
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from bit import bit_mysql, mercado_tokens
from bit.bit_runtime_lock import InterProcessLock, get_lock_owner
from erp.mercadolibre_infraction_store import (
    count_infraction_records,
    get_infraction_sync_context,
    list_due_infraction_token_ids,
    list_missing_infraction_media,
    mark_infraction_sync_finished,
    mark_infraction_sync_started,
    request_infraction_sync,
    upsert_infraction_records,
    update_infraction_media,
)
from mercado_api.client import MercadoAPIError, MercadoLibreClient


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


INFRACTION_SYNC_LOCK_KEY = "mercado_official_infraction_sync_task"
INFRACTION_AUTO_SYNC_HOURS = _env_int(
    "MERCADO_INFRACTION_AUTO_SYNC_HOURS", 12, 1, 168
)
INFRACTION_AUTO_RETRY_MINUTES = _env_int(
    "MERCADO_INFRACTION_AUTO_RETRY_MINUTES", 60, 5, 1440
)
INFRACTION_AUTO_CHECK_SECONDS = _env_int(
    "MERCADO_INFRACTION_AUTO_CHECK_SECONDS", 300, 60, 3600
)
INFRACTION_STORE_WORKERS = _env_int(
    "MERCADO_INFRACTION_STORE_WORKERS", 2, 1, 8
)
INFRACTION_DETAIL_WORKERS = _env_int(
    "MERCADO_INFRACTION_DETAIL_WORKERS", 6, 1, 12
)
INFRACTION_INITIAL_DETECTION_DAYS = _env_int(
    "MERCADO_INFRACTION_INITIAL_DETECTION_DAYS", 2, 1, 30
)
INFRACTION_INITIAL_RIGHTS_HOLDER_DAYS = _env_int(
    "MERCADO_INFRACTION_INITIAL_RIGHTS_HOLDER_DAYS", 365, 30, 3650
)
INFRACTION_MAX_DETECTION_PAGES = _env_int(
    "MERCADO_INFRACTION_MAX_DETECTION_PAGES", 100, 1, 1000
)
INFRACTION_MAX_CASE_PAGES = _env_int(
    "MERCADO_INFRACTION_MAX_CASE_PAGES", 100, 1, 1000
)
LIVE_INFRACTION_REQUEST_TIMEOUT_SECONDS = _env_int(
    "MERCADO_DAILY_INFRACTION_REQUEST_TIMEOUT_SECONDS", 10, 5, 60
)
LIVE_INFRACTION_STORE_TIMEOUT_SECONDS = _env_int(
    "MERCADO_DAILY_INFRACTION_STORE_TIMEOUT_SECONDS", 180, 30, 900
)
INFRACTION_IMAGE_BACKFILL_LIMIT = _env_int(
    "MERCADO_INFRACTION_IMAGE_BACKFILL_LIMIT", 500, 20, 5000
)

DETECTION_PAGE_SIZE = 20
CASE_PAGE_SIZE = 50
BRAND_PROTECTION_SUBGROUP = "BRAND_PROTECTION"

_BRAND_PROTECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bcounterfeit(?:ed|ing)?\b",
        r"\bintellectual propert(?:y|ies)\b",
        r"\btrademark(?:ed|s)?\b",
        r"\bcopyright(?:ed|s)?\b",
        r"\bpatent(?:ed|s)?\b",
        r"\bindustrial design\b",
        r"\bbrand is not generic\b",
        r"\bbrand highly susceptible\b",
        r"\bhighly susceptible to counterfeit\b",
        r"\brestricted brand\b",
        r"\bprotected brand\b",
        r"\bauthenticity\b",
        r"\bnot (?:an )?authentic\b",
        r"falsific",
        r"propiedad intelectual",
        r"propriedade intelectual",
        r"derechos? de autor",
        r"direitos? autorais?",
        r"uso (?:indebido|ileg[ií]timo) de (?:la )?marca",
        r"uso (?:indevido|ileg[ií]timo) da marca",
        r"marca ileg[ií]timamente",
        r"marca (?:no es|n[aã]o [ée]) gen[eé]rica",
        r"dise[nñ]o industrial",
        r"desenho industrial",
        r"produto falsificado",
        r"producto falsificado",
    )
)

_state_guard = threading.RLock()
_scheduler_guard = threading.Lock()
_scheduler_thread: threading.Thread | None = None
_sync_state: dict[str, Any] = {
    "running": False,
    "task_id": "",
    "status": "idle",
    "message": "等待官方侵权数据同步",
    "total_stores": 0,
    "processed_stores": 0,
    "active_stores": [],
    "detection_scanned_count": 0,
    "detection_matched_count": 0,
    "rights_holder_count": 0,
    "failed_count": 0,
    "started_at": "",
    "finished_at": "",
    "results": [],
    "logs": [],
}


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _state_update(**changes: Any) -> None:
    with _state_guard:
        _sync_state.update(changes)


def _state_increment(**deltas: int) -> None:
    with _state_guard:
        for field, value in deltas.items():
            _sync_state[field] = int(_sync_state.get(field) or 0) + int(value or 0)


def _append_log(message: str) -> None:
    line = f"{_now_text()} {str(message or '').strip()}"
    with _state_guard:
        logs = list(_sync_state.get("logs") or [])
        logs.append(line)
        _sync_state["logs"] = logs[-300:]


def _set_store_active(store_name: str, active: bool) -> None:
    with _state_guard:
        names = list(_sync_state.get("active_stores") or [])
        if active and store_name not in names:
            names.append(store_name)
        elif not active and store_name in names:
            names.remove(store_name)
        _sync_state["active_stores"] = names


def official_infraction_sync_status() -> dict[str, Any]:
    with _state_guard:
        state = dict(_sync_state)
        state["active_stores"] = list(state.get("active_stores") or [])
        state["results"] = [dict(row) for row in state.get("results") or []]
        state["logs"] = list(state.get("logs") or [])
    owner = get_lock_owner(INFRACTION_SYNC_LOCK_KEY)
    if owner and not state.get("running"):
        state.update(
            running=True,
            status="running",
            message="官方侵权数据正在其他进程同步",
            lock_owner=owner,
        )
    state.update(
        auto_sync_enabled=True,
        auto_sync_hours=INFRACTION_AUTO_SYNC_HOURS,
        store_workers=INFRACTION_STORE_WORKERS,
        source="Mercado Libre Moderations API + Brand Protection API",
    )
    return state


def _plain_text(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _mysql_datetime(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in (
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%m/%d/%y",
            "%m/%d/%Y",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return text[:19].replace("T", " ") if len(text) >= 10 else None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def is_brand_protection_detection(infraction: Mapping[str, Any]) -> bool:
    subgroup = str(
        infraction.get("filter_subgroup")
        or infraction.get("subgroup")
        or infraction.get("filter_group")
        or ""
    ).strip().upper()
    if subgroup == BRAND_PROTECTION_SUBGROUP:
        return True
    text = " ".join(
        _plain_text(infraction.get(key))
        for key in ("reason", "remedy", "name")
    )
    return any(pattern.search(text) for pattern in _BRAND_PROTECTION_PATTERNS)


def _token_ids(values: Iterable[Any]) -> list[int]:
    result: list[int] = []
    for value in values or ():
        try:
            token_id = int(value)
        except (TypeError, ValueError):
            continue
        if token_id > 0 and token_id not in result:
            result.append(token_id)
    return result


def _token_records(selected_token_ids: Iterable[Any] | None = None) -> list[dict]:
    selected = set(_token_ids(selected_token_ids or ()))
    summaries = (bit_mysql.list_mercado_store_tokens() or {}).get("rows") or []
    disabled = {
        int(summary.get("id") or 0)
        for summary in summaries
        if not bool(summary.get("enabled", True))
    } & selected
    if disabled:
        raise ValueError(f"选择的店铺已关闭：{', '.join(map(str, sorted(disabled)))}")
    records = []
    for summary in summaries:
        if not bool(summary.get("enabled", True)):
            continue
        token_id = int(summary.get("id") or 0)
        if selected and token_id not in selected:
            continue
        record = bit_mysql.get_mercado_store_token(token_id)
        if record:
            records.append(
                {**dict(record), "site_settings": list(summary.get("site_settings") or [])}
            )
    if selected:
        missing = selected.difference(int(row.get("id") or 0) for row in records)
        if missing:
            raise ValueError(f"选择的店铺授权不存在：{', '.join(map(str, sorted(missing)))}")
    if not records:
        raise ValueError("暂无已授权店铺，请先在“店铺授权”中完成授权")
    return records


def _token_expiring(record: Mapping[str, Any]) -> bool:
    expires_at = record.get("expires_at")
    if not expires_at:
        return False
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            return False
    now = datetime.now(expires_at.tzinfo) if getattr(expires_at, "tzinfo", None) else datetime.now()
    return expires_at <= now + timedelta(minutes=5)


def _refresh_token(token_id: int) -> dict:
    mercado_tokens.refresh_and_save(
        int(token_id),
        get_token=bit_mysql.get_mercado_store_token,
        update_token=bit_mysql.update_mercado_store_token,
        record_error=bit_mysql.record_mercado_store_token_error,
    )
    return dict(bit_mysql.get_mercado_store_token(int(token_id)) or {})


def _client_and_token(
    record: dict,
    *,
    timeout: int | None = None,
) -> tuple[MercadoLibreClient, dict]:
    if _token_expiring(record) and record.get("refresh_token"):
        refreshed = _refresh_token(int(record["id"]))
        record = {**refreshed, "site_settings": record.get("site_settings") or []}
    access_token = str(record.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("店铺缺少 Access Token，请重新授权")
    client_kwargs = {"timeout": int(timeout)} if timeout is not None else {}
    return MercadoLibreClient(access_token, **client_kwargs), record


def _is_unauthorized_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "401" in message or "access token" in message


def _marketplace_accounts(client: MercadoLibreClient, root_seller_id: str) -> list[dict]:
    profile = client.request("GET", f"/marketplace/users/{root_seller_id}")
    accounts = []
    seen: set[tuple[str, str]] = set()
    for marketplace in profile.get("marketplaces") or []:
        user_id = str(marketplace.get("user_id") or "").strip()
        site_id = str(marketplace.get("site_id") or "").strip().upper()
        key = (user_id, site_id)
        if user_id and site_id and key not in seen:
            seen.add(key)
            accounts.append({"user_id": user_id, "site_id": site_id})
    return accounts or [
        {
            "user_id": str(root_seller_id),
            "site_id": str(profile.get("site_id") or "CBT").strip().upper(),
        }
    ]


def _site_setting_map(record: Mapping[str, Any]) -> dict[str, dict]:
    return {
        str(row.get("site_id") or "").strip().upper(): dict(row)
        for row in record.get("site_settings") or []
        if str(row.get("site_id") or "").strip()
    }


def _infraction_page_rows(page: Mapping[str, Any]) -> list[dict]:
    for key in ("infractions", "results", "elements"):
        value = page.get(key)
        if isinstance(value, list):
            return [dict(row) for row in value if isinstance(row, Mapping)]
    return []


def _page_total(page: Mapping[str, Any], fallback: int) -> int:
    paging = page.get("paging") if isinstance(page.get("paging"), Mapping) else {}
    for value in (paging.get("total"), page.get("total")):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return fallback


def _fetch_detection_pages(
    client: MercadoLibreClient,
    seller_id: str,
    *,
    date_created_since: str,
    filter_subgroup: str = "",
    stop_event: Any = None,
    deadline: float | None = None,
    progress: Any = None,
) -> tuple[list[dict], int, bool]:
    offset = 0
    pages = 0
    scanned = 0
    matches: list[dict] = []
    seen: set[str] = set()
    while pages < INFRACTION_MAX_DETECTION_PAGES:
        if _stop_requested(stop_event):
            raise RuntimeError("已停止")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("单店侵权 API 读取超过最长等待时间")
        params: dict[str, Any] = {
            "element_type": "ITM",
            "date_created_since": date_created_since,
            "limit": DETECTION_PAGE_SIZE,
            "offset": offset,
            "sort": "date_created_desc",
        }
        if filter_subgroup:
            params["filter_subgroup"] = filter_subgroup
        page = client.request(
            "GET",
            f"/marketplace/moderations/infractions/{seller_id}",
            params=params,
        )
        if not isinstance(page, Mapping):
            break
        rows = _infraction_page_rows(page)
        scanned += len(rows)
        for row in rows:
            source_id = str(row.get("id") or "").strip()
            key = source_id or hashlib.sha1(
                f"{row.get('related_item_id')}|{row.get('date_created')}|{row.get('reason')}".encode(
                    "utf-8", errors="replace"
                )
            ).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            if filter_subgroup or is_brand_protection_detection(row):
                matches.append(row)
        pages += 1
        if progress is not None:
            progress(pages, scanned, len(matches))
        offset += len(rows)
        total = _page_total(page, offset)
        if not rows or offset >= total or len(rows) < DETECTION_PAGE_SIZE:
            return matches, scanned, False
    return matches, scanned, True


def _collect_live_detection_records(
    client: MercadoLibreClient,
    accounts: Iterable[Mapping[str, Any]],
    *,
    date_created_since: str,
    store_name: str,
    stop_event: Any = None,
    deadline: float | None = None,
) -> tuple[list[dict], int, bool]:
    """Lightweight moderation reader for the daily task; never requests item details."""

    records = []
    scanned_total = 0
    truncated = False

    def progress(pages: int, scanned: int, matched: int) -> None:
        if pages % 10 == 0:
            print(
                f"{_now_text()} {store_name} API侵权分页进度："
                f"{pages} 页，扫描 {scanned} 条，命中 {matched} 条"
            )

    for account in accounts:
        seller_id = str(account.get("user_id") or "").strip()
        site_id = str(account.get("site_id") or "").strip().upper()
        try:
            matches, scanned, capped = _fetch_detection_pages(
                client,
                seller_id,
                date_created_since=date_created_since,
                filter_subgroup=BRAND_PROTECTION_SUBGROUP,
                stop_event=stop_event,
                deadline=deadline,
                progress=progress,
            )
        except MercadoAPIError:
            matches, scanned, capped = [], 0, False
        if not matches:
            matches, scanned, capped = _fetch_detection_pages(
                client,
                seller_id,
                date_created_since=date_created_since,
                stop_event=stop_event,
                deadline=deadline,
                progress=progress,
            )
        scanned_total += scanned
        truncated = truncated or capped
        for row in matches:
            item_id = _item_id(row)
            if not item_id:
                continue
            records.append(
                {
                    "source_id": str(row.get("id") or ""),
                    "site_id": str(row.get("site_id") or site_id or item_id[:3]).upper(),
                    "item_id": item_id,
                    "title": str(row.get("title") or ""),
                    "occurred_at": _mysql_datetime(row.get("date_created")),
                }
            )
    return records, scanned_total, truncated


def _item_id(source: Mapping[str, Any]) -> str:
    return str(
        source.get("related_item_id")
        or source.get("element_id")
        or source.get("item_id")
        or ""
    ).strip().upper()


def _http_url(value: Any) -> str:
    url = str(value or "").strip()
    if url.startswith("//"):
        url = f"https:{url}"
    if url.startswith("http://"):
        url = f"https://{url[7:]}"
    return url if url.startswith("https://") else ""


def _thumbnail_url(source: Mapping[str, Any]) -> str:
    for picture in source.get("pictures") or ():
        if isinstance(picture, Mapping):
            url = _http_url(
                picture.get("secure_url")
                or picture.get("url")
                or picture.get("source")
            )
            if url:
                return url
        else:
            url = _http_url(picture)
            if url:
                return url
    return _http_url(source.get("secure_thumbnail") or source.get("thumbnail"))


def _item_details(client: MercadoLibreClient, item_ids: Iterable[str]) -> dict[str, dict]:
    ids = list(dict.fromkeys(str(value or "").strip().upper() for value in item_ids if value))
    if not ids:
        return {}
    details: dict[str, dict] = {}
    with ThreadPoolExecutor(
        max_workers=min(INFRACTION_DETAIL_WORKERS, len(ids)),
        thread_name_prefix="meli-infraction-items",
    ) as executor:
        futures = {
            executor.submit(
                client.get_marketplace_item,
                item_id,
                attributes=(
                    "id",
                    "title",
                    "site_id",
                    "status",
                    "sub_status",
                    "pictures",
                    "secure_thumbnail",
                    "thumbnail",
                    "permalink",
                ),
            ): item_id
            for item_id in ids
        }
        for future in as_completed(futures):
            item_id = futures[future]
            try:
                detail = future.result()
                if isinstance(detail, Mapping):
                    details[item_id] = dict(detail)
            except Exception:
                continue
    return details


def _item_titles(client: MercadoLibreClient, item_ids: Iterable[str]) -> dict[str, str]:
    return {
        item_id: str(detail.get("title") or "")
        for item_id, detail in _item_details(client, item_ids).items()
    }


def _collect_detection_records(
    client: MercadoLibreClient,
    record: Mapping[str, Any],
    accounts: Iterable[Mapping[str, Any]],
    *,
    date_created_since: str,
) -> tuple[list[dict], int, bool]:
    settings = _site_setting_map(record)
    raw_matches: list[tuple[dict, dict]] = []
    scanned_total = 0
    truncated = False
    for account in accounts:
        seller_id = str(account.get("user_id") or "")
        site_id = str(account.get("site_id") or "").upper()
        try:
            matches, scanned, capped = _fetch_detection_pages(
                client,
                seller_id,
                date_created_since=date_created_since,
                filter_subgroup=BRAND_PROTECTION_SUBGROUP,
            )
        except MercadoAPIError:
            matches, scanned, capped = [], 0, False
        if not matches:
            matches, scanned, capped = _fetch_detection_pages(
                client,
                seller_id,
                date_created_since=date_created_since,
            )
        scanned_total += scanned
        truncated = truncated or capped
        raw_matches.extend((row, {"user_id": seller_id, "site_id": site_id}) for row in matches)

    item_details = _item_details(
        client,
        (_item_id(row) for row, _account in raw_matches),
    )
    normalized = []
    for row, account in raw_matches:
        item_id = _item_id(row)
        if not item_id:
            continue
        site_id = str(row.get("site_id") or account.get("site_id") or item_id[:3]).upper()
        setting = settings.get(site_id) or {}
        item_detail = item_details.get(item_id) or {}
        normalized.append(
            {
                "source_type": "detection",
                "source_id": str(row.get("id") or ""),
                "seller_id": str(row.get("user_id") or account.get("user_id") or ""),
                "site_id": site_id,
                "item_id": item_id,
                "title": str(item_detail.get("title") or ""),
                "thumbnail_url": _thumbnail_url(item_detail),
                "permalink": _http_url(item_detail.get("permalink")),
                "occurred_at": _mysql_datetime(row.get("date_created")),
                "status": str(row.get("status") or ""),
                "reason_code": str(
                    row.get("filter_subgroup")
                    or row.get("subgroup")
                    or "BRAND_PROTECTION_REASON"
                ),
                "reason": _plain_text(row.get("reason")),
                "remedy": _plain_text(row.get("remedy")),
                "salesperson": setting.get("salesperson") or "",
                "group_name": setting.get("group_name") or "",
                "raw_json": row,
            }
        )
    return normalized, scanned_total, truncated


def _case_rows(payload: Any) -> tuple[list[dict], dict]:
    if isinstance(payload, Mapping):
        values = payload.get("cases") or payload.get("results") or []
        rows = [dict(row) for row in values if isinstance(row, Mapping)]
        return rows, dict(payload.get("paging") or {})
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


def _fetch_case_pages(
    client: MercadoLibreClient,
    *,
    date_created_since: str,
) -> tuple[list[dict], bool]:
    offset = 0
    pages = 0
    records: list[dict] = []
    seen: set[str] = set()
    while pages < INFRACTION_MAX_CASE_PAGES:
        payload = client.request(
            "GET",
            "/moderations/pppi/cases",
            params={
                "offset": offset,
                "date_created": date_created_since,
                "status": "",
            },
        )
        rows, paging = _case_rows(payload)
        for row in rows:
            case_id = str(row.get("case_id") or "").strip()
            if case_id and case_id not in seen:
                seen.add(case_id)
                records.append(row)
        pages += 1
        limit = int(paging.get("limit") or CASE_PAGE_SIZE)
        total = int(paging.get("total") or (offset + len(rows)))
        if not rows or offset + len(rows) >= total or len(rows) < limit:
            return records, False
        offset += limit
    return records, True


def _case_details(client: MercadoLibreClient, cases: Iterable[Mapping[str, Any]]) -> dict[str, dict]:
    case_ids = list(
        dict.fromkeys(str(row.get("case_id") or "").strip() for row in cases if row.get("case_id"))
    )
    if not case_ids:
        return {}
    details: dict[str, dict] = {}
    with ThreadPoolExecutor(
        max_workers=min(INFRACTION_DETAIL_WORKERS, len(case_ids)),
        thread_name_prefix="meli-infraction-cases",
    ) as executor:
        futures = {
            executor.submit(client.request, "GET", f"/moderations/pppi/case/{case_id}"): case_id
            for case_id in case_ids
        }
        for future in as_completed(futures):
            case_id = futures[future]
            try:
                detail = future.result()
                if isinstance(detail, Mapping):
                    details[case_id] = dict(detail)
            except Exception:
                continue
    return details


def _collect_rights_holder_records(
    client: MercadoLibreClient,
    record: Mapping[str, Any],
    *,
    date_created_since: str,
) -> tuple[list[dict], bool]:
    cases, truncated = _fetch_case_pages(
        client,
        date_created_since=date_created_since,
    )
    details = _case_details(client, cases)
    settings = _site_setting_map(record)
    normalized = []
    for case in cases:
        case_id = str(case.get("case_id") or "").strip()
        detail = details.get(case_id) or {}
        item_info = detail.get("item_info") if isinstance(detail.get("item_info"), Mapping) else {}
        item_id = str(
            item_info.get("item_id") or case.get("item_id") or ""
        ).strip().upper()
        if not item_id or not case_id:
            continue
        site_id = item_id[:3].upper()
        setting = settings.get(site_id) or {}
        normalized.append(
            {
                "source_type": "rights_holder",
                "source_id": case_id,
                "seller_id": str(record.get("meli_user_id") or ""),
                "site_id": site_id,
                "item_id": item_id,
                "title": str(item_info.get("title") or ""),
                "thumbnail_url": _thumbnail_url(item_info),
                "permalink": _http_url(item_info.get("permalink")),
                "occurred_at": _mysql_datetime(
                    detail.get("date_created") or case.get("date_created")
                ),
                "due_at": _mysql_datetime(detail.get("due_date") or case.get("due_date")),
                "status": str(
                    detail.get("current_status") or case.get("current_status") or ""
                ),
                "reason_code": str(detail.get("reason_id") or "PPPI"),
                "reason": _plain_text(
                    detail.get("reason_text") or case.get("reason_text")
                ),
                "remedy": "",
                "rights_holder": str(detail.get("public_member_name") or ""),
                "salesperson": setting.get("salesperson") or "",
                "group_name": setting.get("group_name") or "",
                "raw_json": {"case": case, "detail": detail},
            }
        )
    return normalized, truncated


def _sync_start_dates(context: Mapping[str, Any]) -> tuple[str, str]:
    last_completed = context.get("last_completed_at")
    if last_completed:
        try:
            parsed = datetime.fromisoformat(str(last_completed))
            since = (parsed - timedelta(days=1)).strftime("%Y-%m-%d")
            return since, since
        except ValueError:
            pass
    now = datetime.now()
    return (
        (now - timedelta(days=INFRACTION_INITIAL_DETECTION_DAYS)).strftime("%Y-%m-%d"),
        (now - timedelta(days=INFRACTION_INITIAL_RIGHTS_HOLDER_DAYS)).strftime("%Y-%m-%d"),
    )


def _sync_store_once(client: MercadoLibreClient, record: dict) -> dict:
    token_id = int(record["id"])
    store_name = str(record.get("display_name") or record.get("nickname") or token_id)
    root_seller_id = str(record.get("meli_user_id") or "").strip()
    if not root_seller_id:
        raise ValueError("店铺授权缺少 Seller ID，请刷新 Token 或重新授权")
    context = get_infraction_sync_context(token_id)
    detection_since, rights_since = _sync_start_dates(context)
    accounts = _marketplace_accounts(client, root_seller_id)
    errors = []
    detection_records: list[dict] = []
    rights_records: list[dict] = []
    scanned = 0

    try:
        _append_log(f"{store_name} 读取 {detection_since} 起的平台侵权检测")
        detection_records, scanned, capped = _collect_detection_records(
            client,
            record,
            accounts,
            date_created_since=detection_since,
        )
        if capped:
            errors.append("平台检测记录超过本轮安全分页上限")
        upsert_infraction_records(record, detection_records)
    except Exception as exc:
        if _is_unauthorized_error(exc):
            raise
        errors.append(f"平台检测：{exc}")

    try:
        _append_log(f"{store_name} 读取 {rights_since} 起的权利人举报")
        rights_records, capped = _collect_rights_holder_records(
            client,
            record,
            date_created_since=rights_since,
        )
        if capped:
            errors.append("权利人举报超过本轮安全分页上限")
        upsert_infraction_records(record, rights_records)
    except Exception as exc:
        if _is_unauthorized_error(exc):
            raise
        errors.append(f"权利人举报：{exc}")

    status = "success" if not errors else "partial"
    return {
        "store": store_name,
        "token_id": token_id,
        "status": status,
        "detection_scanned": scanned,
        "detection_matched": len(detection_records),
        "rights_holder": len(rights_records),
        "message": "；".join(errors),
    }


def _sync_store(record: dict) -> dict:
    token_id = int(record["id"])
    client, record = _client_and_token(record)
    refreshed_after_unauthorized = False
    while True:
        try:
            return _sync_store_once(client, record)
        except Exception as exc:
            if (
                refreshed_after_unauthorized
                or not _is_unauthorized_error(exc)
                or not record.get("refresh_token")
            ):
                raise
            refreshed = _refresh_token(token_id)
            record = {**refreshed, "site_settings": record.get("site_settings") or []}
            client = MercadoLibreClient(str(record.get("access_token") or ""))
            refreshed_after_unauthorized = True
            _append_log(f"{record.get('display_name') or token_id} Token 已刷新，继续同步")


def _stop_requested(stop_event: Any = None) -> bool:
    try:
        return bool(stop_event is not None and stop_event.is_set())
    except (BrokenPipeError, EOFError, OSError):
        return True


def _collect_live_detection_target(
    record: dict,
    target: Mapping[str, Any],
    *,
    date_created_since: str,
    stop_event: Any = None,
) -> dict[str, Any]:
    """Read one authorized store's infringement list directly from official APIs."""

    token_id = int(record.get("id") or target.get("token_id") or 0)
    store_name = str(
        target.get("name")
        or record.get("display_name")
        or record.get("nickname")
        or token_id
    ).strip()
    allowed_sites = {
        str(value or "").strip().upper()
        for value in target.get("site_ids") or target.get("sites") or ()
        if str(value or "").strip()
    }
    if _stop_requested(stop_event):
        return {
            "store": store_name,
            "token_id": token_id,
            "status": "stopped",
            "rows": [],
            "scanned": 0,
            "message": "已停止",
        }

    refreshed_after_unauthorized = False
    while True:
        try:
            client, current_record = _client_and_token(
                record,
                timeout=LIVE_INFRACTION_REQUEST_TIMEOUT_SECONDS,
            )
            root_seller_id = str(current_record.get("meli_user_id") or "").strip()
            if not root_seller_id:
                raise ValueError("店铺授权缺少 Seller ID，请刷新 Token 或重新授权")
            accounts = _marketplace_accounts(client, root_seller_id)
            if allowed_sites:
                accounts = [
                    account
                    for account in accounts
                    if str(account.get("site_id") or "").strip().upper()
                    in allowed_sites
                ]
            if not accounts:
                return {
                    "store": store_name,
                    "token_id": token_id,
                    "status": "success",
                    "rows": [],
                    "scanned": 0,
                    "message": "授权站点没有对应的 Marketplace 账号",
                }
            if _stop_requested(stop_event):
                return {
                    "store": store_name,
                    "token_id": token_id,
                    "status": "stopped",
                    "rows": [],
                    "scanned": 0,
                    "message": "已停止",
                }

            deadline = time.monotonic() + LIVE_INFRACTION_STORE_TIMEOUT_SECONDS
            records, scanned, capped = _collect_live_detection_records(
                client,
                accounts,
                date_created_since=date_created_since,
                store_name=store_name,
                stop_event=stop_event,
                deadline=deadline,
            )
            rows = []
            seen_items: set[tuple[str, str]] = set()
            for source in records:
                site_id = str(source.get("site_id") or "").strip().upper()
                item_id = str(source.get("item_id") or "").strip().upper()
                if not item_id or (allowed_sites and site_id not in allowed_sites):
                    continue
                item_key = (site_id, item_id)
                if item_key in seen_items:
                    continue
                seen_items.add(item_key)
                rows.append(
                    {
                        "店铺名": store_name,
                        "站点": site_id,
                        "编号": item_id,
                        "标题": str(source.get("title") or ""),
                        "侵权时间": source.get("occurred_at") or "",
                        "提交时间": "",
                        "执行时间": _now_text(),
                        "类型": "侵权",
                        "source_id": str(source.get("source_id") or ""),
                    }
                )
            return {
                "store": store_name,
                "token_id": token_id,
                "status": "partial" if capped else "success",
                "rows": rows,
                "scanned": scanned,
                "message": "API 分页达到安全上限" if capped else "",
            }
        except Exception as exc:
            if (
                refreshed_after_unauthorized
                or not _is_unauthorized_error(exc)
                or not record.get("refresh_token")
            ):
                raise
            refreshed = _refresh_token(token_id)
            record = {
                **refreshed,
                "site_settings": record.get("site_settings") or [],
            }
            refreshed_after_unauthorized = True


def collect_live_detection_infractions(
    targets: Iterable[Mapping[str, Any]],
    *,
    recent_days: int = 100,
    max_workers: int = 8,
    stop_event: Any = None,
) -> dict[str, Any]:
    """Collect daily-task infringement rows without opening browser windows.

    ``targets`` contains token IDs, display names and the authorized site IDs. Each
    store is isolated: a stale token or unsupported account does not cancel other
    stores. Only official moderation detections are returned; no page DOM, paging
    buttons or browser profile configuration participates in this path.
    """

    normalized_targets = []
    seen_token_ids: set[int] = set()
    for raw_target in targets or ():
        target = dict(raw_target or {})
        try:
            token_id = int(target.get("token_id") or target.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if token_id <= 0 or token_id in seen_token_ids:
            continue
        seen_token_ids.add(token_id)
        target["token_id"] = token_id
        normalized_targets.append(target)
    if not normalized_targets:
        return {
            "data": [],
            "results": [],
            "failed_stores": [],
            "source": "mercado_moderations_api",
        }

    records_by_id: dict[int, dict] = {}
    record_load_errors: dict[int, str] = {}
    try:
        records_by_id = {
            int(record.get("id") or 0): record
            for record in _token_records(seen_token_ids)
        }
    except Exception:
        # A single missing/disabled token must not cancel the other stores.
        for token_id in seen_token_ids:
            try:
                records = _token_records([token_id])
                if records:
                    records_by_id[token_id] = records[0]
            except Exception as exc:
                record_load_errors[token_id] = str(exc)
    recent_days = max(1, int(recent_days or 1))
    date_created_since = (
        datetime.now() - timedelta(days=recent_days)
    ).strftime("%Y-%m-%d")
    worker_count = max(
        1,
        min(int(max_workers or 1), len(normalized_targets), 8),
    )
    print(
        f"{_now_text()} 侵权列表改用 Mercado Moderations API，"
        f"{worker_count} 线程并发读取 {len(normalized_targets)} 家店铺"
    )
    results = []

    def collect_one(target: dict) -> dict:
        token_id = int(target["token_id"])
        record = records_by_id.get(token_id)
        if not record:
            return {
                "store": str(target.get("name") or token_id),
                "token_id": token_id,
                "status": "error",
                "rows": [],
                "scanned": 0,
                "message": record_load_errors.get(token_id) or "店铺 Token 记录不存在",
            }
        try:
            return _collect_live_detection_target(
                record,
                target,
                date_created_since=date_created_since,
                stop_event=stop_event,
            )
        except Exception as exc:
            return {
                "store": str(
                    target.get("name")
                    or record.get("display_name")
                    or token_id
                ),
                "token_id": token_id,
                "status": "error",
                "rows": [],
                "scanned": 0,
                "message": str(exc),
            }

    executor = ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="meli-live-infractions",
    )
    future_map = {
        executor.submit(collect_one, target): target
        for target in normalized_targets
    }
    try:
        for future in as_completed(future_map):
            result = future.result()
            results.append(result)
            print(
                f"{_now_text()} {result['store']} API侵权读取"
                f"{result['status']}：命中 {len(result.get('rows') or [])} 条，"
                f"扫描 {result.get('scanned') or 0} 条"
                + (f"；{result['message']}" if result.get("message") else "")
            )
            if _stop_requested(stop_event):
                for pending in future_map:
                    if not pending.done():
                        pending.cancel()
                break
    finally:
        executor.shutdown(wait=not _stop_requested(stop_event), cancel_futures=True)

    rows = [
        row
        for result in results
        for row in result.get("rows") or ()
    ]
    failures = [
        result
        for result in results
        if result.get("status") in {"error", "partial"}
    ]
    return {
        "data": rows,
        "results": results,
        "failed_stores": failures,
        "source": "mercado_moderations_api",
        "recent_days": recent_days,
    }


def seed_legacy_infraction_snapshot() -> dict[str, int]:
    """Seed the independent dashboard once from the last browser snapshot."""

    if count_infraction_records() > 0:
        return {"seeded": 0, "skipped": 1}
    latest = bit_mysql.get_latest_infraction_info(3650)
    legacy_rows = list(latest.get("rows") or [])
    summaries = (bit_mysql.list_mercado_store_tokens() or {}).get("rows") or []
    tokens_by_name = {
        str(row.get("display_name") or "").strip(): row
        for row in summaries
        if str(row.get("display_name") or "").strip()
    }
    grouped: dict[int, tuple[dict, list[dict]]] = {}
    for source in legacy_rows:
        store_name = str(source.get("店铺名") or "").strip()
        token_summary = tokens_by_name.get(store_name)
        if not token_summary:
            continue
        site_name = str(source.get("站点") or "").strip()
        setting = next(
            (
                row
                for row in token_summary.get("site_settings") or []
                if str(row.get("site_name") or "").strip() == site_name
            ),
            {},
        )
        item_id = str(source.get("编号") or "").strip().upper()
        if not item_id:
            continue
        source_type = (
            "rights_holder"
            if str(source.get("类型") or "").strip() == "权利人"
            else "detection"
        )
        occurred_at = _mysql_datetime(source.get("侵权时间"))
        identity = f"{token_summary.get('id')}|{source_type}|{item_id}|{occurred_at}"
        record = {
            "source_type": source_type,
            "source_id": f"legacy:{hashlib.sha1(identity.encode('utf-8')).hexdigest()}",
            "seller_id": str(token_summary.get("meli_user_id") or ""),
            "site_id": str(setting.get("site_id") or item_id[:3]).upper(),
            "item_id": item_id,
            "title": str(source.get("标题") or ""),
            "occurred_at": occurred_at,
            "status": "历史记录",
            "reason_code": "LEGACY_PAGE",
            "reason": "历史页面采集记录（首次迁移）",
            "salesperson": str(setting.get("salesperson") or ""),
            "group_name": str(setting.get("group_name") or ""),
            "raw_json": {"legacy_snapshot": source},
        }
        token_id = int(token_summary["id"])
        if token_id not in grouped:
            secret_record = bit_mysql.get_mercado_store_token(token_id)
            grouped[token_id] = (
                {
                    **dict(secret_record or token_summary),
                    "site_settings": list(token_summary.get("site_settings") or []),
                },
                [],
            )
        grouped[token_id][1].append(record)
    seeded = 0
    for token, records in grouped.values():
        seeded += upsert_infraction_records(token, records)
    return {"seeded": seeded, "skipped": 0}


def backfill_infraction_images(
    *,
    days: int = 365,
    limit: int | None = None,
) -> dict[str, int]:
    missing = list_missing_infraction_media(
        days=days,
        limit=limit or INFRACTION_IMAGE_BACKFILL_LIMIT,
    )
    if not missing:
        return {"requested": 0, "updated": 0, "failed_stores": 0}

    items_by_token: dict[int, list[str]] = {}
    for row in missing:
        token_id = int(row.get("token_id") or 0)
        item_id = str(row.get("item_id") or "").strip().upper()
        if token_id > 0 and item_id:
            items_by_token.setdefault(token_id, []).append(item_id)
    token_records = {
        int(row["id"]): row
        for row in _token_records(items_by_token)
        if int(row.get("id") or 0) in items_by_token
    }

    def backfill_store(token_id: int) -> tuple[int, bool]:
        record = token_records.get(token_id)
        if not record:
            return 0, True
        try:
            client, _record = _client_and_token(record)
            details = _item_details(client, items_by_token[token_id])
            media = []
            for item_id, detail in details.items():
                thumbnail_url = _thumbnail_url(detail)
                if not thumbnail_url:
                    continue
                media.append(
                    {
                        "token_id": token_id,
                        "item_id": item_id,
                        "thumbnail_url": thumbnail_url,
                        "permalink": _http_url(detail.get("permalink")),
                        "title": str(detail.get("title") or ""),
                    }
                )
            return update_infraction_media(media), False
        except Exception:
            return 0, True

    updated = 0
    failed_stores = 0
    worker_count = max(1, min(INFRACTION_STORE_WORKERS, len(items_by_token)))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="meli-infraction-images",
    ) as executor:
        futures = [
            executor.submit(backfill_store, token_id)
            for token_id in items_by_token
        ]
        for future in as_completed(futures):
            count, failed = future.result()
            updated += count
            failed_stores += int(failed)
    return {
        "requested": len(missing),
        "updated": updated,
        "failed_stores": failed_stores,
    }


def run_official_infraction_sync(
    token_ids: Iterable[Any] | None = None,
) -> dict[str, Any]:
    seed_result = seed_legacy_infraction_snapshot()
    if seed_result.get("seeded"):
        _append_log(f"已迁移 {seed_result['seeded']} 条历史页面侵权记录")
    records = _token_records(token_ids)
    _state_update(
        running=True,
        status="running",
        message="正在同步官方侵权数据",
        total_stores=len(records),
        processed_stores=0,
        active_stores=[],
        detection_scanned_count=0,
        detection_matched_count=0,
        rights_holder_count=0,
        failed_count=0,
        started_at=_now_text(),
        finished_at="",
        results=[],
        logs=list(_sync_state.get("logs") or []),
    )
    worker_count = max(1, min(INFRACTION_STORE_WORKERS, len(records)))
    _append_log(
        f"任务启动，共 {len(records)} 家店铺；{worker_count} 家并行，每 12 小时自动刷新"
    )
    results = []

    def sync_one(record: dict) -> dict:
        store_name = str(record.get("display_name") or record.get("nickname") or record.get("id"))
        token_id = int(record["id"])
        _set_store_active(store_name, True)
        mark_infraction_sync_started(token_id)
        try:
            result = _sync_store(record)
            mark_infraction_sync_finished(
                token_id,
                result["status"],
                detection_scanned_count=result["detection_scanned"],
                detection_matched_count=result["detection_matched"],
                rights_holder_count=result["rights_holder"],
                error=result.get("message") or "",
            )
            _state_increment(
                detection_scanned_count=result["detection_scanned"],
                detection_matched_count=result["detection_matched"],
                rights_holder_count=result["rights_holder"],
            )
            _append_log(
                f"{store_name} 完成：平台命中 {result['detection_matched']}，"
                f"权利人 {result['rights_holder']}"
                + (f"；{result['message']}" if result.get("message") else "")
            )
            return result
        except Exception as exc:
            try:
                mark_infraction_sync_finished(token_id, "error", error=str(exc))
            except Exception as state_exc:
                _append_log(f"{store_name} 同步状态写入失败：{state_exc}")
            _append_log(f"{store_name} 失败：{exc}")
            return {
                "store": store_name,
                "token_id": token_id,
                "status": "error",
                "message": str(exc),
                "detection_scanned": 0,
                "detection_matched": 0,
                "rights_holder": 0,
            }
        finally:
            _set_store_active(store_name, False)

    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="meli-official-infractions",
    ) as executor:
        futures = [executor.submit(sync_one, record) for record in records]
        for future in as_completed(futures):
            results.append(future.result())
            _state_update(
                processed_stores=len(results),
                failed_count=sum(
                    1 for row in results if row.get("status") in {"error", "partial"}
                ),
                results=list(results),
            )

    problem_count = sum(
        1 for row in results if row.get("status") in {"error", "partial"}
    )
    message = (
        f"同步完成：平台侵权 {_sync_state.get('detection_matched_count', 0)} 条，"
        f"权利人举报 {_sync_state.get('rights_holder_count', 0)} 条"
    )
    if problem_count:
        message += f"；{problem_count} 家需重试"
    try:
        image_result = backfill_infraction_images(days=365)
        if image_result.get("requested"):
            _append_log(
                f"产品图补全：请求 {image_result['requested']}，"
                f"更新 {image_result['updated']}"
            )
    except Exception as exc:
        _append_log(f"产品图补全失败：{exc}")
    _state_update(
        running=False,
        status="completed" if problem_count == 0 else "partial",
        message=message,
        active_stores=[],
        finished_at=_now_text(),
        results=list(results),
    )
    _append_log(message)
    return official_infraction_sync_status()


def _run_background(token_ids: list[int]) -> None:
    task_lock = InterProcessLock(
        INFRACTION_SYNC_LOCK_KEY,
        owner="mercado_infraction_sync",
        metadata={"task_id": _sync_state.get("task_id")},
    )
    if not task_lock.acquire(timeout=0):
        _state_update(
            running=False,
            status="busy",
            message="官方侵权数据正在其他进程同步",
            finished_at=_now_text(),
        )
        return
    try:
        run_official_infraction_sync(token_ids)
    except Exception as exc:
        _state_update(
            running=False,
            status="error",
            message=str(exc),
            active_stores=[],
            finished_at=_now_text(),
        )
        _append_log(f"任务失败：{exc}")
    finally:
        task_lock.release()


def start_official_infraction_sync(
    token_ids: Iterable[Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    selected_ids = _token_ids(token_ids or ())
    if selected_ids:
        _token_records(selected_ids)
    with _state_guard:
        if _sync_state.get("running"):
            return False, official_infraction_sync_status()
    if get_lock_owner(INFRACTION_SYNC_LOCK_KEY):
        return False, official_infraction_sync_status()
    queued_ids = selected_ids or _token_ids(
        row.get("id")
        for row in ((bit_mysql.list_mercado_store_tokens() or {}).get("rows") or [])
        if bool(row.get("enabled", True))
    )
    if queued_ids:
        request_infraction_sync(queued_ids)
    with _state_guard:
        _sync_state.update(
            running=True,
            task_id=uuid.uuid4().hex,
            status="starting",
            message="正在启动官方侵权同步",
            total_stores=0,
            processed_stores=0,
            active_stores=[],
            detection_scanned_count=0,
            detection_matched_count=0,
            rights_holder_count=0,
            failed_count=0,
            started_at=_now_text(),
            finished_at="",
            results=[],
            logs=[],
        )
    thread = threading.Thread(
        target=_run_background,
        args=(selected_ids,),
        name="mercado-official-infraction-sync",
        daemon=True,
    )
    thread.start()
    return True, official_infraction_sync_status()


def start_due_official_infraction_sync() -> dict[str, Any]:
    if get_lock_owner(INFRACTION_SYNC_LOCK_KEY):
        return {
            "started": False,
            "due_token_ids": [],
            "state": official_infraction_sync_status(),
        }
    token_ids = list_due_infraction_token_ids(
        interval_hours=INFRACTION_AUTO_SYNC_HOURS,
        retry_minutes=INFRACTION_AUTO_RETRY_MINUTES,
    )
    if not token_ids:
        return {
            "started": False,
            "due_token_ids": [],
            "state": official_infraction_sync_status(),
        }
    started, state = start_official_infraction_sync(token_ids)
    return {"started": bool(started), "due_token_ids": token_ids, "state": state}


def _auto_sync_loop() -> None:
    try:
        seed_legacy_infraction_snapshot()
    except Exception as exc:
        _append_log(f"历史侵权数据迁移失败：{exc}")
    while True:
        try:
            start_due_official_infraction_sync()
        except Exception as exc:
            _append_log(f"自动同步检查失败：{exc}")
        threading.Event().wait(INFRACTION_AUTO_CHECK_SECONDS)


def start_official_infraction_auto_scheduler() -> bool:
    global _scheduler_thread
    with _scheduler_guard:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return False
        _scheduler_thread = threading.Thread(
            target=_auto_sync_loop,
            name="mercado-official-infraction-auto-sync",
            daemon=True,
        )
        _scheduler_thread.start()
        return True


__all__ = [
    "BRAND_PROTECTION_SUBGROUP",
    "INFRACTION_AUTO_SYNC_HOURS",
    "backfill_infraction_images",
    "collect_live_detection_infractions",
    "is_brand_protection_detection",
    "official_infraction_sync_status",
    "run_official_infraction_sync",
    "seed_legacy_infraction_snapshot",
    "start_due_official_infraction_sync",
    "start_official_infraction_auto_scheduler",
    "start_official_infraction_sync",
]
