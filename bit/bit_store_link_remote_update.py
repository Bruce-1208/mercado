"""Push store-link edits to Mercado Libre Global Selling listings."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from bit import bit_mysql
from bit.bit_runtime_lock import InterProcessLock
from bit.bit_store_link_sync import (
    STORE_LINK_DETAIL_ATTRIBUTES,
    STORE_LINK_SYNC_LOCK_KEY,
    _client_and_token,
)
from erp.mercadolibre_collection_store import (
    sync_pulled_product_fields_from_store_links,
)
from erp.mercadolibre_store_link_store import (
    bulk_update_store_links,
    get_store_links_by_ids,
    listing_record,
)
from mercado_api.client import MercadoLibreClient


STORE_LINK_REMOTE_UPDATE_WORKERS = 6
STORE_LINK_REMOTE_UPDATE_FIELDS = (
    "price",
    "weight_g",
    "package_length_cm",
    "package_width_cm",
    "package_height_cm",
    "net_proceeds_usd",
)
PACKAGE_FIELDS = (
    ("weight_g", "PACKAGE_WEIGHT", "g"),
    ("package_length_cm", "PACKAGE_LENGTH", "cm"),
    ("package_width_cm", "PACKAGE_WIDTH", "cm"),
    ("package_height_cm", "PACKAGE_HEIGHT", "cm"),
)

_state_guard = threading.RLock()
_update_state = {
    "running": False,
    "task_id": "",
    "status": "idle",
    "message": "等待修改美客多后台链接",
    "total_links": 0,
    "processed_links": 0,
    "success_count": 0,
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


def store_link_remote_update_status() -> dict[str, Any]:
    with _state_guard:
        state = dict(_update_state)
        state["changes"] = dict(state.get("changes") or {})
        state["results"] = [dict(row) for row in state.get("results") or []]
        state["logs"] = list(state.get("logs") or [])
    return state


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


def _normalize_changes(changes: Mapping[str, Any]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for field in STORE_LINK_REMOTE_UPDATE_FIELDS:
        if field not in changes or changes[field] in (None, ""):
            continue
        try:
            value = Decimal(str(changes[field]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{field} 必须是有效数字") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{field} 必须大于 0")
        result[field] = value
    if not result:
        raise ValueError("请至少填写一个需要修改的字段")
    return result


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
    changes: Mapping[str, Decimal],
) -> list[tuple[str, dict[str, Any], tuple[str, ...]]]:
    groups: list[tuple[str, dict[str, Any], tuple[str, ...]]] = []
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


def _update_one_link(
    row: Mapping[str, Any],
    token: Mapping[str, Any],
    changes: Mapping[str, Decimal],
) -> dict[str, Any]:
    item_id = str(row.get("item_id") or "").strip().upper()
    client = MercadoLibreClient(str(token.get("access_token") or ""))
    applied: dict[str, Decimal] = {}
    errors: list[str] = []
    remote_groups: list[str] = []
    try:
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


def run_store_link_remote_update(
    rows: Iterable[Mapping[str, Any]],
    changes: Mapping[str, Decimal],
) -> dict[str, Any]:
    rows = [dict(row) for row in rows]
    _state_update(
        running=True,
        status="running",
        message="正在等待并修改美客多后台链接",
        total_links=len(rows),
        processed_links=0,
        success_count=0,
        partial_count=0,
        failed_count=0,
        current_item="",
        started_at=_now_text(),
        finished_at="",
        results=[],
        logs=[],
    )
    _append_log(f"任务启动，共 {len(rows)} 条链接；修改将直接写入 Mercado Libre 后台")

    token_records: dict[int, dict[str, Any]] = {}
    token_errors: dict[int, str] = {}
    for token_id in sorted({int(row["token_id"]) for row in rows}):
        try:
            record = dict(bit_mysql.get_mercado_store_token(token_id) or {})
            if not record:
                raise ValueError("店铺授权不存在")
            _client, record = _client_and_token(record)
            token_records[token_id] = record
        except Exception as exc:
            token_errors[token_id] = str(exc)

    results: list[dict[str, Any]] = []
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
                results.append(result)
                _append_log(f"{item_id} 失败：{result['errors'][0]}")
                continue
            futures[executor.submit(_update_one_link, row, token_records[token_id], changes)] = row

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
            results.append(result)
            if result["status"] == "success":
                _append_log(f"{item_id} 后台修改成功：{', '.join(result.get('remote_groups') or [])}")
            else:
                details = list(result.get("errors") or [])
                if result.get("warning"):
                    details.append(str(result["warning"]))
                _append_log(f"{item_id} {result['status']}：{'；'.join(details)}")
            processed = len(results)
            _state_update(
                processed_links=processed,
                success_count=sum(row["status"] == "success" for row in results),
                partial_count=sum(row["status"] == "partial" for row in results),
                failed_count=sum(row["status"] == "error" for row in results),
                results=list(results[-200:]),
                message=f"正在修改美客多后台链接 {processed}/{len(rows)}",
            )

    failed = sum(row["status"] == "error" for row in results)
    partial = sum(row["status"] == "partial" for row in results)
    success = sum(row["status"] == "success" for row in results)
    status = "completed" if not failed and not partial else "partial"
    message = f"后台修改完成：成功 {success}，部分成功 {partial}，失败 {failed}"
    _state_update(
        running=False,
        status=status,
        message=message,
        processed_links=len(results),
        success_count=success,
        partial_count=partial,
        failed_count=failed,
        current_item="",
        finished_at=_now_text(),
        results=list(results[-200:]),
    )
    _append_log(message)
    return store_link_remote_update_status()


def _run_background(rows: list[dict[str, Any]], changes: dict[str, Decimal]) -> None:
    task_lock = InterProcessLock(
        STORE_LINK_SYNC_LOCK_KEY,
        owner="bit_store_link_remote_update",
        metadata={"task_id": _update_state.get("task_id"), "type": "remote_update"},
    )
    _state_update(message="正在等待当前店铺链接同步完成")
    _append_log("正在等待店铺链接同步锁，取得锁后再写入美客多后台")
    if not task_lock.acquire(timeout=None):
        return
    try:
        run_store_link_remote_update(rows, changes)
    except Exception as exc:
        _state_update(
            running=False,
            status="error",
            message=str(exc),
            current_item="",
            finished_at=_now_text(),
        )
        _append_log(f"任务失败：{exc}")
    finally:
        task_lock.release()


def start_store_link_remote_update(
    link_ids: Iterable[int],
    changes: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    ids = _link_ids(link_ids)
    normalized = _normalize_changes(changes)
    rows = get_store_links_by_ids(ids)
    with _state_guard:
        if _update_state.get("running"):
            return False, store_link_remote_update_status()
        _update_state.update(
            running=True,
            task_id=uuid.uuid4().hex,
            status="starting",
            message="正在启动美客多后台修改任务",
            total_links=len(rows),
            processed_links=0,
            success_count=0,
            partial_count=0,
            failed_count=0,
            current_item="",
            started_at=_now_text(),
            finished_at="",
            changes={key: float(value) for key, value in normalized.items()},
            results=[],
            logs=[],
        )
    thread = threading.Thread(
        target=_run_background,
        args=(rows, normalized),
        name="mercado-store-link-remote-update",
        daemon=True,
    )
    thread.start()
    return True, store_link_remote_update_status()


__all__ = [
    "run_store_link_remote_update",
    "start_store_link_remote_update",
    "store_link_remote_update_status",
]
