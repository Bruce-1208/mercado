"""采购物流轨迹查询与快递100结果缓存。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from urllib.parse import quote

import requests

from bit import bit_mysql


KUAIDI100_QUERY_URL = "https://poll.kuaidi100.com/poll/query.do"
TRACKING_CACHE_MINUTES = 30


def external_tracking_url(tracking_number, company=""):
    number = quote(str(tracking_number or "").strip(), safe="")
    courier = quote(str(company or "").strip().lower(), safe="")
    return f"https://www.kuaidi100.com/chaxun?com={courier}&nu={number}"


def _parsed_datetime(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _cached_payload(order):
    checked_at = _parsed_datetime(order.get("tracking_checked_at"))
    if not checked_at or checked_at < datetime.now() - timedelta(minutes=TRACKING_CACHE_MINUTES):
        return None
    try:
        payload = json.loads(str(order.get("tracking_cache_json") or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    payload["cached"] = True
    return payload


def _normalized_events(data):
    events = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        events.append({
            "time": str(row.get("ftime") or row.get("time") or ""),
            "description": str(row.get("context") or row.get("status") or ""),
            "location": str(row.get("location") or row.get("areaName") or ""),
            "status": str(row.get("status") or ""),
        })
    return events


def query_order_tracking(order_id):
    order = bit_mysql.get_mercado_order_procurement(order_id)
    if not order:
        raise KeyError("订单不存在或不属于当前授权店铺")
    tracking_number = str(order.get("purchase_tracking") or "").strip()
    company = str(order.get("logistics_company") or "").strip().lower()
    if not tracking_number:
        raise ValueError("请先在采购单中填写物流号")

    cached = _cached_payload(order)
    if cached:
        return cached

    base = {
        "order_id": str(order.get("order_id") or order_id),
        "tracking_number": tracking_number,
        "company": company,
        "external_url": external_tracking_url(tracking_number, company),
        "provider": "快递100",
        "cached": False,
    }
    customer = str(os.environ.get("KUAIDI100_CUSTOMER") or "").strip()
    api_key = str(os.environ.get("KUAIDI100_KEY") or "").strip()
    if not customer or not api_key:
        return {
            **base,
            "configured": False,
            "status": "unconfigured",
            "message": "尚未配置快递100企业接口，可点击“打开快递100”直接查询",
            "events": [],
        }

    param = json.dumps(
        {
            "com": company,
            "num": tracking_number,
            "resultv2": "1",
            "show": "0",
            "order": "desc",
            "lang": "zh",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    signature = hashlib.md5(
        f"{param}{api_key}{customer}".encode("utf-8")
    ).hexdigest().upper()
    response = requests.post(
        KUAIDI100_QUERY_URL,
        data={"customer": customer, "sign": signature, "param": param},
        timeout=25,
    )
    response.raise_for_status()
    raw = response.json()
    success = str(raw.get("status") or raw.get("returnCode") or "") in ("200", "true", "0")
    payload = {
        **base,
        "configured": True,
        "status": str(raw.get("state") or raw.get("status") or "unknown"),
        "message": str(raw.get("message") or raw.get("returnMsg") or ("查询成功" if success else "暂未查询到轨迹")),
        "events": _normalized_events(raw.get("data")),
    }
    bit_mysql.update_mercado_tracking_cache(order_id, payload)
    return payload
