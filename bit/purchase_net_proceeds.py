"""Purchase-cost net-proceeds updates inside the order-save transaction.

Schema preparation is deliberately separate: MySQL DDL must finish before any
order or ERP rows are locked, and this module never commits a business update.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

from erp import mercadolibre_collection_store as products
from erp import mercadolibre_profitability_cache as rates
from erp import mercadolibre_store_link_store as links


SOURCE = "purchase_cost_ratio"
_MONEY_QUANTUM = Decimal("0.0001")


def normalize_ratio(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        ratio = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("净收益比例只能选择 150% 或 200%") from exc
    if not ratio.is_finite() or ratio not in (Decimal(150), Decimal(200)):
        raise ValueError("净收益比例只能选择 150% 或 200%")
    return int(ratio)


def validate_purchase_cost(value: Any) -> Decimal:
    try:
        cost = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("同步净收益时必须填写大于 0 的采购成本（人民币）") from exc
    if not cost.is_finite() or cost <= 0:
        raise ValueError("同步净收益时必须填写大于 0 的采购成本（人民币）")
    if cost >= Decimal("10000000000000000"):
        raise ValueError("采购成本超出有效金额范围")
    try:
        cost = cost.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("采购成本超出有效金额范围") from exc
    if cost >= Decimal("10000000000000000"):
        raise ValueError("采购成本超出有效金额范围")
    if cost <= 0:
        raise ValueError("同步净收益时必须填写大于 0 的采购成本（人民币）")
    return cost


def prepare_schema(cursor: Any) -> None:
    products.ensure_collection_tables(cursor)
    links.ensure_store_link_table(cursor)
    rates.ensure_profitability_cache_tables(cursor)


def _loads(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        result = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _text(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return value


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _rate_snapshot(row: Mapping[str, Any] | None, table: str, today: date) -> dict | None:
    if not row:
        return None
    rate = _decimal(row.get("rate"))
    rate_date = _date(row.get("rate_date") or row.get("source_created_at") or row.get("refreshed_at"))
    if rate is None or rate <= 0 or rate_date is None:
        return None
    if not today - timedelta(days=7) <= rate_date <= today:
        return None
    payload = _loads(row.get("payload_json"))
    return {
        "cny_per_usd": format(rate, "f"),
        "rate_date": rate_date.isoformat(),
        "source": str(payload.get("source") or table),
        "refreshed_at": _text(row.get("refreshed_at")),
        "source_created_at": _text(row.get("source_created_at")),
    }


def read_exchange_rate(cursor: Any, *, today: date | None = None) -> dict:
    today = today or datetime.now().date()
    cursor.execute(
        f"SELECT `rate`, `source_created_at`, `refreshed_at`, `payload_json` "
        f"FROM `{rates.EXCHANGE_RATE_TABLE}` "
        "WHERE `from_currency_id` = %s AND `to_currency_id` = %s LIMIT 1",
        ("USD", "CNY"),
    )
    snapshot = _rate_snapshot(cursor.fetchone(), rates.EXCHANGE_RATE_TABLE, today)
    if snapshot:
        return snapshot
    cursor.execute(
        f"SELECT `rate`, `rate_date`, `source_created_at`, `refreshed_at`, `payload_json` "
        f"FROM `{rates.DAILY_EXCHANGE_RATE_TABLE}` "
        "WHERE `from_currency_id` = %s AND `to_currency_id` = %s "
        "AND `rate_date` BETWEEN %s AND %s ORDER BY `rate_date` DESC LIMIT 1",
        ("USD", "CNY", (today - timedelta(days=7)).isoformat(), today.isoformat()),
    )
    snapshot = _rate_snapshot(cursor.fetchone(), rates.DAILY_EXCHANGE_RATE_TABLE, today)
    if not snapshot:
        raise ValueError("缺少最近 7 天有效的美元兑人民币系统汇率，请先更新汇率后重试")
    return snapshot


def _order_identity(order: Mapping[str, Any]) -> tuple[str, int]:
    order_id = str(order.get("order_id") or "")
    payload = _loads(order.get("raw_json"))
    items = payload.get("order_items") or payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"订单 {order_id} 缺少商品名称或购买数量，无法同步净收益")
    titles, quantity = set(), 0
    for entry in items:
        if not isinstance(entry, Mapping):
            raise ValueError(f"订单 {order_id} 缺少商品名称或购买数量，无法同步净收益")
        item = entry.get("item") if isinstance(entry.get("item"), Mapping) else entry
        title = str(item.get("title") or item.get("offerName") or "").strip()
        raw_quantity = entry.get("quantity") if entry.get("quantity") is not None else entry.get("count")
        item_quantity = _decimal(raw_quantity)
        if not title or item_quantity is None or item_quantity <= 0 or item_quantity != item_quantity.to_integral_value():
            raise ValueError(f"订单 {order_id} 缺少有效商品名称或购买数量，无法同步净收益")
        titles.add(title)
        quantity += int(item_quantity)
    if len(titles) != 1:
        raise ValueError(f"订单 {order_id} 包含多个不同名称的商品，无法分摊采购成本同步净收益")
    return next(iter(titles)), quantity


def _new_product_snapshot(
    row: Mapping[str, Any], target: Decimal, ratio: int, rate: Mapping[str, Any],
    *, reset_ai_reviews: bool = False,
) -> str:
    snapshot = _loads(row.get("source_snapshot_json"))
    # The snapshot participates in the profitability worker's compare-and-set,
    # and source-specific overrides must agree with the authoritative column.
    snapshot["purchase_net_proceeds"] = {
        "source": SOURCE, "ratio": ratio, "net_proceeds_usd": format(target, "f"),
        "exchange_rate": dict(rate),
    }
    source_type = str(row.get("source_type") or "").strip().lower()
    if source_type == "ai_original":
        overrides = snapshot.get("source_1688_overrides")
        overrides = dict(overrides) if isinstance(overrides, Mapping) else {}
        overrides["net_proceeds_usd"] = float(target)
        snapshot["source_1688_overrides"] = overrides
        if reset_ai_reviews:
            products._mark_ai_marketplace_drafts_unreviewed(snapshot)
    elif source_type == "zying":
        snapshot["zying_net_proceeds_usd"] = float(target)
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))


def synchronize(cursor: Any, orders: list[dict], purchase_cost: Decimal, ratio: int,
                allowed_token_ids: list[int] | None = None, *, now: str | None = None) -> dict:
    """Lock, update and describe targets; caller saves logs and commits atomically."""
    cost = validate_purchase_cost(purchase_cost)
    rate = read_exchange_rate(cursor)
    cny_per_usd = Decimal(rate["cny_per_usd"])
    plans, by_title = [], {}
    for order in orders:
        title, quantity = _order_identity(order)
        unit_cost = cost / quantity
        try:
            target = (unit_cost * Decimal(ratio) / 100 / cny_per_usd).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        except InvalidOperation as exc:
            raise ValueError("计算的净收益超出有效金额范围，请检查采购成本与汇率") from exc
        if target <= 0 or target >= Decimal("10000000000000000"):
            raise ValueError("计算的净收益超出有效金额范围，请检查采购成本与汇率")
        if title in by_title and by_title[title] != target:
            raise ValueError(f"同名商品“{title}”在所选订单中计算出不同净收益，请分别保存")
        by_title[title] = target
        plans.append({
            "order_id": str(order.get("order_id") or ""), "title": title,
            "purchase_cost_cny": format(cost, "f"), "quantity": quantity,
            "unit_cost_cny": format(unit_cost, "f"), "ratio": ratio,
            "net_proceeds_usd": format(target, "f"), "exchange_rate": dict(rate),
        })
    if not plans:
        raise ValueError("所选订单不存在或无权修改")
    title_names = sorted(by_title)
    placeholders = ", ".join(["%s"] * len(title_names))
    _, product_scope, product_scope_values = products._product_token_scope(allowed_token_ids)
    from bit import product_access
    access_sql, access_params = product_access.predicate(products.PRODUCT_TABLE)
    if access_sql:
        product_scope = f"({product_scope}) AND {access_sql}" if product_scope else access_sql
        product_scope_values.extend(access_params)
    cursor.execute(
        f"SELECT `id`, `title`, `source_item_id`, `source_type`, `net_proceeds_usd`, "
        f"`source_snapshot_json`, `profitability_source` FROM `{products.PRODUCT_TABLE}` "
        f"WHERE BINARY TRIM(`title`) IN ({placeholders}) "
        + (f"AND ({product_scope}) " if product_scope else "") + "ORDER BY `id` FOR UPDATE",
        [*title_names, *product_scope_values],
    )
    product_rows = list(cursor.fetchall() or [])
    link_scope, link_scope_values = "", []
    if allowed_token_ids is not None:
        link_scope_values = sorted({int(value) for value in allowed_token_ids})
        if link_scope_values:
            link_scope = "AND `token_id` IN (" + ", ".join(["%s"] * len(link_scope_values)) + ") "
        else:
            link_scope = "AND 1 = 0 "
    cursor.execute(
        f"SELECT `id`, `title`, `token_id`, `store_name`, `item_id`, `net_proceeds_usd`, "
        f"`net_proceeds_manual` FROM `{links.STORE_LINK_TABLE}` "
        f"WHERE `is_current` = 1 AND BINARY TRIM(`title`) IN ({placeholders}) "
        + link_scope + "ORDER BY `id` FOR UPDATE",
        [*title_names, *link_scope_values],
    )
    link_rows = list(cursor.fetchall() or [])
    now = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    targets_by_title = {title: [] for title in title_names}
    product_changed = link_changed = 0
    for target_type, rows in (("product", product_rows), ("store_link", link_rows)):
        for row in rows:
            title = str(row.get("title") or "").strip()
            # Defense in depth if a database or a test adapter ignores binary
            # comparison: never update a case-insensitive near match.
            if title not in by_title:
                continue
            target = by_title[title]
            changed = _decimal(row.get("net_proceeds_usd")) != target
            if target_type == "product":
                reset_ai_reviews = changed and str(row.get("source_type") or "").strip().lower() == "ai_original"
                review_assignment = ", `review_status` = 'unreviewed'" if reset_ai_reviews else ""
                cursor.execute(
                    f"UPDATE `{products.PRODUCT_TABLE}` SET `net_proceeds_usd` = %s, "
                    "`profitability_source` = %s, `profitability_updated_at` = %s, "
                    "`profitability_error` = NULL, `source_snapshot_json` = %s"
                    + review_assignment + ", `updated_at` = %s "
                    "WHERE `id` = %s",
                    (
                        target, SOURCE, now,
                        _new_product_snapshot(
                            row, target, ratio, rate,
                            reset_ai_reviews=reset_ai_reviews,
                        ),
                        now, row["id"],
                    ),
                )
                product_changed += int(changed)
            else:
                cursor.execute(
                    f"UPDATE `{links.STORE_LINK_TABLE}` SET `net_proceeds_usd` = %s, "
                    "`net_proceeds_manual` = 1, `updated_at` = %s WHERE `id` = %s",
                    (target, now, row["id"]),
                )
                link_changed += int(changed)
            detail = {
                "target_type": target_type, "id": int(row["id"]), "title": str(row.get("title") or ""),
                "old_net_proceeds_usd": _text(row.get("net_proceeds_usd")),
                "new_net_proceeds_usd": format(target, "f"), "changed": changed,
            }
            if target_type == "product":
                detail["source_item_id"] = str(row.get("source_item_id") or "")
                detail["source_type"] = str(row.get("source_type") or "")
                detail["old_profitability_source"] = row.get("profitability_source")
                detail["new_profitability_source"] = SOURCE
            else:
                detail.update(token_id=int(row["token_id"]), store_name=str(row.get("store_name") or ""),
                              item_id=str(row.get("item_id") or ""), old_net_proceeds_manual=bool(row.get("net_proceeds_manual")),
                              new_net_proceeds_manual=True)
            targets_by_title[title].append(detail)
    product_matched = sum(detail["target_type"] == "product" for targets in targets_by_title.values() for detail in targets)
    link_matched = sum(detail["target_type"] == "store_link" for targets in targets_by_title.values() for detail in targets)
    unmatched_titles = [title for title, targets in targets_by_title.items() if not targets]
    for plan in plans:
        targets = targets_by_title[plan["title"]]
        plan.update(
            product_matched=sum(item["target_type"] == "product" for item in targets),
            product_changed=sum(item["target_type"] == "product" and item["changed"] for item in targets),
            store_link_matched=sum(item["target_type"] == "store_link" for item in targets),
            store_link_changed=sum(item["target_type"] == "store_link" and item["changed"] for item in targets),
            unmatched_titles=[plan["title"]] if not targets else [], targets=targets,
        )
    return {"ratio": ratio, "product_matched": product_matched, "product_changed": product_changed,
            "store_link_matched": link_matched, "store_link_changed": link_changed,
            "unmatched_titles": unmatched_titles, "results": plans}


def invalidate_caches() -> None:
    links.invalidate_store_link_metadata_cache()
