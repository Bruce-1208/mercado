"""Batch-publish collected products to one centrally authorized store."""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Mapping

import requests

from erp.mercadolibre_follow_sell import MercadoLibreClient, follow_sell
from erp.mercadolibre_translation import marketplace_site_name, normalize_marketplace_site


_refresh_locks_guard = threading.Lock()
_refresh_locks: dict[int, threading.Lock] = {}


def _token_refresh_lock(token_id: int) -> threading.Lock:
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(int(token_id), threading.Lock())


def _token_record(token_id: int) -> dict[str, Any]:
    from bit import bit_mysql

    record = dict(bit_mysql.get_mercado_store_token(int(token_id)) or {})
    if not record:
        raise KeyError("指定的授权店铺不存在")
    if not record.get("access_token"):
        raise RuntimeError("指定店铺缺少 Access Token，请重新授权")
    return record


class DatabaseMercadoLibreClient(MercadoLibreClient):
    """Mercado client whose rotating token is persisted in the central DB."""

    def __init__(
        self,
        token_id: int,
        *,
        session: requests.Session | None = None,
        timeout: int = 45,
    ) -> None:
        record = _token_record(token_id)
        self.token_id = int(token_id)
        self.token_file = None
        self.tokens = record
        self.client_id = str(record.get("client_id") or "")
        self.client_secret = ""
        self.session = session or requests.Session()
        self.timeout = timeout

    def _refresh(self) -> None:
        from bit import bit_mysql, mercado_tokens

        previous_access_token = str(self.tokens.get("access_token") or "")
        with _token_refresh_lock(self.token_id):
            latest = _token_record(self.token_id)
            if str(latest.get("access_token") or "") != previous_access_token:
                self.tokens = latest
                return
            mercado_tokens.refresh_and_save(
                self.token_id,
                get_token=bit_mysql.get_mercado_store_token,
                update_token=bit_mysql.update_mercado_store_token,
                record_error=bit_mysql.record_mercado_store_token_error,
                http=self.session,
                timeout=self.timeout,
            )
            self.tokens = _token_record(self.token_id)


def _published_item_id(publication: Mapping[str, Any]) -> str:
    result = publication.get("result")
    if not isinstance(result, Mapping):
        return ""
    for key in ("id", "global_item_id", "user_product_id", "parent_user_product_id"):
        value = result.get(key)
        if value not in (None, ""):
            return str(value)
    for row in result.get("site_items") or []:
        if isinstance(row, Mapping):
            value = row.get("id") or row.get("item_id")
            if value not in (None, ""):
                return str(value)
    return ""


def _decimal_value(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _row_references(rows: Iterable[Mapping[str, Any]]) -> str:
    references = [
        str(row.get("source_item_id") or row.get("id") or "未知商品")
        for row in rows
    ]
    visible = references[:5]
    suffix = f" 等 {len(references)} 件" if len(references) > len(visible) else ""
    return "、".join(visible) + suffix


def product_publish_issues(product_row: Mapping[str, Any]) -> list[str]:
    """Return every local reason that prevents a product from being published."""

    row = dict(product_row or {})
    issues: list[str] = []
    if row.get("review_status") != "approved":
        issues.append("审核状态未通过")
    if (_decimal_value(row.get("weight_g")) or Decimal("0")) <= 0:
        issues.append("未填写有效重量")
    net_proceeds = _decimal_value(row.get("net_proceeds_usd"))
    if net_proceeds is None:
        issues.append("净收益尚未计算")
    elif net_proceeds <= 0:
        issues.append("净收益小于等于 0")
    return issues


def validate_publishable_products(product_rows: Iterable[Mapping[str, Any]]) -> None:
    """Reject a batch before any remote publication can be created."""

    rows = [dict(row) for row in product_rows or []]
    if not rows:
        raise ValueError("请至少勾选一个产品")
    issues: list[str] = []
    unapproved = [row for row in rows if row.get("review_status") != "approved"]
    if unapproved:
        issues.append(
            "只有审核状态为“通过”的产品可以上架："
            f"{_row_references(unapproved)}"
        )
    missing_weight = [
        row for row in rows
        if (_decimal_value(row.get("weight_g")) or Decimal("0")) <= 0
    ]
    if missing_weight:
        issues.append(f"未填写有效重量 {_row_references(missing_weight)}")
    missing_net = [
        row for row in rows if _decimal_value(row.get("net_proceeds_usd")) is None
    ]
    if missing_net:
        issues.append(f"净收益尚未计算 {_row_references(missing_net)}")
    nonpositive_net = [
        row for row in rows
        if _decimal_value(row.get("net_proceeds_usd")) is not None
        and _decimal_value(row.get("net_proceeds_usd")) <= 0
    ]
    if nonpositive_net:
        issues.append(f"净收益小于等于 0 {_row_references(nonpositive_net)}")
    if issues:
        raise ValueError("不能上架：" + "；".join(issues))


def site_discount_rate(
    token: Mapping[str, Any],
    site_id: str,
    explicit_rate: Any = None,
) -> Decimal:
    """Return the configured percentage; blank configuration means no discount."""

    raw_rate = explicit_rate
    if raw_rate in (None, ""):
        setting = next(
            (
                row for row in token.get("site_settings") or []
                if str(row.get("site_id") or "").strip().upper() == site_id
            ),
            {},
        )
        raw_rate = setting.get("discount_rate")
    if raw_rate in (None, ""):
        raw_rate = 100
    rate = _decimal_value(raw_rate)
    if rate is None or rate <= 0 or rate > 100:
        raise ValueError(f"{site_id} 的折扣比例必须大于 0 且不超过 100")
    return rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def discounted_net_proceeds_usd(row: Mapping[str, Any], discount_rate: Any) -> float:
    net_proceeds = _decimal_value(row.get("net_proceeds_usd"))
    if net_proceeds is None or net_proceeds <= 0:
        raise ValueError("产品净收益必须大于 0")
    rate = _decimal_value(discount_rate)
    if rate is None or rate <= 0 or rate > 100:
        raise ValueError("站点折扣比例必须大于 0 且不超过 100")
    amount = (net_proceeds * rate / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if amount <= 0:
        raise ValueError("折扣后的上架净收益必须大于 0")
    return float(amount)


def _product_source_snapshot(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build the edited publication snapshot without a database round trip."""
    raw_snapshot = row.get("source_snapshot_json")
    if not raw_snapshot:
        return None
    if isinstance(raw_snapshot, Mapping):
        snapshot = dict(raw_snapshot)
    else:
        try:
            decoded = json.loads(str(raw_snapshot))
        except (TypeError, ValueError) as exc:
            raise ValueError("产品源快照格式无效，请重新加入产品列表") from exc
        if not isinstance(decoded, Mapping):
            raise ValueError("产品源快照格式无效，请重新加入产品列表")
        snapshot = dict(decoded)
    source = dict(snapshot.get("source") or {})
    source.update({
        "title": row.get("title") or source.get("title"),
        "price": row.get("price") if row.get("price") is not None else source.get("price"),
        "currency_id": row.get("currency_id") or source.get("currency_id"),
        "category_id": row.get("category_id") or source.get("category_id"),
        "permalink": row.get("source_url") or source.get("permalink"),
    })
    main_image_url = str(row.get("main_image_url") or "").strip()
    if main_image_url:
        pictures = [
            picture for picture in list(source.get("pictures") or [])
            if str(
                picture.get("secure_url") or picture.get("url") or picture.get("source")
                if isinstance(picture, Mapping) else picture
            ).strip() != main_image_url
        ]
        source["pictures"] = [{"source": main_image_url}, *pictures]
    description = snapshot.get("description") or {}
    if row.get("description_text") is not None:
        description = {"plain_text": str(row.get("description_text") or "")}
    return {
        "item_id": row.get("source_item_id"),
        "source_url": row.get("source_url"),
        "final_url": source.get("permalink") or row.get("source_url"),
        "main_image_url": row.get("main_image_url"),
        "title": row.get("title"),
        "price": row.get("price"),
        "currency_id": row.get("currency_id"),
        "category_id": row.get("category_id"),
        "source": source,
        "description": description,
        "page_snapshot": snapshot.get("page_snapshot") or {},
        "plugin_snapshot": snapshot.get("plugin_snapshot") or {},
        "weight_g": row.get("weight_g"),
        "package_length_cm": row.get("package_length_cm"),
        "package_width_cm": row.get("package_width_cm"),
        "package_height_cm": row.get("package_height_cm"),
        "scrape_status": "ok",
    }


def _prepared_listing_from_product_row(
    row: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    snapshot = _product_source_snapshot(row)
    if snapshot is None:
        return None
    source = dict(snapshot["source"])
    source.setdefault("id", str(row.get("source_item_id") or ""))
    source.setdefault("site_id", str(source.get("id") or "")[:3])
    from erp.mercadolibre_source_store import _merge_package_attributes

    source["attributes"] = _merge_package_attributes(
        list(source.get("attributes") or []), snapshot
    )
    return source, dict(snapshot["description"])


def _sync_product_source_snapshot(row: Mapping[str, Any]) -> None:
    """Refresh the publication source row from the selected product snapshot."""
    snapshot = _product_source_snapshot(row)
    if snapshot is None:
        return
    from erp.mercadolibre_source_store import upsert_source_snapshot

    upsert_source_snapshot(snapshot)


def publish_product_batch(
    product_rows: Iterable[Mapping[str, Any]],
    *,
    token_id: int,
    site_id: str = "MLM",
    quantity: int = 1,
    workers: int = 10,
    discount_rate: Any = None,
    update_state: Callable[..., Any],
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    client: MercadoLibreClient | None = None,
    batch_id: str = "",
    created_by: str = "",
    create_records: Callable[..., Mapping[int, int]] | None = None,
    update_record: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    rows = [dict(row) for row in product_rows or []]
    validate_publishable_products(rows)
    quantity = int(quantity)
    if quantity < 1 or quantity > 9999:
        raise ValueError("上架库存必须在 1-9999 之间")
    workers = int(workers)
    if workers < 1:
        raise ValueError("上架并发必须是大于 0 的整数")
    site_id = normalize_marketplace_site(site_id)
    site_name = marketplace_site_name(site_id)
    token = _token_record(token_id)
    resolved_discount_rate = site_discount_rate(token, site_id, discount_rate)
    token_site_id = str(token.get("site_id") or "").strip().upper()
    if token_site_id and token_site_id != "CBT" and token_site_id != site_id:
        raise ValueError(
            f"授权店铺属于 {token_site_id} 站点，不能上架到 {site_id}；"
            "跨站点发布需要 Global Selling(CBT) 店铺"
        )
    store_name = str(token.get("display_name") or token.get("nickname") or token_id)
    worker_count = min(workers, len(rows))
    record_ids: dict[int, int] = {}
    if create_records is not None:
        created_record_ids = create_records(
            rows,
            batch_id=str(batch_id or ""),
            token_id=int(token_id),
            store_name=store_name,
            site_id=site_id,
            site_name=site_name,
            quantity=quantity,
            created_by=str(created_by or ""),
        )
        record_ids = {
            int(product_id): int(record_id)
            for product_id, record_id in dict(created_record_ids or {}).items()
        }
        missing_record_ids = [
            int(row.get("id") or 0)
            for row in rows
            if int(row.get("id") or 0) not in record_ids
        ]
        if missing_record_ids:
            raise RuntimeError("部分产品的上架记录创建失败，已取消本次上架")
    worker_local = threading.local()
    result_lock = threading.Lock()
    result_slots: list[dict[str, Any] | None] = [None] * len(rows)
    published = failed = completed = 0
    batch_started = time.perf_counter()
    stage_totals: dict[str, float] = {}

    def worker_client() -> MercadoLibreClient:
        if client is not None:
            return client
        mercado_client = getattr(worker_local, "mercado_client", None)
        if mercado_client is None:
            mercado_client = DatabaseMercadoLibreClient(token_id)
            worker_local.mercado_client = mercado_client
        return mercado_client

    def save_record(record_id: int, **changes: Any) -> None:
        if not record_id or update_record is None:
            return
        try:
            update_record(record_id, **changes)
        except Exception:
            logging.exception("保存 Mercado 产品上架记录失败: record_id=%s", record_id)

    def finish(
        index: int,
        row: Mapping[str, Any],
        item_result: dict[str, Any],
    ) -> None:
        nonlocal published, failed, completed
        with result_lock:
            result_slots[index] = item_result
            for stage, duration in dict(item_result.get("timings") or {}).items():
                try:
                    stage_totals[str(stage)] = stage_totals.get(str(stage), 0.0) + float(
                        duration or 0
                    )
                except (TypeError, ValueError):
                    continue
            completed += 1
            if item_result["status"] == "published":
                published += 1
            else:
                failed += 1
            progress = {
                "current": completed,
                "total": len(rows),
                "product_id": int(row.get("id") or 0),
                "source_item_id": str(row.get("source_item_id") or ""),
                "published_count": published,
                "failed_count": failed,
                "worker_count": worker_count,
                "average_stage_seconds": {
                    stage: round(total / max(1, completed), 4)
                    for stage, total in stage_totals.items()
                },
                "message": f"多线程上架进度 {completed}/{len(rows)}",
            }
        if on_progress:
            on_progress(progress)

    def publish_one(index: int, row: Mapping[str, Any]) -> None:
        item_started = time.perf_counter()
        product_id = int(row.get("id") or 0)
        record_id = int(record_ids.get(product_id) or 0)
        source_item_id = str(row.get("source_item_id") or "")
        source_url = str(row.get("source_url") or source_item_id)
        source_net_proceeds = float(_decimal_value(row.get("net_proceeds_usd")) or 0)
        publish_net_proceeds = discounted_net_proceeds_usd(
            row, resolved_discount_rate
        )
        try:
            save_record(record_id, status="publishing", started=True)
            update_state(
                product_id,
                status="publishing",
                store_name=store_name,
                token_id=int(token_id),
            )
            prepared_listing = _prepared_listing_from_product_row(row)
            if prepared_listing is None:
                # Legacy rows without an embedded snapshot keep the compatible
                # database path; normal collected rows avoid the write/read pair.
                _sync_product_source_snapshot(row)
            publication = follow_sell(
                worker_client(),
                source_url,
                quantity=quantity,
                net_proceeds=publish_net_proceeds,
                destination_site_id=site_id,
                source_from_database=True,
                prepared_listing=prepared_listing,
                publish=True,
            )
            published_item_id = _published_item_id(publication)
            state_warning = ""
            try:
                update_state(
                    product_id,
                    status="published",
                    store_name=store_name,
                    token_id=int(token_id),
                    published_item_id=published_item_id,
                    result=publication,
                    finished=True,
                )
            except Exception as state_exc:
                state_warning = f"；保存产品最新状态时出错: {state_exc}"[:2000]
                logging.exception(
                    "Mercado 产品已上架，但保存产品状态失败: product_id=%s",
                    product_id,
                )
            save_record(
                record_id,
                status="published",
                published_item_id=published_item_id,
                failure_reason="",
                result=publication,
                finished=True,
            )
            item_result = {
                "record_id": record_id,
                "product_id": product_id,
                "source_item_id": source_item_id,
                "status": "published",
                "published_item_id": published_item_id,
                "discount_rate": float(resolved_discount_rate),
                "source_net_proceeds_usd": source_net_proceeds,
                "publish_net_proceeds_usd": publish_net_proceeds,
                "timings": dict(publication.get("timings") or {}),
                "message": (
                    f"上架成功；净收益 USD {source_net_proceeds:.2f} × "
                    f"{float(resolved_discount_rate):g}% = USD {publish_net_proceeds:.2f}"
                    f"{state_warning}"
                ),
            }
        except Exception as exc:
            message = str(exc)[:2000]
            try:
                update_state(
                    product_id,
                    status="failed",
                    store_name=store_name,
                    token_id=int(token_id),
                    error_message=message,
                    finished=True,
                )
            except Exception as state_exc:
                message = f"{message}；保存失败状态时出错: {state_exc}"[:2000]
            save_record(
                record_id,
                status="failed",
                failure_reason=message,
                finished=True,
            )
            item_result = {
                "record_id": record_id,
                "product_id": product_id,
                "source_item_id": source_item_id,
                "status": "failed",
                "published_item_id": "",
                "timings": {"total": round(time.perf_counter() - item_started, 4)},
                "message": message,
            }
        finish(index, row, item_result)

    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="mercado-publish",
    ) as executor:
        futures = [
            executor.submit(publish_one, index, row)
            for index, row in enumerate(rows)
        ]
        for future in as_completed(futures):
            future.result()

    results = [result for result in result_slots if result is not None]
    elapsed_seconds = time.perf_counter() - batch_started
    timing_averages = {
        stage: round(total / max(1, len(results)), 4)
        for stage, total in stage_totals.items()
    }

    return {
        "batch_id": str(batch_id or ""),
        "store_name": store_name,
        "site_id": site_id,
        "site_name": site_name,
        "token_id": int(token_id),
        "quantity": quantity,
        "discount_rate": float(resolved_discount_rate),
        "worker_count": worker_count,
        "requested_count": len(rows),
        "published_count": published,
        "failed_count": failed,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "items_per_minute": round(
            len(results) / elapsed_seconds * 60, 2
        ) if elapsed_seconds else 0,
        "average_stage_seconds": timing_averages,
        "finished_at": datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
        "results": results,
    }


__all__ = [
    "DatabaseMercadoLibreClient",
    "discounted_net_proceeds_usd",
    "publish_product_batch",
    "site_discount_rate",
    "product_publish_issues",
    "validate_publishable_products",
]
