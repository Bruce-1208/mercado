import os

import requests


DB_API_BASE_URL = os.environ.get("BIT_DB_API_BASE_URL", "http://zeshun.nat100.top").rstrip("/")
DB_API_TOKEN = os.environ.get("BIT_DB_API_TOKEN", "")


def _headers():
    headers = {"Content-Type": "application/json"}
    if DB_API_TOKEN:
        headers["X-Internal-Token"] = DB_API_TOKEN
    return headers


def _request(method, path, **kwargs):
    url = f"{DB_API_BASE_URL}{path}"
    timeout = kwargs.pop("timeout", 60)
    try:
        response = requests.request(method, url, headers=_headers(), timeout=timeout, **kwargs)
        payload = response.json()
    except requests.RequestException as e:
        raise RuntimeError(f"数据库接口请求失败：{url}，请确认 bit_interface.py 已启动。原因：{e}") from e
    except ValueError as e:
        raise RuntimeError(f"数据库接口返回非 JSON：{url}，状态码：{response.status_code}") from e

    if not response.ok or payload.get("status") not in ("success", None):
        raise RuntimeError(payload.get("message") or f"数据库接口请求失败：{url}，状态码：{response.status_code}")
    return payload.get("data")


def insert_task_record(record_list):
    return _request("POST", "/api/db/task-records", json={"records": record_list})


def inset_reputation_info(reputation_list):
    return _request("POST", "/api/db/reputation/bulk", json={"rows": reputation_list})


def inset_infraction_info(infraction_list):
    return _request("POST", "/api/db/infractions/bulk", json={"rows": infraction_list})


def inset_delay_info(delay_list):
    return _request("POST", "/api/db/delays/bulk", json={"rows": delay_list})


def insert_orders(line):
    return _request("POST", "/api/db/orders/bulk", json={"rows": line})


def insert_chat_info(name, site, message, chat, response, time):
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
    path = "/api/db/appeal-chat-records"
    url = f"{DB_API_BASE_URL}{path}"
    try:
        response = requests.post(
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
    return _request("POST", "/api/db/ai-appeal-records", timeout=timeout, json={"record": record or {}})


def get_ai_appeal_records(limit=100):
    return _request("GET", "/api/db/ai-appeal-records", params={"limit": limit})


def get_latest_infraction_info(recent_days=30):
    return _request("GET", "/api/db/infractions/latest", params={"days": recent_days})


def get_latest_reputation_info():
    return _request("GET", "/api/db/reputation/latest")


def login_workbench_user(username, password):
    path = "/api/db/workbench/login"
    url = f"{DB_API_BASE_URL}{path}"
    try:
        response = requests.post(
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
