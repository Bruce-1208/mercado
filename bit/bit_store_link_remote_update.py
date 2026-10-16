"""Push store-link edits to Mercado Libre Global Selling listings."""

from __future__ import annotations

from erp.store_link_audit import audited, contextual_target, record
from collections import Counter
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from bit import bit_mysql
from bit.bit_runtime_lock import InterProcessLock
from bit.store_link_item_lock import store_link_item_locks
from bit.bit_store_link_sync import (
    STORE_LINK_DETAIL_ATTRIBUTES,
    SITE_ITEM_URLS,
    _client_and_token,
)
from erp.mercadolibre_collection_store import (
    sync_pulled_product_fields_from_store_links,
)
from erp.mercadolibre_store_link_store import (
    bulk_update_store_links,
    get_store_links_by_ids,
    listing_record,
    mark_store_links_deleted,
)
from mercado_api.client import MercadoLibreClient


STORE_LINK_REMOTE_UPDATE_WORKERS = max(
    1, min(32, int(os.getenv("MERCADO_STORE_LINK_REMOTE_UPDATE_WORKERS", "16")))
)
STORE_LINK_REMOTE_UPDATE_FIELDS = (
    "price",
    "weight_g",
    "package_length_cm",
    "package_width_cm",
    "package_height_cm",
    "net_proceeds_usd",
)
STORE_LINK_REMOTE_UPDATE_STATUSES = frozenset(("active", "paused"))
PACKAGE_FIELDS = (
    ("weight_g", "PACKAGE_WEIGHT", "g"),
    ("package_length_cm", "PACKAGE_LENGTH", "cm"),
    ("package_width_cm", "PACKAGE_WIDTH", "cm"),
    ("package_height_cm", "PACKAGE_HEIGHT", "cm"),
)
REMOTE_DELETE_VERIFY_DELAYS = (0, 0.25, 0.75, 1.5)


def _is_site_listing_id(value: object, site_id: object = "") -> bool:
    """Accept only a known Mercado site listing, never a CBT parent ID."""
    item_id = str(value or "").strip().upper()
    match = re.fullmatch(r"([A-Z]{3})\d+", item_id)
    if not match or match.group(1) not in SITE_ITEM_URLS:
        return False
    expected = str(site_id or "").strip().upper()
    return not expected or expected == match.group(1)


def _site_listing_retention_reason(value: object) -> str:
    item_id = str(value or "").strip().upper()
    return ("CBT 父商品不支持直接删除，已安全保留" if item_id.startswith("CBT") else
            "站点商品编号与站点不匹配或站点不受支持，已安全保留")

_state_guard = threading.RLock()
_recovery_guard = threading.Lock()
_recovery_thread: threading.Thread | None = None
_recovery_wakeup = threading.Event()
REMOTE_UPDATE_TASK_LOCK_KEY = "mercado_store_link_remote_update_task"
REMOTE_UPDATE_RECOVERY_LOCK_KEY = "mercado_store_link_remote_update_recovery"
_PENDING_STATUSES = ("queued", "starting", "running")
_SAVED_ROW_FIELDS = (
    "id", "token_id", "store_name", "item_id", "site_id", "status",
    "is_current", "currency_id", "net_ratio_base_usd", *STORE_LINK_REMOTE_UPDATE_FIELDS,
)
_update_state = {
    "running": False,
    "task_id": "",
    "status": "idle",
    "message": "等待修改美客多后台链接",
    "total_links": 0,
    "processed_links": 0,
    "success_count": 0,
    "retained_count": 0,
    "partial_count": 0,
    "failed_count": 0,
    "current_item": "",
    "started_at": "",
    "finished_at": "",
    "changes": {},
    "results": [],
    "logs": [],
}


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _state_update(**changes: Any) -> None:
    with _state_guard:
        _update_state.update(changes)


def _append_log(message: str) -> None:
    line = f"{_now_text()} {str(message or '').strip()}"
    with _state_guard:
        logs = list(_update_state.get("logs") or [])
        logs.append(line)
        _update_state["logs"] = logs[-300:]


def _state_snapshot() -> dict[str, Any]:
    with _state_guard:
        state = dict(_update_state)
        state["changes"] = dict(state.get("changes") or {})
        state["results"] = [dict(row) for row in state.get("results") or []]
        state["logs"] = list(state.get("logs") or [])
    return state


def _queue_path() -> Path:
    return Path(os.environ.get("MERCADO_STORE_LINK_REMOTE_UPDATE_PATH") or
                Path(__file__).resolve().parents[1] / "runtime_logs" / "store-link-remote-update.sqlite3")


@contextmanager
def _queue_connection():
    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path, timeout=30)
    database.row_factory = sqlite3.Row
    try:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, created REAL NOT NULL,
            changes_json TEXT NOT NULL, state_json TEXT NOT NULL
        )""")
        database.execute("CREATE INDEX IF NOT EXISTS remote_update_pending ON tasks(status,created)")
        database.execute("""CREATE TABLE IF NOT EXISTS items (
            task_id TEXT NOT NULL, link_id INTEGER NOT NULL,
            row_json TEXT NOT NULL, result_json TEXT,
            PRIMARY KEY(task_id,link_id)
        )""")
        with database:
            yield database
    finally:
        database.close()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def store_link_remote_update_status() -> dict[str, Any]:
    state = _state_snapshot()
    pending_count = 0
    recent_batches = []
    if _queue_path().exists():
        with _queue_connection() as database:
            database.execute("BEGIN")
            pending_count = database.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','starting','running')"
            ).fetchone()[0]
            row = database.execute(
                "SELECT state_json FROM tasks WHERE status IN ('queued','starting','running') "
                "ORDER BY created,rowid LIMIT 1"
            ).fetchone()
            if not row:
                row = database.execute("SELECT state_json FROM tasks ORDER BY created DESC,rowid DESC LIMIT 1").fetchone()
            for finished in database.execute(
                "SELECT state_json FROM tasks WHERE status NOT IN ('queued','starting','running') "
                "ORDER BY created DESC,rowid DESC LIMIT 5"
            ):
                saved = json.loads(finished["state_json"])
                recent_batches.append({key: saved.get(key) for key in (
                    "task_id", "message", "status", "finished_at",
                )})
        if row:
            state = json.loads(row["state_json"])
            state["running"] = pending_count > 0
    state["pending_batches"] = pending_count
    state["queued_batches"] = max(0, pending_count - 1)
    state["recent_batches"] = recent_batches
    return state


def _enqueue_task(rows, changes, state) -> tuple[bool, dict]:
    # Commit each batch independently; submissions never replace worker state.
    # The singleton drain preserves submission order, including overlapping items.
    with _queue_connection() as database:
        database.execute("BEGIN IMMEDIATE")
        database.execute("INSERT INTO tasks VALUES (?,?,?,?,?)",
                         (state["task_id"], "queued", time.time(), _json(changes), _json(state)))
        database.executemany("INSERT INTO items(task_id,link_id,row_json) VALUES(?,?,?)", [
            (state["task_id"], int(row["id"]), _json({field: row.get(field) for field in _SAVED_ROW_FIELDS}))
            for row in rows
        ])
    return True, state


def _persist_task_state(task_id) -> None:
    if not task_id:
        return
    state = _state_snapshot()
    with _queue_connection() as database:
        database.execute("UPDATE tasks SET status=?,state_json=? WHERE id=?",
                         (state["status"], _json(state), task_id))


def _checkpoint_item(task_id, result) -> None:
    if not task_id:
        return
    with _queue_connection() as database:
        database.execute("UPDATE items SET result_json=? WHERE task_id=? AND link_id=? AND result_json IS NULL",
                         (_json(result), task_id, int(result["link_id"])))


def _load_task(task_id) -> dict | None:
    with _queue_connection() as database:
        task = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            return None
        items = database.execute("SELECT row_json,result_json FROM items WHERE task_id=? ORDER BY link_id", (task_id,)).fetchall()
    changes = json.loads(task["changes_json"])
    normalized = {"status": "deleted"} if changes.get("status") == "deleted" else _normalize_changes(changes)
    return {
        "state": json.loads(task["state_json"]), "status": task["status"], "changes": normalized,
        "rows": [json.loads(item["row_json"]) for item in items if item["result_json"] is None],
        "results": [json.loads(item["result_json"]) for item in items if item["result_json"] is not None],
    }


def _link_ids(values: Iterable[int]) -> list[int]:
    ids: list[int] = []
    for value in values or []:
        try:
            link_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"店铺链接编号无效：{value!r}") from exc
        if link_id > 0 and link_id not in ids:
            ids.append(link_id)
    ids.sort()
    if not ids:
        raise ValueError("请至少勾选一条店铺链接")
    if len(ids) > 1000:
        raise ValueError("每次最多修改 1000 条店铺链接")
    return ids


def _normalize_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if "status" in changes and changes["status"] not in (None, ""):
        status = str(changes["status"] or "").strip().lower()
        if status not in STORE_LINK_REMOTE_UPDATE_STATUSES:
            raise ValueError("店铺链接状态只能是 active 或 paused")
        result["status"] = status
    for field in (*STORE_LINK_REMOTE_UPDATE_FIELDS, "net_proceeds_ratio"):
        if field not in changes or changes[field] in (None, ""):
            continue
        try:
            value = Decimal(str(changes[field]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{field} 必须是有效数字") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{field} 必须大于 0")
        result[field] = value
    if "net_proceeds_ratio" in result and {"price", "net_proceeds_usd"}.intersection(result):
        raise ValueError("净收益比例不能与固定售价或固定净收益同时填写")
    if not result:
        raise ValueError("请至少填写一个需要修改的字段")
    return result


def _resolve_net_ratio(row: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    resolved = dict(changes)
    if "net_proceeds_ratio" not in resolved:
        return resolved
    ratio = resolved.pop("net_proceeds_ratio")
    # Freeze the submission snapshot so retries never compound a previous write.
    raw = row.get("net_ratio_base_usd", row.get("net_proceeds_usd"))
    try:
        base = Decimal(str(raw))
        if not base.is_finite() or base <= 0:
            raise ValueError("当前净收益必须大于 0，无法按比例调价")
        target = (base * ratio / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if not target.is_finite() or target <= 0:
            raise ValueError("比例调价后的净收益须至少为 USD 0.01")
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("当前净收益缺失或无效，无法按比例调价") from exc
    resolved["net_proceeds_usd"] = target
    return resolved


def _api_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _value_text(value: Decimal, unit: str) -> str:
    text = format(value.normalize(), "f")
    return f"{text} {unit}"


def _package_payload(row: Mapping[str, Any], changes: Mapping[str, Decimal]) -> dict[str, Any]:
    attributes = []
    missing = []
    for field, attribute_id, unit in PACKAGE_FIELDS:
        raw_value = changes.get(field, row.get(field))
        try:
            value = Decimal(str(raw_value))
        except (InvalidOperation, TypeError, ValueError):
            value = Decimal("0")
        if not value.is_finite() or value <= 0:
            missing.append(field)
            continue
        attributes.append({"id": attribute_id, "value_name": _value_text(value, unit)})
    if missing:
        raise ValueError(
            "修改重量或尺寸时，美客多要求重量、长、宽、高同时完整；缺少："
            + ", ".join(missing)
        )
    return {"attributes": attributes}


def _desired_group_changes(
    row: Mapping[str, Any],
    changes: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any], tuple[str, ...]]]:
    groups: list[tuple[str, dict[str, Any], tuple[str, ...]]] = []
    if "status" in changes:
        groups.append(("链接状态", {"status": changes["status"]}, ("status",)))
    if "price" in changes:
        groups.append(("售价", {"price": _api_number(changes["price"])}, ("price",)))
    dimension_fields = tuple(field for field, _attribute, _unit in PACKAGE_FIELDS)
    selected_dimensions = tuple(field for field in dimension_fields if field in changes)
    if selected_dimensions:
        groups.append(("重量尺寸", _package_payload(row, changes), selected_dimensions))
    if "net_proceeds_usd" in changes:
        groups.append((
            "净收益",
            {"net_proceeds": _api_number(changes["net_proceeds_usd"])},
            ("net_proceeds_usd",),
        ))
    return groups


@audited
def _update_one_link(
    row: Mapping[str, Any],
    token: Mapping[str, Any],
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    if changes.get("status") == "deleted":
        return _delete_one_link(row, token)
    if row.get("status") == "deleted":
        return {
            "link_id": int(row["id"]), "item_id": row.get("item_id"),
            "status": "error", "applied_fields": [],
            "errors": ["链接已删除，不能重新上架或修改"],
        }
    item_id = str(row.get("item_id") or "").strip().upper()
    client = MercadoLibreClient(str(token.get("access_token") or ""))
    applied: dict[str, Decimal] = {}
    errors: list[str] = []
    remote_groups: list[str] = []
    try:
        changes = _resolve_net_ratio(row, changes)
        groups = _desired_group_changes(row, changes)
    except Exception as exc:
        return {
            "link_id": int(row["id"]),
            "item_id": item_id,
            "store": str(row.get("store_name") or ""),
            "status": "error",
            "applied_fields": [],
            "errors": [str(exc)],
        }

    if "net_proceeds_usd" in changes:
        try:
            from bit.mercado_promotions import withdraw_listing_promotions
            outcome = withdraw_listing_promotions(
                token_id=row["token_id"], site_id=row.get("site_id") or item_id[:3],
                item_id=item_id, actor="店铺链接修改净收益自动取消活动", verify_live=True,
            )
            if outcome["cancelled_count"]:
                remote_groups.append(f"取消活动 {outcome['cancelled_count']} 项")
        except Exception as exc:
            return {"link_id": int(row["id"]), "item_id": item_id,
                    "store": str(row.get("store_name") or ""), "status": "error",
                    "applied_fields": [], "errors": [f"取消活动失败，未修改净收益：{exc}"]}

    for label, payload, fields in groups:
        try:
            client.update_global_item(item_id, payload)
            remote_groups.append(label)
            for field in fields:
                applied[field] = changes[field]
        except Exception as exc:
            errors.append(f"{label}：{exc}")

    verify_warning = ""
    local_changes = dict(applied)
    if applied:
        try:
            remote_item = client.get_marketplace_item(
                item_id, attributes=STORE_LINK_DETAIL_ATTRIBUTES
            )
            verified = listing_record(token, remote_item, _now_text())
            requested_financial = {"price", "net_proceeds_usd"}.intersection(applied)
            if requested_financial:
                # Mercado couples target net proceeds and sale price. Persist both
                # final values returned by the platform, regardless of which one
                # the user edited.
                for field in ("price", "net_proceeds_usd"):
                    if verified.get(field) is not None:
                        local_changes[field] = Decimal(str(verified[field]))
            requested_packages = {
                "weight_g", "package_length_cm", "package_width_cm", "package_height_cm",
            }.intersection(applied)
            if requested_packages:
                for field in (
                    "weight_g", "package_length_cm", "package_width_cm", "package_height_cm",
                ):
                    if verified.get(field) is not None:
                        local_changes[field] = Decimal(str(verified[field]))
            for field, desired in applied.items():
                actual = verified.get(field)
                if actual is None:
                    errors.append(f"{field}：后台返回值为空，无法确认修改结果")
                    continue
                if field == "status":
                    if str(actual).strip().lower() != str(desired).strip().lower():
                        errors.append(
                            f"{field}：后台最终值 {actual} 与目标值 {desired} 不一致"
                        )
                    continue
                if abs(Decimal(str(actual)) - desired) > Decimal("0.0001"):
                    errors.append(
                        f"{field}：后台最终值 {actual} 与目标值 {desired} 不一致"
                    )
        except Exception as exc:
            verify_warning = f"后台写入成功，重新读取确认失败：{exc}"

    local_error = ""
    if local_changes:
        try:
            bulk_update_store_links([int(row["id"])], local_changes)
            sync_pulled_product_fields_from_store_links(
                [int(row["id"])], local_changes.keys()
            )
        except Exception as exc:
            local_error = f"后台已修改，但本地回写失败：{exc}"
            errors.append(local_error)

    if applied and not errors and not verify_warning:
        status = "success"
    elif applied:
        status = "partial"
    else:
        status = "error"
    return {
        "link_id": int(row["id"]),
        "item_id": item_id,
        "store": str(row.get("store_name") or ""),
        "status": status,
        "remote_groups": remote_groups,
        "applied_fields": list(applied),
        "local_fields": list(local_changes),
        "errors": errors,
        "warning": verify_warning,
    }


def _update_one_link_locked(row, token, changes):
    with store_link_item_locks(row["token_id"], [row["item_id"]], owner="remote-update"):
        return _update_one_link(row, token, changes)


@audited
def run_store_link_remote_update(
    rows: Iterable[Mapping[str, Any]],
    changes: Mapping[str, Any],
    *,
    task_id: str = "",
    previous_results: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    rows = [dict(row) for row in rows]
    results = [dict(result) for result in previous_results]
    total_links = len(rows) + len(results)
    prior_state = _state_snapshot()
    action = "删除" if changes.get("status") == "deleted" else "修改"
    _state_update(
        running=True,
        status="running",
        message=f"正在等待并{action}美客多后台链接",
        total_links=total_links,
        processed_links=len(results),
        success_count=sum(row["status"] == "success" for row in results),
        retained_count=sum(row["status"] == "retained" for row in results),
        partial_count=sum(row["status"] == "partial" for row in results),
        failed_count=sum(row["status"] == "error" for row in results),
        current_item="",
        started_at=prior_state.get("started_at") or _now_text(),
        finished_at="",
        results=list(results[-200:]),
        logs=list(prior_state.get("logs") or []) if task_id else [],
    )
    _append_log(f"任务启动，共 {total_links} 条链接；{action}将直接写入 Mercado Libre 后台")
    if results:
        _append_log(f"恢复任务：已记录 {len(results)} 条结果，继续处理剩余 {len(rows)} 条链接")
    _persist_task_state(task_id)

    token_records: dict[int, dict[str, Any]] = {}
    token_errors: dict[int, str] = {}
    token_ids = sorted({int(row["token_id"]) for row in rows})

    def prepare_token(token_id):
        token_record = dict(bit_mysql.get_mercado_store_token(token_id) or {})
        if not token_record:
            raise ValueError("店铺授权不存在")
        return _client_and_token(token_record)[1]

    # Authorize different shops concurrently, with the same bounded budget as
    # item updates. Each shop is still prepared only once per batch.
    with ThreadPoolExecutor(max_workers=max(1, min(STORE_LINK_REMOTE_UPDATE_WORKERS, len(token_ids)))) as executor:
        preparations = {executor.submit(contextual_target(prepare_token), token_id): token_id for token_id in token_ids}
        for future in as_completed(preparations):
            token_id = preparations[future]
            try:
                token_records[token_id] = future.result()
            except Exception as exc:
                token_errors[token_id] = str(exc)

    counts = Counter(row["status"] for row in results)
    worker_count = max(1, min(STORE_LINK_REMOTE_UPDATE_WORKERS, len(rows)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {}
        for row in rows:
            token_id = int(row["token_id"])
            item_id = str(row.get("item_id") or "")
            if token_id in token_errors:
                result = {
                    "link_id": int(row["id"]),
                    "item_id": item_id,
                    "store": str(row.get("store_name") or ""),
                    "status": "error",
                    "applied_fields": [],
                    "errors": [f"店铺授权不可用：{token_errors[token_id]}"],
                }
                _checkpoint_item(task_id, result)
                record("remote_update_item", result["status"], {"result": result, "task_id": _update_state.get("task_id")})
                results.append(result)
                counts[result["status"]] += 1
                _append_log(f"{item_id} 失败：{result['errors'][0]}")
                continue
            futures[executor.submit(contextual_target(_update_one_link_locked), row, token_records[token_id], changes)] = row

        for future in as_completed(futures):
            row = futures[future]
            item_id = str(row.get("item_id") or "")
            _state_update(current_item=item_id)
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "link_id": int(row["id"]),
                    "item_id": item_id,
                    "store": str(row.get("store_name") or ""),
                    "status": "error",
                    "applied_fields": [],
                    "errors": [str(exc)],
                }
            # Commit the item result before exposing progress. A service crash
            # never turns an already checkpointed item into a repeated write.
            _checkpoint_item(task_id, result)
            record("remote_update_item", result["status"], {"result": result, "task_id": _update_state.get("task_id")})
            results.append(result)
            counts[result["status"]] += 1
            if result["status"] == "success":
                _append_log(f"{item_id} 后台{action}成功：{', '.join(result.get('remote_groups') or [])}")
            elif result["status"] == "retained":
                _append_log(f"{item_id} 已安全保留：{result.get('retention_reason') or '不满足删除条件'}")
            else:
                details = list(result.get("errors") or [])
                if result.get("warning"):
                    details.append(str(result["warning"]))
                _append_log(f"{item_id} {result['status']}：{'；'.join(details)}")
            processed = len(results)
            _state_update(
                processed_links=processed,
                success_count=counts["success"],
                retained_count=counts["retained"],
                partial_count=counts["partial"],
                failed_count=counts["error"],
                results=list(results[-200:]),
                message=f"正在{action}美客多后台链接 {processed}/{total_links}",
            )
            _persist_task_state(task_id)

    failed = sum(row["status"] == "error" for row in results)
    partial = sum(row["status"] == "partial" for row in results)
    success = sum(row["status"] == "success" for row in results)
    retained = sum(row["status"] == "retained" for row in results)
    status = "completed" if not failed and not partial else "partial"
    message = f"后台{action}完成：成功 {success}，安全保留 {retained}，部分成功 {partial}，失败 {failed}"
    _state_update(
        running=False,
        status=status,
        message=message,
        processed_links=len(results),
        success_count=success,
        retained_count=retained,
        partial_count=partial,
        failed_count=failed,
        current_item="",
        finished_at=_now_text(),
        results=list(results[-200:]),
    )
    _append_log(message)
    _persist_task_state(task_id)
    return _state_snapshot()


def _run_background(rows: list[dict[str, Any]], changes: dict[str, Any], task_id: str = "") -> None:
    task_lock = InterProcessLock(
        REMOTE_UPDATE_TASK_LOCK_KEY,
        owner="bit_store_link_remote_update",
        metadata={"task_id": task_id or _update_state.get("task_id"), "type": "remote_update"},
    )
    _state_update(message="正在准备修改美客多后台链接")
    _append_log("修改任务独立执行，仅与同一商品的同步读写互斥")
    _persist_task_state(task_id)
    if not task_lock.acquire(timeout=None):
        return
    try:
        previous_results = []
        if task_id:
            # Read checkpoints only after taking the remote-update task lock.
            # A different process may have finished this queued job meanwhile.
            task = _load_task(task_id)
            if not task or task["status"] not in _PENDING_STATUSES:
                return
            rows, changes, previous_results = task["rows"], task["changes"], task["results"]
            # A preceding batch may have changed package fields or deleted the
            # link since submission. Use its committed values for omitted fields.
            if rows:
                current = {int(row["id"]): row for row in get_store_links_by_ids(
                    [row["id"] for row in rows]
                )}
                for row in rows:
                    latest = current.get(int(row["id"]))
                    if latest:
                        for field in ("status", *STORE_LINK_REMOTE_UPDATE_FIELDS):
                            row[field] = latest.get(field)
        run_store_link_remote_update(rows, changes, task_id=task_id, previous_results=previous_results)
    except Exception as exc:
        _state_update(
            running=False,
            status="queued" if task_id else "error",
            message=f"后台任务暂时中断，将自动恢复：{exc}" if task_id else str(exc),
            current_item="",
            finished_at=_now_text(),
        )
        _append_log(f"任务失败：{exc}")
        _persist_task_state(task_id)
    finally:
        task_lock.release()


@audited
def start_store_link_remote_update(
    link_ids: Iterable[int],
    changes: Mapping[str, Any],
    *,
    delete: bool = False,
) -> tuple[bool, dict[str, Any]]:
    ids = _link_ids(link_ids)
    normalized = {"status": "deleted"} if delete else _normalize_changes(changes)
    rows = get_store_links_by_ids(ids)
    if "net_proceeds_ratio" in normalized:
        rows = [{**row, "net_ratio_base_usd": row.get("net_proceeds_usd")} for row in rows]
    state = dict(
        running=True,
        task_id=uuid.uuid4().hex,
        status="queued",
        message="美客多后台删除任务已排队" if delete else "美客多后台修改任务已排队，可继续提交下一批",
        total_links=len(rows),
        processed_links=0,
        success_count=0,
        retained_count=0,
        partial_count=0,
        failed_count=0,
        current_item="",
        started_at=_now_text(),
        finished_at="",
        changes={
            key: (float(value) if isinstance(value, Decimal) else value)
            for key, value in normalized.items()
        },
        results=[],
        logs=[],
    )
    _enqueue_task(rows, normalized, state)
    try:
        start_store_link_remote_update_recovery()
        _recovery_wakeup.set()
    except Exception:
        # Submission already committed. Report acceptance rather than inviting
        # a duplicate retry; another recovery worker/startup can drain it.
        logging.exception("店铺链接修改任务已保存，后台调度暂时不可用")
    return True, state


def _resume_next_task() -> bool:
    with _queue_connection() as database:
        pending = database.execute(
            "SELECT id,state_json FROM tasks WHERE status IN ('queued','starting','running') "
            "ORDER BY created,rowid LIMIT 1"
        ).fetchone()
    if not pending:
        return False
    _state_update(**json.loads(pending["state_json"]))
    _state_update(running=True)
    _run_background([], {}, pending["id"])
    return True


def resume_pending_store_link_remote_updates() -> bool:
    """Recover one task under the same singleton lease used by the async drain."""
    lease = InterProcessLock(REMOTE_UPDATE_RECOVERY_LOCK_KEY, owner="store-link-update-recovery")
    if not lease.acquire(timeout=0):
        return False
    try:
        return _resume_next_task()
    finally:
        lease.release()


def _remote_update_recovery_loop() -> None:
    while True:
        lease = InterProcessLock(REMOTE_UPDATE_RECOVERY_LOCK_KEY, owner="store-link-update-recovery")
        try:
            acquired = lease.acquire(timeout=0)
        except Exception:
            logging.exception("启动店铺链接后台恢复调度失败，将自动重试")
            acquired = False
        if acquired:
            try:
                while True:
                    try:
                        recovered = _resume_next_task()
                        if recovered and _state_snapshot().get("status") not in _PENDING_STATUSES:
                            # Successful batches drain immediately. Only idle or
                            # interrupted work needs a retry delay.
                            continue
                    except Exception:
                        recovered = True
                        logging.exception("恢复店铺链接后台修改任务失败，将自动重试")
                    # A failed task stays queued; pace recovery to avoid tight
                    # loops during a database or filesystem outage.
                    _recovery_wakeup.wait(30 if recovered else 10)
                    _recovery_wakeup.clear()
            finally:
                lease.release()
        _recovery_wakeup.wait(30)
        _recovery_wakeup.clear()


def start_store_link_remote_update_recovery() -> bool:
    """Start the persistent job drain once; call from the server worker bootstrap."""
    global _recovery_thread
    if bit_mysql.RUNTIME_SETTINGS.is_client:
        return False
    with _recovery_guard:
        if _recovery_thread and _recovery_thread.is_alive():
            return False
        thread = threading.Thread(target=_remote_update_recovery_loop,
                                  name="mercado-store-link-update-recovery", daemon=True)
        thread.start()
        _recovery_thread = thread
        return True


def start_store_link_remote_delete(link_ids):
    return start_store_link_remote_update(link_ids, {}, delete=True)


@audited
def _delete_one_link(row, token, *, required_title=None, max_sold_quantity=None):
    """Delete only the selected marketplace item and verify before marking locally."""
    item_id = str(row.get("item_id") or "").strip().upper()
    result = {
        "link_id": int(row["id"]), "item_id": item_id,
        "store": str(row.get("store_name") or ""), "status": "error",
        "applied_fields": [], "errors": [], "remote_groups": ["删除链接"],
    }
    try:
        if not _is_site_listing_id(item_id, row.get("site_id")):
            result["status"] = "retained"
            result["retention_reason"] = _site_listing_retention_reason(item_id)
            return result
        client = MercadoLibreClient(str(token.get("access_token") or ""))
        def read_item():
            item = client.get_marketplace_item(item_id)
            if str(item.get("id") or "").upper() != item_id:
                raise ValueError("平台返回的链接编号不匹配，无法确认删除")
            return item

        def is_deleted(item):
            return (item.get("deleted") is True
                    or item.get("status") == "deleted"
                    or "deleted" in (item.get("sub_status") or []))

        def wait_until(predicate):
            latest = None
            for delay in REMOTE_DELETE_VERIFY_DELAYS:
                if delay:
                    time.sleep(delay)
                latest = read_item()
                if predicate(latest):
                    return latest
            return latest

        def update_item(changes):
            try:
                return client.update_global_item(item_id, changes)
            except Exception as exc:
                text = str(exc)
                if "field_not_updatable" in text.lower() or "not modifiable" in text.lower():
                    raise ValueError(
                        f"平台当前不允许修改该链接（审核中或字段不可修改）：{text}") from exc
                raise

        def retention_reason(item):
            if required_title is not None and str(item.get("title") or "").strip() != required_title:
                return "平台标题已变更或无法确认，保留链接"
            if max_sold_quantity is not None:
                value = item.get("sold_quantity")
                try:
                    sales = int(value)
                    if isinstance(value, bool) or str(sales) != str(value) or sales < 0:
                        raise ValueError()
                except (TypeError, ValueError, OverflowError):
                    return "平台销量无法确认，保留链接"
                result["sold_quantity"] = sales
                if sales > max_sold_quantity:
                    return f"销量 {sales} 大于 {max_sold_quantity}，保留链接"
            return ""

        def retain(item):
            reason = retention_reason(item)
            if reason:
                result.update(status="retained", retention_reason=reason)
                return True
            return False

        item = read_item()
        if not is_deleted(item):
            if retain(item):
                return result
            if item.get("status") == "under_review":
                raise ValueError("链接正在平台审核中，当前不可暂停或删除；审核结束后可重试")
            if item.get("status") not in {"paused", "closed"}:
                previous_status = item.get("status")
                update_item({"status": "paused"})
                item = wait_until(lambda value: value.get("status") in {"paused", "closed"})
                if item.get("status") not in {"paused", "closed"}:
                    raise ValueError("平台未确认链接已暂停，未继续删除")
                if retain(item):
                    # Sales/title may have changed while the pause was confirmed.
                    # Restore the status so a protected link is actually retained.
                    update_item({"status": previous_status})
                    restored = wait_until(lambda value: value.get("status") == previous_status)
                    if restored.get("status") != previous_status:
                        raise ValueError("保留链接后恢复原状态未确认，请核验平台状态")
                    return result
            update_item({"deleted": True})
            item = wait_until(is_deleted)
            if not is_deleted(item):
                raise ValueError("已提交删除，但平台尚未确认已删除；保留记录，可重试核验")
        result["applied_fields"] = ["status"]
        try:
            mark_store_links_deleted([int(row["id"])])
        except Exception as exc:
            result["status"] = "partial"
            result["errors"] = [f"平台已删除，本地状态回写失败：{exc}"]
            return result
        result["status"] = "success"
    except Exception as exc:
        result["errors"] = [str(exc)]
    return result


__all__ = [
    "run_store_link_remote_update",
    "start_store_link_remote_update",
    "start_store_link_remote_update_recovery",
    "resume_pending_store_link_remote_updates",
    "store_link_remote_update_status",
]
