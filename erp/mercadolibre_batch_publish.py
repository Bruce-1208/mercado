"""Batch-publish collected products to one centrally authorized store."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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


def _sync_product_source_snapshot(row: Mapping[str, Any]) -> None:
    """Refresh the publication source row from the selected product snapshot."""
    raw_snapshot = row.get("source_snapshot_json")
    if not raw_snapshot:
        return
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
    from erp.mercadolibre_source_store import upsert_source_snapshot

    upsert_source_snapshot(
        {
            "item_id": row.get("source_item_id"),
            "source_url": row.get("source_url"),
            "final_url": source.get("permalink") or row.get("source_url"),
            "main_image_url": row.get("main_image_url"),
            "title": row.get("title"),
            "price": row.get("price"),
            "currency_id": row.get("currency_id"),
            "category_id": row.get("category_id"),
            "source": source,
            "description": snapshot.get("description") or {},
            "page_snapshot": snapshot.get("page_snapshot") or {},
            "plugin_snapshot": snapshot.get("plugin_snapshot") or {},
            "weight_g": row.get("weight_g"),
            "package_length_cm": row.get("package_length_cm"),
            "package_width_cm": row.get("package_width_cm"),
            "package_height_cm": row.get("package_height_cm"),
            "scrape_status": "ok",
        }
    )


def publish_product_batch(
    product_rows: Iterable[Mapping[str, Any]],
    *,
    token_id: int,
    site_id: str = "MLM",
    quantity: int = 1,
    workers: int = 4,
    update_state: Callable[..., Any],
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    client: MercadoLibreClient | None = None,
) -> dict[str, Any]:
    rows = [dict(row) for row in product_rows or []]
    if not rows:
        raise ValueError("请至少勾选一个产品")
    blocked = [row for row in rows if row.get("review_status") != "approved"]
    if blocked:
        raise ValueError(
            f"只有审核状态为“通过”的产品可以上架；当前选择中有 {len(blocked)} 件未通过"
        )
    quantity = int(quantity)
    if quantity < 1 or quantity > 9999:
        raise ValueError("上架库存必须在 1-9999 之间")
    workers = int(workers)
    if workers < 1 or workers > 8:
        raise ValueError("上架线程数必须在 1-8 之间")
    site_id = normalize_marketplace_site(site_id)
    site_name = marketplace_site_name(site_id)
    token = _token_record(token_id)
    token_site_id = str(token.get("site_id") or "").strip().upper()
    if token_site_id and token_site_id != "CBT" and token_site_id != site_id:
        raise ValueError(
            f"授权店铺属于 {token_site_id} 站点，不能上架到 {site_id}；"
            "跨站点发布需要 Global Selling(CBT) 店铺"
        )
    store_name = str(token.get("display_name") or token.get("nickname") or token_id)
    worker_count = min(workers, len(rows))
    worker_local = threading.local()
    result_lock = threading.Lock()
    result_slots: list[dict[str, Any] | None] = [None] * len(rows)
    published = failed = completed = 0

    def worker_client() -> MercadoLibreClient:
        if client is not None:
            return client
        mercado_client = getattr(worker_local, "mercado_client", None)
        if mercado_client is None:
            mercado_client = DatabaseMercadoLibreClient(token_id)
            worker_local.mercado_client = mercado_client
        return mercado_client

    def finish(
        index: int,
        row: Mapping[str, Any],
        item_result: dict[str, Any],
    ) -> None:
        nonlocal published, failed, completed
        with result_lock:
            result_slots[index] = item_result
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
                "message": f"多线程上架进度 {completed}/{len(rows)}",
            }
        if on_progress:
            on_progress(progress)

    def publish_one(index: int, row: Mapping[str, Any]) -> None:
        product_id = int(row.get("id") or 0)
        source_item_id = str(row.get("source_item_id") or "")
        source_url = str(row.get("source_url") or source_item_id)
        try:
            update_state(
                product_id,
                status="publishing",
                store_name=store_name,
                token_id=int(token_id),
            )
            _sync_product_source_snapshot(row)
            publication = follow_sell(
                worker_client(),
                source_url,
                quantity=quantity,
                destination_site_id=site_id,
                source_from_database=True,
                publish=True,
            )
            published_item_id = _published_item_id(publication)
            update_state(
                product_id,
                status="published",
                store_name=store_name,
                token_id=int(token_id),
                published_item_id=published_item_id,
                result=publication,
                finished=True,
            )
            item_result = {
                "product_id": product_id,
                "source_item_id": source_item_id,
                "status": "published",
                "published_item_id": published_item_id,
                "message": "上架成功",
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
            item_result = {
                "product_id": product_id,
                "source_item_id": source_item_id,
                "status": "failed",
                "published_item_id": "",
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

    return {
        "store_name": store_name,
        "site_id": site_id,
        "site_name": site_name,
        "token_id": int(token_id),
        "quantity": quantity,
        "worker_count": worker_count,
        "requested_count": len(rows),
        "published_count": published,
        "failed_count": failed,
        "finished_at": datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
        "results": results,
    }


__all__ = [
    "DatabaseMercadoLibreClient",
    "publish_product_batch",
]
