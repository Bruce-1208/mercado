"""Mercado Libre 订单手动回填、72 小时滚动同步与每日状态刷新。"""

from __future__ import annotations

import os
import json
import logging
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from bit import bit_mysql, mercado_tokens
from bit.bit_runtime_lock import InterProcessLock, get_lock_owner
from mercado_api.client import MercadoAPIError, MercadoLibreClient


ORDER_SYNC_LOCK_KEY = "mercado_order_sync_task"
DEFAULT_SYNC_INTERVAL_SECONDS = 15 * 60
RECENT_ORDER_WINDOW_HOURS = 72
WORKBENCH_LOCAL_TIMEZONE = timezone(timedelta(hours=8))
DAILY_STATUS_MODE = "daily_status"
DAILY_STATUS_STATE_KEY = "last_daily_old_order_status_refresh"

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
    "recent_window_hours": RECENT_ORDER_WINDOW_HOURS,
    "daily_status_last_run_date": "",
    "daily_status_run_date": "",
    "next_daily_status_at": "",
}
_scheduler_guard = threading.Lock()
_scheduler_started = False
_scheduler_stop_event = threading.Event()
_recent_sync_due_event = threading.Event()
_financial_backfill_guard = threading.Lock()
_financial_backfill_started = False
_financial_backfill_stop_event = threading.Event()
_image_backfill_guard = threading.Lock()
_image_backfill_started = False
_image_backfill_stop_event = threading.Event()


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
    def parse_value(value, label):
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"请选择有效的{label}")
        normalized = text.replace("T", " ")
        for date_format, precision in (
            ("%Y-%m-%d", "day"),
            ("%Y-%m-%d %H:%M", "minute"),
            ("%Y-%m-%d %H:%M:%S", "minute"),
        ):
            try:
                parsed = datetime.strptime(normalized, date_format)
                display = parsed.strftime(
                    "%Y-%m-%d" if precision == "day" else "%Y-%m-%dT%H:%M"
                )
                return parsed, precision, display
            except ValueError:
                continue
        raise ValueError(f"{label}必须使用 YYYY-MM-DD HH:MM 格式")

    start, start_precision, start_text = parse_value(start_date, "起始日期时间")
    end, end_precision, end_text = parse_value(end_date, "截止日期时间")
    if end < start:
        raise ValueError("截止日期时间不能早于起始日期时间")

    if start_precision == "day":
        start_at = start.replace(tzinfo=timezone.utc)
    else:
        start_at = start.replace(tzinfo=WORKBENCH_LOCAL_TIMEZONE).astimezone(timezone.utc)
    end_exclusive = end + (
        timedelta(days=1) if end_precision == "day" else timedelta(minutes=1)
    )
    if end_precision == "day":
        end_at = end_exclusive.replace(tzinfo=timezone.utc)
    else:
        end_at = end_exclusive.replace(
            tzinfo=WORKBENCH_LOCAL_TIMEZONE
        ).astimezone(timezone.utc)
    return start_text, end_text, start_at, end_at


def _iso_millis(value):
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _as_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("增量同步游标时间无效")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _daily_status_bootstrap_from(now_utc):
    state = bit_mysql.get_mercado_order_sync_schedule_state(DAILY_STATUS_STATE_KEY) or {}
    updated_at = state.get("updated_at")
    if isinstance(updated_at, str):
        try:
            updated_at = datetime.fromisoformat(updated_at)
        except ValueError:
            updated_at = None
    if isinstance(updated_at, datetime):
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=WORKBENCH_LOCAL_TIMEZONE)
        return updated_at.astimezone(timezone.utc)
    return now_utc - timedelta(days=1)


def _sync_mode(value):
    value = str(value or "").strip().lower()
    if value == DAILY_STATUS_MODE:
        return DAILY_STATUS_MODE
    return "automatic" if value == "automatic" else "manual"


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


def _parallel_api_results(
    client,
    values,
    callback,
    *,
    workers_env="MERCADO_API_BACKFILL_WORKERS",
    default_workers=6,
):
    """Run independent read-only Mercado API calls with one session per worker."""
    values = list(dict.fromkeys(values or ()))
    if not values:
        return {}
    try:
        configured_workers = int(os.environ.get(workers_env, str(default_workers)))
    except (TypeError, ValueError):
        configured_workers = default_workers
    max_workers = max(1, min(12, configured_workers, len(values)))
    worker_local = threading.local()

    def call(value):
        worker_client = getattr(worker_local, "client", None)
        if worker_client is None:
            if isinstance(client, MercadoLibreClient):
                worker_client = MercadoLibreClient(
                    client.access_token,
                    timeout=client.timeout,
                )
            else:
                worker_client = client
            worker_local.client = worker_client
        return callback(worker_client, value)

    if max_workers == 1:
        results = {}
        for value in values:
            try:
                results[value] = (call(value), None)
            except Exception as exc:
                results[value] = (None, exc)
        return results

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_values = {executor.submit(call, value): value for value in values}
        for future in as_completed(future_values):
            value = future_values[future]
            try:
                results[value] = (future.result(), None)
            except Exception as exc:
                results[value] = (None, exc)
    return results


def _is_interpreter_shutdown_error(exc):
    """Return whether a background worker raced with Python interpreter exit."""
    return sys.is_finalizing() or (
        isinstance(exc, RuntimeError)
        and "cannot schedule new futures after interpreter shutdown" in str(exc)
    )


def _fetch_orders(client, seller_id, filters):
    order_ids = list(client.iter_order_ids(str(seller_id), **filters))
    fetched = _parallel_api_results(
        client,
        order_ids,
        lambda worker, order_id: worker.get_order(order_id),
        workers_env="MERCADO_ORDER_STATUS_WORKERS",
        default_workers=8,
    )
    for order_id in order_ids:
        order, error = fetched.get(
            order_id,
            (None, RuntimeError("订单详情接口未返回结果")),
        )
        if error is not None:
            raise error
        yield order


def _item_image(item):
    pictures = item.get("pictures") or []
    first = pictures[0] if pictures and isinstance(pictures[0], dict) else {}
    image_url = str(first.get("secure_url") or first.get("url") or "").strip()
    if image_url:
        return image_url
    return str(item.get("secure_thumbnail") or item.get("thumbnail") or "").strip()


def _variation_image(item, variation_id):
    """Return the original-size picture assigned to the purchased variation."""

    variation_id = str(variation_id or "").strip()
    picture_ids = []
    if variation_id:
        for variation in item.get("variations") or []:
            if str(variation.get("id") or "") == variation_id:
                picture_ids = [
                    str(value or "").strip()
                    for value in (variation.get("picture_ids") or [])
                    if str(value or "").strip()
                ]
                break
    if picture_ids:
        pictures = {
            str(picture.get("id") or "").strip(): picture
            for picture in (item.get("pictures") or [])
            if isinstance(picture, dict)
        }
        for picture_id in picture_ids:
            picture = pictures.get(picture_id) or {}
            image_url = str(
                picture.get("secure_url") or picture.get("url") or ""
            ).strip()
            if image_url:
                return image_url
    return _item_image(item)


def _enrich_order_images(client, orders):
    item_targets = {}
    for order in orders or []:
        for order_item in order.get("order_items") or []:
            product = order_item.get("item") if isinstance(order_item.get("item"), dict) else {}
            item_id = str(product.get("id") or "").strip()
            if item_id:
                item_targets.setdefault(item_id, []).append(product)
    for item_id, products in item_targets.items():
        try:
            listing = client.get_marketplace_item(item_id)
        except Exception as exc:
            _append_log(f"SKU {item_id} 图片读取失败：{exc}")
            continue
        for product in products:
            image_url = _variation_image(listing, product.get("variation_id"))
            if not image_url:
                continue
            product["sku_image_url"] = image_url
            product["secure_thumbnail"] = image_url


def _shipment_sender_cost(payload):
    senders = payload.get("senders") if isinstance(payload, dict) else None
    if not isinstance(senders, list) or not senders:
        raise ValueError("运单成本响应缺少 senders")
    total = Decimal("0")
    for sender in senders:
        try:
            total += Decimal(str((sender or {}).get("cost") or 0))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("运单成本响应包含无效的 sender.cost") from exc
    return max(Decimal("0"), total)


def _cached_cost_entry(row):
    payload = row.get("payload_json") or "{}"
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    return {
        "shipping_id": str(row.get("shipping_id") or ""),
        "seller_cost": row.get("seller_cost"),
        "currency_id": str(row.get("currency_id") or "").upper(),
        "payload": payload,
        "checked_at": row.get("checked_at"),
        "error": str(row.get("last_error") or ""),
    }


def _cache_is_fresh(entry, hours=24):
    checked_at = (entry or {}).get("checked_at")
    if isinstance(checked_at, str):
        try:
            checked_at = datetime.fromisoformat(checked_at)
        except ValueError:
            return False
    return isinstance(checked_at, datetime) and checked_at >= datetime.now() - timedelta(hours=hours)


def _sync_order_financials(
    client,
    record,
    orders,
    shipment_cache=None,
    *,
    force_refresh=False,
):
    """Fetch official shipment costs and allocate each cost across merged orders."""
    shipment_cache = shipment_cache if isinstance(shipment_cache, dict) else {}
    shipping_ids = list(dict.fromkeys(
        str(((order or {}).get("shipping") or {}).get("id") or "").strip()
        for order in orders or ()
        if str(((order or {}).get("shipping") or {}).get("id") or "").strip()
    ))
    if not shipping_ids:
        return {"shipments": 0, "orders": 0, "failed": 0}

    unknown_ids = [value for value in shipping_ids if value not in shipment_cache]
    database_cache = bit_mysql.list_mercado_shipment_cost_cache(unknown_ids)
    entries = []
    failed = 0
    fetch_metadata = {}
    for shipping_id in shipping_ids:
        cached = shipment_cache.get(shipping_id)
        if cached is None and shipping_id in database_cache:
            cached = _cached_cost_entry(database_cache[shipping_id])
        cached_success = bool(
            cached
            and cached.get("seller_cost") is not None
            and cached.get("currency_id")
        )
        if cached and not force_refresh and _cache_is_fresh(cached):
            shipment_cache[shipping_id] = cached
            if cached_success:
                entries.append(cached)
            else:
                failed += 1
            continue
        fetch_metadata[shipping_id] = (cached, cached_success)

    fetched_costs = _parallel_api_results(
        client,
        list(fetch_metadata),
        lambda worker, shipping_id: worker.get_shipment_costs(shipping_id),
    )
    for shipping_id, (cached, cached_success) in fetch_metadata.items():
        payload, fetch_error = fetched_costs.get(
            shipping_id,
            (None, RuntimeError("运单成本接口未返回结果")),
        )
        try:
            if fetch_error is not None:
                raise fetch_error
            currency_id = str(payload.get("currency_id") or "").strip().upper()
            if not currency_id:
                raise ValueError("运单成本响应缺少 currency_id")
            entry = {
                "shipping_id": shipping_id,
                "seller_cost": _shipment_sender_cost(payload),
                "currency_id": currency_id,
                "payload": payload,
                "checked_at": datetime.now(),
                "error": "",
            }
        except Exception as exc:
            failed += 1
            _append_log(f"Shipment {shipping_id} 运费读取失败：{exc}")
            if cached_success:
                entry = {
                    **cached,
                    "checked_at": datetime.now(),
                    "error": str(exc),
                }
            else:
                entry = {
                    "shipping_id": shipping_id,
                    "seller_cost": None,
                    "currency_id": "",
                    "payload": {},
                    "checked_at": datetime.now(),
                    "error": str(exc),
                }
        shipment_cache[shipping_id] = entry
        entries.append(entry)
    saved = bit_mysql.save_mercado_shipment_costs(int(record["id"]), entries)
    return {**saved, "failed": failed}


def backfill_order_images(token_ids=None, limit_per_store=200):
    results = []
    for record in _token_records(token_ids):
        client, record = _client_and_token(record)
        token_id = int(record["id"])
        rows = bit_mysql.list_mercado_missing_order_images(token_id, limit_per_store)
        updated_products = updated_orders = failed = 0
        listing_cache = {}
        for row in rows:
            item_id = str(row.get("product_id") or "").strip()
            try:
                raw_order = row.get("raw_json") or {}
                if isinstance(raw_order, str):
                    raw_order = json.loads(raw_order or "{}")
                order_items = raw_order.get("order_items") or []
                first_product = (
                    (order_items[0].get("item") or {}) if order_items else {}
                )
                if item_id not in listing_cache:
                    listing_cache[item_id] = client.get_marketplace_item(item_id)
                image_url = _variation_image(
                    listing_cache[item_id],
                    first_product.get("variation_id"),
                )
                if not image_url:
                    failed += 1
                    continue
                updated_orders += bit_mysql.update_mercado_order_image(
                    token_id,
                    row.get("order_id"),
                    image_url,
                )
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


def backfill_order_sku_images(limit=50):
    """Fill original purchased-variation images for historical orders."""
    pending = bit_mysql.list_mercado_pending_order_image_rows(limit=limit)
    grouped = {}
    for row in pending:
        grouped.setdefault(int(row["token_id"]), []).append(row)
    checked = updated = failed = 0
    for token_id, rows in grouped.items():
        record = bit_mysql.get_mercado_store_token(token_id)
        results = []
        if not record:
            results = [
                {
                    "order_id": row.get("order_id"),
                    "token_id": token_id,
                    "error": "店铺授权不存在",
                }
                for row in rows
            ]
        else:
            try:
                client, record = _client_and_token(record)
                product_ids = [
                    str(row.get("product_id") or "").strip()
                    for row in rows
                    if str(row.get("product_id") or "").strip()
                ]
                listing_results = _parallel_api_results(
                    client,
                    product_ids,
                    lambda worker, product_id: worker.get_marketplace_item(product_id),
                )
                for row in rows:
                    order_id = str(row.get("order_id") or "")
                    product_id = str(row.get("product_id") or "").strip()
                    try:
                        raw_order = row.get("raw_json") or {}
                        if isinstance(raw_order, str):
                            raw_order = json.loads(raw_order or "{}")
                        if not isinstance(raw_order, dict):
                            raise ValueError("订单原始数据无效")
                        products = [
                            item.get("item")
                            for item in (raw_order.get("order_items") or [])
                            if isinstance(item, dict) and isinstance(item.get("item"), dict)
                        ]
                        product = next(
                            (
                                value for value in products
                                if str(value.get("id") or "") == product_id
                            ),
                            products[0] if products else None,
                        )
                        if not product:
                            raise ValueError("订单缺少 SKU 数据")
                        listing, listing_error = listing_results.get(
                            product_id,
                            (None, RuntimeError("商品接口未返回结果")),
                        )
                        if listing_error is not None:
                            raise listing_error
                        image_url = _variation_image(
                            listing,
                            product.get("variation_id"),
                        )
                        if not image_url:
                            raise ValueError("商品接口未返回 SKU 图片")
                        product["sku_image_url"] = image_url
                        product["secure_thumbnail"] = image_url
                        results.append({
                            "order_id": order_id,
                            "token_id": token_id,
                            "image_url": image_url,
                            "raw_order": raw_order,
                            "error": "",
                        })
                    except Exception as exc:
                        results.append({
                            "order_id": order_id,
                            "token_id": token_id,
                            "error": str(exc),
                        })
            except Exception as exc:
                results = [
                    {
                        "order_id": row.get("order_id"),
                        "token_id": token_id,
                        "error": str(exc),
                    }
                    for row in rows
                ]
        saved = bit_mysql.save_mercado_order_image_results(results)
        checked += int(saved.get("checked") or 0)
        updated += int(saved.get("updated") or 0)
        failed += int(saved.get("failed") or 0)
    return {
        "requested": len(pending),
        "checked": checked,
        "updated": updated,
        "failed": failed,
    }


def _image_backfill_loop():
    lock = InterProcessLock(
        "mercado_order_image_backfill",
        owner="bit_order_sync",
        metadata={"task": "order_sku_image_backfill"},
    )
    while not _image_backfill_stop_event.is_set():
        if not lock.acquire(timeout=0):
            _image_backfill_stop_event.wait(60)
            continue
        try:
            result = backfill_order_sku_images(limit=50)
            if result["requested"]:
                logging.info(
                    "订单 SKU 图补全：检查 %s，成功 %s，失败 %s",
                    result["checked"], result["updated"], result["failed"],
                )
        except Exception:
            logging.exception("历史订单 SKU 图补全任务失败")
            result = {"requested": 0}
        finally:
            lock.release()
        wait_seconds = 2 if int(result.get("requested") or 0) >= 50 else 6 * 60 * 60
        _image_backfill_stop_event.wait(wait_seconds)


def ensure_order_image_backfill_worker():
    global _image_backfill_started
    if str(os.environ.get("MERCADO_ORDER_IMAGE_BACKFILL_DISABLED") or "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return False
    with _image_backfill_guard:
        if _image_backfill_started:
            return True
        thread = threading.Thread(
            target=_image_backfill_loop,
            name="mercado-order-image-backfill",
            daemon=True,
        )
        thread.start()
        _image_backfill_started = True
    return True


def backfill_order_financials(limit=200):
    """Fill official shipment costs for existing orders, newest shipments first."""
    pending = bit_mysql.list_mercado_pending_shipment_cost_rows(limit=limit)
    grouped = {}
    for row in pending:
        grouped.setdefault(int(row["token_id"]), []).append(str(row["shipping_id"]))
    processed = failed = updated_orders = 0
    for token_id, shipping_ids in grouped.items():
        record = bit_mysql.get_mercado_store_token(token_id)
        if not record:
            failed += len(shipping_ids)
            continue
        try:
            client, record = _client_and_token(record)
            result = _sync_order_financials(
                client,
                record,
                [{"shipping": {"id": value}} for value in shipping_ids],
                {},
                force_refresh=True,
            )
            processed += len(shipping_ids)
            failed += int(result.get("failed") or 0)
            updated_orders += int(result.get("orders") or 0)
        except Exception as exc:
            if _is_interpreter_shutdown_error(exc):
                raise
            failed += len(shipping_ids)
            logging.exception("店铺 %s 历史订单费用补全失败", token_id)
    return {
        "requested": len(pending),
        "processed": processed,
        "failed": failed,
        "updated_orders": updated_orders,
    }


def _financial_backfill_loop():
    lock = InterProcessLock(
        "mercado_order_financial_backfill",
        owner="bit_order_sync",
        metadata={"task": "shipment_cost_backfill"},
    )
    try:
        fee_result = bit_mysql.backfill_mercado_order_sale_fees()
        if fee_result.get("updated"):
            logging.info("历史订单手续费补算：%s 笔", fee_result["updated"])
    except Exception:
        logging.exception("历史订单手续费补算失败")
    while not _financial_backfill_stop_event.is_set():
        if not lock.acquire(timeout=0):
            _financial_backfill_stop_event.wait(60)
            continue
        try:
            result = backfill_order_financials(limit=200)
            quoted_result = bit_mysql.refresh_mercado_order_quoted_freight(limit=200)
            if result["requested"]:
                logging.info(
                    "订单实际运费每日同步：运单 %s，订单 %s，失败 %s",
                    result["processed"], result["updated_orders"], result["failed"],
                )
            if quoted_result["requested"]:
                logging.info(
                    "订单标价运费计算：运单 %s，命中 %s，缺少资料 %s，订单 %s",
                    quoted_result["requested"],
                    quoted_result["quoted_shipments"],
                    quoted_result["missing_shipments"],
                    quoted_result["updated_orders"],
                )
        except Exception as exc:
            if _is_interpreter_shutdown_error(exc):
                logging.info("服务进程正在退出，停止历史订单费用补全")
                return
            logging.exception("历史订单手续费、运费补全任务失败")
            result = {"requested": 0}
            quoted_result = {"requested": 0}
        finally:
            lock.release()
        has_full_batch = (
            int(result.get("requested") or 0) >= 200
            or int(quoted_result.get("requested") or 0) >= 200
        )
        wait_seconds = 2 if has_full_batch else 6 * 60 * 60
        _financial_backfill_stop_event.wait(wait_seconds)


def ensure_order_financial_backfill_worker():
    global _financial_backfill_started
    if str(os.environ.get("MERCADO_ORDER_FINANCIAL_BACKFILL_DISABLED") or "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return False
    with _financial_backfill_guard:
        if _financial_backfill_started:
            return True
        thread = threading.Thread(
            target=_financial_backfill_loop,
            name="mercado-order-financial-backfill",
            daemon=True,
        )
        thread.start()
        _financial_backfill_started = True
    return True


def _sync_store(record, filters, *, enrich_images=True):
    token_id = int(record["id"])
    display_name = str(record.get("display_name") or record.get("nickname") or token_id)
    seller_id = str(record.get("meli_user_id") or "").strip()
    if not seller_id:
        raise ValueError("店铺授权缺少 Seller ID，请刷新 Token 或重新授权")

    client, record = _client_and_token(record)
    refreshed_after_unauthorized = False
    shipment_cost_cache = {}
    while True:
        try:
            orders = _fetch_orders(client, seller_id, filters)
            batch = []
            totals = {"fetched": 0, "inserted": 0, "updated": 0}
            for order in orders:
                batch.append(order)
                totals["fetched"] += 1
                if len(batch) >= 50:
                    if enrich_images:
                        _enrich_order_images(client, batch)
                    result = bit_mysql.upsert_mercado_synced_orders(record, batch)
                    _sync_order_financials(client, record, batch, shipment_cost_cache)
                    totals["inserted"] += int(result.get("inserted") or 0)
                    totals["updated"] += int(result.get("updated") or 0)
                    batch = []
                    _state_update(
                        fetched_count=int(_sync_state.get("fetched_count") or 0) + 50,
                        inserted_count=int(_sync_state.get("inserted_count") or 0) + int(result.get("inserted") or 0),
                        updated_count=int(_sync_state.get("updated_count") or 0) + int(result.get("updated") or 0),
                    )
            if batch:
                if enrich_images:
                    _enrich_order_images(client, batch)
                result = bit_mysql.upsert_mercado_synced_orders(record, batch)
                _sync_order_financials(client, record, batch, shipment_cost_cache)
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


def _sync_old_store_statuses(
    record,
    cutoff,
    *,
    run_date,
    default_from,
    window_to,
):
    """Incrementally refresh old orders and checkpoint every search page."""

    token_id = int(record["id"])
    display_name = str(record.get("display_name") or record.get("nickname") or token_id)
    seller_id = str(record.get("meli_user_id") or "").strip()
    if not seller_id:
        raise ValueError("店铺授权缺少 Seller ID，请刷新 Token 或重新授权")
    checkpoint = bit_mysql.begin_mercado_order_status_window(
        token_id,
        run_date,
        _as_utc(default_from).replace(tzinfo=None),
        _as_utc(window_to).replace(tzinfo=None),
    )
    if (
        str(checkpoint.get("run_date") or "") == str(run_date)
        and int(checkpoint.get("completed_for_run") or 0)
    ):
        return {
            "store": display_name,
            "status": "success",
            "fetched": 0,
            "inserted": 0,
            "updated": 0,
            "failed": 0,
            "skipped": True,
        }

    client, record = _client_and_token(record)
    offset = max(0, int(checkpoint.get("next_offset") or 0))
    checked_count = max(0, int(checkpoint.get("checked_count") or 0))
    saved_count = max(0, int(checkpoint.get("updated_count") or 0))
    failed_count = max(0, int(checkpoint.get("failed_count") or 0))
    totals = {"fetched": 0, "inserted": 0, "updated": 0, "failed": 0}
    filters = {
        "sort": "date_asc",
        "last_updated.from": _iso_millis(_as_utc(checkpoint["window_from"])),
        "last_updated.to": _iso_millis(_as_utc(checkpoint["window_to"])),
        "date_created.to": _iso_millis(_as_utc(cutoff)),
    }

    while True:
        if _recent_sync_due_event.is_set():
            return {"store": display_name, "status": "paused", "yielded": True, **totals}
        page = client.search_order_ids_page(
            seller_id,
            offset=offset,
            limit=50,
            **filters,
        )
        order_ids = list(page.get("order_ids") or [])
        details = _parallel_api_results(
            client,
            order_ids,
            lambda worker, order_id: worker.get_order(order_id),
            workers_env="MERCADO_ORDER_STATUS_WORKERS",
            default_workers=8,
        )
        orders = []
        page_failed = 0
        for order_id in order_ids:
            order, error = details.get(
                order_id,
                (None, RuntimeError("订单详情接口未返回结果")),
            )
            if error is not None:
                page_failed += 1
                _append_log(f"{display_name} 订单 {order_id} 状态读取失败：{error}")
                continue
            orders.append(order)

        result = (
            bit_mysql.upsert_mercado_synced_orders(record, orders)
            if orders else {"inserted": 0, "updated": 0}
        )
        batch_size = len(orders)
        batch_inserted = int(result.get("inserted") or 0)
        batch_updated = int(result.get("updated") or 0)
        totals["fetched"] += batch_size
        totals["inserted"] += batch_inserted
        totals["updated"] += batch_updated
        totals["failed"] += page_failed
        checked_count += len(order_ids)
        saved_count += batch_size
        failed_count += page_failed
        offset = int(page.get("next_offset") or offset)
        bit_mysql.checkpoint_mercado_order_status_window(
            token_id,
            offset,
            checked_count,
            saved_count,
            failed_count,
        )
        _state_update(
            fetched_count=int(_sync_state.get("fetched_count") or 0) + batch_size,
            inserted_count=int(_sync_state.get("inserted_count") or 0) + batch_inserted,
            updated_count=int(_sync_state.get("updated_count") or 0) + batch_updated,
        )

        if not int(page.get("result_count") or 0) or offset >= int(page.get("total") or 0):
            bit_mysql.complete_mercado_order_status_window(token_id)
            return {
                "store": display_name,
                "status": "success" if totals["failed"] == 0 else "error",
                **totals,
            }
        if _recent_sync_due_event.is_set():
            return {"store": display_name, "status": "paused", "yielded": True, **totals}


def run_order_sync(start_date="", end_date="", token_ids=None, mode="manual"):
    mode = _sync_mode(mode)
    records = _token_records(token_ids)
    start_text = end_text = ""
    manual_filters = None
    scheduled_filters = None
    daily_context = None
    now_utc = datetime.now(timezone.utc)
    if mode == "manual":
        start_text, end_text, start_at, end_at = _date_range(start_date, end_date)
        manual_filters = {
            "sort": "date_asc",
            "order.date_created.from": _iso_millis(start_at),
            "order.date_created.to": _iso_millis(end_at),
        }
    elif mode == "automatic":
        recent_start = now_utc - timedelta(hours=RECENT_ORDER_WINDOW_HOURS)
        start_text = recent_start.astimezone(WORKBENCH_LOCAL_TIMEZONE).strftime(
            "%Y-%m-%dT%H:%M"
        )
        end_text = now_utc.astimezone(WORKBENCH_LOCAL_TIMEZONE).strftime(
            "%Y-%m-%dT%H:%M"
        )
        scheduled_filters = {
            "sort": "date_asc",
            "order.date_created.from": _iso_millis(recent_start),
            "order.date_created.to": _iso_millis(now_utc),
        }
    else:
        old_order_cutoff = now_utc - timedelta(hours=RECENT_ORDER_WINDOW_HOURS)
        daily_run_date = now_utc.astimezone(WORKBENCH_LOCAL_TIMEZONE).date().isoformat()
        end_text = old_order_cutoff.astimezone(WORKBENCH_LOCAL_TIMEZONE).strftime(
            "%Y-%m-%dT%H:%M"
        )
        scheduled_filters = {"old_order_cutoff": old_order_cutoff}
        daily_context = {
            "run_date": daily_run_date,
            "default_from": _daily_status_bootstrap_from(now_utc),
            "window_to": now_utc,
        }

    mode_messages = {
        "manual": f"正在拉取订单：{start_text} 至 {end_text}",
        "automatic": "正在更新最近 72 小时订单",
        DAILY_STATUS_MODE: "正在执行每日老订单状态刷新",
    }

    _state_update(
        running=True,
        mode=mode,
        status="running",
        message=mode_messages[mode],
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
        daily_status_run_date=(daily_context or {}).get("run_date", ""),
        results=[],
        logs=[],
    )
    if mode == "automatic":
        _append_log(f"十五分钟任务启动：更新最近 {RECENT_ORDER_WINDOW_HOURS} 小时，共 {len(records)} 家店铺")
    elif mode == DAILY_STATUS_MODE:
        _append_log(f"每日老订单状态刷新启动：更新 72 小时以前订单，共 {len(records)} 家店铺")
    else:
        _append_log(
            f"手动订单任务启动：北京时间 {start_text} 至 {end_text}，"
            f"共 {len(records)} 家店铺"
        )
    results = []
    paused_for_recent = False
    for index, record in enumerate(records, start=1):
        if mode == DAILY_STATUS_MODE and _recent_sync_due_event.is_set():
            paused_for_recent = True
            break
        store_name = str(record.get("display_name") or record.get("nickname") or record.get("id"))
        _state_update(current_store=store_name, processed_stores=index - 1)
        _append_log(f"开始同步 {store_name}")
        try:
            filters = dict(manual_filters or scheduled_filters or {})
            if mode == DAILY_STATUS_MODE:
                result = _sync_old_store_statuses(
                    record,
                    filters["old_order_cutoff"],
                    **daily_context,
                )
            else:
                result = _sync_store(record, filters, enrich_images=True)
            results.append(result)
            if result.get("yielded"):
                paused_for_recent = True
                _append_log(f"{store_name} 已保存增量断点，让出执行权给最近 72 小时任务")
                _state_update(processed_stores=index - 1, results=list(results))
                break
            _append_log(
                f"{store_name} 完成：读取 {result['fetched']}，新增 {result['inserted']}，"
                f"更新 {result['updated']}，失败 {result.get('failed', 0)}"
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

    if paused_for_recent:
        message = "每日老订单增量刷新已保存断点，正在让出执行权给十五分钟任务"
        _state_update(
            running=False,
            status="paused",
            message=message,
            current_store="",
            finished_at=_now_text(),
            results=list(results),
        )
        _append_log(message)
        return order_sync_status()

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
        with _state_guard:
            final_status = str(_sync_state.get("status") or "")
            daily_run_date = str(_sync_state.get("daily_status_run_date") or "")
        if mode == DAILY_STATUS_MODE and final_status in ("completed", "partial"):
            run_date = str(
                daily_run_date
                or datetime.now(WORKBENCH_LOCAL_TIMEZONE).date().isoformat()
            )
            bit_mysql.set_mercado_order_sync_schedule_value(
                DAILY_STATUS_STATE_KEY,
                run_date,
            )
            _state_update(daily_status_last_run_date=run_date)
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
    mode = _sync_mode(mode)
    if mode == "manual":
        _date_range(start_date, end_date)
    selected_ids = _token_ids(token_ids)
    if selected_ids:
        _token_records(selected_ids)
    with _state_guard:
        if _sync_state.get("running"):
            return False, order_sync_status()
        task_id = uuid.uuid4().hex
        _sync_state.update(
            running=True,
            task_id=task_id,
            mode=mode,
            status="starting",
            message=(
                "正在启动每日老订单状态刷新"
                if mode == DAILY_STATUS_MODE
                else "正在启动订单同步"
            ),
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
    next_recent_run = time.monotonic() + interval_seconds
    while not _scheduler_stop_event.is_set():
        now_local = datetime.now(WORKBENCH_LOCAL_TIMEZONE)
        today = now_local.date().isoformat()
        schedule_state_available = True
        try:
            last_daily_date = bit_mysql.get_mercado_order_sync_schedule_value(
                DAILY_STATUS_STATE_KEY
            )
        except Exception:
            schedule_state_available = False
            last_daily_date = str(_sync_state.get("daily_status_last_run_date") or "")
        daily_due = schedule_state_available and last_daily_date != today
        lock_owner = get_lock_owner(ORDER_SYNC_LOCK_KEY)
        task_busy = bool(_sync_state.get("running") or lock_owner)
        recent_due = time.monotonic() >= next_recent_run
        active_mode = str(
            ((lock_owner or {}).get("metadata") or {}).get("mode")
            or _sync_state.get("mode")
            or ""
        )
        if recent_due and task_busy:
            if active_mode == DAILY_STATUS_MODE:
                _recent_sync_due_event.set()
        elif recent_due and not task_busy:
            _recent_sync_due_event.clear()
            started, _state = start_order_sync(mode="automatic")
            if started:
                next_recent_run = time.monotonic() + interval_seconds
        elif daily_due and not task_busy:
            _recent_sync_due_event.clear()
            start_order_sync(mode=DAILY_STATUS_MODE)

        seconds_to_recent = max(0, int(next_recent_run - time.monotonic()))
        next_recent_at = datetime.now() + timedelta(seconds=seconds_to_recent)
        next_daily_at = (
            now_local
            if daily_due
            else datetime.combine(
                now_local.date() + timedelta(days=1),
                datetime.min.time(),
                tzinfo=WORKBENCH_LOCAL_TIMEZONE,
            )
        )
        _state_update(
            scheduler_enabled=True,
            sync_interval_seconds=interval_seconds,
            recent_window_hours=RECENT_ORDER_WINDOW_HOURS,
            next_run_at=next_recent_at.strftime("%Y-%m-%d %H:%M:%S"),
            daily_status_last_run_date=last_daily_date,
            next_daily_status_at=next_daily_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if _scheduler_stop_event.wait(min(30, max(1, seconds_to_recent))):
            break


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
