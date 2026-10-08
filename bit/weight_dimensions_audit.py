"""Idempotent mirror of saved weight/dimension records and execution history."""
import hashlib
import json
import logging
import threading

from erp.store_link_audit import record_once

_backfill_lock = threading.Lock()
_backfilled = False


def mirror_record(row):
    order = str(row.get("order_number") or "").strip()
    if not order:
        return
    identity = {key: row.get(key) for key in (
        "order_number", "company_store", "salesperson", "product_id", "marketplace_item_id",
        "declared_weight_g", "declared_dimensions_cm", "actual_weight_g", "actual_dimensions_cm",
    )}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    actor_name = str(row.get("salesperson") or "").strip()
    actor = {"type": "system", "display_name": actor_name} if actor_name else None
    record_once(f"weight-record:{order}:{digest}", "weight_dimensions_record", "completed",
                {"source": "weight_dimensions_records", "salesperson": actor_name, "record": {
                    key: value for key, value in row.items() if key != "execution_logs"}},
                created_at=row.get("time"), actor=actor)
    for index, entry in enumerate(row.get("execution_logs") or []):
        if not isinstance(entry, dict):
            continue
        digest = hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        phase = {"失败": "failed", "保存失败": "failed", "部分成功": "partial", "进行中": "started",
                 "成功": "success", "跳过": "completed"}.get(entry.get("status"), "completed")
        name = str(entry.get("operator") or entry.get("salesperson") or actor_name).strip()
        record_once(f"weight-log:{order}:{index}:{digest}", "weight_dimensions_execution", phase,
                    {"source": "weight_dimensions_records", "order_number": order,
                     "salesperson": str(entry.get("salesperson") or actor_name), "entry": entry},
                    created_at=entry.get("time"), actor={"type": "system", "display_name": name} if name else None)


def mirror_records(rows):
    for row in rows:
        mirror_record(row)


def mirror_saved_records(rows):
    """Source records are already durable; an audit outage can be retried by backfill."""
    global _backfilled
    try:
        mirror_records(rows)
    except Exception:
        _backfilled = False
        logging.exception("同步重量尺寸记录到店铺操作记录失败，历史补齐将重试")


def ensure_history_backfilled():
    global _backfilled
    if _backfilled:
        return
    with _backfill_lock:
        if _backfilled:
            return
        from bit import bit_db_api
        mirror_records(bit_db_api.list_weight_dimensions_records())
        _backfilled = True
