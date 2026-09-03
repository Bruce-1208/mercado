"""Synchronize current prohibited listings through Mercado Libre's official API."""

from __future__ import annotations

import html
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from bit import bit_mysql, mercado_tokens
from bit.bit_runtime_lock import InterProcessLock, get_lock_owner
from erp.mercadolibre_prohibited_store import (
    get_prohibited_sync_context,
    list_due_prohibited_token_ids,
    mark_prohibited_sync_finished,
    mark_prohibited_sync_started,
    replace_prohibited_snapshot,
    request_prohibited_sync,
)
from mercado_api.client import MercadoAPIError, MercadoLibreClient


PROHIBITED_REASON = "The product is prohibited."
PROHIBITED_SYNC_LOCK_KEY = "mercado_prohibited_listing_sync_task"
PROHIBITED_AUTO_SYNC_HOURS = max(
    1, int(os.getenv("MERCADO_PROHIBITED_AUTO_SYNC_HOURS", "24"))
)
PROHIBITED_AUTO_RETRY_MINUTES = max(
    1, int(os.getenv("MERCADO_PROHIBITED_AUTO_RETRY_MINUTES", "60"))
)
PROHIBITED_AUTO_CHECK_SECONDS = max(
    60, int(os.getenv("MERCADO_PROHIBITED_AUTO_CHECK_SECONDS", "300"))
)
PROHIBITED_STORE_WORKERS = max(
    1, int(os.getenv("MERCADO_PROHIBITED_STORE_WORKERS", "2"))
)
PROHIBITED_DETAIL_WORKERS = max(
    1, int(os.getenv("MERCADO_PROHIBITED_DETAIL_WORKERS", "6"))
)
PROHIBITED_PAGE_SIZE = 20
PROHIBITED_DETAIL_ATTRIBUTES = (
    "id", "site_id", "title", "permalink", "secure_thumbnail", "thumbnail",
    "pictures", "status", "sub_status", "seller_id", "user_product_id",
    "family_id", "cbt_item_id",
)

_state_guard = threading.RLock()
_scheduler_guard = threading.Lock()
_scheduler_thread: threading.Thread | None = None
_sync_state = {
    "running": False,
    "task_id": "",
    "status": "idle",
    "message": "等待同步禁限售列表",
    "total_stores": 0,
    "processed_stores": 0,
    "current_store": "",
    "active_stores": [],
    "scanned_count": 0,
    "reason_matched_count": 0,
    "prohibited_count": 0,
    "detail_failed_count": 0,
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
        _sync_state["current_store"] = "、".join(names[:PROHIBITED_STORE_WORKERS])


def prohibited_listing_sync_status() -> dict[str, Any]:
    with _state_guard:
        state = dict(_sync_state)
        state["results"] = [dict(row) for row in state.get("results") or []]
        state["logs"] = list(state.get("logs") or [])
    owner = get_lock_owner(PROHIBITED_SYNC_LOCK_KEY)
    if owner and not state.get("running"):
        state.update(
            running=True,
            status="running",
            message="禁限售列表正在其他进程同步",
            lock_owner=owner,
        )
    state.update(
        auto_sync_enabled=True,
        auto_sync_hours=PROHIBITED_AUTO_SYNC_HOURS,
        store_workers=PROHIBITED_STORE_WORKERS,
        detail_workers_per_store=PROHIBITED_DETAIL_WORKERS,
        target_reason=PROHIBITED_REASON,
        source="Mercado Libre official Moderations API",
    )
    return state


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
            enriched = dict(record)
            enriched["site_settings"] = list(summary.get("site_settings") or [])
            records.append(enriched)
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


def _client_and_token(record: dict) -> tuple[MercadoLibreClient, dict]:
    if _token_expiring(record) and record.get("refresh_token"):
        record = {**_refresh_token(int(record["id"])), "site_settings": record.get("site_settings") or []}
    token = str(record.get("access_token") or "").strip()
    if not token:
        raise ValueError("店铺缺少 Access Token，请重新授权")
    return MercadoLibreClient(token), record


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
    return accounts or [{
        "user_id": str(root_seller_id),
        "site_id": str(profile.get("site_id") or "CBT").strip().upper(),
    }]


def _infraction_rows(page: Mapping[str, Any]) -> list[dict]:
    for key in ("infractions", "results", "elements"):
        value = page.get(key)
        if isinstance(value, list):
            return [dict(row) for row in value if isinstance(row, Mapping)]
    return []


def _paging_total(page: Mapping[str, Any], fallback: int) -> int:
    paging = page.get("paging") if isinstance(page.get("paging"), Mapping) else {}
    for value in (paging.get("total"), page.get("total")):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            pass
    return fallback


def _iter_infractions(
    client: MercadoLibreClient,
    seller_id: str,
    *,
    date_created_since: str = "",
):
    offset = 0
    seen_ids: set[str] = set()
    while True:
        params = {
            "element_type": "ITM",
            "limit": PROHIBITED_PAGE_SIZE,
            "offset": offset,
            "sort": "date_created_desc",
        }
        if date_created_since:
            params["date_created_since"] = date_created_since
        page = client.request(
            "GET",
            f"/marketplace/moderations/infractions/{seller_id}",
            params=params,
        )
        rows = _infraction_rows(page)
        for row in rows:
            infraction_id = str(row.get("id") or "")
            dedupe_key = infraction_id or f"{row.get('related_item_id')}:{row.get('date_created')}"
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            yield row
        offset += len(rows)
        total = _paging_total(page, offset)
        if not rows or offset >= total or len(rows) < PROHIBITED_PAGE_SIZE:
            break


def _plain_text(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _mysql_datetime(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text[:19].replace("T", " ") if len(text) >= 19 else None


def _thumbnail(detail: Mapping[str, Any]) -> str:
    pictures = detail.get("pictures") or []
    if pictures and isinstance(pictures[0], Mapping):
        return str(pictures[0].get("secure_url") or pictures[0].get("url") or "")
    return str(detail.get("secure_thumbnail") or detail.get("thumbnail") or "")


def _item_id(infraction: Mapping[str, Any]) -> str:
    return str(
        infraction.get("related_item_id")
        or infraction.get("element_id")
        or infraction.get("item_id")
        or ""
    ).strip().upper()


def _is_current_prohibited(detail: Mapping[str, Any]) -> bool:
    status = str(detail.get("status") or "").strip().lower()
    sub_status = detail.get("sub_status") or []
    if isinstance(sub_status, str):
        sub_status = [part.strip() for part in sub_status.split(",")]
    return status == "under_review" and "forbidden" in {
        str(value or "").strip().lower() for value in sub_status
    }


def _site_setting_map(record: Mapping[str, Any]) -> dict[str, dict]:
    return {
        str(row.get("site_id") or "").strip().upper(): dict(row)
        for row in record.get("site_settings") or []
        if str(row.get("site_id") or "").strip()
    }


def _enrich_candidates(
    client: MercadoLibreClient,
    candidates: list[tuple[dict, dict]],
    settings: Mapping[str, dict],
) -> tuple[list[dict], int]:
    if not candidates:
        return [], 0
    details: dict[str, dict] = {}
    failures = 0
    unauthorized_error: MercadoAPIError | None = None
    with ThreadPoolExecutor(max_workers=min(PROHIBITED_DETAIL_WORKERS, len(candidates))) as executor:
        futures = {
            executor.submit(
                client.get_marketplace_item,
                _item_id(infraction),
                attributes=PROHIBITED_DETAIL_ATTRIBUTES,
            ): _item_id(infraction)
            for infraction, _account in candidates
        }
        for future in as_completed(futures):
            item_id = futures[future]
            try:
                detail = future.result()
                if isinstance(detail, Mapping):
                    details[item_id] = dict(detail)
                else:
                    failures += 1
            except MercadoAPIError as exc:
                if _is_unauthorized_error(exc):
                    unauthorized_error = exc
                else:
                    failures += 1
            except Exception:
                failures += 1
    if unauthorized_error is not None:
        raise unauthorized_error

    current = []
    for infraction, account in candidates:
        item_id = _item_id(infraction)
        detail = details.get(item_id) or {}
        if not _is_current_prohibited(detail):
            continue
        site_id = str(detail.get("site_id") or account.get("site_id") or item_id[:3]).upper()
        setting = settings.get(site_id) or {}
        sub_status = detail.get("sub_status") or []
        if isinstance(sub_status, str):
            sub_status = [sub_status]
        current.append({
            "item_id": item_id,
            "global_item_id": detail.get("user_product_id") or detail.get("cbt_item_id") or "",
            "family_id": detail.get("family_id") or "",
            "seller_id": str(detail.get("seller_id") or account.get("user_id") or ""),
            "site_id": site_id,
            "salesperson": setting.get("salesperson") or "",
            "group_name": setting.get("group_name") or "",
            "title": detail.get("title") or "",
            "permalink": detail.get("permalink") or "",
            "thumbnail_url": _thumbnail(detail),
            "status": detail.get("status") or "",
            "sub_status": ",".join(str(value) for value in sub_status if value),
            "infraction_id": infraction.get("id") or "",
            "infraction_reason": _plain_text(infraction.get("reason")) or PROHIBITED_REASON,
            "remedy": _plain_text(infraction.get("remedy")),
            "infraction_date": _mysql_datetime(infraction.get("date_created")),
            "raw_json": {"infraction": infraction, "item": detail},
        })
    return current, failures


def _sync_store(record: dict) -> dict:
    token_id = int(record["id"])
    store_name = str(record.get("display_name") or record.get("nickname") or token_id)
    root_seller_id = str(record.get("meli_user_id") or "").strip()
    if not root_seller_id:
        raise ValueError("店铺授权缺少 Seller ID，请刷新 Token 或重新授权")
    client, record = _client_and_token(record)
    refreshed_after_unauthorized = False
    while True:
        try:
            accounts = _marketplace_accounts(client, root_seller_id)
            candidates: list[tuple[dict, dict]] = []
            scanned = 0
            seen_items: set[str] = set()
            context = get_prohibited_sync_context(token_id)
            last_completed_at = context.get("last_completed_at")
            date_created_since = ""
            if last_completed_at:
                try:
                    last_completed = datetime.fromisoformat(str(last_completed_at))
                    date_created_since = (
                        last_completed - timedelta(days=1)
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    date_created_since = ""
            for previous in context.get("rows") or []:
                item_id = str(previous.get("item_id") or "").strip().upper()
                if not item_id:
                    continue
                seen_items.add(item_id)
                candidates.append(({
                    "id": previous.get("infraction_id") or "",
                    "related_item_id": item_id,
                    "reason": previous.get("infraction_reason") or PROHIBITED_REASON,
                    "remedy": previous.get("remedy") or "",
                    "date_created": previous.get("infraction_date") or "",
                }, {
                    "user_id": previous.get("seller_id") or root_seller_id,
                    "site_id": previous.get("site_id") or item_id[:3],
                }))
            for account in accounts:
                mode_label = f"自 {date_created_since} 增量" if date_created_since else "首次全量"
                _append_log(f"{store_name} 开始扫描 {account['site_id']} 官方处罚记录（{mode_label}）")
                for infraction in _iter_infractions(
                    client,
                    account["user_id"],
                    date_created_since=date_created_since,
                ):
                    scanned += 1
                    if scanned % 200 == 0:
                        _state_update(message=f"正在扫描 {store_name}：已读取 {scanned} 条处罚记录")
                    reason = _plain_text(infraction.get("reason"))
                    item_id = _item_id(infraction)
                    if reason.casefold() != PROHIBITED_REASON.casefold() or not item_id:
                        continue
                    if item_id in seen_items:
                        continue
                    seen_items.add(item_id)
                    candidates.append((infraction, account))
            current, detail_failures = _enrich_candidates(
                client, candidates, _site_setting_map(record)
            )
            replace_prohibited_snapshot(
                record,
                current,
                sync_marker=uuid.uuid4().hex,
                checked_at=_now_text(),
                finalize=detail_failures == 0,
            )
            _state_increment(
                scanned_count=scanned,
                reason_matched_count=len(candidates),
                prohibited_count=len(current),
                detail_failed_count=detail_failures,
            )
            return {
                "store": store_name,
                "token_id": token_id,
                "status": "partial" if detail_failures else "success",
                "scanned": scanned,
                "reason_matched": len(candidates),
                "prohibited": len(current),
                "detail_failed": detail_failures,
            }
        except MercadoAPIError as exc:
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
            _append_log(f"{store_name} Token 已刷新，继续同步")


def run_prohibited_listing_sync(token_ids: Iterable[Any] | None = None) -> dict[str, Any]:
    records = _token_records(token_ids)
    _state_update(
        running=True, status="running", message="正在同步禁限售列表",
        total_stores=len(records), processed_stores=0, current_store="", active_stores=[],
        scanned_count=0, reason_matched_count=0, prohibited_count=0,
        detail_failed_count=0, failed_count=0, started_at=_now_text(), finished_at="",
        results=[], logs=[],
    )
    worker_count = max(1, min(PROHIBITED_STORE_WORKERS, len(records)))
    _append_log(
        f"任务启动，共 {len(records)} 家店铺；使用官方 Moderations API，"
        f"{worker_count} 家店铺并行"
    )
    results = []

    def sync_one(record: dict) -> dict:
        store_name = str(record.get("display_name") or record.get("nickname") or record.get("id"))
        token_id = int(record["id"])
        _set_store_active(store_name, True)
        mark_prohibited_sync_started(token_id)
        try:
            result = _sync_store(record)
            mark_prohibited_sync_finished(
                token_id,
                result["status"],
                scanned_count=result["scanned"],
                prohibited_count=result["prohibited"],
            )
            _append_log(
                f"{store_name} 完成：扫描 {result['scanned']}，命中原因 "
                f"{result['reason_matched']}，当前禁售 {result['prohibited']}"
            )
            return result
        except Exception as exc:
            try:
                mark_prohibited_sync_finished(token_id, "error", error=str(exc))
            except Exception as state_exc:
                _append_log(f"{store_name} 同步状态写入失败：{state_exc}")
            _append_log(f"{store_name} 失败：{exc}")
            return {"store": store_name, "token_id": token_id, "status": "error", "message": str(exc)}
        finally:
            _set_store_active(store_name, False)

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="meli-prohibited") as executor:
        futures = [executor.submit(sync_one, record) for record in records]
        for future in as_completed(futures):
            results.append(future.result())
            _state_update(
                processed_stores=len(results),
                failed_count=sum(1 for row in results if row.get("status") == "error"),
                results=list(results),
            )
    failed = sum(1 for row in results if row.get("status") == "error")
    message = (
        f"同步完成：当前禁售 {_sync_state.get('prohibited_count', 0)} 条"
        if failed == 0 else f"同步完成，{failed} 家店铺失败"
    )
    _state_update(
        running=False, status="completed" if failed == 0 else "partial",
        message=message, current_store="", active_stores=[], finished_at=_now_text(),
        results=list(results),
    )
    _append_log(message)
    return prohibited_listing_sync_status()


def _run_background(token_ids: list[int]) -> None:
    task_lock = InterProcessLock(
        PROHIBITED_SYNC_LOCK_KEY,
        owner="bit_prohibited_listing_sync",
        metadata={"task_id": _sync_state.get("task_id")},
    )
    if not task_lock.acquire(timeout=0):
        _state_update(
            running=False, status="busy", message="禁限售列表正在其他进程同步",
            finished_at=_now_text(),
        )
        return
    try:
        run_prohibited_listing_sync(token_ids)
    except Exception as exc:
        _state_update(
            running=False, status="error", message=str(exc), current_store="",
            active_stores=[], finished_at=_now_text(),
        )
        _append_log(f"任务失败：{exc}")
    finally:
        task_lock.release()


def start_prohibited_listing_sync(
    token_ids: Iterable[Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    selected_ids = _token_ids(token_ids or ())
    if selected_ids:
        _token_records(selected_ids)
    with _state_guard:
        if _sync_state.get("running"):
            return False, prohibited_listing_sync_status()
    if get_lock_owner(PROHIBITED_SYNC_LOCK_KEY):
        return False, prohibited_listing_sync_status()
    queued_ids = selected_ids or _token_ids(
        row.get("id")
        for row in ((bit_mysql.list_mercado_store_tokens() or {}).get("rows") or [])
        if bool(row.get("enabled", True))
    )
    if queued_ids:
        request_prohibited_sync(queued_ids)
    with _state_guard:
        _sync_state.update(
            running=True, task_id=uuid.uuid4().hex, status="starting",
            message="正在启动禁限售同步", total_stores=0, processed_stores=0,
            current_store="", active_stores=[], scanned_count=0,
            reason_matched_count=0, prohibited_count=0, detail_failed_count=0,
            failed_count=0, started_at=_now_text(), finished_at="", results=[], logs=[],
        )
    thread = threading.Thread(
        target=_run_background,
        args=(selected_ids,),
        name="mercado-prohibited-sync",
        daemon=True,
    )
    thread.start()
    return True, prohibited_listing_sync_status()


def start_due_prohibited_listing_sync() -> dict[str, Any]:
    if get_lock_owner(PROHIBITED_SYNC_LOCK_KEY):
        return {
            "started": False,
            "due_token_ids": [],
            "state": prohibited_listing_sync_status(),
        }
    token_ids = list_due_prohibited_token_ids(
        interval_hours=PROHIBITED_AUTO_SYNC_HOURS,
        retry_minutes=PROHIBITED_AUTO_RETRY_MINUTES,
    )
    if not token_ids:
        return {"started": False, "due_token_ids": [], "state": prohibited_listing_sync_status()}
    started, state = start_prohibited_listing_sync(token_ids)
    return {"started": bool(started), "due_token_ids": token_ids, "state": state}


def _auto_sync_loop() -> None:
    while True:
        try:
            start_due_prohibited_listing_sync()
        except Exception as exc:
            _append_log(f"自动同步检查失败：{exc}")
        threading.Event().wait(PROHIBITED_AUTO_CHECK_SECONDS)


def start_prohibited_listing_auto_scheduler() -> bool:
    global _scheduler_thread
    with _scheduler_guard:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return False
        _scheduler_thread = threading.Thread(
            target=_auto_sync_loop,
            name="mercado-prohibited-auto-sync",
            daemon=True,
        )
        _scheduler_thread.start()
        return True


__all__ = [
    "PROHIBITED_AUTO_SYNC_HOURS", "PROHIBITED_REASON",
    "prohibited_listing_sync_status", "run_prohibited_listing_sync",
    "start_due_prohibited_listing_sync", "start_prohibited_listing_auto_scheduler",
    "start_prohibited_listing_sync",
]
