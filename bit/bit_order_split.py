"""Validate and submit Mercado Libre Global Selling shipment splits."""

from __future__ import annotations

import json
import threading
from datetime import datetime

from bit import bit_mysql, mercado_tokens
from mercado_api.client import MercadoAPIError, MercadoLibreClient


SPLIT_REASONS = {
    "FRAGILE": "易碎商品",
    "ANOTHER_WAREHOUSE": "不同仓库",
    "IRREGULAR_SHAPE": "形状不规则",
    "OTHER_MOTIVE": "其他原因",
    "DIMENSIONS_EXCEEDED": "尺寸超限",
}
_BLOCKED_STATUSES = {
    "cancelled", "canceled", "delivered", "shipped", "returned",
    "returned_to_sender", "not_delivered",
}
_ALLOWED_LOGISTIC_TYPES = {"drop_off", "cross_docking", "xd_drop_off"}
_split_lock = threading.Lock()


class MercadoOrderSplitError(RuntimeError):
    """The selected shipment cannot be split safely."""


def _is_token_error(exc):
    message = str(exc or "").casefold()
    return "401" in message or any(
        marker in message
        for marker in (
            "invalid_token", "token_not_valid", "malformed access_token",
            "access token expired", "access token 已失效",
        )
    )


def _refresh_store_token(token_id):
    mercado_tokens.refresh_and_save(
        int(token_id),
        get_token=bit_mysql.get_mercado_store_token,
        update_token=bit_mysql.update_mercado_store_token,
        record_error=bit_mysql.record_mercado_store_token_error,
    )
    return bit_mysql.get_mercado_store_token(int(token_id))


def _token_expiring(context):
    expires_at = context.get("expires_at")
    if not expires_at:
        return False
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            return False
    now = datetime.now(expires_at.tzinfo) if expires_at.tzinfo else datetime.now()
    return expires_at <= now


def _client_call(context, method_name, *args, **kwargs):
    current = dict(context or {})
    if _token_expiring(current) and current.get("refresh_token"):
        current.update(_refresh_store_token(current.get("token_id")) or {})
    client = MercadoLibreClient(str(current.get("access_token") or ""))
    method = getattr(client, method_name)
    try:
        return method(*args, **kwargs), current
    except MercadoAPIError as exc:
        if not current.get("refresh_token") or not _is_token_error(exc):
            raise
        current.update(_refresh_store_token(current.get("token_id")) or {})
        client = MercadoLibreClient(str(current.get("access_token") or ""))
        return getattr(client, method_name)(*args, **kwargs), current


def _split_context(order_ids):
    contexts = bit_mysql.get_mercado_order_split_contexts(order_ids)
    if not contexts:
        raise MercadoOrderSplitError("没有找到当前授权店铺下可拆分的订单")
    context = dict(contexts[0])
    shipment_id = str(context.get("shipping_id") or "").strip()
    if not shipment_id:
        raise MercadoOrderSplitError("所选订单尚未生成 Shipment ID，不能拆分发货")
    if not str(context.get("access_token") or "").strip():
        raise MercadoOrderSplitError("订单所属店铺缺少 Access Token")
    context["order_ids"] = [
        str(row.get("order_id") or "").strip()
        for row in contexts
        if str(row.get("order_id") or "").strip()
    ]
    context["_order_contexts"] = contexts
    return context


def _shipment_logistic_type(shipment):
    logistic = shipment.get("logistic")
    if isinstance(logistic, dict):
        value = logistic.get("type") or logistic.get("logistic_type")
    else:
        value = None
    return str(value or shipment.get("logistic_type") or "").strip().lower()


def _shipment_orders(items):
    grouped = {}
    for item in items or ():
        item = dict(item or {})
        order_id = str(item.get("order_id") or "").strip()
        try:
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if not order_id or quantity <= 0:
            continue
        entry = grouped.setdefault(order_id, {
            "order_id": order_id,
            "quantity": 0,
            "items": [],
        })
        entry["quantity"] += quantity
        entry["items"].append({
            "item_id": str(item.get("item_id") or ""),
            "variation_id": str(item.get("variation_id") or ""),
            "description": str(item.get("description") or item.get("title") or ""),
            "quantity": quantity,
        })
    return list(grouped.values())


def _stored_shipment_orders(contexts):
    """Fallback for older shipment-item payloads that omit order_id."""
    result = []
    for context in contexts or ():
        raw_value = context.get("raw_json") or {}
        if isinstance(raw_value, dict):
            raw_order = raw_value
        else:
            try:
                raw_order = json.loads(raw_value or "{}")
            except (TypeError, ValueError):
                raw_order = {}
        order_id = str(context.get("order_id") or raw_order.get("id") or "").strip()
        item_rows = []
        total = 0
        for order_item in raw_order.get("order_items") or ():
            try:
                quantity = int(order_item.get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0
            if quantity <= 0:
                continue
            item = order_item.get("item") if isinstance(order_item.get("item"), dict) else {}
            total += quantity
            item_rows.append({
                "item_id": str(item.get("id") or ""),
                "variation_id": str(item.get("variation_id") or ""),
                "description": str(item.get("title") or ""),
                "quantity": quantity,
            })
        if order_id and total > 0:
            result.append({"order_id": order_id, "quantity": total, "items": item_rows})
    return result


def preview_order_split(order_ids):
    """Return live eligibility and exact per-order quantities for a two-box split."""
    context = _split_context(order_ids)
    shipment_id = str(context["shipping_id"])
    try:
        shipment, context = _client_call(context, "get_shipment", shipment_id)
        items, context = _client_call(context, "get_shipment_items", shipment_id)
    except MercadoAPIError as exc:
        raise MercadoOrderSplitError(f"读取美客多运单拆分信息失败：{exc}") from exc

    shipment = dict(shipment or {})
    orders = _shipment_orders(items)
    if not orders:
        orders = _stored_shipment_orders(context.get("_order_contexts") or [])
    if not orders:
        raise MercadoOrderSplitError("美客多运单没有返回可拆分的订单商品明细")
    status = str(shipment.get("status") or "").strip().lower()
    mode = str(shipment.get("mode") or "").strip().lower()
    logistic_type = _shipment_logistic_type(shipment)
    if status in _BLOCKED_STATUSES:
        raise MercadoOrderSplitError(f"运单状态为 {status}，当前不能拆分")
    if mode and mode != "me2":
        raise MercadoOrderSplitError(f"仅 ME2 运单支持拆分，当前模式为 {mode}")
    if logistic_type and logistic_type not in _ALLOWED_LOGISTIC_TYPES:
        raise MercadoOrderSplitError(
            "仅 drop-off / cross-docking 运单支持拆分，"
            f"当前物流类型为 {logistic_type}"
        )
    total_quantity = sum(int(row["quantity"]) for row in orders)
    if total_quantity < 2:
        raise MercadoOrderSplitError("运单至少需要 2 件商品才能拆成两个包裹")
    live_order_ids = {row["order_id"] for row in orders}
    local_order_ids = set(context.get("order_ids") or ())
    missing_local = sorted(live_order_ids - local_order_ids)
    if missing_local:
        raise MercadoOrderSplitError(
            "本地订单资料尚未同步完整，请先刷新订单后重试："
            + "、".join(missing_local)
        )
    return {
        "shipment_id": shipment_id,
        "order_ids": [row["order_id"] for row in orders],
        "orders": orders,
        "total_quantity": total_quantity,
        "status": status,
        "mode": mode,
        "logistic_type": logistic_type,
        "eligible": True,
        "reasons": [
            {"value": value, "label": label}
            for value, label in SPLIT_REASONS.items()
        ],
    }


def _normalize_packs(packs, quantities):
    if not isinstance(packs, list) or len(packs) != 2:
        raise MercadoOrderSplitError("拆分发货必须且只能生成 2 个包裹")
    totals = {order_id: 0 for order_id in quantities}
    normalized = []
    for index, pack in enumerate(packs, 1):
        if not isinstance(pack, dict) or not isinstance(pack.get("orders"), list):
            raise MercadoOrderSplitError(f"包裹 {index} 的订单明细无效")
        pack_orders = []
        seen = set()
        for row in pack["orders"]:
            if not isinstance(row, dict):
                raise MercadoOrderSplitError(f"包裹 {index} 的订单明细无效")
            order_id = str(row.get("id") or "").strip()
            raw_quantity = row.get("quantity")
            if isinstance(raw_quantity, bool):
                raise MercadoOrderSplitError("拆分数量必须是整数")
            try:
                quantity = int(raw_quantity or 0)
            except (TypeError, ValueError) as exc:
                raise MercadoOrderSplitError("拆分数量必须是整数") from exc
            if isinstance(raw_quantity, float) and not raw_quantity.is_integer():
                raise MercadoOrderSplitError("拆分数量必须是整数")
            if isinstance(raw_quantity, str) and str(quantity) != raw_quantity.strip():
                raise MercadoOrderSplitError("拆分数量必须是整数")
            if order_id not in quantities:
                raise MercadoOrderSplitError(f"订单 {order_id or '-'} 不属于当前运单")
            if order_id in seen:
                raise MercadoOrderSplitError(f"包裹 {index} 中订单 {order_id} 重复")
            seen.add(order_id)
            if quantity <= 0:
                continue
            if quantity > quantities[order_id]:
                raise MercadoOrderSplitError(f"订单 {order_id} 的拆分数量超过原数量")
            totals[order_id] += quantity
            pack_orders.append({"id": order_id, "quantity": quantity})
        if not pack_orders:
            raise MercadoOrderSplitError(f"包裹 {index} 不能为空")
        normalized.append({"orders": pack_orders})
    mismatched = [
        order_id for order_id, quantity in quantities.items()
        if totals.get(order_id) != quantity
    ]
    if mismatched:
        raise MercadoOrderSplitError(
            "两个包裹的数量合计必须与原运单完全一致：" + "、".join(mismatched)
        )
    return normalized


def split_order_shipment(
    order_ids,
    *,
    shipment_id,
    reason,
    packs,
    operator_id=None,
    operator_name="",
):
    """Revalidate the live shipment immediately before the irreversible split call."""
    reason = str(reason or "").strip().upper()
    if reason not in SPLIT_REASONS:
        raise MercadoOrderSplitError("请选择有效的拆分原因")
    with _split_lock:
        preview = preview_order_split(order_ids)
        if str(preview["shipment_id"]) != str(shipment_id or "").strip():
            raise MercadoOrderSplitError("Shipment ID 已变化，请重新打开拆分窗口")
        quantities = {
            str(row["order_id"]): int(row["quantity"])
            for row in preview["orders"]
        }
        normalized_packs = _normalize_packs(packs, quantities)
        context = _split_context(preview["order_ids"])
        try:
            _, context = _client_call(
                context,
                "split_shipment",
                preview["shipment_id"],
                reason=reason,
                packs=normalized_packs,
            )
        except MercadoAPIError as exc:
            raise MercadoOrderSplitError(f"美客多拒绝拆分运单：{exc}") from exc

        warnings = []
        try:
            bit_mysql.record_mercado_order_split_logs(
                preview["order_ids"],
                shipment_id=preview["shipment_id"],
                reason=reason,
                packs=normalized_packs,
                operator_id=operator_id,
                operator_name=operator_name,
            )
        except Exception as exc:
            warnings.append(f"拆分已提交，但操作日志保存失败：{exc}")
        return {
            "submitted": True,
            "shipment_id": preview["shipment_id"],
            "order_ids": preview["order_ids"],
            "reason": reason,
            "reason_label": SPLIT_REASONS[reason],
            "packs": normalized_packs,
            "warnings": warnings,
            "message": "拆分请求已提交；新订单和新 Shipment ID 将由美客多异步生成",
        }
