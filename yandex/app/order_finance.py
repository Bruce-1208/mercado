"""Order display amounts; rewards are not cash settlement.

Sources: getBusinessOrders (item amounts are line totals), getOrdersStats,
getPricesByOfferIds and getOfferMappings in the official Partner API.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from yandex.app.order_media import build_order_item_media


SHIPPING_FEE_TYPES = {
    "DELIVERY_TO_CUSTOMER", "EXPRESS_DELIVERY_TO_CUSTOMER", "CROSSREGIONAL_DELIVERY",
}
Money = dict[str, Any] | None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _money(value: Any, currency: Any = None) -> Money:
    amount = _decimal(value)
    if amount is None:
        return None
    normalized_currency = str(currency).strip().upper() if currency else None
    if normalized_currency == "RUB":
        normalized_currency = "RUR"
    try:
        return {"value": float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                "currency": normalized_currency}
    except InvalidOperation:
        return None


def _api_money(value: Any) -> Money:
    return _money(value.get("value"), value.get("currencyId")) if isinstance(value, dict) else None


def _sum_money(values: list[Money]) -> Money:
    # An incomplete/mixed-currency subtotal must not masquerade as a complete sum.
    if not values or any(value is None or not value.get("currency") for value in values):
        return None
    currencies = {value["currency"] for value in values if value is not None}
    if len(currencies) != 1:
        return None
    return _money(sum(Decimal(str(value["value"])) for value in values if value is not None),
                  next(iter(currencies)))


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def build_order_finance(
    order: dict[str, Any], *, listing_prices: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None, notes: list[str] | None = None,
) -> dict[str, Any]:
    """Build currency-preserving display fields without inventing missing amounts."""
    messages = list(notes or [])
    listings = listing_prices or {}
    items: list[dict[str, Any]] = []
    for raw in order.get("items") or []:
        if not isinstance(raw, dict):
            continue
        sku = str(raw.get("offerId") or "")
        prices = _dict(raw.get("prices"))
        listing = _api_money(listings.get(sku))
        count = _decimal(raw.get("count"))
        if count is None or count < 0 or count != count.to_integral_value():
            count = None
        listing_total = (
            _money(Decimal(str(listing["value"])) * count, listing["currency"])
            if listing is not None and count is not None else None
        )
        raw_statuses = raw.get("itemStatuses")
        items.append({
            "offer_id": sku, "name": str(raw.get("offerName") or sku),
            "item_id": raw.get("id"),
            "item_statuses": [dict(entry) for entry in raw_statuses if isinstance(entry, dict)]
                             if isinstance(raw_statuses, list) else [],
            "vat": prices.get("vat"),
            **build_order_item_media(raw),
            "count": int(count) if count is not None else None,
            "listing_unit": listing, "listing_total": listing_total,
            # Yandex already multiplies these three fields by count.
            "buyer_payment": _api_money(prices.get("payment")),
            "cashback": _api_money(prices.get("cashback")),
            "seller_subsidy": _api_money(prices.get("subsidy")),
        })
    prices = _dict(order.get("prices"))
    delivery = _dict(prices.get("delivery"))
    finance: dict[str, Any] = {
        "items": items, "listing_total": _sum_money([item["listing_total"] for item in items]),
        "buyer_shipping": _api_money(delivery.get("payment")),
        "delivery_subsidy": _api_money(delivery.get("subsidy")),
        "seller_gross": None, "seller_gross_kind": "reported_transfers",
        "seller_net": None, "platform_fees": None, "seller_shipping": None,
        "settlement_status": "pending", "settlement_label": "待结算核实",
    }
    for target, source in (("buyer_payment", "payment"), ("cashback", "cashback"),
                           ("seller_subsidy", "subsidy")):
        finance[target] = _api_money(prices.get(source))
        if finance[target] is None:
            finance[target] = _sum_money([item[target] for item in items])
    finance["delivery_total"] = _sum_money([finance["buyer_shipping"], finance["delivery_subsidy"]])
    if finance["listing_total"] is None:
        messages.append("部分商品当前链接价未返回或币种不一致，合计暂不可用。")
    else:
        messages.append("链接价按当前店铺设置价计算；历史下单价、买家页面活动价可能不同。")
    if finance["buyer_payment"] is None:
        messages.append("买家商品付款金额未完整返回，暂不合计。")

    if stats is None:
        messages.append("订单财务统计尚未返回；新订单或更新最多可能延迟约 40 分钟。")
    else:
        currency = stats.get("currency")
        commissions = stats.get("commissions")
        if isinstance(commissions, list) and commissions:
            finance["platform_fees"] = _sum_money([
                _money(_dict(fee).get("actual"), currency) for fee in commissions
            ])
            finance["seller_shipping"] = _sum_money([
                _money(fee.get("actual"), currency) for fee in commissions
                if isinstance(fee, dict) and fee.get("type") in SHIPPING_FEE_TYPES
            ])
        if finance["platform_fees"] is None:
            messages.append("平台费用尚未完整返回，空费用列表不代表免费。")
        payments = stats.get("payments")
        transfers: list[Money] = []
        payment_orders_complete = bool(isinstance(payments, list) and payments)
        seen_payments: dict[str, tuple[Any, Any, Any]] = {}
        for raw_payment in payments if isinstance(payments, list) else []:
            payment = _dict(raw_payment)
            identity = str(payment.get("id") or "")
            signature = (payment.get("type"), payment.get("source"), payment.get("total"))
            if identity in seen_payments:
                if seen_payments[identity] != signature:
                    transfers.append(None)
                continue
            if identity:
                seen_payments[identity] = signature
            amount = _decimal(payment.get("total"))
            kind = payment.get("type")
            if amount is None or amount < 0 or kind not in {"PAYMENT", "REFUND"}:
                transfers.append(None)
            else:
                transfers.append(_money(amount if kind == "PAYMENT" else -amount, currency))
            payment_order = _dict(payment.get("paymentOrder"))
            if not payment_order.get("id") or not payment_order.get("date"):
                payment_orders_complete = False
        finance["seller_gross"] = _sum_money(transfers)

        credits = [finance["cashback"], finance["seller_subsidy"], finance["delivery_subsidy"]]
        has_credits = any(value is not None and value["value"] != 0 for value in credits)
        subsidies = stats.get("subsidies")
        if isinstance(subsidies, list):
            # These are fee-offset points, not additional cash transfers.
            has_credits = has_credits or any(
                _decimal(_dict(entry).get("amount")) not in (None, Decimal("0"))
                for entry in subsidies
            )
        credits_known = isinstance(subsidies, list) and all(
            _decimal(_dict(entry).get("amount")) is not None for entry in subsidies
        )
        if has_credits:
            messages.append("积分和卖家补贴可能用于抵扣平台服务费，不能直接当作现金到账；卖家结余待结算核实。")
        if not payment_orders_complete:
            messages.append("资金流水未齐备或尚无完整付款凭证，已回传资金不代表已到账。")
        gross, fees = finance["seller_gross"], finance["platform_fees"]
        order_currency = (finance["buyer_payment"] or {}).get("currency")
        can_estimate = (
            gross is not None and fees is not None and payment_orders_complete
            and credits_known and not has_credits and order_currency
            and order_currency == gross["currency"] == fees["currency"]
        )
        if can_estimate:
            finance["seller_net"] = _money(Decimal(str(gross["value"])) - Decimal(str(fees["value"])),
                                          gross["currency"])
            finance["settlement_status"] = "estimate"
            finance["settlement_label"] = "资金扣费估算"
            messages.append("卖家结余为已回传资金净额减已回传平台费用的估算，最终到账以结算单为准。")
        elif gross is not None and order_currency and gross["currency"] != order_currency:
            messages.append("订单金额与财务统计币种不同，未进行换汇或结余估算。")
    if finance["seller_shipping"] is None:
        messages.append("卖家物流费仅显示平台回传的配送费用；未回传不代表 0，自有承运商费用不包含在内。")
    finance["notes"] = list(dict.fromkeys(messages))
    return finance


async def enrich_order_finances(
    client: Any, business_id: int, campaign_id: int, orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Batch each page; supplementary read failures never remove the order list."""
    if not orders:
        return []
    offer_ids = list(dict.fromkeys(
        str(item["offerId"]) for order in orders for item in order.get("items") or []
        if isinstance(item, dict) and item.get("offerId")
    ))
    order_ids = list(dict.fromkeys(
        int(order["orderId"]) for order in orders
        if str(order.get("orderId") or "").isdigit()
    ))
    semaphore = asyncio.Semaphore(4)

    async def read(method: str, owner_id: int, ids: list[Any]) -> Any:
        try:
            async with semaphore:
                return await asyncio.wait_for(getattr(client, method)(owner_id, ids), timeout=12)
        except Exception:
            # Do not send raw API errors (which may include account data) to the page.
            return None

    jobs: list[tuple[str, list[Any], Any]] = []
    for start in range(0, len(offer_ids), 100):
        batch = offer_ids[start:start + 100]
        jobs.append(("campaign", batch, read("get_campaign_offer_prices", campaign_id, batch)))
        jobs.append(("business", batch, read("get_business_offer_prices", business_id, batch)))
    for start in range(0, len(order_ids), 200):
        batch = order_ids[start:start + 200]
        jobs.append(("stats", batch, read("get_order_stats", campaign_id, batch)))
    results = await asyncio.gather(*(job[2] for job in jobs))
    campaign_prices: dict[str, Any] = {}
    business_prices: dict[str, Any] = {}
    catalogue_by_sku: dict[str, dict[str, Any]] = {}
    stats_by_id: dict[int, dict[str, Any]] = {}
    campaign_checked: set[str] = set()
    failed_prices: set[str] = set()
    failed_stats: set[int] = set()
    for (kind, ids, _), result in zip(jobs, results):
        if not isinstance(result, list):
            (failed_stats if kind == "stats" else failed_prices).update(ids)
            continue
        if kind == "stats":
            stats_by_id.update({int(item["id"]): item for item in result
                                if isinstance(item, dict) and item.get("id") in ids})
        else:
            if kind == "campaign":
                campaign_checked.update(ids)
            target = campaign_prices if kind == "campaign" else business_prices
            target.update({str(item["offerId"]): item.get("price") for item in result
                           if isinstance(item, dict) and item.get("offerId") in ids})
            if kind == "business":
                catalogue_by_sku.update({str(item["offerId"]): item for item in result
                                         if isinstance(item, dict) and item.get("offerId") in ids})
    listings = {sku: campaign_prices.get(sku, business_prices.get(sku))
                for sku in campaign_checked}
    result_orders: list[dict[str, Any]] = []
    for order in orders:
        local_notes: list[str] = []
        skus = {str(item.get("offerId")) for item in order.get("items") or [] if isinstance(item, dict)}
        if skus & failed_prices:
            local_notes.append("部分链接价格查询暂不可用，刷新可重试；订单仍正常显示。")
        order_id = int(order["orderId"]) if str(order.get("orderId") or "").isdigit() else None
        if order_id in failed_stats:
            local_notes.append("财务统计暂不可用或缺少访问权限，付款与运费仍显示订单已返回的数据。")
        enriched_items = [
            {**item, **build_order_item_media(
                item, catalogue_by_sku.get(str(item.get("offerId") or "")), campaign_id=campaign_id,
            )}
            for item in order.get("items") or [] if isinstance(item, dict)
        ]
        enriched_order = {**order, "items": enriched_items}
        result_orders.append({**enriched_order, "finance": build_order_finance(
            enriched_order, listing_prices=listings, stats=stats_by_id.get(order_id), notes=local_notes,
        )})
    return result_orders
