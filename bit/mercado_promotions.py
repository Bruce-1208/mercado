"""Promotion synchronization and guarded batch enrollment orchestration."""

from __future__ import annotations

import json
import logging
import math
import os
import queue
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from bit import bit_mysql
from bit.bit_store_link_sync import _client_and_token, _marketplace_accounts, _refresh_token
from bit.bit_runtime_lock import InterProcessLock
from erp.mercadolibre_promotion_store import (
    PromotionStore, APPLIED_STATUSES, snapshot_started_after,
)
from mercado_api.client import MercadoAPIError
from mercado_api.promotions import MercadoPromotionsClient, WRITABLE_TYPES, WITHDRAWABLE_TYPES, OFFER_ENROLLMENT_TYPES


_sync_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_summaries() -> list[dict[str, Any]]:
    return list((bit_mysql.list_mercado_store_tokens() or {}).get("rows") or [])


def visible_store_options(allowed_token_ids: Iterable[int] | None = None) -> list[dict[str, Any]]:
    allowed = None if allowed_token_ids is None else {int(value) for value in allowed_token_ids}
    result = []
    for token in _token_summaries():
        token_id = int(token.get("id") or 0)
        if not token_id or (allowed is not None and token_id not in allowed):
            continue
        sites = []
        for setting in token.get("site_settings") or ():
            site_id = str(setting.get("site_id") or "").strip().upper()
            if site_id:
                sites.append({
                    "site_id": site_id,
                    "salesperson": str(setting.get("salesperson") or ""),
                    "group_name": str(setting.get("group_name") or ""),
                })
        result.append({
            "id": token_id,
            "name": str(token.get("display_name") or token.get("nickname") or f"店铺 {token_id}"),
            "enabled": bool(token.get("enabled", True)),
            "sites": sites,
        })
    return result


def _adapter(token: dict[str, Any]) -> tuple[MercadoPromotionsClient, dict[str, Any]]:
    client, token = _client_and_token(token)
    token_id = int(token.get("id") or 0)
    if token_id:
        def refresh_from_store():
            # Long jobs outlive access tokens. Reuse a token rotated by another
            # worker, otherwise refresh through the shared persisted OAuth path.
            lease = InterProcessLock(f"promotion_token_refresh_{token_id}", owner="promotion-auth")
            if not lease.acquire(timeout=30):
                raise MercadoAPIError("店铺授权正在刷新，请稍后继续")
            try:
                saved = dict(bit_mysql.get_mercado_store_token(token_id) or {})
                if not saved.get("access_token") or saved["access_token"] == client.access_token:
                    saved = _refresh_token(token_id)
                access_token = str(saved.get("access_token") or "").strip()
                if not access_token:
                    raise MercadoAPIError("店铺授权刷新失败，请重新授权后继续")
                client.access_token = access_token
            finally:
                lease.release()
        client._refresh_access_token = refresh_from_store
    caller_id = str(token.get("meli_user_id") or "")
    return MercadoPromotionsClient(
        client,
        client_id=str(token.get("client_id") or ""),
        caller_id=caller_id,
    ), token


def sync_promotions(
    token_ids: Iterable[int],
    *,
    store: PromotionStore | None = None,
) -> dict[str, Any]:
    """Synchronously refresh selected stores without deleting data on partial failure."""
    ids = list(dict.fromkeys(int(value) for value in token_ids if int(value or 0) > 0))
    if not ids:
        raise ValueError("请至少选择一个店铺")
    if not _sync_lock.acquire(blocking=False):
        raise ValueError("已有活动同步正在执行，请稍后再试")
    store = store or PromotionStore()
    results, total_promotions, total_items = [], 0, 0
    try:
        for token_id in ids:
            summary = next((row for row in _token_summaries() if int(row.get("id") or 0) == token_id), None)
            if not summary:
                results.append({"token_id": token_id, "status": "failed", "message": "店铺授权不存在"})
                continue
            if not bool(summary.get("enabled", True)):
                results.append({"token_id": token_id, "status": "failed", "message": "店铺已关闭"})
                continue
            store_name = str(summary.get("display_name") or summary.get("nickname") or token_id)
            try:
                secret = dict(bit_mysql.get_mercado_store_token(token_id) or {})
                adapter, secret = _adapter(secret)
                accounts = _marketplace_accounts(adapter.client, str(secret.get("meli_user_id") or ""))
                promotion_count = item_count = 0
                product_cache = {}
                for account in accounts:
                    user_id = str(account.get("user_id") or "")
                    site_id = str(account.get("site_id") or "").upper()
                    store.record_scope_sync(token_id, site_id, 'running')
                    site_setting = next(
                        (
                            setting for setting in summary.get("site_settings") or ()
                            if str(setting.get("site_id") or "").strip().upper() == site_id
                        ),
                        {},
                    )
                    seen_promotion_ids = []
                    for promotion in adapter.iter_promotions(user_id):
                        local_id = store.upsert_promotion(
                            token_id=token_id,
                            store_name=store_name,
                            salesperson=str(site_setting.get("salesperson") or ""),
                            group_name=str(site_setting.get("group_name") or ""),
                            application_id=str(secret.get("application_id") or ""),
                            seller_id=user_id,
                            site_id=site_id,
                            row=promotion,
                        )
                        promotion_count += 1
                        promotion_id = str(promotion.get("id") or "").strip()
                        seen_promotion_ids.append(promotion_id)
                        if not promotion_id:
                            continue
                        # Replace only after the iterator finishes completely.
                        snapshot_started_at = _now().isoformat()
                        items = list(adapter.iter_items(user_id, promotion_id))
                        # Promotion responses may omit product names and images.
                        for item in items:
                            item_id = str(item.get("id") or item.get("item_id") or "")
                            net = item.get("net_proceeds") or {}
                            has_original_net = isinstance(net, dict) and net.get("original_amount") is not None
                            if not item_id or (item.get("title") and item.get("thumbnail") and has_original_net):
                                continue
                            if item_id not in product_cache:
                                try:
                                    product_cache[item_id] = adapter.client.get_marketplace_item(item_id)
                                except Exception:
                                    product_cache[item_id] = {}
                            product = product_cache[item_id] or {}
                            pictures = product.get("pictures") or []
                            main_image = pictures[0] if pictures else {}
                            item["title"] = item.get("title") or product.get("title") or ""
                            item["thumbnail"] = (item.get("thumbnail") or item.get("thumbnail_url")
                                                 or main_image.get("secure_url") or main_image.get("url")
                                                 or product.get("secure_thumbnail") or product.get("thumbnail") or "")
                            if product.get("net_proceeds"):
                                item["_listing_snapshot"] = {
                                    "price": product.get("price"), "currency_id": product.get("currency_id"),
                                    "net_proceeds": product["net_proceeds"],
                                }
                        item_count += store.replace_items(
                            local_id, items, snapshot_started_at=snapshot_started_at,
                        )
                        store.reconcile_completed_jobs(local_id, items, snapshot_started_at=snapshot_started_at)
                    store.record_scope_sync(token_id, site_id, 'succeeded', seen_promotion_ids)
                total_promotions += promotion_count
                total_items += item_count
                results.append({
                    "token_id": token_id, "store_name": store_name, "status": "succeeded",
                    "promotion_count": promotion_count, "item_count": item_count,
                })
            except Exception as exc:
                results.append({"token_id": token_id, "store_name": store_name, "status": "failed", "message": str(exc)})
        return {
            "promotion_count": total_promotions,
            "item_count": total_items,
            "succeeded": sum(1 for row in results if row["status"] == "succeeded"),
            "failed": sum(1 for row in results if row["status"] == "failed"),
            "results": results,
        }
    finally:
        _sync_lock.release()


def _finite_number(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _same_price(left, right):
    left, right = _finite_number(left), _finite_number(right)
    return left is not None and right is not None and abs(left - right) < 0.005


def _proceeds_record(value, *, price, price_currency, source, amount_key="amount"):
    """Preserve an official amount and its quoted price without estimating it."""
    price = _finite_number(price)
    if price is None or price <= 0 or not price_currency:
        return None
    values = value if isinstance(value, list) else [value]
    candidates = []
    for entry in values:
        if not isinstance(entry, dict):
            continue
        amount = _finite_number(entry.get(amount_key))
        currency = str(entry.get("currency_id") or entry.get("currency") or "").strip().upper()
        if amount is None or not currency:
            continue
        record = {
            "amount": amount, "currency_id": currency, "source": source,
            "applies_to_price": price, "price_currency_id": str(price_currency or "").upper(),
        }
        # Some promotion responses label local-currency amounts as USD. Do not
        # invent an exchange rate or present an amount above its same-currency
        # sale price as a verified quote. Preserve the raw response for diagnosis.
        if currency == record["price_currency_id"] and amount > price + 0.01:
            record.update(amount=None, reported_amount=amount,
                          warning="平台净收益超过同币种售价，金额或币种异常，待核实")
        candidates.append(record)
    valid = [record for record in candidates if record["amount"] is not None]
    preferred = valid or candidates
    return next((record for record in preferred if record["currency_id"] == "USD"), preferred[0] if preferred else None)


def _normalise_item_proceeds(promotion, row):
    raw = row.get("raw") or {}
    currency = str(row.get("currency_id") or raw.get("currency_id") or (
        "USD" if promotion.get("promotion_type") == "DEAL" else ""
    )).strip().upper()
    row["currency_id"] = currency
    original_price = row.get("original_price")
    net = row.get("net_proceeds") or raw.get("net_proceeds") or {}
    before = _proceeds_record(net, price=original_price, price_currency=currency,
                              source="promotion_api", amount_key="original_amount")
    after = _proceeds_record(net, price=row.get("price"), price_currency=currency, source="promotion_api")
    options = [after] if after else []
    if isinstance(net, dict):
        for reference in ("suggested_discounted_price", "max_discounted_price", "min_discounted_price"):
            record = _proceeds_record(net.get(reference), price=raw.get(reference),
                                      price_currency=currency, source="promotion_api")
            if record:
                options.append(record)
    result = row.get("enrollment_result") or {}
    response = result.get("response") or {}
    if result.get("status") == "succeeded" and isinstance(response, dict):
        response_currency = str(response.get("currency_id") or currency).strip().upper()
        if response_currency == currency and _same_price(response.get("original_price"), original_price):
            before = before or _proceeds_record(
                response.get("net_proceeds"), price=original_price, price_currency=currency,
                source="promotion_submission", amount_key="original_amount",
            )
        record = _proceeds_record(response.get("net_proceeds"), price=response.get("price"),
                                  price_currency=response_currency, source="promotion_submission")
        if record and response_currency == currency:
            options.append(record)
            if after is None and _same_price(row.get("price"), record["applies_to_price"]):
                after = record
    # A listing snapshot is a before-promotion reference only when its public
    # price and currency match the promotion's original price.
    listing = raw.get("_listing_snapshot") or {}
    if isinstance(listing, dict) and listing.get("currency_id") == currency and _same_price(listing.get("price"), original_price):
        before = before or _proceeds_record(listing.get("net_proceeds"), price=original_price,
                                            price_currency=currency, source="listing_api")
    row["net_proceeds_before"] = before
    row["net_proceeds_after"] = after or (options[0] if options else None)
    row["net_proceeds_options"] = options
    return row


def enrich_item_products(promotion, rows):
    """Fill metadata and exact price references in the same authorized store/site."""
    from erp.mercadolibre_store_link_store import _connect, STORE_LINK_TABLE
    for row in rows:
        _normalise_item_proceeds(promotion, row)
    missing = [row for row in rows if not row.get("title") or not row.get("thumbnail") or not row.get("net_proceeds_before")]
    if not missing:
        return rows
    connection = None
    try:
        connection = _connect()
        with connection.cursor() as cursor:
            ids = list(dict.fromkeys(row["item_id"] for row in missing))
            products = {}
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                placeholders = ",".join(["%s"] * len(batch))
                cursor.execute(
                    f"SELECT item_id, title, thumbnail_url, price, currency_id, net_proceeds_usd, "
                    f"net_proceeds_manual, remote_json FROM `{STORE_LINK_TABLE}` "
                    f"WHERE token_id=%s AND site_id=%s AND item_id IN ({placeholders})",
                    (promotion["token_id"], promotion["site_id"], *batch),
                )
                products.update({row["item_id"]: row for row in cursor.fetchall()})
            for row in missing:
                product = products.get(row["item_id"], {})
                row["title"] = row.get("title") or product.get("title") or ""
                row["thumbnail"] = row.get("thumbnail") or product.get("thumbnail_url") or ""
                if row.get("net_proceeds_before"):
                    continue
                try:
                    remote = json.loads(product.get("remote_json") or "{}")
                except (TypeError, ValueError):
                    remote = {}
                if not isinstance(remote, dict):
                    remote = {}
                manual = bool(product.get("net_proceeds_manual"))
                listing_price = product.get("price") if manual else remote.get("price")
                listing_currency = product.get("currency_id") if manual else remote.get("currency_id")
                if listing_currency != row.get("currency_id") or not _same_price(listing_price, row.get("original_price")):
                    continue
                values = {"amount": product.get("net_proceeds_usd"), "currency_id": "USD"} if manual else remote.get("net_proceeds")
                row["net_proceeds_before"] = _proceeds_record(
                    values, price=row.get("original_price"), price_currency=listing_currency,
                    source="listing_manual" if manual else "listing_api",
                )
    except Exception:
        logging.getLogger(__name__).warning("Unable to enrich promotion product metadata", exc_info=True)
    finally:
        if connection is not None:
            connection.close()
    return rows


def create_preview(
    *,
    promotion_fk: int,
    action: str,
    rows: list[dict[str, Any]],
    actor: str,
    store: PromotionStore | None = None,
    promotion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = store or PromotionStore()
    promotion = promotion or store.get_promotion(promotion_fk, include_summary=False)
    if not promotion:
        raise ValueError("活动不存在或已失效")
    action = str(action or "").strip().lower()
    if action not in {"enroll", "withdraw"}:
        raise ValueError("不支持的活动操作")
    promotion_type = str(promotion.get("promotion_type") or "UNKNOWN").upper()
    if promotion_type not in (WRITABLE_TYPES if action == "enroll" else WITHDRAWABLE_TYPES):
        raise ValueError(f"{promotion_type} 当前仅支持查看")
    if action == "enroll":
        if str(promotion.get("status_raw") or "").lower() in {"finished", "ended", "cancelled", "canceled", "deleted"}:
            raise ValueError("活动已结束或关闭，不能重新报名")
        finish = str(promotion.get("finish_date") or "")
        if finish:
            try:
                finished_at = datetime.fromisoformat(finish.replace("Z", "+00:00"))
                if finished_at.tzinfo is None:
                    finished_at = finished_at.replace(tzinfo=timezone.utc)
            except ValueError:
                finished_at = None
            if finished_at and finished_at <= _now():
                raise ValueError("活动已结束，不能重新报名")
    campaign_block_reason = ""
    if action == "enroll" and promotion.get("deadline_date"):
        deadline = str(promotion.get("deadline_date") or "").strip()
        try:
            deadline_at = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            if deadline_at.tzinfo is None:
                deadline_at = deadline_at.replace(tzinfo=timezone.utc)
        except ValueError:
            campaign_block_reason = "活动报名截止时间无效，请同步活动后重试"
        else:
            if deadline_at <= _now():
                campaign_block_reason = "活动报名截止时间已过，请同步活动后重试"
    if not isinstance(rows, list):
        raise ValueError("报名商品格式无效")
    if len(rows) > 20000:
        raise ValueError("每次最多处理 20000 件商品，请缩小范围后重试")
    requested_ids = []
    for intent in rows:
        if not isinstance(intent, dict):
            continue
        raw_local_id = intent.get("item_local_id")
        local_id = raw_local_id if type(raw_local_id) is int else 0
        if local_id > 0:
            requested_ids.append(local_id)
    # Submission validation only needs the selected promotion rows.  Avoid the
    # historical-result scan and remote product metadata enrichment used by the
    # visible product table; those made large campaign previews take minutes.
    selected = store.list_items_by_local_ids(promotion_fk, requested_ids)
    indexed = {int(item["id"]): _normalise_item_proceeds(promotion, item) for item in selected}
    preview_rows = []
    seen_ids = set()
    for intent in rows or []:
        if not isinstance(intent, dict):
            preview_rows.append({"status": "blocked", "reason": "商品数据格式无效", "item_id": ""})
            continue
        raw_local_id = intent.get("item_local_id")
        item_local_id = raw_local_id if type(raw_local_id) is int else 0
        if item_local_id <= 0:
            preview_rows.append({"item_local_id": item_local_id, "status": "blocked", "reason": "商品编号无效", "item_id": ""})
            continue
        if item_local_id in seen_ids:
            item = indexed.get(item_local_id)
            preview_rows.append({
                "item_local_id": item_local_id,
                "status": "blocked", "reason": "重复商品已自动排除",
                "item_id": (item or {}).get("item_id", ""),
            })
            continue
        seen_ids.add(item_local_id)
        item = indexed.get(item_local_id)
        if not item:
            preview_rows.append({"item_local_id": item_local_id, "status": "blocked", "reason": "商品不属于该活动", "item_id": ""})
            continue
        raw = item.get("raw") or {}
        status = str(item.get("status_raw") or "").lower()
        reason = ""
        price = intent.get("deal_price")
        if action == "enroll" and promotion_type in OFFER_ENROLLMENT_TYPES:
            price = None
            if status == "candidate" and not item.get("offer_id"):
                reason = "缺少官方候选优惠编号，请同步活动后重新报名"
        if action == "enroll" and status not in {"candidate", "withdrawn"}:
            reason = f"当前官方状态 {status or 'unknown'} 不可报名"
        if action == "withdraw" and status not in APPLIED_STATUSES:
            reason = f"当前官方状态 {status or 'unknown'} 不可退出"
        if action == "enroll" and promotion_type in {"DEAL", "PRICE_DISCOUNT"}:
            price = _finite_number(price)
            if price is None or price <= 0:
                reason = "请填写有效活动价"
            minimum = _finite_number(item.get("min_price"))
            maximum = _finite_number(item.get("max_price"))
            if not reason and item.get("min_price") not in (None, "") and minimum is None:
                reason = "官方活动价下限无效，请同步活动后重试"
            if not reason and item.get("max_price") not in (None, "") and maximum is None:
                reason = "官方活动价上限无效，请同步活动后重试"
            if not reason and minimum is not None and price < minimum:
                reason = f"活动价低于官方下限 {item.get('min_price')}"
            if not reason and maximum is not None and price > maximum:
                reason = f"活动价高于官方上限 {item.get('max_price')}"
        if action == "enroll" and promotion_type == "PRICE_DISCOUNT":
            start_date = str(intent.get("start_date") or "").strip()
            finish_date = str(intent.get("finish_date") or "").strip()
            if not start_date or not finish_date:
                reason = "请选择折扣开始和结束日期"
            else:
                try:
                    start_value = datetime.fromisoformat(start_date).date()
                    finish_value = datetime.fromisoformat(finish_date).date()
                except ValueError:
                    reason = "折扣日期格式无效"
                else:
                    if finish_value < start_value:
                        reason = "折扣结束日期不能早于开始日期"
        if campaign_block_reason:
            reason = campaign_block_reason
        preview_rows.append({
            "item_local_id": int(item["id"]),
            "requires_eligibility_check": status == "withdrawn",
            "item_id": item.get("item_id"),
            "offer_id": item.get("offer_id"),
            "status": "blocked" if reason else "ready",
            "reason": reason,
            "deal_price": price,
            "original_price": item.get("original_price"),
            "last_synced_at": item.get("last_synced_at"),
            "source_status": status,
            "currency_id": item.get("currency_id") or raw.get("currency_id") or ("USD" if promotion_type == "DEAL" else ""),
            "start_date": str(intent.get("start_date") or ""),
            "finish_date": str(intent.get("finish_date") or ""),
            "net_proceeds_before": item.get("net_proceeds_before"),
            "net_proceeds_after": next((net for net in item.get("net_proceeds_options") or []
                                         if _same_price(net.get("applies_to_price"), price)), None)
            if action == "enroll" and promotion_type in {"DEAL", "PRICE_DISCOUNT"}
            else item.get("net_proceeds_after"),
        })
    ready = [row for row in preview_rows if row["status"] == "ready"]
    expires_at = (_now() + timedelta(minutes=5)).replace(microsecond=0).isoformat()
    blocked = [row for row in preview_rows if row["status"] == "blocked"]
    blocked_reasons = {}
    for row in blocked:
        reason = str(row.get("reason") or "不符合报名要求")
        blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1
    # Only executable rows are required by the worker.  Keep a compact reason
    # summary for audit instead of persisting thousands of blocked row payloads.
    payload = {
        "promotion": promotion,
        "rows": ready,
        "excluded_count": len(blocked),
        "excluded_reasons": [{"reason": reason, "count": count} for reason, count in blocked_reasons.items()],
        "excluded_results": [{
            "item_id": row.get("item_id"),
            "item_local_id": row.get("item_local_id"),
            "offer_id": row.get("offer_id"),
            "status": "blocked",
            "message": row.get("reason") or "不符合报名要求，已自动屏蔽",
        } for row in blocked],
    }
    preview_id = store.create_preview(
        actor=actor, action=action, promotion_fk=promotion_fk, payload=payload, expires_at=expires_at
    )
    return {
        "preview_id": preview_id,
        "expires_at": expires_at,
        "action": action,
        "promotion": promotion,
        "rows": preview_rows,
        "ready_count": len(ready),
        "blocked_count": len(preview_rows) - len(ready),
    }


def start_enrollment(
    *,
    promotion_fk: int,
    rows: list[dict[str, Any]],
    actor: str,
    store: PromotionStore | None = None,
    promotion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate, screen and enqueue enrollment without a confirmation round trip."""
    store = store or PromotionStore()
    preview = create_preview(
        promotion_fk=promotion_fk,
        action="enroll",
        rows=rows,
        actor=actor,
        store=store,
        promotion=promotion,
    )
    blocked = [row for row in preview["rows"] if row.get("status") == "blocked"]
    reasons = {}
    for row in blocked:
        reason = str(row.get("reason") or "不符合报名要求")
        reasons[reason] = reasons.get(reason, 0) + 1
    result = {
        "status": "skipped",
        "job_id": "",
        "submitted_count": int(preview["ready_count"]),
        "excluded_count": int(preview["blocked_count"]),
        "excluded_reasons": [{"reason": reason, "count": count} for reason, count in reasons.items()],
    }
    job = execute_preview(
        preview["preview_id"], actor=actor, store=store,
        background=bool(preview["ready_count"]),
    )
    response = {**result, **job}
    # The durable execution log owns per-item details. Returning thousands of
    # blocked rows here would make the direct-enrollment response slow again.
    response.pop("results", None)
    return response


def _interrupted_job_results(store, job: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve saved outcomes and account for every row of an interrupted job."""
    results = [dict(row) for row in job.get("results") or [] if isinstance(row, dict)]
    preview = store.get_preview(job.get("preview_id") or "") or {}
    payload = preview.get("payload") or {}
    blocked_rows = [
        dict(row) for row in payload.get("excluded_results") or []
        if isinstance(row, dict)
    ]
    ready_rows = [
        row for row in payload.get("rows") or []
        if isinstance(row, dict) and row.get("status") == "ready"
    ]

    def identity(row):
        return (
            row.get("item_local_id"),
            str(row.get("item_id") or ""),
            str(row.get("offer_id") or ""),
        )

    missing_message = (
        "旧执行进程已中断，请按官方状态核实；未自动重提报名"
        if job.get("status") == "running"
        else "历史执行日志不完整，请按官方状态核实；未自动重提"
    )

    # Match preflight exclusions first. They are known not to have reached the
    # platform and must not be downgraded to an ambiguous remote result.
    used = set()
    for expected in blocked_rows:
        match = next((
            index for index, current in enumerate(results)
            if index not in used
            and current.get("status") == "blocked"
            and identity(current) == identity(expected)
        ), None)
        if match is None:
            results.append(expected)
            used.add(len(results) - 1)
        else:
            used.add(match)

    for row in ready_rows:
        match = next((
            index for index, current in enumerate(results)
            if index not in used and identity(current) == identity(row)
        ), None)
        if match is not None:
            used.add(match)
            continue
        results.append({
            "item_id": row.get("item_id"),
            "item_local_id": row.get("item_local_id"),
            "offer_id": row.get("offer_id"),
            "status": "unknown",
            "message": missing_message,
        })
        used.add(len(results) - 1)

    expected_total = max(int(job.get("total_count") or 0), len(results))
    while len(results) < expected_total:
        results.append({
            "status": "unknown",
            "message": missing_message,
        })
    return results


def _recover_jobs_for_campaign(
    store, promotion_fk: int, *, include_terminal: bool = True,
) -> int:
    """Repair interrupted or incomplete jobs while holding the campaign lock."""
    recovered = 0
    for job in store.list_jobs(
        limit=500, promotion_fk=int(promotion_fk), status="running",
    ):
        results = _interrupted_job_results(store, job)
        store.set_job_total(
            job["job_id"], max(int(job.get("total_count") or 0), len(results)),
        )
        store.finish_job(job["job_id"], results=results)
        if hasattr(store, "compact_preview"):
            store.compact_preview(job.get("preview_id") or "")
        recovered += 1
    if include_terminal:
        # list_jobs is deliberately bounded. Drain successive batches so
        # starting a new submission can never leave a 501st incomplete guard.
        while True:
            repairable = store.list_jobs(
                limit=500, promotion_fk=int(promotion_fk), repairable_only=True,
            )
            if not repairable:
                break
            for job in repairable:
                results = _interrupted_job_results(store, job)
                # finish_job atomically re-derives all counters and total_count
                # from preserved plus reconstructed results. Padding guarantees
                # this job will not match repairable_only on the next batch.
                store.finish_job(
                    job["job_id"], results=results, preserve_finished_at=True,
                )
                if hasattr(store, "compact_preview"):
                    store.compact_preview(job.get("preview_id") or "")
                recovered += 1
    return recovered


def recover_stale_running_jobs(
    *,
    store: PromotionStore | None = None,
    token_ids: Iterable[int] | None = None,
    promotion_fks: Iterable[int] | None = None,
    job_ids: Iterable[str] | None = None,
    action: str = "",
    include_terminal: bool = True,
) -> int:
    """Recover visible incomplete jobs only when no live campaign worker owns them."""
    store = store or PromotionStore()
    allowed_tokens = None if token_ids is None else tuple(dict.fromkeys(
        int(value) for value in token_ids
    ))
    selected_promotions = None if promotion_fks is None else tuple(dict.fromkeys(
        int(value) for value in promotion_fks if int(value) > 0
    ))
    if selected_promotions is None:
        candidates = store.list_jobs(
            limit=500,
            token_ids=allowed_tokens,
            job_ids=job_ids,
            action=str(action or ""),
            status="running",
            summary_only=True,
        )
        if include_terminal:
            candidates.extend(store.list_jobs(
                limit=500,
                token_ids=allowed_tokens,
                job_ids=job_ids,
                action=str(action or ""),
                repairable_only=True,
                summary_only=True,
            ))
    else:
        candidates = []
        for promotion_fk in selected_promotions:
            candidates.extend(store.list_jobs(
                limit=500,
                token_ids=allowed_tokens,
                promotion_fk=promotion_fk,
                job_ids=job_ids,
                action=str(action or ""),
                status="running",
                summary_only=True,
            ))
            if include_terminal:
                candidates.extend(store.list_jobs(
                    limit=500,
                    token_ids=allowed_tokens,
                    promotion_fk=promotion_fk,
                    job_ids=job_ids,
                    action=str(action or ""),
                    repairable_only=True,
                    summary_only=True,
                ))
    recovered = 0
    for promotion_fk in dict.fromkeys(
        int(job["promotion_fk"]) for job in candidates
    ):
        lease = InterProcessLock(
            f"promotion_campaign_{promotion_fk}", owner="promotion-job-recovery",
        )
        if not lease.acquire(timeout=0):
            continue
        try:
            # Re-read after taking the lock. A worker may have completed
            # between the candidate query and this exclusive probe.
            recovered += _recover_jobs_for_campaign(
                store, promotion_fk, include_terminal=include_terminal,
            )
        finally:
            lease.release()
    return recovered


def execute_preview(
    preview_id: str,
    *,
    actor: str,
    store: PromotionStore | None = None,
    background: bool = False,
) -> dict[str, Any]:
    store = store or PromotionStore()
    preview = store.get_preview(preview_id)
    if not preview:
        raise ValueError("报名预览不存在")
    if preview.get("actor") != actor:
        raise ValueError("只能提交本人创建的报名预览")
    try:
        expires_at = datetime.fromisoformat(str(preview.get("expires_at")))
    except ValueError as exc:
        raise ValueError("报名预览有效期无效") from exc
    if expires_at <= _now():
        raise ValueError("报名预览已过期，请重新预览")
    payload = preview.get("payload") or {}
    promotion = payload.get("promotion") or {}
    rows = [row for row in payload.get("rows") or [] if row.get("status") == "ready"]
    excluded_results = [row for row in payload.get("excluded_results") or [] if isinstance(row, dict)]
    whole_campaign = payload.get("stop_campaign") is True
    if not rows and not whole_campaign and not excluded_results:
        raise ValueError("没有可提交的商品")
    lease = InterProcessLock(f"promotion_campaign_{promotion['id']}", owner={"actor": actor})
    if not lease.acquire(timeout=0):
        raise ValueError("该活动已提交任务，正在执行报名或停止，请等待该活动完成；其他活动可继续操作")
    try:
        # A held campaign lease belongs to a live worker. Remaining running rows
        # with no lease are interrupted jobs, never evidence to replay a request.
        _recover_jobs_for_campaign(
            store, int(promotion["id"]), include_terminal=False,
        )
        action = str(preview.get("action") or "")
        job_id = store.create_job(
            preview_id=preview_id, actor=actor, action=action,
            total=len(rows) + len(excluded_results),
        )
    except Exception:
        lease.release()
        raise

    def run():
        adapter = None
        try:
            lease.transfer_to_current_thread()
            if not rows and excluded_results:
                store.finish_job(job_id, results=excluded_results)
                return {"job_id": job_id, "action": action, "results": excluded_results}
            secret = dict(bit_mysql.get_mercado_store_token(int(promotion['token_id'])) or {})
            adapter, _ = _adapter(secret)
            if whole_campaign:
                return _execute_stop_job(store, job_id, adapter, promotion)
            return _execute_job(
                store, job_id, adapter, promotion, rows, action,
                prior_results=excluded_results,
            )
        except Exception as exc:
            current = store.list_jobs(job_ids=[job_id])[0]
            results = list(current.get("results") or excluded_results)
            # A blocked preflight row and an executable row can share the same
            # local/item/offer identity (for example, a duplicate selection).
            # Only a non-blocked result can account for an executable row, and
            # occurrence counts matter when identities repeat.
            accounted_ready = Counter(
                (row.get("item_local_id"), str(row.get("item_id") or ""), str(row.get("offer_id") or ""))
                for row in results
                if row.get("status") != "blocked"
            )
            missing_status = "failed" if adapter is None else "unknown"
            missing_message = (
                "任务初始化失败，未向平台提交：" if adapter is None
                else "任务未完成，需核实官方状态："
            ) + str(exc)
            for row in rows:
                identity = (row.get("item_local_id"), str(row.get("item_id") or ""), str(row.get("offer_id") or ""))
                if accounted_ready[identity]:
                    accounted_ready[identity] -= 1
                    continue
                results.append({
                    "item_id": row.get("item_id"), "item_local_id": row.get("item_local_id"),
                    "offer_id": row.get("offer_id"), "status": missing_status,
                    "message": missing_message,
                })
            store.set_job_total(job_id, max(int(current.get('total_count') or 0), len(results)))
            store.finish_job(job_id, results=results)
            return {"job_id": job_id, "action": action, "results": results}
        finally:
            lease.release()
            if isinstance(adapter, MercadoPromotionsClient):
                adapter.client.session.close()
            if hasattr(store, "compact_preview"):
                try:
                    store.compact_preview(preview_id)
                except Exception:
                    logging.getLogger(__name__).warning("Unable to compact promotion preview", exc_info=True)

    if background:
        try:
            threading.Thread(target=run, name=f"promotion-{job_id[:10]}", daemon=True).start()
        except Exception:
            lease.release()
            results = list(excluded_results) + [{
                "item_id": row.get("item_id"), "item_local_id": row.get("item_local_id"),
                "offer_id": row.get("offer_id"), "status": "failed",
                "message": "无法启动执行线程，未向平台提交",
            } for row in rows]
            store.finish_job(job_id, results=results)
            if hasattr(store, "compact_preview"):
                store.compact_preview(preview_id)
            raise
        return {"job_id": job_id, "action": action, "status": "running", "results": []}
    return run()


def stop_campaign(*, promotion_fk, actor, store=None, background=True):
    """Discover all current offers remotely, then withdraw and verify in a worker."""
    store = store or PromotionStore()
    promotion = store.get_promotion(promotion_fk)
    if not promotion:
        raise ValueError("活动不存在")
    if promotion["promotion_type"] not in WITHDRAWABLE_TYPES:
        reason = "清仓活动由平台自动管理，官方接口禁止取消" if promotion["promotion_type"] == "CLEARANCE" else "该活动类型官方接口暂不支持停止"
        raise ValueError(reason)
    rows = [dict(row, item_local_id=row["id"], status="ready")
            for row in store.list_items(promotion_fk)
            if str(row.get("status_raw") or "").lower() in APPLIED_STATUSES]
    preview_id = store.create_preview(actor=actor, action="withdraw", promotion_fk=promotion_fk,
        payload={"promotion": promotion, "rows": rows, "stop_campaign": True},
        expires_at=(_now() + timedelta(minutes=5)).isoformat())
    return {"total": len(rows), **execute_preview(
        preview_id, actor=actor, store=store, background=background,
    )}


def _stop_snapshot(store, adapter, promotion):
    snapshot_started_at = _now().isoformat()
    if promotion['promotion_type'] == 'PRICE_DISCOUNT' and promotion['promotion_id'].startswith('ITEM-PRICE_DISCOUNT-'):
        item_id = promotion['promotion_id'].removeprefix('ITEM-PRICE_DISCOUNT-')
        rows = [dict(entry, id=item_id) for entry in _matching_item_promotions(adapter, promotion, item_id)]
    else:
        rows = list(adapter.iter_items(str(promotion['seller_id']), promotion['promotion_id'],
                                       promotion_type=promotion['promotion_type']))
    inactive = {"candidate", "finished", "ended", "cancelled", "canceled", "deleted", "withdrawn", "rejected"}
    if any(_official_item_status(row) not in inactive | set(APPLIED_STATUSES) for row in rows):
        raise MercadoAPIError("官方返回未知商品状态，无法确认活动已全部停止")
    store.replace_items(
        promotion['id'], rows, snapshot_started_at=snapshot_started_at,
    )
    return [dict(row, item_local_id=row['id'], status="ready") for row in store.list_items(promotion['id'])
            if row['status_raw'].lower() in APPLIED_STATUSES]


def _execute_stop_job(store, job_id, adapter, promotion):
    results = {}
    key = lambda row: (str(row.get('item_id') or ''), str(row.get('offer_id') or ''))
    try:
        active = _stop_snapshot(store, adapter, promotion)
        # A second pass covers confirmed leftovers and newly discovered offers.
        # Every retry is preceded by a complete official snapshot, never blind.
        for _ in range(2):
            work = [row for row in active if results.get(key(row), {}).get('status') != 'failed']
            if not work:
                break
            for row in work:
                results.setdefault(key(row), dict(_item_result(row), status="unknown", message="等待取消"))
            store.set_job_total(job_id, len(results))
            batch = _execute_job(store, job_id, adapter, promotion, work, 'withdraw', finish=False,
                                 prior_results=[r for k, r in results.items() if k not in {key(w) for w in work}])
            results.update((key(row), row) for row in batch['results'])
            active = _stop_snapshot(store, adapter, promotion)
            remaining = {key(row) for row in active}
            for identity, result in results.items():
                if identity not in remaining:
                    result.update(status='succeeded', message='已通过官方完整商品列表核实取消')
            if not active:
                break
        for row in active:
            result = results.setdefault(key(row), _item_result(row))
            if result.get('status') != 'failed' or not result.get('message'):
                result.update(status='unknown', message='官方仍显示参与，活动未全部停止；可再次停止以核实并处理剩余商品')
    except Exception as exc:
        # A successful DELETE alone is not proof that the whole activity ended.
        for result in results.values():
            if result['status'] == 'succeeded':
                result.update(status='unknown', message='取消已提交，最终官方核验未完成')
        results[('', '')] = {'status': 'unknown', 'message': '活动未核实完成：' + str(exc)}
    final = list(results.values())
    store.set_job_total(job_id, len(final))
    store.finish_job(job_id, results=final)
    return {'job_id': job_id, 'action': 'withdraw', 'total': len(final), 'results': final}


def _live_listing_activities(adapter, seller_id, item_id):
    rows = adapter.get_item_promotions(seller_id, item_id)
    active = []
    inactive = {"candidate", "finished", "ended", "cancelled", "canceled", "deleted", "withdrawn", "rejected"}
    for row in rows:
        status = row.get("status")
        status = str(status.get("id") if isinstance(status, dict) else status or "").lower()
        if status in APPLIED_STATUSES:
            if not row.get("type") or (not row.get("id") and row["type"] != "PRICE_DISCOUNT"):
                raise ValueError("平台活动缺少类型或编号，不能确认取消目标")
            active.append(dict(row, status=status))
        elif status not in inactive:
            raise ValueError(f"平台活动状态 {status or 'unknown'} 无法确认，停止修改净收益")
    return active


def _refresh_live_listing_activities(store, adapter, token, key):
    # The listing's child seller is authoritative; the parent token ID must
    # never be used as the promotion API's user_id.
    snapshot_started_at = _now().isoformat()
    item = adapter.client.get_marketplace_item(key[2])
    seller_id = str(item.get("seller_id") or "")
    if str(item.get("id") or "").upper() != key[2] or not seller_id:
        raise ValueError("无法确认该商品的站点卖家，停止修改净收益")
    active = _live_listing_activities(adapter, seller_id, key[2])
    old = store.applied_item_details([key]).get(key, [])
    identities = {(str(row.get("id") or ""), str(row["type"]).upper()) for row in active}
    for row in old:
        identity = (row["promotion_id"], row["promotion_type"])
        if row["promotion_type"] == "PRICE_DISCOUNT" and row["promotion_id"].startswith("ITEM-PRICE_DISCOUNT-"):
            identity = ("", "PRICE_DISCOUNT")
        if identity not in identities:
            store.set_item_status(
                row["promotion_fk"], key[2], "withdrawn",
                snapshot_started_at=snapshot_started_at,
            )
    for row in active:
        campaign = dict(row)
        if not campaign.get("id"):
            campaign["id"] = f"ITEM-PRICE_DISCOUNT-{key[2]}"
        pk = store.upsert_promotion(token_id=key[0], site_id=key[1], seller_id=seller_id,
            application_id=str(token.get("client_id") or ""),
            store_name=str(token.get("display_name") or token.get("nickname") or key[0]), row=campaign)
        store.replace_items(
            pk, [dict(row, id=key[2])], item_id=key[2],
            snapshot_started_at=snapshot_started_at,
        )
    return seller_id


def withdraw_listing_promotions(*, token_id, site_id, item_id, actor, promotion_fk=None, store=None, adapter=None, verify_live=False):
    """Withdraw recorded enrollments through the same durable job path as the workbench."""
    store = store or PromotionStore()
    key = (int(token_id), str(site_id).upper(), str(item_id).upper())
    seller_id = ""
    if verify_live:
        token = dict(bit_mysql.get_mercado_store_token(key[0]) or {})
        if adapter is None:
            adapter, token = _adapter(token)
        seller_id = _refresh_live_listing_activities(store, adapter, token, key)
    activities = store.applied_item_details([key]).get(key, [])
    if promotion_fk is not None:
        activities = [row for row in activities if row["promotion_fk"] == int(promotion_fk)]
        if not activities:
            raise ValueError("该商品当前没有参加此活动，请刷新后重试")
    if any(row["promotion_type"] not in WITHDRAWABLE_TYPES for row in activities):
        raise ValueError("该商品包含暂不支持自动取消的活动，请先在平台取消后同步活动")
    if activities and adapter is None:
        adapter, _ = _adapter(dict(bit_mysql.get_mercado_store_token(int(token_id)) or {}))
    results = []
    grouped = {}
    for row in activities:
        grouped.setdefault(row["promotion_fk"], []).append(dict(row, item_id=key[2], status="ready"))
    for pk, rows in grouped.items():
        lease = InterProcessLock(
            f"promotion_campaign_{pk}", owner={"actor": actor, "action": "withdraw"},
        )
        if not lease.acquire(timeout=0):
            raise ValueError("该活动正在执行报名或停止，请等待完成后重试")
        try:
            _recover_jobs_for_campaign(store, int(pk))
            promotion = store.get_promotion(pk)
            preview_id = store.create_preview(actor=actor, action="withdraw", promotion_fk=pk,
                payload={"promotion": promotion, "rows": rows},
                expires_at=(_now() + timedelta(minutes=5)).isoformat())
            job_id = store.create_job(preview_id=preview_id, actor=actor, action="withdraw", total=len(rows))
            outcome = _execute_job(store, job_id, adapter, promotion, rows, "withdraw")
        finally:
            lease.release()
        results.extend(outcome["results"])
        failed = [row for row in outcome["results"] if row["status"] != "succeeded"]
        if failed:
            raise ValueError("取消活动未完成：" + "; ".join(row.get("message") or "结果待核实" for row in failed))
    remaining = store.applied_item_details([key]).get(key, [])
    if any(promotion_fk is None or row["promotion_fk"] == int(promotion_fk) for row in remaining):
        raise ValueError("平台取消已提交，但本地活动状态更新失败，请同步活动后重试")
    if verify_live:
        # Promotion reads are eventually consistent after a successful DELETE.
        # Retry reads only; never replay an ambiguous remote mutation.
        remaining_live = []
        for delay in (0, 1, 2, 4):
            if delay:
                time.sleep(delay)
            remaining_live = _live_listing_activities(adapter, seller_id, key[2])
            if not remaining_live:
                break
        if remaining_live:
            # Restore the true active snapshot, rather than leaving the UI with
            # an unverified local "withdrawn" state after a successful DELETE.
            _refresh_live_listing_activities(store, adapter, token, key)
            raise ValueError("平台仍返回已参与活动，取消尚未确认，停止修改净收益")
    from bit.store_link_metrics import invalidate
    invalidate(key[0], key[2])
    return {"cancelled_count": len(results), "results": results}


def _matching_item_promotions(adapter, promotion, item_id):
    rows = adapter.get_item_promotions(str(promotion["seller_id"]), str(item_id))
    promotion_id = str(promotion.get("promotion_id") or "")
    kind = str(promotion["promotion_type"]).upper()
    return [row for row in rows if str(row.get("type") or row.get("promotion_type") or "").upper() == kind
            and (str(row.get("id") or "") == promotion_id
                 or (kind == "PRICE_DISCOUNT" and promotion_id.startswith("ITEM-PRICE_DISCOUNT-")))]


def _official_item_status(row):
    status = row.get("status")
    return str(status.get("id") if isinstance(status, dict) else status or "").lower()


def _enrollment_identity(promotion, row):
    """Return the remote mutation identity used by submit-once guards."""
    return (
        str(row.get("item_id") or row.get("id") or ""),
        str(row.get("offer_id") or "")
        if str(promotion.get("promotion_type") or "").upper() in OFFER_ENROLLMENT_TYPES
        else "",
    )


def _identity_matches(promotion, row, entry):
    if str(promotion.get("promotion_type") or "").upper() not in OFFER_ENROLLMENT_TYPES:
        return True
    offer_id = str(row.get("offer_id") or "")
    return bool(offer_id) and str(entry.get("offer_id") or "") == offer_id


def _same_enrollment_terms(promotion, row, entry):
    if str(promotion.get("promotion_type") or "").upper() not in {"DEAL", "PRICE_DISCOUNT"}:
        return True
    if not _same_price(entry.get("price"), row.get("deal_price")):
        return False
    return not (
        entry.get("currency_id") and row.get("currency_id")
        and entry["currency_id"] != row["currency_id"]
    )


def _probe_enrollment(adapter, promotion, row):
    """Read the exact item/offer state without performing a mutation.

    A candidate is explicit proof that the guarded identity is currently
    eligible again.  An empty or failed response is kept unverified because it
    cannot safely distinguish replication lag from a withdrawn offer.
    """
    if not callable(getattr(adapter, "get_item_promotions", None)):
        return "unverified", None
    for delay in (0, 1, 2):
        if delay:
            time.sleep(delay)
        try:
            matches = [
                entry for entry in _matching_item_promotions(
                    adapter, promotion, row["item_id"],
                )
                if _identity_matches(promotion, row, entry)
            ]
        except Exception:
            continue
        applied = next((
            entry for entry in matches
            if _official_item_status(entry) in APPLIED_STATUSES
            and _same_enrollment_terms(promotion, row, entry)
        ), None)
        if applied is not None:
            return "applied", applied
        mismatched = next((
            entry for entry in matches
            if _official_item_status(entry) in APPLIED_STATUSES
        ), None)
        if mismatched is not None:
            return "applied_mismatch", mismatched
        candidate = next((
            entry for entry in matches if _official_item_status(entry) == "candidate"
        ), None)
        if candidate is not None:
            return "candidate", candidate
    return "unverified", None


def _verify_mutation(adapter, promotion, row, action):
    """Confirm an ambiguous mutation by reads only; never repeat its POST/DELETE."""
    if not callable(getattr(adapter, "get_item_promotions", None)):
        return None
    for delay in (0, 1, 2):
        if delay:
            time.sleep(delay)
        try:
            matches = _matching_item_promotions(adapter, promotion, row["item_id"])
            if action == "withdraw":
                offer_id = str(row.get("offer_id") or "")
                relevant = [entry for entry in matches if not offer_id
                            or not entry.get("offer_id") or str(entry["offer_id"]) == offer_id]
                if not relevant or all(_official_item_status(entry) in {"candidate", "finished", "withdrawn", "cancelled", "canceled"}
                                       for entry in relevant):
                    return {}
            else:
                for entry in matches:
                    if not _identity_matches(promotion, row, entry):
                        continue
                    if _official_item_status(entry) not in APPLIED_STATUSES:
                        continue
                    if not _same_enrollment_terms(promotion, row, entry):
                        continue
                    return entry
        except Exception:
            continue
    return None


def _item_result(row):
    return {
        "item_id": row.get("item_id"), "item_local_id": row.get("item_local_id"),
        "offer_id": row.get("offer_id"), "status": "failed", "message": "",
        "deal_price": row.get("deal_price"), "currency_id": row.get("currency_id"),
        "original_price": row.get("original_price"),
        "net_proceeds_before": row.get("net_proceeds_before"),
        "net_proceeds_after": row.get("net_proceeds_after"),
    }


def _timestamp_after(value, reference):
    return snapshot_started_after(value, reference)


def _execute_item(store, job_id, adapter, promotion, row, action, previous_results):
    result = _item_result(row)
    lease = InterProcessLock(f"promotion_item_{promotion.get('token_id')}_{row.get('item_id')}", owner={"job_id": job_id})
    try:
        if not lease.acquire(timeout=30 if action == "withdraw" else 0):
            result.update(status="blocked", message="该商品仍在执行其他活动操作，尚未提交取消" if action == "withdraw" else "该商品正在执行其他活动操作，请完成后再提交")
            return result
        if action == "enroll" and row.get("requires_eligibility_check"):
            candidates = [entry for entry in _matching_item_promotions(adapter, promotion, row["item_id"])
                          if _official_item_status(entry) == "candidate"]
            if not candidates:
                raise ValueError("平台尚未恢复该商品的报名资格，请同步活动后重试")
            if promotion["promotion_type"] in OFFER_ENROLLMENT_TYPES:
                if len(candidates) != 1 or not candidates[0].get("offer_id"):
                    raise ValueError("无法确认新的候选优惠编号，请同步活动后重新报名")
                row = dict(row, offer_id=candidates[0]["offer_id"])
                result["offer_id"] = row["offer_id"]
            candidate = candidates[0]
            minimum, maximum = candidate.get("min_discounted_price"), candidate.get("max_discounted_price")
            price = _finite_number(row.get("deal_price"))
            if price is not None and ((minimum is not None and price < float(minimum))
                                      or (maximum is not None and price > float(maximum))):
                raise ValueError("平台活动价格区间已变化，请同步活动后重新设置折扣")
        current = store.item_rows(int(promotion.get("id") or 0), str(row["item_id"])) if hasattr(store, "item_rows") else []
        offer_scoped = promotion["promotion_type"] in OFFER_ENROLLMENT_TYPES
        exact_current = [
            item for item in current
            if not offer_scoped
            or (row.get("offer_id") and str(item.get("offer_id") or "") == str(row["offer_id"]))
        ]
        applied = [
            item for item in exact_current
            if str(item.get("status_raw") or "").lower() in APPLIED_STATUSES
        ]
        existing = None
        if action == "enroll" and applied:
            existing = next((item for item in applied if promotion["promotion_type"] in ({"MARKETPLACE_CAMPAIGN"} | OFFER_ENROLLMENT_TYPES)
                             or (_same_price(item.get("price"), row.get("deal_price"))
                                 and (not row.get("currency_id") or item.get("currency_id") == row["currency_id"]))), None)
            if existing is None:
                raise ValueError("该商品已参与活动，请刷新后核对价格，勿重复报名")
        previous = previous_results.get(_enrollment_identity(promotion, row), {})
        if (
            action == "enroll" and existing is None
            and previous.get("status") in {"unknown", "succeeded"}
            and not row.get("requires_eligibility_check")
        ):
            snapshot = next((
                item for item in exact_current
                if row.get("item_local_id") is not None
                and int(item.get("id") or 0) == int(row["item_local_id"])
            ), exact_current[0] if len(exact_current) == 1 else None)
            snapshot_status = str(
                (snapshot or {}).get("status_raw") or row.get("source_status") or ""
            ).lower()
            snapshot_at = (snapshot or {}).get("last_synced_at") or row.get("last_synced_at")
            fresh_candidate = (
                snapshot_status == "candidate"
                and _timestamp_after(snapshot_at, previous.get("finished_at"))
            )
            if not fresh_candidate:
                state, verified = _probe_enrollment(adapter, promotion, row)
                if state == "applied":
                    existing = verified
                elif state == "applied_mismatch":
                    raise ValueError("上次报名已生效，但价格与本次不同，请刷新后核对")
                elif state == "candidate":
                    raise MercadoAPIError("官方已只读核实为候选，当前结果未知；未重复提交，请同步活动后重试")
                else:
                    prefix = "上次报名结果未知" if previous.get("status") == "unknown" else "存在上次成功报名记录，当前结果未知"
                    raise MercadoAPIError(prefix + "，官方状态仍未确认；未重复提交，请同步活动后重试")
        try:
            if existing is not None:
                response = existing
                result["message"] = "该商品已按相同价格参与，已跳过重复报名"
            elif action == "enroll":
                response = adapter.enroll_item(
                    str(promotion.get("seller_id") or ""), str(row.get("item_id") or ""),
                    promotion_id=str(promotion.get("promotion_id") or ""),
                    promotion_type=str(promotion.get("promotion_type") or ""),
                    deal_price=row.get("deal_price"), start_date=row.get("start_date") or "",
                    finish_date=row.get("finish_date") or "",
                    **({"offer_id": row.get("offer_id") or ""} if promotion["promotion_type"] in OFFER_ENROLLMENT_TYPES else {}),
                )
            else:
                response = adapter.withdraw_item(
                    str(promotion.get("seller_id") or ""), str(row.get("item_id") or ""),
                    promotion_id=("" if str(promotion.get("promotion_id") or "").startswith("ITEM-PRICE_DISCOUNT-")
                                  else str(promotion.get("promotion_id") or "")),
                    promotion_type=str(promotion.get("promotion_type") or ""),
                    offer_id=str(row.get("offer_id") or ""),
                )
        except MercadoAPIError as exc:
            if "结果未知" not in str(exc):
                raise
            response = _verify_mutation(adapter, promotion, row, action)
            if response is None:
                raise
            result["message"] = "平台返回异常，已通过官方状态核实成功"
        result.update(status="succeeded", response=response)
        if action == "enroll":
            response_net = (response or {}).get("net_proceeds")
            response_currency = str((response or {}).get("currency_id") or row.get("currency_id") or "").strip().upper()
            same_currency = response_currency == str(row.get("currency_id") or "").strip().upper()
            applied_price = (response or {}).get("price")
            if applied_price is None and same_currency and promotion.get("promotion_type") in {"DEAL", "PRICE_DISCOUNT"}:
                applied_price = row.get("deal_price")
            original_price = (response or {}).get("original_price")
            if original_price is None and same_currency:
                original_price = row.get("original_price")
            result["net_proceeds_after"] = _proceeds_record(
                response_net, price=applied_price,
                price_currency=response_currency, source="promotion_submission",
            ) or (result.get("net_proceeds_after") if same_currency else None)
            result["net_proceeds_before"] = _proceeds_record(
                response_net, price=original_price,
                price_currency=response_currency, source="promotion_submission", amount_key="original_amount",
            ) or (result.get("net_proceeds_before") if same_currency else None)
        try:
            if hasattr(store, "set_item_status"):
                response_status = _official_item_status(response or {})
                local_item_id = row.get("item_local_id")
                updated = store.set_item_status(
                    int(promotion.get("id") or 0),
                    str(row.get("item_id") or ""),
                    response_status if action == "enroll" and response_status in APPLIED_STATUSES
                    else "pending_approval" if action == "enroll" else "withdrawn",
                    item_local_id=local_item_id,
                    **({"offer_id": row["offer_id"]} if row.get("offer_id") else {}),
                )
                if updated == 0 and hasattr(store, "item_rows"):
                    refreshed = store.item_rows(
                        int(promotion.get("id") or 0), str(row.get("item_id") or ""),
                    )
                    if row.get("offer_id"):
                        refreshed = [item for item in refreshed if str(item.get("offer_id") or "") == str(row["offer_id"])]
                    if len(refreshed) == 1:
                        local_item_id = refreshed[0]["id"]
                        updated = store.set_item_status(
                            int(promotion.get("id") or 0), str(row.get("item_id") or ""),
                            response_status if action == "enroll" and response_status in APPLIED_STATUSES
                            else "pending_approval" if action == "enroll" else "withdrawn",
                            item_local_id=local_item_id,
                        )
                if updated == 0:
                    raise ValueError("活动商品快照已变化，未能写回最新数量")
            if action == "enroll" and hasattr(store, "record_item_submission"):
                applied_response = dict(response or {})
                if applied_response.get("price") is None and same_currency and promotion.get("promotion_type") in {"DEAL", "PRICE_DISCOUNT"}:
                    applied_response["price"] = row.get("deal_price")
                store.record_item_submission(
                    int(promotion.get("id") or 0), str(row.get("item_id") or ""), applied_response,
                    item_local_id=local_item_id,
                    offer_id=str(row.get("offer_id") or "") if row.get("offer_id") else None,
                )
        except Exception as exc:
            # The remote mutation already succeeded; a stale local snapshot
            # must not report that operation as a failed enrollment.
            warning = "平台操作成功，但本地候选/参与数量更新失败，请同步活动：" + str(exc)
            result["message"] = ((result.get("message") or "") + "；" + warning).lstrip("；")
            result["local_sync_failed"] = True
    except MercadoAPIError as exc:
        message = str(exc)
        result.update(status="unknown" if "结果未知" in message else "failed", message=message)
    except Exception as exc:
        result["message"] = str(exc)
    finally:
        lease.release()
    return result


def _promotion_worker_count(total):
    try:
        configured = int(os.environ.get("BIT_PROMOTION_WORKERS", "8"))
    except (TypeError, ValueError):
        configured = 8
    return max(1, min(16, configured, total or 1))


def _execute_job(store, job_id, adapter, promotion, rows, action, *, finish=True, prior_results=None):
    """Bounded independent workers with compact, throttled progress checkpoints."""
    selected_item_ids = [str(row.get("item_id") or "") for row in rows if row.get("item_id")]
    result_loader = getattr(
        store, "enrollment_submission_guards",
        getattr(store, "unresolved_enrollment_results", store.enrollment_results),
    )
    previous_results = {
        _enrollment_identity(promotion, entry): entry
        for entry in result_loader(
            int(promotion.get("id") or 0), item_ids=selected_item_ids,
        )
    } if action == "enroll" else {}
    groups = {}
    for index, row in enumerate(rows):
        # Multiple offers for one listing must never overlap remotely.
        groups.setdefault(str(row.get("item_id") or ""), []).append((index, row))
    pending = queue.Queue()
    for group in groups.values():
        pending.put(group)
    results_by_index = {}
    progress_lock = threading.Lock()
    progress = {"saved": False, "completed": 0, "last_saved_at": 0.0}

    def save(index, result, *, completed=False):
        with progress_lock:
            results_by_index[index] = result
            if completed:
                progress["completed"] += 1
            now = time.monotonic()
            should_checkpoint = (
                not progress["saved"]
                or completed and (
                    progress["completed"] % 64 == 0
                    or now - progress["last_saved_at"] >= 2
                )
            )
            if should_checkpoint:
                store.save_job_progress(
                    job_id,
                    results=list(prior_results or [])
                    + [results_by_index[key] for key in sorted(results_by_index)],
                )
                progress["saved"] = True
                progress["last_saved_at"] = now

    def run_worker():
        worker_adapter = adapter
        owns_adapter = isinstance(adapter, MercadoPromotionsClient)
        initialization_error = None
        if owns_adapter:
            try:
                token = dict(bit_mysql.get_mercado_store_token(int(promotion["token_id"])) or {})
                worker_adapter, _ = _adapter(token)
            except Exception as exc:
                initialization_error = str(exc)
        try:
            while True:
                try:
                    group = pending.get_nowait()
                except queue.Empty:
                    return
                uncertain_item = False
                for index, row in group:
                    save(index, dict(_item_result(row), status="unknown", message="正在提交，平台结果尚未确认，请勿重复报名"))
                    if uncertain_item and action == "enroll":
                        result = dict(
                            _item_result(row), status="blocked",
                            message="同一商品前一优惠报名结果未知，为避免重复提交，本优惠已自动屏蔽",
                        )
                    elif initialization_error:
                        result = dict(_item_result(row), message="授权初始化失败，未提交：" + initialization_error)
                    else:
                        try:
                            result = _execute_item(store, job_id, worker_adapter, promotion, row, action, previous_results)
                        except Exception as exc:
                            # Unexpected failures cannot silently discard the rest of a batch.
                            result = dict(_item_result(row), status="unknown", message="执行异常，需核实官方状态：" + str(exc))
                    if action == "enroll" and result.get("status") == "unknown":
                        uncertain_item = True
                    if action == "enroll" and result.get("status") in {"unknown", "succeeded"}:
                        previous_results[_enrollment_identity(promotion, result)] = dict(
                            result, finished_at=_now().isoformat(),
                        )
                    save(index, result, completed=True)
        finally:
            if owns_adapter and worker_adapter is not adapter:
                try:
                    worker_adapter.client.session.close()
                except Exception:
                    logging.getLogger(__name__).warning("Unable to close promotion worker session", exc_info=True)

    workers = _promotion_worker_count(len(groups))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="promotion-item") as executor:
        futures = [executor.submit(run_worker) for _ in range(workers)]
        worker_errors = []
        for future in futures:
            try:
                future.result()
            except Exception as exc:
                worker_errors.append(str(exc))
    if worker_errors:
        for index, row in enumerate(rows):
            if index not in results_by_index:
                results_by_index[index] = dict(_item_result(row), status="blocked",
                    message="执行线程异常，该商品未提交：" + worker_errors[0])
    completed_results = [results_by_index[index] for index in sorted(results_by_index)]
    # Keep remotely submitted outcomes first so a bounded execution-log view
    # does not get filled entirely by preflight exclusions.
    results = completed_results + list(prior_results or []) if finish else completed_results
    if finish:
        store.finish_job(job_id, results=results)
    return {"job_id": job_id, "action": action, "results": results}
