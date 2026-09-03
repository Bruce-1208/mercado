import os
import requests

from bit.workbench_runtime import bootstrap_runtime


RUNTIME_SETTINGS = bootstrap_runtime()

DB_API_BASE_URL = os.environ.get("BIT_DB_API_BASE_URL", "http://zeshun.nat100.top").rstrip("/")
DB_API_TOKEN = os.environ.get("BIT_DB_API_TOKEN", "")
DB_API_SESSION = requests.Session()
DB_API_SESSION.trust_env = False


def _resolve_db_mode():
    return "api" if RUNTIME_SETTINGS.is_client else "mysql"


DB_MODE = _resolve_db_mode()


def _local_call(function_name, *args, **kwargs):
    """延迟导入 MySQL 实现，避免 Windows/API 客户端加载本地数据库依赖。"""
    from bit import bit_mysql

    function = getattr(bit_mysql, function_name)
    # timeout 是 HTTP 层参数，本地 MySQL 函数不需要。
    kwargs.pop("timeout", None)
    return function(*args, **kwargs)


def _local_interface_call(function_name, *args, **kwargs):
    from bit import bit_interface

    return getattr(bit_interface, function_name)(*args, **kwargs)


def _local_inventory_call(function_name, *args, **kwargs):
    from bit import bit_inventory

    return getattr(bit_inventory, function_name)(*args, **kwargs)


def _headers():
    headers = {"Content-Type": "application/json"}
    if DB_API_TOKEN:
        headers["X-Internal-Token"] = DB_API_TOKEN
    return headers


def _request(method, path, **kwargs):
    url = f"{DB_API_BASE_URL}{path}"
    timeout = kwargs.pop("timeout", 60)
    try:
        response = DB_API_SESSION.request(method, url, headers=_headers(), timeout=timeout, **kwargs)
    except requests.RequestException as e:
        raise RuntimeError(f"数据库接口请求失败：{url}，请确认 bit_interface.py 已启动。原因：{e}") from e

    try:
        payload = response.json()
    except ValueError as e:
        content_type = response.headers.get("Content-Type", "")
        body_preview = (response.text or "")[:500].replace("\n", " ").replace("\r", " ")
        raise RuntimeError(
            f"数据库接口返回非 JSON：{url}，状态码：{response.status_code}，"
            f"Content-Type：{content_type}，返回内容：{body_preview}"
        ) from e

    if not response.ok or payload.get("status") not in ("success", None):
        raise RuntimeError(payload.get("message") or f"数据库接口请求失败：{url}，状态码：{response.status_code}")
    return payload.get("data")


def insert_task_record(record_list):
    if DB_MODE == "mysql":
        return _local_call("insert_task_record", record_list)
    return _request("POST", "/api/db/task-records", json={"records": record_list})


def get_latest_order_print_records():
    if DB_MODE == "mysql":
        return _local_call("get_latest_order_print_records")
    return _request("GET", "/api/db/task-records/order-print/latest")


def get_database_api_health():
    if DB_MODE == "mysql":
        from bit.bit_mysql import config

        return {"role": "server", "database_host": config.get("host")}
    return _request("GET", "/api/db/health", timeout=10)


def list_official_infraction_dashboard(**filters):
    if DB_MODE == "mysql":
        from erp.mercadolibre_infraction_store import list_infraction_dashboard

        return list_infraction_dashboard(**filters)
    return _request(
        "GET",
        "/api/db/official-infractions/dashboard",
        params={key: value for key, value in filters.items() if value is not None},
    )


def get_current_infraction_counts_by_token_site(days=100):
    if DB_MODE == "mysql":
        from erp.mercadolibre_infraction_store import (
            current_infraction_counts_by_token_site,
        )

        return current_infraction_counts_by_token_site(days)
    data = _request(
        "GET",
        "/api/db/official-infractions/current-counts",
        params={"days": days},
    ) or {}
    rows = data.pop("count_rows", [])
    data["counts"] = {
        (int(row.get("token_id") or 0), str(row.get("site_id") or "").upper()): {
            "infraction_count": int(row.get("infraction_count") or 0),
            "rights_holder_count": int(row.get("rights_holder_count") or 0),
            "latest_infraction_at": row.get("latest_infraction_at"),
        }
        for row in rows
        if int(row.get("token_id") or 0) > 0 and str(row.get("site_id") or "").strip()
    }
    return data


def start_official_infraction_sync(token_ids=None):
    if DB_MODE == "mysql":
        from bit import mercado_infraction_sync

        return mercado_infraction_sync.start_official_infraction_sync(token_ids or [])
    data = _request(
        "POST",
        "/api/db/official-infractions/sync",
        json={"token_ids": list(token_ids or [])},
    ) or {}
    return bool(data.get("started")), dict(data.get("state") or {})


def get_official_infraction_sync_status():
    if DB_MODE == "mysql":
        from bit import mercado_infraction_sync

        return mercado_infraction_sync.official_infraction_sync_status()
    return _request("GET", "/api/db/official-infractions/sync/status")


def inset_reputation_info(
    reputation_list,
    merge_latest=False,
    replace_targets=None,
):
    if DB_MODE == "mysql":
        return _local_call(
            "inset_reputation_info",
            reputation_list,
            merge_latest,
            replace_targets,
        )
    return _request(
        "POST",
        "/api/db/reputation/bulk",
        json={
            "rows": reputation_list,
            "merge_latest": bool(merge_latest),
            "replace_targets": list(replace_targets or ()),
        },
    )


def inset_infraction_info(
    infraction_list,
    merge_latest=False,
    replace_targets=None,
):
    if DB_MODE == "mysql":
        return _local_call(
            "inset_infraction_info",
            infraction_list,
            merge_latest,
            replace_targets,
        )
    return _request(
        "POST",
        "/api/db/infractions/bulk",
        json={
            "rows": infraction_list,
            "merge_latest": bool(merge_latest),
            "replace_targets": list(replace_targets or ()),
        },
    )


def inset_delay_info(delay_list):
    if DB_MODE == "mysql":
        return _local_call("inset_delay_info", delay_list)
    return _request("POST", "/api/db/delays/bulk", json={"rows": delay_list})


def inset_pago_info(pago_list):
    if DB_MODE == "mysql":
        return _local_call("inset_pago_info", pago_list)
    return _request("POST", "/api/db/pago/bulk", json={"rows": pago_list})


def insert_zying_product_info(product_list):
    if DB_MODE == "mysql":
        return _local_call("insert_zying_product_info", product_list)
    data = _request("POST", "/api/db/zying-products/bulk", json={"rows": product_list})
    return (data or {}).get("count", 0)


def upsert_zying_products_to_products(product_list):
    if DB_MODE == "mysql":
        from erp.mercadolibre_collection_store import (
            upsert_zying_products_to_products as local_upsert,
        )

        return local_upsert(product_list)
    return _request(
        "POST",
        "/api/db/zying-products/product-list",
        json={"rows": product_list},
    )


def get_existing_zying_product_ids(product_ids):
    normalized_ids = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in (product_ids or ())
            if str(value or "").strip()
        )
    )
    if not normalized_ids:
        return set()
    if DB_MODE == "mysql":
        return set(_local_call("get_existing_zying_product_ids", normalized_ids))
    data = _request(
        "POST",
        "/api/db/zying-products/existing",
        json={"product_ids": normalized_ids},
    )
    return set((data or {}).get("product_ids") or ())


def get_zying_risk_candidates(
    hours=24,
    limit=0,
    zying_category=None,
    include_checked=False,
):
    if DB_MODE == "mysql":
        return _local_call(
            "get_zying_risk_candidates",
            hours,
            limit,
            zying_category,
            include_checked,
        )
    return _request(
        "GET",
        "/api/db/zying-risk/candidates",
        params={
            "hours": hours,
            "limit": limit,
            "category": zying_category or "",
            "include_checked": "1" if include_checked else "0",
        },
    )


def update_zying_product_risks(results):
    if DB_MODE == "mysql":
        return _local_call("update_zying_product_risks", results)
    data = _request(
        "POST",
        "/api/db/zying-risk/bulk",
        json={"results": results or []},
    )
    return int((data or {}).get("count", 0))


def list_zying_risk_categories():
    if DB_MODE == "mysql":
        return _local_call("list_zying_risk_categories")
    return _request("GET", "/api/db/zying-risk/categories")


def get_zying_risk_results(
    zying_category=None,
    risk_level=None,
    search="",
    sort_by="risk_level",
    sort_dir="desc",
    limit=1000,
):
    if DB_MODE == "mysql":
        return _local_call(
            "get_zying_risk_results",
            zying_category,
            risk_level,
            search,
            sort_by,
            sort_dir,
            limit,
        )
    return _request(
        "GET",
        "/api/db/zying-risk/results",
        params={
            "category": zying_category or "",
            "risk_level": risk_level if risk_level is not None else "",
            "search": search or "",
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            "limit": limit,
        },
    )


def insert_orders(line):
    if DB_MODE == "mysql":
        return _local_call("insert_orders", line)
    return _request("POST", "/api/db/orders/bulk", json={"rows": line})


def get_high_after_sale_alerts(
    sort_by="after_sale_quantity",
    sort_dir="desc",
    search="",
    date_from="",
    date_to="",
    limit=100,
):
    if DB_MODE == "mysql":
        return _local_call(
            "get_high_after_sale_alerts",
            sort_by,
            sort_dir,
            search,
            date_from,
            date_to,
            limit,
        )
    return _request(
        "GET",
        "/api/db/orders/high-after-sales",
        params={
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            "search": search,
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
        },
    )


def get_high_profit_products(
    sort_by="total_profit",
    sort_dir="desc",
    search="",
    date_from="",
    date_to="",
    limit=100,
):
    if DB_MODE == "mysql":
        return _local_call(
            "get_high_profit_products",
            sort_by,
            sort_dir,
            search,
            date_from,
            date_to,
            limit,
        )
    return _request(
        "GET",
        "/api/db/orders/high-profits",
        params={
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            "search": search,
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
        },
    )


def list_orders(
    country="",
    status="",
    salesperson="",
    group_name="",
    search="",
    start_date="",
    end_date="",
    origin="",
    freight_variance="",
    page=1,
    page_size=200,
    store_ids=None,
    salespeople=None,
):
    normalized_store_ids = [int(value) for value in store_ids or [] if str(value or "").isdigit()]
    normalized_salespeople = [
        str(value or "").strip() for value in salespeople or [] if str(value or "").strip()
    ]
    local_params = {
        "country": country or "",
        "status": status or "",
        "salesperson": salesperson or "",
        "group_name": group_name or "",
        "search": search or "",
        "start_date": start_date or "",
        "end_date": end_date or "",
        "origin": origin or "",
        "freight_variance": freight_variance or "",
        "page": int(page or 1),
        "page_size": int(page_size or 200),
    }
    if normalized_store_ids:
        local_params["store_ids"] = normalized_store_ids
    if normalized_salespeople:
        local_params["salespeople"] = normalized_salespeople
    if DB_MODE == "mysql":
        return _local_call("list_orders", **local_params)
    params = dict(local_params)
    if normalized_store_ids:
        params.pop("store_ids", None)
        params["store_id"] = normalized_store_ids
    if normalized_salespeople:
        params.pop("salespeople", None)
        params["salesperson"] = normalized_salespeople
    path = "/api/db/orders"
    try:
        return _request("GET", path, params=params)
    except RuntimeError as exc:
        message = str(exc or "")
        if "404" not in message or path not in message:
            raise
        return _local_call("list_orders", **local_params)


def get_order_weight_quote(order_ids):
    payload = {
        "order_ids": [
            str(value or "").strip()
            for value in order_ids or []
            if str(value or "").strip()
        ]
    }
    if DB_MODE == "mysql":
        return _local_call("get_mercado_order_weight_quote", payload["order_ids"])
    return _request("POST", "/api/db/orders/weight-quote", json=payload)


def list_inventory_stock(**filters):
    if DB_MODE == "mysql":
        return _local_inventory_call("list_inventory_stock", **filters)
    return _request("GET", "/api/db/inventory/stocks", params=filters)


def list_inventory_shelves(include_inactive=True):
    if DB_MODE == "mysql":
        return _local_inventory_call(
            "list_inventory_shelves", include_inactive=bool(include_inactive)
        )
    return _request(
        "GET",
        "/api/db/inventory/shelves",
        params={"include_inactive": "1" if include_inactive else "0"},
    )


def create_inventory_shelf(record):
    if DB_MODE == "mysql":
        return _local_inventory_call("create_inventory_shelf", record or {})
    return _request("POST", "/api/db/inventory/shelves", json=record or {})


def update_inventory_shelf(shelf_id, record):
    if DB_MODE == "mysql":
        return _local_inventory_call(
            "update_inventory_shelf", int(shelf_id), record or {}
        )
    return _request(
        "PATCH", f"/api/db/inventory/shelves/{int(shelf_id)}", json=record or {}
    )


def list_inventory_matches(search="", limit=30):
    if DB_MODE == "mysql":
        return _local_inventory_call(
            "list_inventory_matches", search=search, limit=limit
        )
    return _request(
        "GET", "/api/db/inventory/matches", params={"search": search, "limit": limit}
    )


def create_inventory_movement(record):
    if DB_MODE == "mysql":
        return _local_inventory_call("create_inventory_movement", record or {})
    return _request("POST", "/api/db/inventory/movements", json=record or {})


def list_inventory_movements(**filters):
    if DB_MODE == "mysql":
        return _local_inventory_call("list_inventory_movements", **filters)
    return _request("GET", "/api/db/inventory/movements", params=filters)


def start_order_sync(start_date="", end_date="", token_ids=None, mode="manual"):
    payload = {
        "start_date": start_date or "",
        "end_date": end_date or "",
        "token_ids": [int(value) for value in token_ids or []],
        "mode": "automatic" if str(mode) == "automatic" else "manual",
    }
    if DB_MODE == "mysql":
        from bit.bit_order_sync import start_order_sync as local_start

        started, state = local_start(**payload)
        return {"started": bool(started), "state": state}
    return _request("POST", "/api/db/order-sync/start", json=payload)


def get_order_sync_status():
    if DB_MODE == "mysql":
        from bit.bit_order_sync import order_sync_status

        return order_sync_status()
    return _request("GET", "/api/db/order-sync/status")


def _store_link_store_call(function_name, *args, **kwargs):
    from erp import mercadolibre_store_link_store

    return getattr(mercadolibre_store_link_store, function_name)(*args, **kwargs)


def list_mercado_store_links(
    search="",
    token_id=None,
    site_id="",
    group_name="",
    status="",
    sales_sort="desc",
    current_only=True,
    page=1,
    page_size=1000,
):
    params = {
        "search": search or "",
        "site_id": str(site_id or "").strip().upper(),
        "group_name": str(group_name or "").strip(),
        "status": status or "",
        "sales_sort": "asc" if str(sales_sort or "").strip().lower() == "asc" else "desc",
        "current_only": "1" if current_only else "0",
        "page": int(page or 1),
        "page_size": 1000,
    }
    if token_id not in (None, ""):
        params["token_id"] = int(token_id)
    if DB_MODE == "mysql":
        return _store_link_store_call(
            "list_store_links",
            search=params["search"],
            token_id=params.get("token_id"),
            site_id=params["site_id"],
            group_name=params["group_name"],
            status=params["status"],
            sales_sort=params["sales_sort"],
            current_only=bool(current_only),
            page=params["page"],
            page_size=params["page_size"],
        )
    return _request("GET", "/api/db/store-links", params=params)


def bulk_update_mercado_store_links(link_ids, **changes):
    payload = {"link_ids": [int(value) for value in link_ids or []], "changes": changes}
    if DB_MODE == "mysql":
        from bit.bit_store_link_remote_update import start_store_link_remote_update

        started, state = start_store_link_remote_update(payload["link_ids"], changes)
        return {"started": bool(started), "state": state}
    return _request("POST", "/api/db/store-links/bulk-update", json=payload)


def get_mercado_store_link_remote_update_status():
    if DB_MODE == "mysql":
        from bit.bit_store_link_remote_update import store_link_remote_update_status

        return store_link_remote_update_status()
    return _request("GET", "/api/db/store-links/bulk-update/status")


def start_store_link_sync(token_ids=None):
    payload = {"token_ids": [int(value) for value in token_ids or []]}
    if DB_MODE == "mysql":
        from bit.bit_store_link_sync import start_store_link_sync as local_start

        started, state = local_start(payload["token_ids"])
        return {"started": bool(started), "state": state}
    return _request("POST", "/api/db/store-links/sync/start", json=payload)


def get_store_link_sync_status():
    if DB_MODE == "mysql":
        from bit.bit_store_link_sync import store_link_sync_status

        return store_link_sync_status()
    return _request("GET", "/api/db/store-links/sync/status")


def list_mercado_prohibited_listings(
    search="",
    token_id=None,
    site_id="",
    salesperson="",
    risk_type="",
    page=1,
    page_size=100,
):
    params = {
        "search": str(search or "").strip(),
        "site_id": str(site_id or "").strip().upper(),
        "salesperson": str(salesperson or "").strip(),
        "risk_type": str(risk_type or "").strip().lower(),
        "page": int(page or 1),
        "page_size": max(20, min(int(page_size or 100), 500)),
    }
    if token_id not in (None, ""):
        params["token_id"] = int(token_id)
    if DB_MODE == "mysql":
        from erp.mercadolibre_prohibited_store import list_prohibited_listings

        return list_prohibited_listings(**params)
    return _request("GET", "/api/db/prohibited-listings", params=params)


def start_prohibited_listing_sync(token_ids=None):
    payload = {"token_ids": [int(value) for value in token_ids or []]}
    if DB_MODE == "mysql":
        from bit.bit_prohibited_listing_sync import start_prohibited_listing_sync as local_start
        from bit.mercado_infraction_sync import start_official_infraction_sync

        started, state = local_start(payload["token_ids"])
        rights_started, rights_state = start_official_infraction_sync(payload["token_ids"])
        combined_state = dict(state or {})
        combined_state["prohibited_running"] = bool(combined_state.get("running"))
        combined_state["rights_holder_sync"] = dict(rights_state or {})
        combined_state["running"] = bool(
            combined_state.get("prohibited_running") or rights_state.get("running")
        )
        return {"started": bool(started or rights_started), "state": combined_state}
    return _request("POST", "/api/db/prohibited-listings/sync/start", json=payload)


def get_prohibited_listing_sync_status():
    if DB_MODE == "mysql":
        from bit.bit_prohibited_listing_sync import prohibited_listing_sync_status
        from bit.mercado_infraction_sync import official_infraction_sync_status

        state = dict(prohibited_listing_sync_status() or {})
        rights_state = dict(official_infraction_sync_status() or {})
        state["prohibited_running"] = bool(state.get("running"))
        state["rights_holder_sync"] = rights_state
        state["running"] = bool(state.get("prohibited_running") or rights_state.get("running"))
        return state
    return _request("GET", "/api/db/prohibited-listings/sync/status")


def bulk_update_orders(order_ids, operator_id=None, operator_name="", **changes):
    payload = {"order_ids": [str(value) for value in order_ids or []]}
    for field in (
        "workflow_status", "purchase_order", "purchase_tracking",
        "logistics_company", "purchase_cost", "purchase_remark",
    ):
        if field in changes:
            payload[field] = changes.get(field)
    payload["operator_id"] = operator_id
    payload["operator_name"] = str(operator_name or "")
    if DB_MODE == "mysql":
        return _local_call(
            "bulk_update_mercado_orders",
            payload.pop("order_ids"),
            **payload,
        )
    return _request("POST", "/api/db/orders/bulk-update", json=payload)


def download_order_labels(order_ids):
    payload = {"order_ids": [str(value) for value in order_ids or []]}
    if DB_MODE == "mysql":
        from bit.bit_order_labels import download_order_labels as local_download

        return local_download(payload["order_ids"])
    url = f"{DB_API_BASE_URL}/api/db/orders/labels"
    try:
        response = DB_API_SESSION.post(
            url,
            headers=_headers(),
            timeout=120,
            json=payload,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"美客多面单接口请求失败：{exc}") from exc
    if not response.ok:
        try:
            message = (response.json() or {}).get("message")
        except ValueError:
            message = (response.text or "")[:500]
        raise RuntimeError(message or f"美客多面单接口返回 {response.status_code}")
    printed_header = response.headers.get("X-Mercado-Printed-Order-Ids", "")
    printed_order_ids = [value for value in printed_header.split(",") if value]
    if not printed_header:
        # Compatibility with a database service that has not yet been updated
        # to return partial-batch metadata.
        printed_order_ids = payload["order_ids"]
    return {
        "content": bytes(response.content),
        "filename": response.headers.get("X-Mercado-Label-Filename") or "mercado-labels.pdf",
        "order_ids": printed_order_ids,
        "requested_order_ids": payload["order_ids"],
        "shipment_count": int(response.headers.get("X-Mercado-Shipment-Count") or 0),
        "skipped_order_count": int(
            response.headers.get("X-Mercado-Skipped-Order-Count") or 0
        ),
        "failed_order_count": int(
            response.headers.get("X-Mercado-Failed-Order-Count") or 0
        ),
    }


def record_order_print_logs(order_ids, operator_id=None, operator_name=""):
    payload = {
        "order_ids": [str(value) for value in order_ids or []],
        "operator_id": operator_id,
        "operator_name": str(operator_name or ""),
    }
    if DB_MODE == "mysql":
        return _local_call(
            "record_mercado_order_print_logs",
            payload["order_ids"],
            operator_id=operator_id,
            operator_name=operator_name,
        )
    data = _request("POST", "/api/db/orders/print-logs", json=payload)
    return int((data or {}).get("count") or 0)


def list_order_operation_logs(order_id, limit=100):
    if DB_MODE == "mysql":
        return _local_call("list_mercado_order_operation_logs", str(order_id), limit=limit)
    return _request(
        "GET",
        f"/api/db/orders/{str(order_id)}/logs",
        params={"limit": int(limit or 100)},
    )


def get_order_tracking(order_id):
    if DB_MODE == "mysql":
        from bit.bit_logistics import query_order_tracking

        return query_order_tracking(str(order_id))
    return _request("GET", f"/api/db/orders/{str(order_id)}/tracking")


def insert_chat_info(name, site, message, chat, response, time):
    if DB_MODE == "mysql":
        return _local_call("insert_chat_info", name, site, message, chat, response, time)
    payload = {
        "name": name,
        "site": site,
        "message": message,
        "chat": chat,
        "response": response,
        "time": time,
    }
    return _request("POST", "/api/db/chat", json=payload)


def insert_appeal_chat_record(record, timeout=5):
    if DB_MODE == "mysql":
        return _local_call("insert_appeal_chat_record", record)
    path = "/api/db/appeal-chat-records"
    url = f"{DB_API_BASE_URL}{path}"
    try:
        response = DB_API_SESSION.post(
            url,
            headers=_headers(),
            timeout=timeout,
            json={"record": record or {}},
        )
        payload = response.json()
    except requests.RequestException as e:
        raise RuntimeError(f"AI申诉聊天记录接口请求失败：{url}，原因：{e}") from e
    except ValueError as e:
        raise RuntimeError(f"AI申诉聊天记录接口返回非 JSON：{url}，状态码：{response.status_code}") from e

    if not response.ok or payload.get("status") not in ("success", None):
        raise RuntimeError(payload.get("message") or f"AI申诉聊天记录接口请求失败：{url}，状态码：{response.status_code}")
    return payload.get("data")


def insert_ai_appeal_record(record, timeout=10):
    if DB_MODE == "mysql":
        return _local_call("insert_ai_appeal_record", record)
    return _request("POST", "/api/db/ai-appeal-records", timeout=timeout, json={"record": record or {}})


def get_ai_appeal_records(limit=100):
    if DB_MODE == "mysql":
        return _local_call("get_ai_appeal_records", limit)
    return _request("GET", "/api/db/ai-appeal-records", params={"limit": limit})


def list_appeal_phrases():
    if DB_MODE == "mysql":
        return _local_call("list_appeal_phrases")
    return _request("GET", "/api/db/appeal-phrases")


def get_random_appeal_phrase(appeal_type):
    if DB_MODE == "mysql":
        return _local_call("get_random_appeal_phrase", appeal_type)
    return _request(
        "GET",
        "/api/db/appeal-phrases/random",
        params={"appeal_type": appeal_type},
    )


def create_appeal_phrase(record):
    if DB_MODE == "mysql":
        return _local_call("create_appeal_phrase", record)
    return _request("POST", "/api/db/appeal-phrases", json=record or {})


def update_appeal_phrase(phrase_id, record):
    if DB_MODE == "mysql":
        return _local_call("update_appeal_phrase", phrase_id, record)
    return _request(
        "PUT",
        f"/api/db/appeal-phrases/{int(phrase_id)}",
        json=record or {},
    )


def delete_appeal_phrase(phrase_id):
    if DB_MODE == "mysql":
        return _local_call("delete_appeal_phrase", phrase_id)
    return _request("DELETE", f"/api/db/appeal-phrases/{int(phrase_id)}")


def list_infringement_knowledge(list_type="", search="", limit=2000):
    if DB_MODE == "mysql":
        return _local_call(
            "list_infringement_knowledge",
            list_type=list_type,
            search=search,
            limit=limit,
        )
    return _request(
        "GET",
        "/api/db/infringement-knowledge",
        params={"list_type": list_type, "search": search, "limit": limit},
    )


def create_infringement_knowledge(record):
    if DB_MODE == "mysql":
        return _local_call("create_infringement_knowledge", record)
    return _request("POST", "/api/db/infringement-knowledge", json=record or {})


def update_infringement_knowledge(record_id, record):
    if DB_MODE == "mysql":
        return _local_call("update_infringement_knowledge", record_id, record)
    return _request(
        "PUT",
        f"/api/db/infringement-knowledge/{int(record_id)}",
        json=record or {},
    )


def delete_infringement_knowledge(record_id):
    if DB_MODE == "mysql":
        return _local_call("delete_infringement_knowledge", record_id)
    return _request(
        "DELETE",
        f"/api/db/infringement-knowledge/{int(record_id)}",
    )


def bulk_create_infringement_knowledge(records):
    if DB_MODE == "mysql":
        return _local_call("bulk_create_infringement_knowledge", records)
    return _request(
        "POST",
        "/api/db/infringement-knowledge/bulk",
        json={"records": list(records or [])},
    )


def get_infringement_knowledge_analysis_sources(
    infraction_limit=10000,
    active_limit=5000,
):
    if DB_MODE == "mysql":
        return _local_call(
            "get_infringement_knowledge_analysis_sources",
            infraction_limit=infraction_limit,
            active_limit=active_limit,
        )
    return _request(
        "GET",
        "/api/db/infringement-knowledge/analysis-sources",
        params={
            "infraction_limit": infraction_limit,
            "active_limit": active_limit,
        },
        timeout=120,
    )


def upsert_analyzed_infringement_knowledge(records):
    if DB_MODE == "mysql":
        return _local_call("upsert_analyzed_infringement_knowledge", records)
    return _request(
        "POST",
        "/api/db/infringement-knowledge/analyzed",
        json={"records": list(records or [])},
        timeout=120,
    )


def get_latest_infraction_info(recent_days=30):
    if DB_MODE == "mysql":
        return _local_call("get_latest_infraction_info", recent_days)
    return _request("GET", "/api/db/infractions/latest", params={"days": recent_days})


def get_latest_reputation_info():
    if DB_MODE == "mysql":
        return _local_call("get_latest_reputation_info")
    return _request("GET", "/api/db/reputation/latest")


def get_latest_pago_info(salesperson=""):
    if DB_MODE == "mysql":
        return _local_call("get_latest_pago_info", salesperson)
    return _request(
        "GET",
        "/api/db/pago/latest",
        params={"salesperson": salesperson},
    )


def list_bit_browser_configs(include_ignored=True):
    del include_ignored
    raise RuntimeError(
        "bit_browser_configs 已停用；请读取店铺授权及站点任务开关"
    )


def get_bit_browser_config(shop_name="", window_id="", include_ignored=True):
    del shop_name, window_id, include_ignored
    raise RuntimeError(
        "bit_browser_configs 已停用；请读取店铺授权及站点任务开关"
    )


def upsert_bit_browser_configs(records, replace=False):
    if DB_MODE == "mysql":
        return _local_call("upsert_bit_browser_configs", records, replace)
    return _request(
        "POST",
        "/api/db/browser-configs/bulk",
        json={"records": records or [], "replace": bool(replace)},
    )


def create_bit_browser_config(record):
    if DB_MODE == "mysql":
        return _local_call("create_bit_browser_config", record)
    return _request("POST", "/api/db/browser-configs", json=record or {})


def update_bit_browser_config(config_id, record):
    if DB_MODE == "mysql":
        return _local_call("update_bit_browser_config", config_id, record)
    return _request(
        "PUT",
        f"/api/db/browser-configs/{int(config_id)}",
        json=record or {},
    )


def delete_bit_browser_config(config_id):
    if DB_MODE == "mysql":
        return _local_call("delete_bit_browser_config", config_id)
    return _request("DELETE", f"/api/db/browser-configs/{int(config_id)}")


def _mercado_token_route_missing(exc, path="/api/db/mercado-tokens"):
    message = str(exc or "")
    return DB_MODE == "api" and "404" in message and path in message


def get_mercado_token_authorization_info():
    # The authorization URL contains no secret and can be generated by the
    # current console.  Keeping it local also supports an older DB API server.
    from bit.mercado_tokens import authorization_info

    return authorization_info()


def list_mercado_store_tokens():
    if DB_MODE == "mysql":
        return _local_call("list_mercado_store_tokens")
    try:
        return _request("GET", "/api/db/mercado-tokens")
    except RuntimeError as exc:
        if not _mercado_token_route_missing(exc):
            raise
        return _local_call("list_mercado_store_tokens")


def list_mercado_store_site_settings(token_id):
    token_id = int(token_id)
    if DB_MODE == "mysql":
        return _local_call("list_mercado_store_site_settings", token_id)
    path = f"/api/db/mercado-tokens/{token_id}/site-settings"
    try:
        return _request("GET", path)
    except RuntimeError as exc:
        if not _mercado_token_route_missing(exc, path):
            raise
        return _local_call("list_mercado_store_site_settings", token_id)


def update_mercado_store_site_settings(token_id, settings):
    token_id = int(token_id)
    if DB_MODE == "mysql":
        return _local_call("upsert_mercado_store_site_settings", token_id, settings)
    path = f"/api/db/mercado-tokens/{token_id}/site-settings"
    try:
        return _request("PUT", path, json={"settings": settings or []})
    except RuntimeError as exc:
        if not _mercado_token_route_missing(exc, path):
            raise
        return _local_call("upsert_mercado_store_site_settings", token_id, settings)


def exchange_mercado_store_token(display_name, callback_or_code):
    if DB_MODE == "mysql":
        from bit import bit_mysql
        from bit.mercado_tokens import exchange_and_save

        return exchange_and_save(
            display_name,
            callback_or_code,
            upsert=bit_mysql.upsert_mercado_store_token,
        )
    try:
        return _request(
            "POST",
            "/api/db/mercado-tokens/exchange",
            timeout=60,
            json={"display_name": display_name, "code": callback_or_code},
        )
    except RuntimeError as exc:
        if not _mercado_token_route_missing(exc, "/api/db/mercado-tokens/exchange"):
            raise
        from bit import bit_mysql
        from bit.mercado_tokens import exchange_and_save

        return exchange_and_save(
            display_name,
            callback_or_code,
            upsert=bit_mysql.upsert_mercado_store_token,
        )


def refresh_mercado_store_token(token_id):
    if DB_MODE == "mysql":
        from bit import bit_mysql
        from bit.mercado_tokens import refresh_and_save

        return refresh_and_save(
            int(token_id),
            get_token=bit_mysql.get_mercado_store_token,
            update_token=bit_mysql.update_mercado_store_token,
            record_error=bit_mysql.record_mercado_store_token_error,
        )
    path = f"/api/db/mercado-tokens/{int(token_id)}/refresh"
    try:
        return _request("POST", path, timeout=60, json={})
    except RuntimeError as exc:
        if not _mercado_token_route_missing(exc, path):
            raise
        from bit import bit_mysql
        from bit.mercado_tokens import refresh_and_save

        return refresh_and_save(
            int(token_id),
            get_token=bit_mysql.get_mercado_store_token,
            update_token=bit_mysql.update_mercado_store_token,
            record_error=bit_mysql.record_mercado_store_token_error,
        )


def _fetch_mercado_store_reputation_local(token_id):
    from bit import bit_mysql
    from bit.mercado_reputation import fetch_store_reputation
    from bit.mercado_tokens import refresh_and_save

    def refresh(identifier):
        return refresh_and_save(
            int(identifier),
            get_token=bit_mysql.get_mercado_store_token,
            update_token=bit_mysql.update_mercado_store_token,
            record_error=bit_mysql.record_mercado_store_token_error,
        )

    return fetch_store_reputation(
        int(token_id),
        get_token=bit_mysql.get_mercado_store_token,
        refresh_token=refresh,
    )


def get_mercado_store_reputation(token_id):
    """在 token 所在服务端调用官方 API，绝不向控制台返回密钥。"""

    token_id = int(token_id)
    if DB_MODE == "mysql":
        return _fetch_mercado_store_reputation_local(token_id)
    return _request(
        "GET",
        f"/api/db/mercado-tokens/{token_id}/reputation",
        timeout=60,
    )


def _execute_mercado_store_communication_local(token_id, action, payload=None):
    from bit import bit_mysql
    from bit.mercado_communications import execute_store_communication
    from bit.mercado_tokens import refresh_and_save

    token_id = int(token_id)

    def refresh(identifier):
        return refresh_and_save(
            int(identifier),
            get_token=bit_mysql.get_mercado_store_token,
            update_token=bit_mysql.update_mercado_store_token,
            record_error=bit_mysql.record_mercado_store_token_error,
        )

    return execute_store_communication(
        token_id,
        action,
        payload,
        get_token=bit_mysql.get_mercado_store_token,
        refresh_token=refresh,
        get_order_contexts=bit_mysql.list_mercado_after_sale_order_contexts,
    )


def execute_mercado_store_communication(token_id, action, payload=None):
    """在保存密钥的数据库服务端执行售前、售后或投诉操作。"""
    token_id = int(token_id)
    action = str(action or "").strip()
    if DB_MODE == "mysql":
        return _execute_mercado_store_communication_local(token_id, action, payload)
    return _request(
        "POST",
        f"/api/db/mercado-communications/{token_id}/{action}",
        timeout=60,
        json=dict(payload or {}),
    )


def rename_mercado_store_token(token_id, display_name):
    if DB_MODE == "mysql":
        return _local_call(
            "rename_mercado_store_token", int(token_id), display_name
        )
    path = f"/api/db/mercado-tokens/{int(token_id)}"
    try:
        return _request(
            "PATCH",
            path,
            json={"display_name": display_name},
        )
    except RuntimeError as exc:
        if not _mercado_token_route_missing(exc, path):
            raise
        return _local_call("rename_mercado_store_token", int(token_id), display_name)


def set_mercado_store_token_enabled(token_id, enabled):
    token_id = int(token_id)
    enabled = bool(enabled)
    if DB_MODE == "mysql":
        return _local_call("set_mercado_store_token_enabled", token_id, enabled)
    path = f"/api/db/mercado-tokens/{token_id}"
    try:
        return _request("PATCH", path, json={"enabled": enabled})
    except RuntimeError as exc:
        if not _mercado_token_route_missing(exc, path):
            raise
        return _local_call("set_mercado_store_token_enabled", token_id, enabled)


def delete_mercado_store_token(token_id):
    if DB_MODE == "mysql":
        return _local_call("delete_mercado_store_token", int(token_id))
    path = f"/api/db/mercado-tokens/{int(token_id)}"
    try:
        return _request("DELETE", path)
    except RuntimeError as exc:
        if not _mercado_token_route_missing(exc, path):
            raise
        return _local_call("delete_mercado_store_token", int(token_id))


def upsert_window_anomaly(
    window_id,
    window_name,
    site="",
    anomaly_type="需要登录",
    reason="",
    source="bit_daily_task",
):
    payload = {
        "window_id": window_id,
        "window_name": window_name,
        "site": site,
        "anomaly_type": anomaly_type,
        "reason": reason,
        "source": source,
    }
    if DB_MODE == "mysql":
        return _local_call("upsert_window_anomaly", **payload)
    return _request("POST", "/api/db/window-anomalies", timeout=10, json=payload)


def resolve_window_anomaly(window_id):
    if DB_MODE == "mysql":
        return _local_call("resolve_window_anomaly", window_id)
    return _request(
        "POST",
        "/api/db/window-anomalies/resolve",
        timeout=10,
        json={"window_id": window_id},
    )


def get_window_anomalies(active_only=True, limit=500):
    if DB_MODE == "mysql":
        return _local_call("get_window_anomalies", active_only, limit)
    return _request(
        "GET",
        "/api/db/window-anomalies",
        params={"active_only": "1" if active_only else "0", "limit": limit},
    )


def _collection_store_call(function_name, *args, **kwargs):
    """Call the ERP collection store without adding it to legacy bit_mysql."""
    from erp import mercadolibre_collection_store

    return getattr(mercadolibre_collection_store, function_name)(*args, **kwargs)


def _collection_route_missing(exc, path):
    message = str(exc or "")
    return DB_MODE == "api" and "404" in message and str(path) in message


def create_mercado_collection_task(source_url, requested_count, created_by=""):
    if DB_MODE == "mysql":
        return _collection_store_call(
            "create_collection_task", source_url, requested_count, created_by
        )
    path = "/api/db/mercado-collection/tasks"
    try:
        data = _request(
            "POST",
            path,
            json={
                "source_url": source_url,
                "requested_count": requested_count,
                "created_by": created_by,
            },
        )
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call(
            "create_collection_task", source_url, requested_count, created_by
        )
    return int(data["task_id"])


def update_mercado_collection_task(task_id, **changes):
    if DB_MODE == "mysql":
        return _collection_store_call(
            "update_collection_task", int(task_id), **changes
        )
    path = f"/api/db/mercado-collection/tasks/{int(task_id)}"
    try:
        return _request("PATCH", path, json=changes)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("update_collection_task", int(task_id), **changes)


def get_mercado_collection_task(task_id):
    if DB_MODE == "mysql":
        return _collection_store_call("get_collection_task", int(task_id))
    path = f"/api/db/mercado-collection/tasks/{int(task_id)}"
    try:
        return _request("GET", path)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("get_collection_task", int(task_id))


def upsert_mercado_collection_items(task_id, rows):
    rows = list(rows or [])
    if DB_MODE == "mysql":
        return _collection_store_call(
            "upsert_collection_items", int(task_id), rows
        )
    path = "/api/db/mercado-collection/items"
    try:
        data = _request(
            "POST", path, timeout=120,
            json={"task_id": int(task_id), "rows": rows},
        )
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("upsert_collection_items", int(task_id), rows)
    return int(data.get("count") or 0)


def list_mercado_collection_items(
    search="", limit=500, offset=0, task_id=None,
    weight_min=None, weight_max=None, price_min=None, price_max=None,
    net_proceeds_min=None, net_proceeds_max=None, date_from="", date_to="",
    exclude_added=False, management_category_id=None,
):
    params = {
        "search": search,
        "limit": limit,
        "offset": offset,
        "weight_min": weight_min,
        "weight_max": weight_max,
        "price_min": price_min,
        "price_max": price_max,
        "net_proceeds_min": net_proceeds_min,
        "net_proceeds_max": net_proceeds_max,
        "date_from": str(date_from or "").strip(),
        "date_to": str(date_to or "").strip(),
        "exclude_added": bool(exclude_added),
        "management_category_id": management_category_id,
    }
    if task_id not in (None, ""):
        params["task_id"] = int(task_id)
    if DB_MODE == "mysql":
        return _collection_store_call(
            "list_collection_items", **params
        )
    path = "/api/db/mercado-collection/items"
    try:
        return _request("GET", path, params=params)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("list_collection_items", **params)


def list_mercado_product_items(
    search="", limit=500, offset=0, source_type="", review_status="",
    publish_status="", weight_min=None, weight_max=None, price_min=None,
    price_max=None, net_proceeds_min=None, net_proceeds_max=None,
    date_from="", date_to="", management_category_id=None,
):
    params = {
        "search": search,
        "limit": limit,
        "offset": offset,
        "source_type": str(source_type or "").strip().lower(),
        "review_status": str(review_status or "").strip().lower(),
        "publish_status": str(publish_status or "").strip().lower(),
        "weight_min": weight_min,
        "weight_max": weight_max,
        "price_min": price_min,
        "price_max": price_max,
        "net_proceeds_min": net_proceeds_min,
        "net_proceeds_max": net_proceeds_max,
        "date_from": str(date_from or "").strip(),
        "date_to": str(date_to or "").strip(),
        "management_category_id": management_category_id,
    }
    if DB_MODE == "mysql":
        return _collection_store_call(
            "list_product_items", **params
        )
    path = "/api/db/mercado-products"
    try:
        return _request(
            "GET", path,
            params=params,
        )
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call(
            "list_product_items", **params
        )


def list_mercado_management_categories():
    if DB_MODE == "mysql":
        return _collection_store_call("list_management_categories")
    path = "/api/db/mercado-management-categories"
    try:
        return _request("GET", path)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("list_management_categories")


def create_mercado_management_category(name):
    payload = {"name": str(name or "")}
    if DB_MODE == "mysql":
        return _collection_store_call("create_management_category", payload["name"])
    path = "/api/db/mercado-management-categories"
    try:
        return _request("POST", path, json=payload)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("create_management_category", payload["name"])


def update_mercado_management_category(category_id, name):
    normalized_id = int(category_id)
    payload = {"name": str(name or "")}
    if DB_MODE == "mysql":
        return _collection_store_call(
            "update_management_category", normalized_id, payload["name"]
        )
    path = f"/api/db/mercado-management-categories/{normalized_id}"
    try:
        return _request("PATCH", path, json=payload)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call(
            "update_management_category", normalized_id, payload["name"]
        )


def delete_mercado_management_category(category_id):
    normalized_id = int(category_id)
    if DB_MODE == "mysql":
        return _collection_store_call("delete_management_category", normalized_id)
    path = f"/api/db/mercado-management-categories/{normalized_id}"
    try:
        return _request("DELETE", path)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("delete_management_category", normalized_id)


def assign_mercado_management_category(item_type, item_ids, category_id=None):
    payload = {
        "item_type": str(item_type or ""),
        "item_ids": [int(value) for value in item_ids or []],
        "category_id": category_id,
    }
    if DB_MODE == "mysql":
        return _collection_store_call(
            "assign_management_category",
            payload["item_type"],
            payload["item_ids"],
            payload["category_id"],
        )
    path = "/api/db/mercado-management-categories/assign"
    try:
        return _request("POST", path, json=payload)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call(
            "assign_management_category",
            payload["item_type"],
            payload["item_ids"],
            payload["category_id"],
        )


def update_mercado_product_review_status(product_item_ids, review_status):
    item_ids = [int(value) for value in product_item_ids or []]
    payload = {
        "product_item_ids": item_ids,
        "review_status": str(review_status or "").strip().lower(),
    }
    if DB_MODE == "mysql":
        return _collection_store_call(
            "update_product_review_status", item_ids, payload["review_status"]
        )
    path = "/api/db/mercado-products/review-status"
    try:
        return _request("POST", path, json=payload)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call(
            "update_product_review_status", item_ids, payload["review_status"]
        )


def update_mercado_product_item(product_item_id, changes):
    row_id = int(product_item_id)
    payload = dict(changes or {})
    if DB_MODE == "mysql":
        return _collection_store_call("update_product_item", row_id, payload)
    path = f"/api/db/mercado-products/{row_id}"
    try:
        return _request("PATCH", path, json=payload)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("update_product_item", row_id, payload)


def add_mercado_collection_items_to_products(collection_item_ids):
    item_ids = [int(value) for value in collection_item_ids or []]
    if DB_MODE == "mysql":
        return _collection_store_call(
            "add_collection_items_to_products", item_ids
        )
    path = "/api/db/mercado-products/add"
    try:
        return _request(
            "POST", path, timeout=120,
            json={"collection_item_ids": item_ids},
        )
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("add_collection_items_to_products", item_ids)


def delete_mercado_collection_items(collection_item_ids):
    item_ids = [int(value) for value in collection_item_ids or []]
    if DB_MODE == "mysql":
        return _collection_store_call("delete_collection_items", item_ids)
    path = "/api/db/mercado-collection/items/delete"
    try:
        return _request("POST", path, json={"collection_item_ids": item_ids})
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("delete_collection_items", item_ids)


def delete_mercado_product_items(product_item_ids):
    item_ids = [int(value) for value in product_item_ids or []]
    if DB_MODE == "mysql":
        return _collection_store_call("delete_product_items", item_ids)
    path = "/api/db/mercado-products/delete"
    try:
        return _request("POST", path, json={"product_item_ids": item_ids})
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("delete_product_items", item_ids)


def move_mercado_product_items_to_collection(product_item_ids, reason="不可上架"):
    item_ids = [int(value) for value in product_item_ids or []]
    payload = {
        "product_item_ids": item_ids,
        "reason": str(reason or "不可上架"),
    }
    if DB_MODE == "mysql":
        return _collection_store_call(
            "move_product_items_to_collection",
            item_ids,
            reason=payload["reason"],
        )
    path = "/api/db/mercado-products/move-to-collection"
    try:
        return _request("POST", path, json=payload)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call(
            "move_product_items_to_collection",
            item_ids,
            reason=payload["reason"],
        )


def get_mercado_product_items_by_ids(product_item_ids):
    item_ids = [int(value) for value in product_item_ids or []]
    if DB_MODE == "mysql":
        return _collection_store_call("get_product_items_by_ids", item_ids)
    path = "/api/db/mercado-products/by-ids"
    try:
        data = _request("POST", path, json={"product_item_ids": item_ids})
        return list(data.get("rows") or [])
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("get_product_items_by_ids", item_ids)


def update_mercado_product_publish_state(product_item_id, **changes):
    if DB_MODE == "mysql":
        return _collection_store_call(
            "update_product_publish_state", int(product_item_id), **changes
        )
    path = f"/api/db/mercado-products/{int(product_item_id)}/publish-state"
    try:
        return _request("PATCH", path, json=changes)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call(
            "update_product_publish_state", int(product_item_id), **changes
        )


def create_mercado_product_publish_records(
    product_rows,
    *,
    batch_id,
    token_id,
    store_name,
    site_id,
    site_name="",
    quantity=1,
    created_by="",
):
    rows = list(product_rows or [])
    payload = {
        "rows": rows,
        "batch_id": str(batch_id or ""),
        "token_id": int(token_id),
        "store_name": str(store_name or ""),
        "site_id": str(site_id or ""),
        "site_name": str(site_name or ""),
        "quantity": int(quantity),
        "created_by": str(created_by or ""),
    }
    if DB_MODE == "mysql":
        return _collection_store_call(
            "create_product_publish_records",
            rows,
            **{key: value for key, value in payload.items() if key != "rows"},
        )
    path = "/api/db/mercado-publish-records/bulk"
    try:
        data = _request("POST", path, timeout=120, json=payload)
        return {
            int(product_id): int(record_id)
            for product_id, record_id in (data.get("record_ids") or {}).items()
        }
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call(
            "create_product_publish_records",
            rows,
            **{key: value for key, value in payload.items() if key != "rows"},
        )


def get_published_mercado_product_item_ids(product_item_ids, *, token_id, site_id):
    item_ids = [int(value) for value in product_item_ids or []]
    payload = {
        "product_item_ids": item_ids,
        "token_id": int(token_id),
        "site_id": str(site_id or ""),
    }
    if DB_MODE == "mysql":
        return _collection_store_call(
            "get_published_product_item_ids",
            item_ids,
            token_id=payload["token_id"],
            site_id=payload["site_id"],
        )
    path = "/api/db/mercado-publish-records/published-product-ids"
    try:
        data = _request("POST", path, json=payload)
        return [int(value) for value in data.get("product_item_ids") or []]
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call(
            "get_published_product_item_ids",
            item_ids,
            token_id=payload["token_id"],
            site_id=payload["site_id"],
        )


def update_mercado_product_publish_record(record_id, **changes):
    if DB_MODE == "mysql":
        return _collection_store_call(
            "update_product_publish_record", int(record_id), **changes
        )
    path = f"/api/db/mercado-publish-records/{int(record_id)}"
    try:
        return _request("PATCH", path, json=changes)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call(
            "update_product_publish_record", int(record_id), **changes
        )


def list_mercado_product_publish_records(
    search="", status="", store_name="", site_id="", limit=500, offset=0,
):
    params = {
        "search": str(search or ""),
        "status": str(status or "").strip().lower(),
        "store_name": str(store_name or "").strip(),
        "site_id": str(site_id or "").strip().upper(),
        "limit": int(limit),
        "offset": int(offset),
    }
    if DB_MODE == "mysql":
        return _collection_store_call("list_product_publish_records", **params)
    path = "/api/db/mercado-publish-records"
    try:
        return _request("GET", path, params=params)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("list_product_publish_records", **params)


def get_mercado_product_publish_records_by_ids(record_ids):
    ids = [int(value) for value in record_ids or []]
    if DB_MODE == "mysql":
        return _collection_store_call("get_product_publish_records_by_ids", ids)
    path = "/api/db/mercado-publish-records/by-ids"
    try:
        data = _request("POST", path, json={"record_ids": ids})
        return list(data.get("rows") or [])
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call("get_product_publish_records_by_ids", ids)


def login_workbench_user(username, password):
    path = "/api/db/workbench/login"
    url = f"{DB_API_BASE_URL}{path}"
    try:
        response = DB_API_SESSION.post(
            url,
            headers=_headers(),
            timeout=30,
            json={"username": username, "password": password},
        )
        payload = response.json()
    except requests.RequestException as e:
        raise RuntimeError(f"登录接口请求失败：{url}，请确认 bit_interface.py 已启动。原因：{e}") from e
    except ValueError as e:
        raise RuntimeError(f"登录接口返回非 JSON：{url}，状态码：{response.status_code}") from e

    if response.status_code == 401:
        return None
    if not response.ok or payload.get("status") not in ("success", None):
        raise RuntimeError(payload.get("message") or f"登录接口请求失败：{url}，状态码：{response.status_code}")
    return payload.get("data")


def get_workbench_session_user(user_id):
    if DB_MODE == "mysql":
        from bit import bit_interface

        row = bit_interface.get_workbench_user(user_id=user_id)
        if not row or not row.get("is_active"):
            return None
        return bit_interface.build_workbench_session_user(row)
    return _request(
        "GET",
        "/api/db/workbench/session-user",
        params={"user_id": user_id},
        timeout=15,
    )


def list_workbench_roles():
    if DB_MODE == "mysql":
        return _local_interface_call("list_workbench_roles_local")
    return _request("GET", "/api/db/workbench/roles", timeout=15)


def create_workbench_role(data):
    if DB_MODE == "mysql":
        return _local_interface_call("create_workbench_role_local", data)
    return _request("POST", "/api/db/workbench/roles", json=data, timeout=15)


def update_workbench_role(role_key, data):
    if DB_MODE == "mysql":
        return _local_interface_call("update_workbench_role_local", role_key, data)
    return _request(
        "PUT",
        f"/api/db/workbench/roles/{role_key}",
        json=data,
        timeout=15,
    )


def delete_workbench_role(role_key):
    if DB_MODE == "mysql":
        return _local_interface_call("delete_workbench_role_local", role_key)
    return _request(
        "DELETE",
        f"/api/db/workbench/roles/{role_key}",
        timeout=15,
    )


def list_workbench_users():
    if DB_MODE == "mysql":
        return _local_interface_call("list_workbench_users_local")
    return _request("GET", "/api/db/workbench/users", timeout=15)


def create_workbench_user(data):
    if DB_MODE == "mysql":
        return _local_interface_call("create_workbench_user_local", data)
    return _request("POST", "/api/db/workbench/users", json=data, timeout=15)


def update_workbench_user(user_id, data):
    if DB_MODE == "mysql":
        return _local_interface_call("update_workbench_user_local", user_id, data)
    return _request(
        "PUT",
        f"/api/db/workbench/users/{int(user_id)}",
        json=data,
        timeout=15,
    )


def reset_workbench_user_password(user_id, password):
    if DB_MODE == "mysql":
        return _local_interface_call(
            "reset_workbench_user_password_local", user_id, password
        )
    return _request(
        "POST",
        f"/api/db/workbench/users/{int(user_id)}/password",
        json={"password": password},
        timeout=15,
    )
