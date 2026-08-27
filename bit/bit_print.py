"""Mercado Libre official-API order label printing.

The previous implementation drove the Global Selling web page through
BitBrowser and Selenium. This module intentionally has no browser dependency:
it synchronizes selected authorized stores through Mercado's orders API,
downloads official shipment-label PDFs, records each successful order, and
creates one combined PDF for the user to print.
"""

from __future__ import annotations

import argparse
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from bit import bit_mysql, bit_order_labels, mercado_tokens
from bit.bit_db_api import insert_task_record
from bit.bit_runtime_lock import InterProcessLock, get_lock_owner
from bit.bit_utils import get_now_time
from mercado_api.client import MercadoAPIError, MercadoLibreClient


ORDER_PRINT_LOCK_KEY = "bit_order_print_task"
DEFAULT_FALLBACK_HOURS = 72
ORDER_SCAN_OVERLAP_MINUTES = 5
ORDER_PRINT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "order_print"

SITE_IDS = {
    "墨西哥": "MLM",
    "巴西": "MLB",
    "智利": "MLC",
    "哥伦比亚": "MCO",
    "阿根廷": "MLA",
    "乌拉圭": "MLU",
}
SITE_NAMES = {site_id: name for name, site_id in SITE_IDS.items()}


class PrintTaskStopped(RuntimeError):
    """Raised only at a safe boundary after a stop request."""


def _now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _emit(logger: Callable[[str], None] | None, message: str):
    text = f"{get_now_time()} {message}"
    if logger is None:
        print(text)
    else:
        logger(text)


def _interruptible_wait(seconds, stop_event=None):
    seconds = max(0, float(seconds or 0))
    if not seconds:
        return False
    if stop_event is not None:
        return bool(stop_event.wait(seconds))
    time.sleep(seconds)
    return False


def acquire_order_print_lock(owner="bit_print", mode="once"):
    task_lock = InterProcessLock(
        ORDER_PRINT_LOCK_KEY,
        owner=owner,
        metadata={"mode": str(mode or "once"), "task_type": "order_print_api"},
    )
    return task_lock if task_lock.acquire(timeout=0) else None


def get_order_print_lock_owner():
    return get_lock_owner(ORDER_PRINT_LOCK_KEY)


def _normalized_selection(values: Iterable[str] | str | None):
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    return tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        )
    )


def _normalized_targets(targets):
    normalized = []
    seen = set()
    for target in targets or ():
        if isinstance(target, dict):
            shop_name = str(target.get("shop_name") or "").strip()
            site = str(target.get("site") or "").strip()
        elif isinstance(target, (list, tuple)) and len(target) >= 2:
            shop_name = str(target[0] or "").strip()
            site = str(target[1] or "").strip()
        else:
            continue
        key = (shop_name, site)
        if not shop_name or not site or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return tuple(normalized)


def _store_sites(row):
    settings = list(row.get("site_settings") or [])
    sites = []
    for setting in settings:
        site_id = str(setting.get("site_id") or "").strip().upper()
        site_name = str(setting.get("site_name") or SITE_NAMES.get(site_id) or "").strip()
        if site_name and site_name not in sites:
            sites.append(site_name)
    default_site = SITE_NAMES.get(str(row.get("site_id") or "").strip().upper())
    if default_site and default_site not in sites:
        sites.append(default_site)
    return sites or list(SITE_IDS)


def build_print_jobs(rows=None, selected_shops=None, selected_sites=None, selected_targets=None):
    """Build API jobs from Mercado token authorizations, not browser windows."""

    if rows is None:
        rows = (bit_mysql.list_mercado_store_tokens() or {}).get("rows") or []
    shop_filter = set(_normalized_selection(selected_shops))
    site_filter = set(_normalized_selection(selected_sites))
    target_filter = set(_normalized_targets(selected_targets))
    jobs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        token_id = int(row.get("id") or row.get("token_id") or 0)
        shop_name = str(
            row.get("display_name") or row.get("shop_name") or row.get("nickname") or ""
        ).strip()
        if not token_id or not shop_name or (shop_filter and shop_name not in shop_filter):
            continue
        sites = []
        for site in _store_sites(row):
            if target_filter:
                if (shop_name, site) not in target_filter:
                    continue
            elif site_filter and site not in site_filter:
                continue
            sites.append(site)
        if sites:
            jobs.append({"token_id": token_id, "shop_name": shop_name, "sites": sites})
    return jobs


def _as_utc(value, default=None):
    if value in (None, ""):
        return default
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return default
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_millis(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _token_expiring(record):
    expires_at = record.get("expires_at")
    if not expires_at:
        return False
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            return False
    now = datetime.now(expires_at.tzinfo) if expires_at.tzinfo else datetime.now()
    return expires_at <= now + timedelta(minutes=5)


def _refresh_store_token(token_id):
    mercado_tokens.refresh_and_save(
        int(token_id),
        get_token=bit_mysql.get_mercado_store_token,
        update_token=bit_mysql.update_mercado_store_token,
        record_error=bit_mysql.record_mercado_store_token_error,
    )
    return bit_mysql.get_mercado_store_token(int(token_id))


def _client_and_record(record):
    if _token_expiring(record) and record.get("refresh_token"):
        record = _refresh_store_token(record["id"])
    access_token = str(record.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("店铺授权缺少 Access Token")
    return MercadoLibreClient(access_token), record


def _is_unauthorized(exc):
    message = str(exc or "").lower()
    return "(401)" in message or " 401" in message or "invalid_token" in message


def _scan_store_orders(job, *, fallback_hours, stop_event=None, logger=None):
    """Refresh the local order snapshot directly from Mercado's orders API."""

    token_id = int(job["token_id"])
    record = bit_mysql.get_mercado_store_token(token_id)
    if not record:
        raise ValueError("店铺授权不存在或已被删除")
    seller_id = str(record.get("meli_user_id") or "").strip()
    if not seller_id:
        raise ValueError("店铺授权缺少 Seller ID，请刷新 Token 或重新授权")

    state = bit_mysql.get_mercado_order_print_state(token_id)
    now_utc = datetime.now(timezone.utc)
    first_run = not state
    tracking_since = (
        now_utc - timedelta(hours=max(1, int(fallback_hours or DEFAULT_FALLBACK_HOURS)))
        if first_run
        else _as_utc(state.get("tracking_since"), now_utc - timedelta(hours=fallback_hours))
    )
    last_scan_at = None if first_run else _as_utc(state.get("last_scan_at"))
    if last_scan_at:
        filters = {
            "sort": "updated_asc",
            "last_updated.from": _iso_millis(last_scan_at - timedelta(minutes=ORDER_SCAN_OVERLAP_MINUTES)),
            "last_updated.to": _iso_millis(now_utc),
        }
        scope_message = f"增量检查 {last_scan_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')} 之后更新的订单"
    else:
        filters = {
            "sort": "date_asc",
            "order.date_created.from": _iso_millis(tracking_since),
            "order.date_created.to": _iso_millis(now_utc),
        }
        scope_message = f"首次检查，按安全规则读取最近 {fallback_hours} 小时订单"
    _emit(logger, f"{job['shop_name']}：{scope_message}")

    refreshed = False
    while True:
        client, record = _client_and_record(record)
        try:
            batch = []
            fetched = inserted = updated = 0
            for order_id in client.iter_order_ids(seller_id, **filters):
                if stop_event is not None and stop_event.is_set():
                    raise PrintTaskStopped("已收到停止请求")
                batch.append(client.get_order(order_id))
                fetched += 1
                if len(batch) >= 50:
                    saved = bit_mysql.upsert_mercado_synced_orders(record, batch)
                    inserted += int(saved.get("inserted") or 0)
                    updated += int(saved.get("updated") or 0)
                    batch = []
            if batch:
                saved = bit_mysql.upsert_mercado_synced_orders(record, batch)
                inserted += int(saved.get("inserted") or 0)
                updated += int(saved.get("updated") or 0)
            bit_mysql.save_mercado_order_print_state(token_id, tracking_since, now_utc)
            _emit(
                logger,
                f"{job['shop_name']}：API 订单同步完成，读取 {fetched}，新增 {inserted}，更新 {updated}",
            )
            return {
                "record": record,
                "first_run": first_run,
                "tracking_since": tracking_since,
                "fetched": fetched,
            }
        except PrintTaskStopped:
            raise
        except MercadoAPIError as exc:
            if refreshed or not _is_unauthorized(exc) or not record.get("refresh_token"):
                raise
            _emit(logger, f"{job['shop_name']}：Access Token 已失效，正在刷新后重试")
            record = _refresh_store_token(token_id)
            refreshed = True


def _result_row(
    shop_name,
    site,
    status,
    message,
    attempts=0,
    selected_count=0,
    shipment_count=0,
    failed_count=0,
    fallback_used=False,
):
    return {
        "shop_name": str(shop_name or ""),
        "site": str(site or ""),
        "status": str(status or "failed"),
        "message": str(message or ""),
        "attempts": int(attempts or 0),
        "selected_count": int(selected_count or 0),
        "shipment_count": int(shipment_count or 0),
        "failed_count": int(failed_count or 0),
        "fallback_used": bool(fallback_used),
        "finished_at": _now_text(),
    }


def _download_label(context, *, max_retries, retry_delay_seconds, stop_event, logger):
    last_error = None
    for attempt in range(1, max(1, int(max_retries or 1)) + 1):
        if stop_event is not None and stop_event.is_set():
            raise PrintTaskStopped("已收到停止请求")
        try:
            shipment_id, content = bit_order_labels._download_one(context)
            return shipment_id, content, attempt
        except Exception as exc:
            last_error = exc
            order_id = str(context.get("order_id") or "")
            _emit(logger, f"订单 {order_id} 第 {attempt} 次读取面单失败：{exc}")
            if attempt < max(1, int(max_retries or 1)):
                if _interruptible_wait(retry_delay_seconds, stop_event):
                    raise PrintTaskStopped("已收到停止请求")
    raise bit_order_labels.MercadoLabelError(str(last_error or "面单下载失败"))


def _record_printed_orders(order_ids):
    normalized = list(dict.fromkeys(str(value) for value in order_ids if str(value or "").strip()))
    recorded = 0
    for offset in range(0, len(normalized), 100):
        recorded += bit_mysql.record_mercado_order_print_logs(
            normalized[offset : offset + 100], operator_name="订单打印/API"
        )
    return recorded


def _run_shop_job(
    job,
    *,
    max_retries=3,
    retry_delay_seconds=3,
    fallback_hours=DEFAULT_FALLBACK_HOURS,
    stop_event=None,
    logger=None,
    document_sink=None,
    printed_order_sink=None,
    **_legacy_options,
):
    """Generate labels for one API-authorized store and return per-site rows."""

    document_sink = document_sink if document_sink is not None else []
    printed_order_sink = printed_order_sink if printed_order_sink is not None else []
    shop_name = job["shop_name"]
    sites = list(job.get("sites") or SITE_IDS)
    try:
        scan = _scan_store_orders(
            job,
            fallback_hours=fallback_hours,
            stop_event=stop_event,
            logger=logger,
        )
        selected_site_ids = [SITE_IDS[site] for site in sites if site in SITE_IDS]
        contexts = bit_mysql.list_mercado_order_print_candidates(
            job["token_id"],
            tracking_since=scan["tracking_since"],
            site_ids=selected_site_ids,
            include_previously_printed=scan["first_run"],
        )
    except PrintTaskStopped:
        _emit(logger, f"{shop_name} 已在安全边界停止")
        return []
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        _emit(logger, f"{shop_name} API 订单读取失败：{message}")
        return [_result_row(shop_name, site, "failed", message) for site in sites]

    by_site = {site: [] for site in sites}
    for context in contexts:
        site_name = SITE_NAMES.get(str(context.get("site_id") or "").upper())
        if site_name in by_site:
            by_site[site_name].append(context)

    results = []
    downloaded_shipments = set()
    for site in sites:
        if stop_event is not None and stop_event.is_set():
            break
        site_contexts = by_site.get(site) or []
        if not site_contexts:
            results.append(
                _result_row(
                    shop_name,
                    site,
                    "no_orders",
                    "没有未打印订单",
                    fallback_used=scan["first_run"],
                )
            )
            continue

        shipments = {}
        for context in site_contexts:
            shipments.setdefault(str(context.get("shipping_id") or ""), []).append(context)
        successful_orders = []
        successful_shipments = 0
        failed_messages = []
        attempts = 0
        for shipment_id, shipment_orders in shipments.items():
            key = (int(job["token_id"]), shipment_id)
            if key in downloaded_shipments:
                successful_orders.extend(str(row.get("order_id") or "") for row in shipment_orders)
                continue
            try:
                _, content, used_attempts = _download_label(
                    shipment_orders[0],
                    max_retries=max_retries,
                    retry_delay_seconds=retry_delay_seconds,
                    stop_event=stop_event,
                    logger=logger,
                )
                attempts = max(attempts, used_attempts)
                downloaded_shipments.add(key)
                document_sink.append(content)
                successful_shipments += 1
                successful_orders.extend(str(row.get("order_id") or "") for row in shipment_orders)
            except PrintTaskStopped:
                break
            except Exception as exc:
                order_ids = "、".join(str(row.get("order_id") or "") for row in shipment_orders)
                failed_messages.append(f"{order_ids}: {exc}")

        if successful_orders:
            try:
                _record_printed_orders(successful_orders)
                printed_order_sink.extend(successful_orders)
            except Exception as exc:
                failed_messages.append(f"打印记录写入失败：{exc}")
        failed_count = max(0, len(shipments) - successful_shipments)
        if failed_messages:
            message = (
                f"已生成 {successful_shipments} 个面单，{failed_count} 个失败；"
                + "；".join(failed_messages[:3])
            )
            status = "failed"
        else:
            prefix = f"首次运行按最近 {fallback_hours} 小时回退；" if scan["first_run"] else ""
            message = f"{prefix}已通过 API 生成 {successful_shipments} 个面单"
            status = "printed"
        _emit(logger, f"{shop_name} / {site}：{message}")
        results.append(
            _result_row(
                shop_name,
                site,
                status,
                message,
                attempts=attempts,
                selected_count=len(site_contexts),
                shipment_count=successful_shipments,
                failed_count=failed_count,
                fallback_used=scan["first_run"],
            )
        )
    return results


def _task_record(result):
    if result["status"] == "printed":
        outcome = f"成功：API 生成 {result.get('shipment_count', 0)} 个面单"
    elif result["status"] == "no_orders":
        outcome = "成功：无待打印订单"
    elif result["status"] == "skipped":
        outcome = f"跳过：{result['message']}"
    else:
        outcome = f"失败：{result['message']}"
    return ("后台打印订单", result["shop_name"], result["site"], outcome, result["finished_at"])


def _write_output(documents, *, output_dir, task_id):
    if not documents:
        return None, None
    content = bit_order_labels._merge_pdfs(documents)
    if not content.startswith(b"%PDF"):
        raise RuntimeError("合并面单不是有效 PDF")
    directory = Path(output_dir or ORDER_PRINT_OUTPUT_DIR).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"mercado-api-labels-{timestamp}-{str(task_id)[:8]}.pdf"
    path = directory / filename
    path.write_bytes(content)
    return path, filename


def _summary(results, started_at, stopped=False):
    counts = {
        "printed": sum(result["status"] == "printed" for result in results),
        "no_orders": sum(result["status"] == "no_orders" for result in results),
        "failed": sum(result["status"] == "failed" for result in results),
        "skipped": sum(result["status"] == "skipped" for result in results),
    }
    return {
        "started_at": started_at,
        "finished_at": _now_text(),
        "stopped": bool(stopped),
        "total": len(results),
        **counts,
        "results": results,
    }


def print_orders_all(
    selected_shops=None,
    selected_sites=None,
    selected_targets=None,
    *,
    max_retries=3,
    retry_delay_seconds=3,
    fallback_hours=DEFAULT_FALLBACK_HOURS,
    stop_event=None,
    logger=None,
    persist=True,
    output_dir=None,
    task_id=None,
    **_legacy_options,
):
    """Run one API-only print round and create a combined official-label PDF."""

    started_at = _now_text()
    task_id = str(task_id or uuid.uuid4().hex)
    jobs = build_print_jobs(
        selected_shops=selected_shops,
        selected_sites=selected_sites,
        selected_targets=selected_targets,
    )
    if not jobs:
        raise ValueError("没有匹配的美客多授权店铺，请先完成店铺 API 授权")
    _emit(
        logger,
        f"开始 API 订单打印：{len(jobs)} 家授权店铺；只处理未打印订单，首次无法判断时回退最近 {fallback_hours} 小时",
    )
    results = []
    documents = []
    printed_order_ids = []
    for job in jobs:
        if stop_event is not None and stop_event.is_set():
            break
        results.extend(
            _run_shop_job(
                job,
                max_retries=max_retries,
                retry_delay_seconds=retry_delay_seconds,
                fallback_hours=fallback_hours,
                stop_event=stop_event,
                logger=logger,
                document_sink=documents,
                printed_order_sink=printed_order_ids,
            )
        )

    output_path, output_name = _write_output(
        documents, output_dir=output_dir, task_id=task_id
    )
    if persist and results:
        try:
            insert_task_record([_task_record(result) for result in results])
        except Exception as exc:
            _emit(logger, f"打印结果写入任务记录失败：{exc}")

    summary = _summary(
        results,
        started_at,
        stopped=bool(stop_event is not None and stop_event.is_set()),
    )
    summary.update(
        {
            "task_id": task_id,
            "download_path": str(output_path) if output_path else "",
            "download_name": output_name or "",
            "printed_order_count": len(set(printed_order_ids)),
            "shipment_count": len(documents),
            "fallback_store_count": len(
                {row["shop_name"] for row in results if row.get("fallback_used")}
            ),
        }
    )
    _emit(
        logger,
        "本轮 API 打印完成："
        f"订单 {summary['printed_order_count']}，面单 {summary['shipment_count']}，"
        f"无订单站点 {summary['no_orders']}，失败站点 {summary['failed']}",
    )
    return summary


def print_orders(shop_name, site=None):
    """Compatibility entry point using an authorized store display name."""

    summary = print_orders_all(
        selected_shops=[str(shop_name)],
        selected_sites=[str(site)] if site else None,
        max_retries=1,
    )
    return summary["failed"] == 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mercado Libre 官方 API 订单面单打印")
    parser.add_argument("--shop", action="append", default=[], help="指定授权店铺，可重复")
    parser.add_argument("--site", action="append", default=[], help="指定站点，可重复")
    parser.add_argument("--max-retries", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--retry-delay-seconds", type=int, default=3)
    args = parser.parse_args(argv)

    task_lock = acquire_order_print_lock(owner="bit_print.py", mode="once")
    if task_lock is None:
        raise RuntimeError(f"订单打印任务已在运行：{get_order_print_lock_owner()}")
    try:
        summary = print_orders_all(
            selected_shops=args.shop or None,
            selected_sites=args.site or None,
            max_retries=args.max_retries,
            retry_delay_seconds=max(0, args.retry_delay_seconds),
        )
        if summary.get("download_path"):
            print(f"合并面单：{summary['download_path']}")
    finally:
        task_lock.release()


if __name__ == "__main__":
    main()
