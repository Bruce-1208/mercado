"""Verified package identifiers and unmodified screenshots for appeals."""
from __future__ import annotations

import re
import uuid
from datetime import datetime

ORDER_FORMS = frozenset(("延误", "延误率", "取消率", "投诉"))
SITE_CODES = {"MX": "MLM", "BR": "MLB", "CL": "MLC", "CO": "MCO", "AR": "MLA", "UY": "MLU",
              "墨西哥": "MLM", "巴西": "MLB", "智利": "MLC", "哥伦比亚": "MCO", "阿根廷": "MLA", "乌拉圭": "MLU"}


def normalize_ids(values):
    if not isinstance(values, (list, tuple)) or len(values) > 10000:
        raise ValueError("申诉订单编号必须为数组且不超过 10000 个")
    result = []
    for value in values:
        # Never coerce floats: long IDs lose precision in spreadsheets/JSON.
        text = str(value or "").strip().lstrip("'")
        if not re.fullmatch(r"[1-9]\d{9,19}", text):
            raise ValueError("申诉订单或包裹编号无效")
        if text not in result:
            result.append(text)
    return result


def validate_package_message(message, package_ids):
    allowed = set(normalize_ids(list(package_ids)))
    if not allowed:
        raise ValueError("没有已核实的包裹号，停止发送")
    unknown = set(re.findall(r"(?<!\d)\d{10,20}(?!\d)", str(message))) - allowed
    if unknown:
        raise ValueError("申诉话术含未核实的编号，请使用当前包裹号，不要填写销售单号")
    return str(message).replace("销售单号", "包裹号").replace("订单编号", "包裹号")


def resolve_with_token(token, site, order_ids, *, accounts=None):
    """Only the API's explicit pack_id is usable; a sale/shipping ID is never a fallback."""
    from bit import mercado_infraction_sync as sync
    ids = normalize_ids(order_ids)
    if not ids:
        return []
    code = SITE_CODES.get(site, site)
    client, token = sync._client_and_token(token, timeout=15)
    accounts = accounts if accounts is not None else sync._marketplace_accounts(client, str(token.get("meli_user_id") or ""))
    sellers = {str(row.get("user_id") or row.get("seller_id") or "") for row in accounts if row.get("site_id") == code}
    if not sellers:
        raise ValueError("无法核实申诉站点卖家账号")
    packages = []
    for order_id in ids:
        order = client.request("GET", f"/marketplace/orders/{order_id}", max_attempts=1)
        if str(order.get("id") or "") != order_id or str((order.get("seller") or {}).get("id") or "") not in sellers:
            raise ValueError("订单与当前申诉店铺/站点不匹配，停止发送")
        if order.get("site_id") and order["site_id"] != code:
            raise ValueError("订单站点与申诉站点不匹配，停止发送")
        pack_id = order.get("pack_id")
        if not pack_id:
            raise ValueError(f"销售单 {order_id} 缺少包裹号，停止发送；不会使用销售单号代替")
        packages.extend(normalize_ids([pack_id]))
    return list(dict.fromkeys(packages))


def fetch_store_package_ids(shop_name, site, order_ids):
    from bit import bit_mysql
    order_ids = normalize_ids(order_ids)
    if not order_ids:
        return []
    matches = [row for row in (bit_mysql.list_mercado_store_tokens() or {}).get("rows", [])
               if shop_name and shop_name in {str(row.get("display_name") or "").strip(), str(row.get("nickname") or "").strip()}
               and row.get("enabled", True)]
    if len(matches) != 1:
        raise ValueError("无法唯一匹配申诉店铺授权，不能核实包裹号")
    return resolve_with_token(bit_mysql.get_mercado_store_token(matches[0]["id"]), site, order_ids)


def save_chat_screenshot(driver, name, site, form, group_index):
    from bit.bit_utils import get_bit_path
    from bit.bit_appeal_ai import activate_ai_chat_context
    # Opening/reading the real chat is allowed; evidence text and styles are untouched.
    if not activate_ai_chat_context(driver, require_input=False):
        raise ValueError("客服对话框不可见，未保存空白截图")
    driver.switch_to.default_content()
    safe = lambda value: re.sub(r'[^\w\u4e00-\u9fff.-]', '_', str(value))[:80]
    folder = get_bit_path() / "申诉截图" / safe(name)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe(site)}-{safe(form)}-{group_index}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.png"
    if not driver.save_screenshot(str(path)):
        raise ValueError("浏览器没有保存截图")
    return str(path.resolve())
