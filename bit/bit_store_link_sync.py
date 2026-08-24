"""Manual synchronization of every listing owned by authorized stores."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from bit import bit_mysql, mercado_tokens
from bit.bit_runtime_lock import InterProcessLock, get_lock_owner
from erp.mercadolibre_store_link_store import (
    finalize_store_snapshot,
    replace_store_snapshot,
)
from erp.mercadolibre_collection_store import upsert_pulled_store_links_to_products
from mercado_api.client import MercadoAPIError, MercadoLibreClient


STORE_LINK_SYNC_LOCK_KEY = "mercado_store_link_sync_task"
STORE_LINK_WRITE_BATCH_SIZE = 100
STORE_LINK_DETAIL_WORKERS = 12
STORE_LINK_DETAIL_ATTRIBUTES = (
    "id", "site_id", "title", "permalink", "secure_thumbnail", "thumbnail",
    "pictures", "status", "price", "currency_id", "available_quantity",
    "sold_quantity", "seller_custom_field", "category_id", "listing_type_id",
    "shipping", "attributes", "seller_id", "cbt_item_id", "net_proceeds",
)
STORE_LINK_STATUSES = ("active", "paused", "closed", "under_review")
SITE_ITEM_URLS = {
    "MLM": "https://articulo.mercadolibre.com.mx",
    "MLB": "https://produto.mercadolivre.com.br",
    "MLC": "https://articulo.mercadolibre.cl",
    "MCO": "https://articulo.mercadolibre.com.co",
    "MLA": "https://articulo.mercadolibre.com.ar",
    "MLU": "https://articulo.mercadolibre.com.uy",
    "MPE": "https://articulo.mercadolibre.com.pe",
    "MEC": "https://articulo.mercadolibre.com.ec",
}

_state_guard = threading.RLock()
_sync_state = {
    "running": False,
    "task_id": "",
    "status": "idle",
    "message": "等待同步店铺链接",
    "total_stores": 0,
    "processed_stores": 0,
    "current_store": "",
    "discovered_count": 0,
    "inserted_count": 0,
    "updated_count": 0,
    "detail_count": 0,
    "detail_failed_count": 0,
    "product_count": 0,
    "failed_count": 0,
    "started_at": "",
    "finished_at": "",
    "results": [],
    "logs": [],
}


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _state_update(**changes) -> None:
    with _state_guard:
        _sync_state.update(changes)


def _append_log(message: str) -> None:
    line = f"{_now_text()} {str(message or '').strip()}"
    with _state_guard:
        logs = list(_sync_state.get("logs") or [])
        logs.append(line)
        _sync_state["logs"] = logs[-200:]


def store_link_sync_status() -> dict:
    with _state_guard:
        state = dict(_sync_state)
        state["results"] = [dict(row) for row in state.get("results") or []]
        state["logs"] = list(state.get("logs") or [])
    owner = get_lock_owner(STORE_LINK_SYNC_LOCK_KEY)
    if owner and not state.get("running"):
        state.update(
            running=True,
            status="running",
            message="店铺链接正在其他进程同步",
            lock_owner=owner,
        )
    return state


def _token_ids(values) -> list[int]:
    result: list[int] = []
    for value in values or ():
        try:
            token_id = int(value)
        except (TypeError, ValueError):
            continue
        if token_id > 0 and token_id not in result:
            result.append(token_id)
    return result


def _token_records(selected_token_ids=None) -> list[dict]:
    selected = set(_token_ids(selected_token_ids))
    summaries = (bit_mysql.list_mercado_store_tokens() or {}).get("rows") or []
    records = []
    for summary in summaries:
        token_id = int(summary.get("id") or 0)
        if selected and token_id not in selected:
            continue
        record = bit_mysql.get_mercado_store_token(token_id)
        if record:
            records.append(dict(record))
    if selected:
        missing = selected.difference(int(row.get("id") or 0) for row in records)
        if missing:
            raise ValueError(f"选择的店铺 Token 不存在：{', '.join(map(str, sorted(missing)))}")
    if not records:
        raise ValueError("暂无已授权店铺，请先在“店铺 Token”中完成授权")
    return records


def _token_expiring(record: dict) -> bool:
    expires_at = record.get("expires_at")
    if not expires_at:
        return False
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            return False
    now = datetime.now(expires_at.tzinfo) if getattr(expires_at, "tzinfo", None) else datetime.now()
    return expires_at <= now + timedelta(minutes=5)


def _refresh_token(token_id: int) -> dict:
    mercado_tokens.refresh_and_save(
        int(token_id),
        get_token=bit_mysql.get_mercado_store_token,
        update_token=bit_mysql.update_mercado_store_token,
        record_error=bit_mysql.record_mercado_store_token_error,
    )
    return dict(bit_mysql.get_mercado_store_token(int(token_id)) or {})


def _client_and_token(record: dict) -> tuple[MercadoLibreClient, dict]:
    if _token_expiring(record) and record.get("refresh_token"):
        _append_log(f"{record.get('display_name') or record['id']} Token 即将过期，正在刷新")
        record = _refresh_token(int(record["id"]))
    token = str(record.get("access_token") or "").strip()
    if not token:
        raise ValueError("店铺缺少 Access Token，请重新授权")
    return MercadoLibreClient(token), record


def _marketplace_accounts(client: MercadoLibreClient, root_seller_id: str) -> list[dict]:
    profile = client.request("GET", f"/marketplace/users/{root_seller_id}")
    accounts = []
    for marketplace in profile.get("marketplaces") or []:
        user_id = str(marketplace.get("user_id") or "").strip()
        site_id = str(marketplace.get("site_id") or "").strip().upper()
        if user_id and site_id:
            accounts.append({"user_id": user_id, "site_id": site_id})
    if accounts:
        return accounts
    return [{"user_id": str(root_seller_id), "site_id": str(profile.get("site_id") or "CBT")}]


def _public_item_url(item_id: str, site_id: str) -> str:
    item_id = str(item_id or "").strip().upper()
    site_id = str(site_id or item_id[:3]).strip().upper()
    base_url = SITE_ITEM_URLS.get(site_id)
    digits = item_id[len(site_id) :] if item_id.startswith(site_id) else item_id
    if not base_url or not digits.isdigit():
        return ""
    return f"{base_url}/{site_id}-{digits}-_JM"


def _marketplace_link_stub(
    item_id: str,
    *,
    site_id: str,
    status: str,
    seller_id: str,
) -> dict:
    return {
        "id": str(item_id),
        "site_id": str(site_id),
        "seller_id": str(seller_id),
        "status": str(status),
        "permalink": _public_item_url(item_id, site_id),
    }


def _is_unauthorized_error(exc: BaseException) -> bool:
    message = str(exc)
    return "401" in message or "access token" in message.lower()


def _enrich_marketplace_items(
    client: MercadoLibreClient,
    stubs: list[dict],
) -> tuple[list[dict], int]:
    """Fetch local marketplace details concurrently while preserving every link."""

    if not stubs:
        return [], 0
    details_by_id: dict[str, dict] = {}
    failures = 0
    unauthorized_error: MercadoAPIError | None = None
    worker_count = max(1, min(STORE_LINK_DETAIL_WORKERS, len(stubs)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                client.get_marketplace_item,
                str(stub["id"]),
                attributes=STORE_LINK_DETAIL_ATTRIBUTES,
            ): str(stub["id"])
            for stub in stubs
        }
        for future in as_completed(futures):
            item_id = futures[future]
            try:
                detail = future.result()
                if isinstance(detail, dict) and detail.get("id"):
                    details_by_id[item_id] = detail
                else:
                    failures += 1
            except MercadoAPIError as exc:
                if _is_unauthorized_error(exc):
                    unauthorized_error = exc
                else:
                    failures += 1
            except Exception:
                failures += 1
    if unauthorized_error is not None:
        raise unauthorized_error

    enriched = []
    for stub in stubs:
        item_id = str(stub["id"])
        detail = details_by_id.get(item_id) or {}
        merged = {**stub, **detail}
        merged["id"] = item_id
        merged["site_id"] = str(detail.get("site_id") or stub.get("site_id") or "")
        merged["seller_id"] = str(detail.get("seller_id") or stub.get("seller_id") or "")
        merged["status"] = str(detail.get("status") or stub.get("status") or "")
        merged["permalink"] = str(detail.get("permalink") or stub.get("permalink") or "")
        enriched.append(merged)
    return enriched, failures


def _sync_store(record: dict) -> dict:
    token_id = int(record["id"])
    store_name = str(record.get("display_name") or record.get("nickname") or token_id)
    seller_id = str(record.get("meli_user_id") or "").strip()
    if not seller_id:
        raise ValueError("店铺授权缺少 Seller ID，请刷新 Token 或重新授权")

    client, record = _client_and_token(record)
    refreshed_after_unauthorized = False
    base_discovered = int(_sync_state.get("discovered_count") or 0)
    base_inserted = int(_sync_state.get("inserted_count") or 0)
    base_updated = int(_sync_state.get("updated_count") or 0)
    base_details = int(_sync_state.get("detail_count") or 0)
    base_detail_failures = int(_sync_state.get("detail_failed_count") or 0)
    base_products = int(_sync_state.get("product_count") or 0)
    while True:
        try:
            marker = uuid.uuid4().hex
            batch_ids = []
            batch_items = []
            totals = {
                "discovered": 0,
                "stored": 0,
                "inserted": 0,
                "updated": 0,
                "details": 0,
                "failed": 0,
                "products": 0,
            }

            def write_batch(item_ids, items):
                _state_update(
                    message=f"正在读取 {store_name} 商品详情：{totals['details']}/{totals['discovered'] + len(item_ids)}"
                )
                detailed_items, detail_failures = _enrich_marketplace_items(client, items)
                result = replace_store_snapshot(
                    record,
                    detailed_items,
                    current_item_ids=item_ids,
                    sync_marker=marker,
                    finalize=False,
                    synced_at=_now_text(),
                )
                product_result = upsert_pulled_store_links_to_products(record, detailed_items)
                totals["discovered"] += len(item_ids)
                totals["stored"] += int(result.get("total") or 0)
                totals["inserted"] += int(result.get("inserted") or 0)
                totals["updated"] += int(result.get("updated") or 0)
                totals["details"] += len(item_ids) - detail_failures
                totals["failed"] += detail_failures
                totals["products"] += int(product_result.get("count") or 0)
                _state_update(
                    message=(
                        f"正在同步 {store_name}：链接 {totals['stored']} 条，"
                        f"详情 {totals['details']} 条，产品 {totals['products']} 件"
                    ),
                    discovered_count=base_discovered + totals["discovered"],
                    inserted_count=base_inserted + totals["inserted"],
                    updated_count=base_updated + totals["updated"],
                    detail_count=base_details + totals["details"],
                    detail_failed_count=base_detail_failures + totals["failed"],
                    product_count=base_products + totals["products"],
                )

            seen_ids = set()
            accounts = _marketplace_accounts(client, seller_id)
            for account in accounts:
                site_id = account["site_id"]
                child_seller_id = account["user_id"]
                for listing_status in STORE_LINK_STATUSES:
                    _state_update(
                        message=f"正在扫描 {store_name} · {site_id} · {listing_status}"
                    )
                    for item_id in client.iter_listing_ids(
                        child_seller_id,
                        status=listing_status,
                    ):
                        item_id = str(item_id)
                        if item_id in seen_ids:
                            continue
                        seen_ids.add(item_id)
                        batch_ids.append(item_id)
                        batch_items.append(
                            _marketplace_link_stub(
                                item_id,
                                site_id=site_id,
                                status=listing_status,
                                seller_id=child_seller_id,
                            )
                        )
                        if len(batch_ids) >= STORE_LINK_WRITE_BATCH_SIZE:
                            write_batch(batch_ids, batch_items)
                            batch_ids, batch_items = [], []
            if batch_ids:
                write_batch(batch_ids, batch_items)
            finalize_store_snapshot(token_id, marker)
            return {
                "store": store_name,
                "token_id": token_id,
                "status": "success" if not totals["failed"] else "partial",
                **totals,
            }
        except MercadoAPIError as exc:
            message = str(exc)
            unauthorized = _is_unauthorized_error(exc)
            if refreshed_after_unauthorized or not unauthorized or not record.get("refresh_token"):
                raise
            record = _refresh_token(token_id)
            client = MercadoLibreClient(str(record.get("access_token") or ""))
            refreshed_after_unauthorized = True


def run_store_link_sync(token_ids=None) -> dict:
    records = _token_records(token_ids)
    _state_update(
        running=True,
        status="running",
        message="正在同步店铺链接",
        total_stores=len(records),
        processed_stores=0,
        current_store="",
        discovered_count=0,
        inserted_count=0,
        updated_count=0,
        detail_count=0,
        detail_failed_count=0,
        product_count=0,
        failed_count=0,
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
            result = _sync_store(record)
            results.append(result)
            _append_log(
                f"{store_name} 完成：发现 {result['discovered']}，新增 {result['inserted']}，更新 {result['updated']}"
            )
        except Exception as exc:
            result = {"store": store_name, "status": "error", "message": str(exc)}
            results.append(result)
            _append_log(f"{store_name} 失败：{exc}")
        _state_update(
            processed_stores=index,
            discovered_count=sum(int(row.get("discovered") or 0) for row in results),
            inserted_count=sum(int(row.get("inserted") or 0) for row in results),
            updated_count=sum(int(row.get("updated") or 0) for row in results),
            detail_count=sum(int(row.get("details") or 0) for row in results),
            detail_failed_count=sum(int(row.get("failed") or 0) for row in results),
            product_count=sum(int(row.get("products") or 0) for row in results),
            failed_count=sum(1 for row in results if row.get("status") == "error"),
            results=list(results),
        )

    failed = sum(1 for row in results if row.get("status") == "error")
    message = (
        f"同步完成：新增 {_sync_state.get('inserted_count', 0)}，更新 {_sync_state.get('updated_count', 0)}"
        if failed == 0
        else f"同步完成，{failed} 家店铺失败"
    )
    _state_update(
        running=False,
        status="completed" if failed == 0 else "partial",
        message=message,
        current_store="",
        finished_at=_now_text(),
        results=list(results),
    )
    _append_log(message)
    return store_link_sync_status()


def _run_background(token_ids) -> None:
    task_lock = InterProcessLock(
        STORE_LINK_SYNC_LOCK_KEY,
        owner="bit_store_link_sync",
        metadata={"task_id": _sync_state.get("task_id")},
    )
    if not task_lock.acquire(timeout=0):
        _state_update(
            running=False,
            status="busy",
            message="店铺链接同步已在其他进程运行",
            finished_at=_now_text(),
        )
        return
    try:
        run_store_link_sync(token_ids)
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


def start_store_link_sync(token_ids=None) -> tuple[bool, dict]:
    selected_ids = _token_ids(token_ids)
    with _state_guard:
        if _sync_state.get("running"):
            return False, store_link_sync_status()
        _sync_state.update(
            running=True,
            task_id=uuid.uuid4().hex,
            status="starting",
            message="正在启动店铺链接同步",
            total_stores=0,
            processed_stores=0,
            current_store="",
            discovered_count=0,
            inserted_count=0,
            updated_count=0,
            detail_count=0,
            detail_failed_count=0,
            product_count=0,
            failed_count=0,
            started_at=_now_text(),
            finished_at="",
            results=[],
            logs=[],
        )
    thread = threading.Thread(
        target=_run_background,
        args=(selected_ids,),
        name="mercado-store-link-sync",
        daemon=True,
    )
    thread.start()
    return True, store_link_sync_status()


__all__ = [
    "run_store_link_sync",
    "start_store_link_sync",
    "store_link_sync_status",
]
