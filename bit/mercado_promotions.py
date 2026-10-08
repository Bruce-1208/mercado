"""Promotion synchronization and guarded batch enrollment orchestration."""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from bit import bit_mysql
from bit.bit_store_link_sync import _client_and_token, _marketplace_accounts
from erp.mercadolibre_promotion_store import PromotionStore
from mercado_api.client import MercadoAPIError
from mercado_api.promotions import MercadoPromotionsClient, WRITABLE_TYPES


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
                        item_count += store.replace_items(local_id, items)
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
        candidates.append({
            "amount": amount, "currency_id": currency, "source": source,
            "applies_to_price": price, "price_currency_id": str(price_currency or "").upper(),
        })
    return next((record for record in candidates if record["currency_id"] == "USD"), candidates[0] if candidates else None)


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
) -> dict[str, Any]:
    store = store or PromotionStore()
    promotion = store.get_promotion(promotion_fk)
    if not promotion:
        raise ValueError("活动不存在或已失效")
    action = str(action or "").strip().lower()
    if action not in {"enroll", "withdraw"}:
        raise ValueError("不支持的活动操作")
    promotion_type = str(promotion.get("promotion_type") or "UNKNOWN").upper()
    if promotion_type not in WRITABLE_TYPES:
        raise ValueError(f"{promotion_type} 当前仅支持查看")
    indexed = {int(item["id"]): item for item in enrich_item_products(promotion, store.list_items(promotion_fk))}
    preview_rows = []
    for intent in rows or []:
        item = indexed.get(int(intent.get("item_local_id") or 0))
        if not item:
            preview_rows.append({"status": "blocked", "reason": "商品不属于该活动", "item_id": ""})
            continue
        raw = item.get("raw") or {}
        status = str(item.get("status_raw") or "").lower()
        reason = ""
        price = intent.get("deal_price")
        if action == "enroll" and status not in {"candidate", "pending_approval"}:
            reason = f"当前官方状态 {status or 'unknown'} 不可报名"
        if action == "withdraw" and status not in {"started", "active", "pending", "programmed"}:
            reason = f"当前官方状态 {status or 'unknown'} 不可退出"
        if action == "enroll" and promotion_type in {"DEAL", "PRICE_DISCOUNT"}:
            try:
                price = float(price)
                if not math.isfinite(price) or price <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                reason = "请填写有效活动价"
            minimum = item.get("min_price")
            maximum = item.get("max_price")
            if not reason and minimum is not None and price < float(minimum):
                reason = f"活动价低于官方下限 {minimum}"
            if not reason and maximum is not None and price > float(maximum):
                reason = f"活动价高于官方上限 {maximum}"
        if action == "enroll" and promotion_type == "PRICE_DISCOUNT":
            start_date = str(intent.get("start_date") or "").strip()
            finish_date = str(intent.get("finish_date") or "").strip()
            if not start_date or not finish_date:
                reason = "请选择折扣开始和结束日期"
            elif finish_date < start_date:
                reason = "折扣结束日期不能早于开始日期"
        preview_rows.append({
            "item_local_id": int(item["id"]),
            "item_id": item.get("item_id"),
            "offer_id": item.get("offer_id"),
            "status": "blocked" if reason else "ready",
            "reason": reason,
            "deal_price": price,
            "original_price": item.get("original_price"),
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
    payload = {"promotion": promotion, "rows": preview_rows}
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
    if not rows:
        raise ValueError("没有可提交的商品")
    token_id = int(promotion.get("token_id") or 0)
    secret = dict(bit_mysql.get_mercado_store_token(token_id) or {})
    adapter, _ = _adapter(secret)
    action = str(preview.get("action") or "")
    job_id = store.create_job(preview_id=preview_id, actor=actor, action=action, total=len(rows))
    if background:
        thread = threading.Thread(
            target=_execute_job, args=(store, job_id, adapter, promotion, rows, action),
            name=f"promotion-{job_id[:10]}", daemon=True,
        )
        thread.start()
        return {"job_id": job_id, "action": action, "status": "running", "results": []}
    return _execute_job(store, job_id, adapter, promotion, rows, action)


def _execute_job(store, job_id, adapter, promotion, rows, action):
    results = []
    for row in rows:
        result = {
            "item_id": row.get("item_id"), "item_local_id": row.get("item_local_id"),
            "offer_id": row.get("offer_id"), "status": "failed", "message": "",
            "deal_price": row.get("deal_price"), "currency_id": row.get("currency_id"),
            "original_price": row.get("original_price"),
            "net_proceeds_before": row.get("net_proceeds_before"),
            "net_proceeds_after": row.get("net_proceeds_after"),
        }
        store.save_job_progress(job_id, results=results + [{
            **result, "status": "unknown", "message": "正在提交，平台结果尚未确认，请勿重复报名",
        }])
        try:
            if action == "enroll":
                response = adapter.enroll_item(
                    str(promotion.get("seller_id") or ""), str(row.get("item_id") or ""),
                    promotion_id=str(promotion.get("promotion_id") or ""),
                    promotion_type=str(promotion.get("promotion_type") or ""),
                    deal_price=row.get("deal_price"), start_date=row.get("start_date") or "",
                    finish_date=row.get("finish_date") or "",
                )
            else:
                response = adapter.withdraw_item(
                    str(promotion.get("seller_id") or ""), str(row.get("item_id") or ""),
                    promotion_id=str(promotion.get("promotion_id") or ""),
                    promotion_type=str(promotion.get("promotion_type") or ""),
                    offer_id=str(row.get("offer_id") or ""),
                )
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
                    store.set_item_status(
                        int(promotion.get("id") or 0),
                        str(row.get("item_id") or ""),
                        "pending_approval" if action == "enroll" else "withdrawn",
                    )
                if action == "enroll" and hasattr(store, "record_item_submission"):
                    applied_response = dict(response or {})
                    if applied_response.get("price") is None and same_currency and promotion.get("promotion_type") in {"DEAL", "PRICE_DISCOUNT"}:
                        applied_response["price"] = row.get("deal_price")
                    store.record_item_submission(int(promotion.get("id") or 0), str(row.get("item_id") or ""), applied_response)
            except Exception:
                # The remote mutation already succeeded; a stale local snapshot
                # must not report that operation as a failed enrollment.
                pass
        except MercadoAPIError as exc:
            message = str(exc)
            result.update(status="unknown" if "结果未知" in message else "failed", message=message)
        except Exception as exc:
            result["message"] = str(exc)
        results.append(result)
        store.save_job_progress(job_id, results=results)
    store.finish_job(job_id, results=results)
    return {"job_id": job_id, "action": action, "results": results}
