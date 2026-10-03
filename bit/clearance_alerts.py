"""Daily central clearance scans, scoped to explicitly configured half-managed stores."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from bit import bit_mysql
from bit.bit_runtime_lock import InterProcessLock
from bit.sync_capacity import run_store as run_capacity_store
from erp.mercadolibre_clearance_store import ClearanceStore, LOCAL

_guard = threading.RLock()
_running_tokens = set()
_scheduler = None
_stop = threading.Event()
_last_attempt = {}
LOCK_KEY = "mercado_clearance_alert_scan"


def current_scopes(allowed_token_ids=None, token_id=None, site_id=""):
    from bit.clearance_sources import eligible_scopes

    summaries = (bit_mysql.list_mercado_store_tokens() or {}).get("rows", [])
    scopes = eligible_scopes(summaries, allowed_token_ids)
    if token_id is not None:
        token_id = int(token_id)
        if allowed_token_ids is not None and token_id not in allowed_token_ids:
            raise PermissionError("无权访问此店铺")
        scopes = {key: row for key, row in scopes.items() if key[0] == token_id}
    site_id = str(site_id or "").strip().upper()
    if site_id:
        scopes = {key: row for key, row in scopes.items() if key[1] == site_id}
    return summaries, scopes


def list_alerts(*, allowed_token_ids=None, token_id=None, site_id="", **filters):
    _, all_scopes = current_scopes(allowed_token_ids)
    if token_id is not None and allowed_token_ids is not None and int(token_id) not in allowed_token_ids:
        raise PermissionError("无权访问此店铺")
    scopes = {key: value for key, value in all_scopes.items()
              if (token_id is None or key[0] == int(token_id))
              and (not site_id or key[1] == str(site_id).strip().upper())}
    data = ClearanceStore().list_alerts(scopes, **filters)
    try:
        from bit.clearance_sources import fill_missing_images
        fill_missing_images(data["rows"])
    except Exception:
        logging.warning("清退告警主图补充失败，保留检测结果", exc_info=True)
    stores = {key[0]: {"token_id": key[0], "store_name": row["store_name"]}
              for key, row in all_scopes.items()}
    data["stores"] = list(stores.values())
    with _guard:
        data["checking"] = bool(_running_tokens & {key[0] for key in scopes})
    data["schedule"] = "每天自动检测；失败后每小时重试"
    return data


def save_facts(link_id, payload, *, allowed_token_ids=None):
    _, scopes = current_scopes(allowed_token_ids)
    if not isinstance(payload, dict):
        raise ValueError("核实信息须为 JSON 对象")
    sku_key = payload.get("sku_key")
    if not isinstance(sku_key, str) or not sku_key or len(sku_key) > 100:
        raise ValueError("请明确选择需要核实的 SKU")
    return ClearanceStore().save_facts(link_id, sku_key, payload, scopes)


def _scan(summaries, scopes, store, lock):
    from bit.clearance_sources import collect_store

    token_ids = {key[0] for key in scopes}
    try:
        for summary in summaries:
            token_id = int(summary.get("id") or 0)
            if token_id not in token_ids:
                continue
            now = datetime.now(timezone.utc)
            scoped = {key: value for key, value in scopes.items() if key[0] == token_id}
            try:
                # Collector generators are exhausted while the capacity slot is
                # held, and before any snapshot replacement or DB transaction.
                rows = run_capacity_store("clearance-alerts", token_id,
                    lambda: list(collect_store(summary, scoped, now=now)))
                store.replace_store(token_id, rows, now=now, site_ids={key[1] for key in scoped})
            except Exception:
                logging.exception("半托管清退检测失败：token_id=%s", token_id)
                store.mark_failed(token_id, now=now, site_ids={key[1] for key in scoped})
            finally:
                with _guard:
                    _last_attempt[token_id] = now
    finally:
        lock.release()
        with _guard:
            _running_tokens.clear()


def start_refresh(*, allowed_token_ids=None, token_id=None, site_id=""):
    summaries, scopes = current_scopes(allowed_token_ids, token_id, site_id)
    if not scopes:
        return {"started": False, "checking": False, "message": "当前权限内没有启用的半托管店铺，请检查店铺授权中的站点分组"}
    with _guard:
        if _running_tokens:
            return {"started": False, "checking": bool(_running_tokens & {key[0] for key in scopes}),
                    "message": "清退检测正在进行，请稍后重新查看"}
        lock = InterProcessLock(LOCK_KEY, owner="clearance-alerts")
        if not lock.acquire(timeout=0):
            return {"started": False, "checking": True, "message": "清退检测正在其他进程执行"}
        try:
            store = ClearanceStore()
            _running_tokens.update(key[0] for key in scopes)
            thread = threading.Thread(target=_scan, args=(summaries, scopes, store, lock),
                                      name="mercado-clearance-alert-scan", daemon=True)
            thread.start()
        except BaseException:
            _running_tokens.clear()
            lock.release()
            raise
    return {"started": True, "checking": True}


def _scheduler_loop():
    while not _stop.is_set():
        try:
            now = datetime.now(timezone.utc)
            _, scopes = current_scopes()
            store = ClearanceStore()
            today = now.astimezone(LOCAL).date().isoformat()
            with _guard:
                due = {token_id for token_id in {key[0] for key in scopes}
                       if store.last_success_date(token_id, {k[1] for k in scopes if k[0] == token_id}) != today
                       and now - _last_attempt.get(token_id, now - timedelta(hours=2)) >= timedelta(hours=1)}
                running = bool(_running_tokens)
            if due and not running:
                start_refresh(allowed_token_ids=due)
        except Exception:
            logging.exception("半托管清退告警调度失败")
        _stop.wait(60)


def start_scheduler():
    global _scheduler
    with _guard:
        if _scheduler and _scheduler.is_alive():
            return
        _stop.clear()
        _scheduler = threading.Thread(target=_scheduler_loop, name="mercado-clearance-alert-scheduler", daemon=True)
        _scheduler.start()
