import os
import platform

import requests


DB_API_BASE_URL = os.environ.get("BIT_DB_API_BASE_URL", "http://zeshun.nat100.top").rstrip("/")
DB_API_TOKEN = os.environ.get("BIT_DB_API_TOKEN", "")
DB_API_SESSION = requests.Session()
DB_API_SESSION.trust_env = False


def _resolve_db_mode():
    """macOS 默认直连 MySQL，Windows/其他系统默认使用数据库 HTTP 接口。"""
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
    return "mysql" if platform.system() == "Darwin" else "api"


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


def inset_reputation_info(reputation_list):
    if DB_MODE == "mysql":
        return _local_call("inset_reputation_info", reputation_list)
    return _request("POST", "/api/db/reputation/bulk", json={"rows": reputation_list})


def inset_infraction_info(infraction_list):
    if DB_MODE == "mysql":
        return _local_call("inset_infraction_info", infraction_list)
    return _request("POST", "/api/db/infractions/bulk", json={"rows": infraction_list})


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


def insert_orders(line):
    if DB_MODE == "mysql":
        return _local_call("insert_orders", line)
    return _request("POST", "/api/db/orders/bulk", json={"rows": line})


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
