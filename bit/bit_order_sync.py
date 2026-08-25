"""Mercado Libre 订单手动回填与十五分钟增量同步。"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import date, datetime, time as datetime_time, timedelta, timezone

from bit import bit_mysql, mercado_tokens
from bit.bit_runtime_lock import InterProcessLock, get_lock_owner
from mercado_api.client import MercadoAPIError, MercadoLibreClient


ORDER_SYNC_LOCK_KEY = "mercado_order_sync_task"
DEFAULT_SYNC_INTERVAL_SECONDS = 15 * 60
FIRST_SYNC_LOOKBACK_HOURS = 48

SITE_COUNTRIES = {
    "MLM": "墨西哥",
    "MLB": "巴西",
    "MLC": "智利",
    "MCO": "哥伦比亚",
    "MLA": "阿根廷",
    "MLU": "乌拉圭",
}
STATUS_LABELS = {
    "payment_required": "待付款",
    "payment_in_process": "待付款",
    "partially_paid": "待付款",
    "confirmed": "审核",
    "paid": "找货",
    "ready_to_ship": "待发",
    "shipped": "已发",
    "delivered": "交付",
    "cancelled": "取消",
    "invalid": "问题",
    "partially_refunded": "退货",
    "refunded": "退货",
}

_state_guard = threading.RLock()
_sync_state = {
    "running": False,
    "task_id": "",
    "mode": "idle",
    "status": "idle",
    "message": "等待订单同步",
    "start_date": "",
    "end_date": "",
    "total_stores": 0,
    "processed_stores": 0,
    "current_store": "",
    "fetched_count": 0,
    "inserted_count": 0,
    "updated_count": 0,
    "failed_stores": 0,
    "started_at": "",
    "finished_at": "",
    "results": [],
    "logs": [],
    "scheduler_enabled": False,
    "sync_interval_seconds": DEFAULT_SYNC_INTERVAL_SECONDS,
    "next_run_at": "",
}
_scheduler_guard = threading.Lock()
_scheduler_started = False
_scheduler_stop_event = threading.Event()


def _now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _state_update(**changes):
    with _state_guard:
        _sync_state.update(changes)


def _append_log(message):
    line = f"{_now_text()} {str(message or '').strip()}"
    with _state_guard:
        logs = list(_sync_state.get("logs") or [])
        logs.append(line)
        _sync_state["logs"] = logs[-200:]


def order_sync_status():
    with _state_guard:
        state = dict(_sync_state)
        state["results"] = [dict(row) for row in state.get("results") or []]
        state["logs"] = list(state.get("logs") or [])
    owner = get_lock_owner(ORDER_SYNC_LOCK_KEY)
    if owner and not state.get("running"):
        state.update(
            running=True,
            status="running",
            message="订单同步正在其他进程运行",
            lock_owner=owner,
        )
    return state


def _date_range(start_date, end_date):
    try:
        start = date.fromisoformat(str(start_date or ""))
        end = date.fromisoformat(str(end_date or ""))
    except ValueError as exc:
        raise ValueError("请选择有效的起始日期和截止日期") from exc
    if end < start:
        raise ValueError("截止日期不能早于起始日期")
    start_at = datetime.combine(start, datetime_time.min, timezone.utc)
    end_at = datetime.combine(end + timedelta(days=1), datetime_time.min, timezone.utc)
    return start, end, start_at, end_at


def _iso_millis(value):
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _token_ids(values):
    result = []
    for value in values or ():
        try:
            token_id = int(value)
        except (TypeError, ValueError):
            continue
        if token_id > 0 and token_id not in result:
            result.append(token_id)
    return result


def _token_records(selected_token_ids=None):
    selected = set(_token_ids(selected_token_ids))
    summaries = (bit_mysql.list_mercado_store_tokens() or {}).get("rows") or []
    records = []
    for summary in summaries:
        token_id = int(summary.get("id") or 0)
        if selected and token_id not in selected:
            continue
        record = bit_mysql.get_mercado_store_token(token_id)
        if record:
            records.append(record)
    if selected:
        missing = selected.difference(int(row.get("id") or 0) for row in records)
        if missing:
            raise ValueError(f"选择的店铺授权不存在：{', '.join(map(str, sorted(missing)))}")
    if not records:
        raise ValueError("暂无已授权店铺，请先在“店铺授权”中完成授权")
    return records


def _refresh_token(token_id):
    mercado_tokens.refresh_and_save(
        int(token_id),
        get_token=bit_mysql.get_mercado_store_token,
        update_token=bit_mysql.update_mercado_store_token,
        record_error=bit_mysql.record_mercado_store_token_error,
    )
    return bit_mysql.get_mercado_store_token(int(token_id))


def _token_expiring(record):
    expires_at = record.get("expires_at")
    if not expires_at:
        return False
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            return False
    return expires_at <= datetime.now() + timedelta(minutes=5)


def _client_and_token(record):
    token_id = int(record["id"])
    if _token_expiring(record) and record.get("refresh_token"):
        _append_log(f"{record.get('display_name') or token_id} Token 即将过期，正在刷新")
        record = _refresh_token(token_id)
    return MercadoLibreClient(str(record.get("access_token") or "")), record


def _fetch_orders(client, seller_id, filters):
    for order_id in client.iter_order_ids(str(seller_id), **filters):
        yield client.get_order(order_id)


def _item_image(item):
    image_url = str(item.get("secure_thumbnail") or item.get("thumbnail") or "").strip()
    if image_url:
        return image_url
    pictures = item.get("pictures") or []
    first = pictures[0] if pictures and isinstance(pictures[0], dict) else {}
    return str(first.get("secure_url") or first.get("url") or "").strip()


def _enrich_order_images(client, orders):
    item_targets = {}
    for order in orders or []:
        for order_item in order.get("order_items") or []:
            product = order_item.get("item") if isinstance(order_item.get("item"), dict) else {}
            item_id = str(product.get("id") or "").strip()
            if item_id and not (product.get("thumbnail") or product.get("secure_thumbnail")):
                item_targets.setdefault(item_id, []).append(product)
    for item_id, products in item_targets.items():
        try:
            listing = client.get_marketplace_item(item_id)
            image_url = _item_image(listing)
        except Exception as exc:
            _append_log(f"SKU {item_id} 图片读取失败：{exc}")
            continue
        if not image_url:
            continue
        for product in products:
            product["secure_thumbnail"] = image_url


def backfill_order_images(token_ids=None, limit_per_store=200):
    results = []
    for record in _token_records(token_ids):
        client, record = _client_and_token(record)
        token_id = int(record["id"])
        rows = bit_mysql.list_mercado_missing_product_images(token_id, limit_per_store)
        updated_products = updated_orders = failed = 0
        for row in rows:
            item_id = str(row.get("product_id") or "").strip()
            try:
                image_url = _item_image(client.get_marketplace_item(item_id))
                if not image_url:
                    failed += 1
                    continue
                updated_orders += bit_mysql.update_mercado_product_image(token_id, item_id, image_url)
                updated_products += 1
            except Exception:
                failed += 1
        results.append({
            "token_id": token_id,
            "products": updated_products,
            "orders": updated_orders,
            "failed": failed,
        })
    return results


def _sync_store(record, filters):
    token_id = int(record["id"])
    display_name = str(record.get("display_name") or record.get("nickname") or token_id)
    seller_id = str(record.get("meli_user_id") or "").strip()
    if not seller_id:
        raise ValueError("店铺授权缺少 Seller ID，请刷新 Token 或重新授权")

    client, record = _client_and_token(record)
    refreshed_after_unauthorized = False
    while True:
        try:
            orders = _fetch_orders(client, seller_id, filters)
            batch = []
            totals = {"fetched": 0, "inserted": 0, "updated": 0}
            for order in orders:
                batch.append(order)
                totals["fetched"] += 1
                if len(batch) >= 50:
                    _enrich_order_images(client, batch)
                    result = bit_mysql.upsert_mercado_synced_orders(record, batch)
                    totals["inserted"] += int(result.get("inserted") or 0)
                    totals["updated"] += int(result.get("updated") or 0)
                    batch = []
                    _state_update(
                        fetched_count=int(_sync_state.get("fetched_count") or 0) + 50,
                        inserted_count=int(_sync_state.get("inserted_count") or 0) + int(result.get("inserted") or 0),
                        updated_count=int(_sync_state.get("updated_count") or 0) + int(result.get("updated") or 0),
                    )
            if batch:
                _enrich_order_images(client, batch)
                result = bit_mysql.upsert_mercado_synced_orders(record, batch)
                totals["inserted"] += int(result.get("inserted") or 0)
                totals["updated"] += int(result.get("updated") or 0)
                _state_update(
                    fetched_count=int(_sync_state.get("fetched_count") or 0) + len(batch),
                    inserted_count=int(_sync_state.get("inserted_count") or 0) + int(result.get("inserted") or 0),
                    updated_count=int(_sync_state.get("updated_count") or 0) + int(result.get("updated") or 0),
                )
            return {"store": display_name, "status": "success", **totals}
        except MercadoAPIError as exc:
            message = str(exc)
            unauthorized = "401" in message or "access token" in message.lower()
            if refreshed_after_unauthorized or not unauthorized or not record.get("refresh_token"):
                raise
            record = _refresh_token(token_id)
            client = MercadoLibreClient(str(record.get("access_token") or ""))
            refreshed_after_unauthorized = True


def run_order_sync(start_date="", end_date="", token_ids=None, mode="manual"):
    mode = "automatic" if str(mode) == "automatic" else "manual"
    records = _token_records(token_ids)
    start_text = end_text = ""
    manual_filters = None
    if mode == "manual":
        start, end, start_at, end_at = _date_range(start_date, end_date)
        start_text, end_text = start.isoformat(), end.isoformat()
        manual_filters = {
            "sort": "date_asc",
            "order.date_created.from": _iso_millis(start_at),
            "order.date_created.to": _iso_millis(end_at),
        }

    _state_update(
        running=True,
        mode=mode,
        status="running",
        message="正在拉取订单" if mode == "manual" else "正在执行十五分钟增量同步",
        start_date=start_text,
        end_date=end_text,
        total_stores=len(records),
        processed_stores=0,
        current_store="",
        fetched_count=0,
        inserted_count=0,
        updated_count=0,
        failed_stores=0,
        started_at=_now_text(),
        finished_at="",
        results=[],
        logs=[],
    )
    _append_log(f"任务启动，共 {len(records)} 家店铺")
    results = []
    for index, record in enumerate(records, start=1):
        store_name = str(record.get("display_name") or record.get("nickname") or record.get("id"))
        _state_update(current_store=store_name, processed_stores=index - 1)
        _append_log(f"开始同步 {store_name}")
        try:
            if manual_filters is not None:
                filters = dict(manual_filters)
            else:
                cursor = bit_mysql.get_mercado_order_sync_cursor(int(record["id"]))
                start_at = cursor or datetime.now(timezone.utc) - timedelta(hours=FIRST_SYNC_LOOKBACK_HOURS)
                if start_at.tzinfo is None:
                    start_at = start_at.replace(tzinfo=timezone.utc)
                start_at -= timedelta(minutes=5)
                filters = {
                    "sort": "updated_asc",
                    "last_updated.from": _iso_millis(start_at),
                    "last_updated.to": _iso_millis(datetime.now(timezone.utc)),
                }
            result = _sync_store(record, filters)
            results.append(result)
            _append_log(
                f"{store_name} 完成：读取 {result['fetched']}，新增 {result['inserted']}，更新 {result['updated']}"
            )
        except Exception as exc:
            result = {"store": store_name, "status": "error", "message": str(exc)}
            results.append(result)
            _append_log(f"{store_name} 失败：{exc}")
        _state_update(
            processed_stores=index,
            failed_stores=sum(1 for row in results if row.get("status") == "error"),
            results=list(results),
        )

    failed = sum(1 for row in results if row.get("status") == "error")
    final_status = "completed" if failed == 0 else "partial"
    message = (
        f"同步完成：新增 {_sync_state.get('inserted_count', 0)}，更新 {_sync_state.get('updated_count', 0)}"
        if failed == 0
        else f"同步完成，{failed} 家店铺失败"
    )
    _state_update(
        running=False,
        status=final_status,
        message=message,
        current_store="",
        finished_at=_now_text(),
        results=list(results),
    )
    _append_log(message)
    return order_sync_status()


def _run_background(start_date, end_date, token_ids, mode):
    task_lock = InterProcessLock(
        ORDER_SYNC_LOCK_KEY,
        owner="bit_order_sync",
        metadata={"mode": mode, "task_id": _sync_state.get("task_id")},
    )
    if not task_lock.acquire(timeout=0):
        _state_update(
            running=False,
            status="busy",
            message="订单同步已在其他进程运行",
            finished_at=_now_text(),
        )
        return
    try:
        run_order_sync(start_date, end_date, token_ids, mode)
    except Exception as exc:
        _state_update(
            running=False,
            status="error",
            message=str(exc),
            current_store="",
            finished_at=_now_text(),
        )
        _append_log(f"任务失败：{exc}")
    finally:
        task_lock.release()


def start_order_sync(start_date="", end_date="", token_ids=None, mode="manual"):
    mode = "automatic" if str(mode) == "automatic" else "manual"
    if mode == "manual":
        _date_range(start_date, end_date)
    selected_ids = _token_ids(token_ids)
    with _state_guard:
        if _sync_state.get("running"):
            return False, order_sync_status()
        task_id = uuid.uuid4().hex
        _sync_state.update(
            running=True,
            task_id=task_id,
            mode=mode,
            status="starting",
            message="正在启动订单同步",
            start_date=str(start_date or ""),
            end_date=str(end_date or ""),
            finished_at="",
        )
    thread = threading.Thread(
        target=_run_background,
        args=(start_date, end_date, selected_ids, mode),
        name=f"mercado-order-sync-{mode}",
        daemon=True,
    )
    thread.start()
    return True, order_sync_status()


def _scheduler_loop(interval_seconds):
    while not _scheduler_stop_event.is_set():
        next_run = datetime.now() + timedelta(seconds=interval_seconds)
        _state_update(
            scheduler_enabled=True,
            sync_interval_seconds=interval_seconds,
            next_run_at=next_run.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if _scheduler_stop_event.wait(interval_seconds):
            break
        start_order_sync(mode="automatic")


def ensure_order_sync_scheduler():
    global _scheduler_started
    if str(os.environ.get("MERCADO_ORDER_SYNC_DISABLED") or "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        _state_update(scheduler_enabled=False, next_run_at="")
        return False
    try:
        interval = int(os.environ.get("MERCADO_ORDER_SYNC_INTERVAL_SECONDS", DEFAULT_SYNC_INTERVAL_SECONDS))
    except ValueError:
        interval = DEFAULT_SYNC_INTERVAL_SECONDS
    interval = max(60, interval)
    with _scheduler_guard:
        if _scheduler_started:
            return True
        thread = threading.Thread(
            target=_scheduler_loop,
            args=(interval,),
            name="mercado-order-sync-scheduler",
            daemon=True,
        )
        thread.start()
        _scheduler_started = True
    return True
