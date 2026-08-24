import os
import requests


DB_API_BASE_URL = os.environ.get("BIT_DB_API_BASE_URL", "http://zeshun.nat100.top").rstrip("/")
DB_API_TOKEN = os.environ.get("BIT_DB_API_TOKEN", "")
DB_API_SESSION = requests.Session()
DB_API_SESSION.trust_env = False


def _resolve_db_mode():
    """默认直连局域网 MySQL；显式设置 ``api`` 时才请求数据库服务端。"""
    mode = (
        os.environ.get("BIT_DB_MODE")
        or os.environ.get("BIT_INTERFACE_DB_MODE")
        or ""
    ).strip().lower()
    if mode in ("direct", "local", "server", "mysql"):
        return "mysql"
    if mode in ("api", "client", "remote"):
        return "api"
    legacy_use_api = os.environ.get("BIT_INTERFACE_USE_DB_API")
    if legacy_use_api is not None:
        return "api" if str(legacy_use_api).strip().lower() in ("1", "true", "yes", "on") else "mysql"
    return "mysql"


DB_MODE = _resolve_db_mode()


def _local_call(function_name, *args, **kwargs):
    """延迟导入 MySQL 实现，避免 Windows/API 客户端加载本地数据库依赖。"""
    from bit import bit_mysql

    function = getattr(bit_mysql, function_name)
    # timeout 是 HTTP 层参数，本地 MySQL 函数不需要。
    kwargs.pop("timeout", None)
    return function(*args, **kwargs)


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


def list_orders(
    country="",
    status="",
    salesperson="",
    search="",
    start_date="",
    end_date="",
    origin="",
    page=1,
    page_size=50,
):
    params = {
        "country": country or "",
        "status": status or "",
        "salesperson": salesperson or "",
        "search": search or "",
        "start_date": start_date or "",
        "end_date": end_date or "",
        "origin": origin or "",
        "page": int(page or 1),
        "page_size": int(page_size or 50),
    }
    if DB_MODE == "mysql":
        return _local_call("list_orders", **params)
    path = "/api/db/orders"
    try:
        return _request("GET", path, params=params)
    except RuntimeError as exc:
        message = str(exc or "")
        if "404" not in message or path not in message:
            raise
        return _local_call("list_orders", **params)


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
    status="",
    sales_sort="desc",
    current_only=True,
    page=1,
    page_size=100,
):
    params = {
        "search": search or "",
        "site_id": str(site_id or "").strip().upper(),
        "status": status or "",
        "sales_sort": "asc" if str(sales_sort or "").strip().lower() == "asc" else "desc",
        "current_only": "1" if current_only else "0",
        "page": int(page or 1),
        "page_size": int(page_size or 100),
    }
    if token_id not in (None, ""):
        params["token_id"] = int(token_id)
    if DB_MODE == "mysql":
        return _store_link_store_call(
            "list_store_links",
            search=params["search"],
            token_id=params.get("token_id"),
            site_id=params["site_id"],
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
        return _store_link_store_call(
            "bulk_update_store_links", payload["link_ids"], changes
        )
    return _request("POST", "/api/db/store-links/bulk-update", json=payload)


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
    return {
        "content": bytes(response.content),
        "filename": response.headers.get("X-Mercado-Label-Filename") or "mercado-labels.pdf",
        "order_ids": payload["order_ids"],
        "shipment_count": int(response.headers.get("X-Mercado-Shipment-Count") or 0),
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
    if DB_MODE == "mysql":
        return _local_call("list_bit_browser_configs", include_ignored)
    return _request(
        "GET",
        "/api/db/browser-configs",
        params={"include_ignored": "1" if include_ignored else "0"},
    )


def get_bit_browser_config(shop_name="", window_id="", include_ignored=True):
    if DB_MODE == "mysql":
        return _local_call(
            "get_bit_browser_config",
            shop_name,
            window_id,
            include_ignored,
        )
    return _request(
        "GET",
        "/api/db/browser-configs/lookup",
        params={
            "shop_name": shop_name,
            "window_id": window_id,
            "include_ignored": "1" if include_ignored else "0",
        },
    )


def upsert_bit_browser_configs(records, replace=False):
    if DB_MODE == "mysql":
        return _local_call("upsert_bit_browser_configs", records, replace)
    return _request(
        "POST",
        "/api/db/browser-configs/bulk",
        json={"records": records or [], "replace": bool(replace)},
    )


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


def list_mercado_collection_items(search="", limit=500, offset=0, task_id=None):
    if DB_MODE == "mysql":
        return _collection_store_call(
            "list_collection_items",
            search=search,
            limit=limit,
            offset=offset,
            task_id=task_id,
        )
    params = {"search": search, "limit": limit, "offset": offset}
    if task_id not in (None, ""):
        params["task_id"] = int(task_id)
    path = "/api/db/mercado-collection/items"
    try:
        return _request("GET", path, params=params)
    except RuntimeError as exc:
        if not _collection_route_missing(exc, path):
            raise
        return _collection_store_call(
            "list_collection_items", search=search, limit=limit, offset=offset, task_id=task_id
        )


def list_mercado_product_items(
    search="", limit=500, offset=0, source_type="", review_status="",
):
    params = {
        "search": search,
        "limit": limit,
        "offset": offset,
        "source_type": str(source_type or "").strip().lower(),
        "review_status": str(review_status or "").strip().lower(),
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
