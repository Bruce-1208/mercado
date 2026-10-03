"""Read-only source collection for semi-managed SKU clearance warnings.

Sales are trusted only after the whole 14-day order query has completed.
Warehouse dates and current SKU-level visits are deliberately supplied by the
companion store's verified facts, rather than inferred from listing creation,
domestic inventory, or delayed item-level visit data.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Mapping


LINK_BATCH_SIZE = 500
ITEM_ATTRIBUTES = (
    "id", "site_id", "title", "status", "available_quantity",
    "seller_custom_field", "attributes", "variations",
    "pictures", "secure_thumbnail", "thumbnail",
)
_SALES_ERROR = "近 14 天完整订单数据读取失败，请重新同步"
_INVENTORY_ERROR = "当前平台库存读取失败，请重新同步"
_VARIATION_ERROR = "订单缺少变体信息，无法确认该 SKU 的销量"
_VISITS_ERROR = "平台商品访问量读取失败"
_LOCAL_ITEM = re.compile(r"^(ML[A-Z]|MCO|MPE|MEC)\d+$")


def _enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _utc(value: Any = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("日期格式无效")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _quantity(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("数量缺失")
    quantity = int(value)
    if quantity < 0 or str(value).strip() != str(quantity):
        raise ValueError("数量无效")
    return quantity


def eligible_scopes(
    token_summaries: Iterable[Mapping[str, Any]],
    allowed_token_ids: Iterable[int] | None = None,
) -> dict[tuple[int, str], dict[str, Any]]:
    from erp.mercadolibre_clearance_rules import is_semi_managed_group

    allowed = None if allowed_token_ids is None else {int(value) for value in allowed_token_ids}
    scopes: dict[tuple[int, str], dict[str, Any]] = {}
    for summary in token_summaries:
        token_id = int(summary.get("id") or summary.get("token_id") or 0)
        if token_id <= 0 or not _enabled(summary.get("enabled", True)):
            continue
        if allowed is not None and token_id not in allowed:
            continue
        for setting in summary.get("site_settings") or []:
            site_id = str(setting.get("site_id") or "").strip().upper()
            group_name = str(setting.get("group_name") or "").strip()
            if not site_id or not is_semi_managed_group(group_name):
                continue
            scopes[(token_id, site_id)] = {
                "token_id": token_id,
                "site_id": site_id,
                "store_name": str(summary.get("display_name") or summary.get("nickname") or token_id),
                "group_name": group_name,
                "salesperson": str(setting.get("salesperson") or ""),
            }
    return scopes


def _connect():
    from bit.bit_mysql import config, pymysql

    return pymysql.connect(**config)


def load_store_links(token_id: int, site_ids: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Keyset pages; every connection is closed before yielding any listing."""
    sites = sorted({str(value or "").strip().upper() for value in site_ids if str(value or "").strip()})
    if not sites:
        return
    after_id = 0
    while True:
        try:
            connection = _connect()
        except Exception:
            raise RuntimeError("店铺商品列表读取失败") from None
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT `id` AS `link_id`, `token_id`, `site_id`, `item_id`, "
                    "`title`, `thumbnail_url`, `seller_sku`, `status`, `available_quantity`, "
                    "`last_synced_at`, `remote_json` "
                    "FROM `erp_mercadolibre_store_links` "
                    "WHERE `token_id` = %s AND `is_current` = 1 "
                    f"AND `site_id` IN ({','.join(['%s'] * len(sites))}) "
                    "AND `id` > %s ORDER BY `id` ASC LIMIT %s",
                    (int(token_id), *sites, after_id, LINK_BATCH_SIZE),
                )
                rows = [dict(row) for row in cursor.fetchall()]
        except Exception:
            raise RuntimeError("店铺商品列表读取失败") from None
        finally:
            connection.close()
        if not rows:
            break
        next_id = int(rows[-1].get("link_id") or 0)
        if next_id <= after_id:
            raise RuntimeError("店铺商品分页未能完成")
        for row in rows:
            yield row
        after_id = next_id
        if len(rows) < LINK_BATCH_SIZE:
            break


def _client_for_store(token_id: int):
    from bit import bit_mysql, bit_order_sync

    try:
        token = bit_mysql.get_mercado_store_token(token_id)
        if not token:
            raise ValueError("授权不存在")
        return bit_order_sync._client_and_token(dict(token))
    except Exception:
        raise RuntimeError("店铺授权读取失败，请检查授权后重试") from None


def _fetch_orders(client, seller_id: str, filters: Mapping[str, Any]):
    from bit import bit_order_sync

    return bit_order_sync._fetch_orders(client, seller_id, dict(filters))


def _line_site(product: Mapping[str, Any], order: Mapping[str, Any]) -> str:
    item_id = str(product.get("id") or "").upper()
    if _LOCAL_ITEM.fullmatch(item_id):
        return item_id[:3]
    return str(product.get("site_id") or order.get("site_id") or "").strip().upper()


def _order_sales(
    orders: Iterable[Mapping[str, Any]], site_ids: set[str], now: datetime,
) -> tuple[dict[tuple[str, str, str], tuple[int, int]], set[tuple[str, str]]]:
    """Return complete per-variation totals, or fail the entire coverage gate."""
    quantities: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    missing_variations: set[tuple[str, str]] = set()
    seen_orders: set[str] = set()
    start14, start7 = now - timedelta(days=14), now - timedelta(days=7)
    for order in orders:
        if not isinstance(order, Mapping):
            raise ValueError("订单数据不完整")
        if str(order.get("status") or "").lower() in {"cancelled", "canceled", "invalid"}:
            continue
        order_id = str(order.get("id") or "")
        if not order_id:
            raise ValueError("订单标识缺失")
        if order_id in seen_orders:
            continue
        seen_orders.add(order_id)
        created = _utc(order.get("date_created")) if order.get("date_created") else None
        if created is None:
            raise ValueError("订单日期缺失")
        if not start14 <= created < now:
            continue
        lines = order.get("order_items")
        if not isinstance(lines, list) or not lines:
            raise ValueError("订单商品明细缺失")
        for line in lines:
            if not isinstance(line, Mapping) or not isinstance(line.get("item"), Mapping):
                raise ValueError("订单商品明细无效")
            product = line["item"]
            site_id = _line_site(product, order)
            if site_id not in site_ids:
                continue
            item_id = str(product.get("id") or "").strip().upper()
            if not item_id:
                raise ValueError("订单商品标识缺失")
            variation_id = str(product.get("variation_id") or "").strip()
            amount = _quantity(line.get("quantity"))
            quantities[(site_id, item_id, variation_id)][1] += amount
            if created >= start7:
                quantities[(site_id, item_id, variation_id)][0] += amount
            if not variation_id and amount:
                missing_variations.add((site_id, item_id))
    return {key: tuple(values) for key, values in quantities.items()}, missing_variations


def _cached_item(link: Mapping[str, Any]) -> dict[str, Any]:
    raw = link.get("remote_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        result = json.loads(raw or "{}")
        return dict(result) if isinstance(result, Mapping) else {}
    except (TypeError, ValueError):
        return {}


def _main_image(item: Mapping[str, Any], link: Mapping[str, Any]) -> str:
    for product in (item, _cached_item(link)):
        pictures = product.get("pictures") or []
        if isinstance(pictures, list) and pictures and isinstance(pictures[0], Mapping):
            value = pictures[0].get("secure_url") or pictures[0].get("url")
            if value:
                return str(value)
        value = product.get("secure_thumbnail") or product.get("thumbnail")
        if value:
            return str(value)
    return str(link.get("thumbnail_url") or "")


def fill_missing_images(rows: list[dict[str, Any]]) -> None:
    """Enrich old snapshots from current links without rerunning the scan."""
    missing = [row for row in rows if not row.get("thumbnail_url")]
    if not missing:
        return
    ids = sorted({int(row["link_id"]) for row in missing})
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT `id` AS `link_id`, `token_id`, `site_id`, `item_id`, `thumbnail_url` "
                "FROM `erp_mercadolibre_store_links` WHERE `is_current` = 1 "
                f"AND `id` IN ({','.join(['%s'] * len(ids))})", tuple(ids))
            links = {int(link["link_id"]): link for link in cursor.fetchall()}
    finally:
        connection.close()
    for row in missing:
        link = links.get(int(row["link_id"]))
        if link and all(str(link.get(key)) == str(row.get(key)) for key in ("token_id", "site_id", "item_id")):
            row["thumbnail_url"] = str(link.get("thumbnail_url") or "")


def _sku(product: Mapping[str, Any]) -> str:
    value = product.get("seller_sku") or product.get("seller_custom_field")
    if value:
        return str(value)
    for attribute in product.get("attributes") or []:
        if isinstance(attribute, Mapping) and str(attribute.get("id") or "").upper() == "SELLER_SKU":
            return str(attribute.get("value_name") or "")
    return ""


def _variation_label(variation: Mapping[str, Any]) -> str:
    labels = []
    for attribute in variation.get("attribute_combinations") or []:
        if isinstance(attribute, Mapping) and attribute.get("value_name"):
            name = str(attribute.get("name") or attribute.get("id") or "")
            labels.append(f"{name}: {attribute['value_name']}" if name else str(attribute["value_name"]))
    return " · ".join(labels)


def _item_inventory(client, link: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    cached = _cached_item(link)
    try:
        current = client.get_marketplace_item(str(link["item_id"]), attributes=ITEM_ATTRIBUTES)
        if not isinstance(current, Mapping):
            raise ValueError("库存数据缺失")
        current = dict(current)
        if str(current.get("id") or "").upper() != str(link["item_id"]).upper():
            raise ValueError("商品标识不匹配")
        if current.get("site_id") and str(current["site_id"]).upper() != str(link["site_id"]).upper():
            raise ValueError("站点不匹配")
        if not str(current.get("status") or "").strip():
            raise ValueError("商品状态缺失")
        if cached.get("variations") and "variations" not in current:
            raise ValueError("变体库存缺失")
        variations = current.get("variations") or []
        if not isinstance(variations, list):
            raise ValueError("变体库存无效")
        seen = set()
        for variation in variations:
            if not isinstance(variation, Mapping) or not variation.get("id"):
                raise ValueError("变体标识缺失")
            identity = str(variation["id"])
            if identity in seen:
                raise ValueError("变体标识重复")
            seen.add(identity)
            _quantity(variation.get("available_quantity"))
        if not variations:
            _quantity(current.get("available_quantity"))
        return current, True
    except Exception:
        return cached, False


def _visits_reference(client, item_id: str, now: datetime) -> dict[str, Any]:
    # The official Visits API reports local-item VIP visits within 48 hours.
    # Keep that older item-level window separate from current SKU-level facts.
    result: dict[str, Any] = {
        "visits_api_30d": None, "visits_api_as_of": "", "visits_api_from": "",
        "visits_api_error": "", "visits_api_scope": "item",
    }
    if not _LOCAL_ITEM.fullmatch(item_id):
        result["visits_api_error"] = "平台访问量仅支持本地商品，CBT 商品不支持"
        return result
    ending = (now - timedelta(hours=48)).date().isoformat()
    try:
        response = client.request(
            "GET", f"/items/{item_id}/visits/time_window",
            params={"last": 30, "unit": "day", "ending": ending},
        )
        if not isinstance(response, Mapping):
            raise ValueError("访问量数据缺失")
        result["visits_api_30d"] = _quantity(response.get("total_visits"))
        result["visits_api_as_of"] = str(response.get("date_to") or ending)
        result["visits_api_from"] = str(response.get("date_from") or "")
    except Exception:
        result["visits_api_error"] = _VISITS_ERROR
    return result


def collect_store(
    token_summary: Mapping[str, Any],
    scopes: Mapping[tuple[int, str], Mapping[str, Any]],
    now: Any = None,
) -> Iterator[dict[str, Any]]:
    """Yield one snapshot row per SKU; store callers commit only on completion."""
    current_time = _utc(now)
    token_id = int(token_summary.get("id") or token_summary.get("token_id") or 0)
    # Recheck the summary so passing an unrelated or disabled scope cannot
    # collect a self-shipping store or another token's site.
    confirmed = eligible_scopes([token_summary], {token_id})
    selected = {key: value for key, value in confirmed.items() if key in scopes}
    sites = {key[1] for key in selected}
    if not sites:
        return
    client, token = _client_for_store(token_id)
    seller_id = str(token.get("meli_user_id") or token.get("seller_id") or "")
    if not seller_id:
        raise RuntimeError("店铺授权缺少卖家标识，请检查授权后重试")
    checked_at = current_time.replace(microsecond=0).isoformat()
    sales_complete = True
    quantities: dict[tuple[str, str, str], tuple[int, int]] = {}
    ambiguous: set[tuple[str, str]] = set()
    try:
        filters = {
            "sort": "date_asc",
            "date_created.from": (current_time - timedelta(days=14)).isoformat(timespec="milliseconds"),
            "date_created.to": current_time.isoformat(timespec="milliseconds"),
        }
        quantities, ambiguous = _order_sales(_fetch_orders(client, seller_id, filters), sites, current_time)
    except Exception:
        sales_complete = False
    for link in load_store_links(token_id, sites):
        site_id = str(link.get("site_id") or "").strip().upper()
        scope = selected.get((token_id, site_id))
        if scope is None:
            continue
        item_id = str(link.get("item_id") or "").strip().upper()
        if not item_id:
            raise RuntimeError("店铺商品标识缺失，无法完成同步")
        item, inventory_complete = _item_inventory(client, link)
        raw_variations = item.get("variations") or []
        variants = [value for value in raw_variations if isinstance(value, Mapping) and value.get("id")] if isinstance(raw_variations, list) else []
        variants = variants or [{}]
        rows = []
        for variation in variants:
            variation_id = str(variation.get("id") or "")
            row_sales_complete = sales_complete and not (variation_id and (site_id, item_id) in ambiguous)
            errors = []
            if not inventory_complete:
                errors.append(_INVENTORY_ERROR)
            if not sales_complete:
                errors.append(_SALES_ERROR)
            elif not row_sales_complete:
                errors.append(_VARIATION_ERROR)
            sales7, sales14 = quantities.get((site_id, item_id, variation_id), (0, 0))
            quantity = variation.get("available_quantity") if variation_id else item.get("available_quantity")
            status = str(item.get("status") or link.get("status") or "").lower()
            row = {
                **scope,
                "link_id": link.get("link_id"), "item_id": item_id,
                "sku_key": f"variation:{variation_id}" if variation_id else "item",
                "variation_id": variation_id,
                "variation": _variation_label(variation),
                "seller_sku": _sku(variation) or _sku(item) or str(link.get("seller_sku") or ""),
                "title": str(item.get("title") or link.get("title") or item_id),
                "thumbnail_url": _main_image(item, link),
                "status": status, "on_sale": status == "active",
                "quantity": int(quantity) if inventory_complete else None,
                "available_quantity": int(quantity) if inventory_complete else None,
                "inventory_complete": inventory_complete,
                "sales_7d": sales7 if row_sales_complete else None,
                "sales_14d": sales14 if row_sales_complete else None,
                "sales_complete": row_sales_complete,
                "sales_window_from": (current_time - timedelta(days=14)).isoformat(),
                "sales_window_to": current_time.isoformat(),
                "visits_complete": False, "visits_30d": None,
                "checked_at": checked_at, "source_errors": errors,
                "pending_data": bool(errors),
                "last_synced_at": str(link.get("last_synced_at") or ""),
            }
            rows.append(row)
        need_visits = any(
            (not row["inventory_complete"] or (row["on_sale"] and row["quantity"] > 0))
            and (not row["sales_complete"] or row["sales_7d"] == 0 or row["sales_14d"] < 7)
            for row in rows
        )
        visits = _visits_reference(client, item_id, current_time) if need_visits else {
            "visits_api_30d": None, "visits_api_as_of": "", "visits_api_from": "",
            "visits_api_error": "", "visits_api_scope": "item",
        }
        for row in rows:
            row.update(visits)
            row["visits_api_shared_by_variants"] = bool(row["variation_id"])
            yield row
