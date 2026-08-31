"""Inventory shelves, stock balances and immutable movement logs.

The workbench can run either beside MySQL or as a thin client of another
workbench instance.  This module contains the direct MySQL implementation; the
HTTP adapter lives in :mod:`bit.bit_db_api`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping


SHELF_TABLE = "inventory_shelves"
STOCK_TABLE = "inventory_stocks"
MOVEMENT_TABLE = "inventory_movements"
PRODUCT_TABLE = "erp_mercadolibre_products"
ORDER_TABLE = "mercado_synced_orders"

MONEY_QUANTUM = Decimal("0.0001")
MOVEMENT_TYPES = {"inbound", "outbound"}


def _connect():
    import pymysql
    from bit.bit_mysql import config

    return pymysql.connect(**config)


def _now() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _json_row(row: Mapping[str, Any] | None) -> dict[str, Any]:
    result = {key: _json_value(value) for key, value in dict(row or {}).items()}
    if "is_active" in result:
        result["is_active"] = bool(result["is_active"])
    return result


def _positive_int(value: Any, label: str, *, maximum: int = 1_000_000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数") from exc
    if number <= 0 or number > maximum:
        raise ValueError(f"{label}必须在 1–{maximum} 之间")
    return number


def _nonnegative_decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{label}不能小于 0")
    return number.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _optional_capacity(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return _positive_int(value, "货架容量", maximum=100_000_000)


def _parse_datetime(value: Any, label: str = "业务时间") -> str:
    text = str(value or "").strip()
    if not text:
        return _now()
    normalized = text.replace("T", " ")
    for date_format in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(normalized, date_format)
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValueError(f"{label}必须使用 YYYY-MM-DD HH:MM 格式")


def movement_effect(
    current_quantity: Any,
    current_unit_cost: Any,
    movement_type: str,
    quantity: Any,
    inbound_unit_cost: Any = None,
) -> dict[str, Any]:
    """Calculate a balance mutation without touching the database.

    Inbound stock uses a moving weighted-average cost.  Outbound stock retains
    that average and cannot make the balance negative.
    """

    before = max(0, int(current_quantity or 0))
    amount = _positive_int(quantity, "数量")
    old_cost = _nonnegative_decimal(current_unit_cost or 0, "当前成本")
    kind = str(movement_type or "").strip().lower()
    if kind not in MOVEMENT_TYPES:
        raise ValueError("操作类型只能是入库或出库")
    if kind == "outbound":
        if amount > before:
            raise ValueError(f"出库数量不能超过当前库存 {before}")
        return {
            "before_quantity": before,
            "after_quantity": before - amount,
            "unit_cost": old_cost,
            "movement_unit_cost": old_cost,
        }

    inbound_cost = _nonnegative_decimal(inbound_unit_cost, "单位成本")
    after = before + amount
    weighted_cost = (
        ((old_cost * before) + (inbound_cost * amount)) / after
    ).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    return {
        "before_quantity": before,
        "after_quantity": after,
        "unit_cost": weighted_cost,
        "movement_unit_cost": inbound_cost,
    }


def ensure_inventory_tables(cursor: Any) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{SHELF_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `code` VARCHAR(64) NOT NULL,
            `name` VARCHAR(128) NOT NULL,
            `warehouse` VARCHAR(128) NULL,
            `location` VARCHAR(255) NULL,
            `capacity` INT NULL,
            `remark` VARCHAR(1000) NULL,
            `is_active` TINYINT(1) NOT NULL DEFAULT 1,
            `created_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_inventory_shelf_code` (`code`),
            KEY `idx_inventory_shelf_active` (`is_active`, `code`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{STOCK_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `shelf_id` BIGINT NOT NULL,
            `order_id` VARCHAR(64) NOT NULL,
            `product_id` VARCHAR(64) NOT NULL,
            `product_name` VARCHAR(255) NOT NULL,
            `image_url` VARCHAR(1500) NULL,
            `quantity` INT NOT NULL DEFAULT 0,
            `unit_cost` DECIMAL(20,4) NOT NULL DEFAULT 0,
            `first_inbound_at` DATETIME NOT NULL,
            `last_inbound_at` DATETIME NOT NULL,
            `created_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_inventory_stock_lot` (`shelf_id`, `order_id`, `product_id`),
            KEY `idx_inventory_stock_product` (`product_id`, `quantity`),
            KEY `idx_inventory_stock_order` (`order_id`),
            KEY `idx_inventory_stock_inbound` (`last_inbound_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{MOVEMENT_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `stock_id` BIGINT NOT NULL,
            `shelf_id` BIGINT NOT NULL,
            `movement_type` VARCHAR(16) NOT NULL,
            `order_id` VARCHAR(64) NOT NULL,
            `product_id` VARCHAR(64) NOT NULL,
            `product_name` VARCHAR(255) NOT NULL,
            `image_url` VARCHAR(1500) NULL,
            `quantity` INT NOT NULL,
            `unit_cost` DECIMAL(20,4) NOT NULL DEFAULT 0,
            `total_cost` DECIMAL(20,4) NOT NULL DEFAULT 0,
            `before_quantity` INT NOT NULL,
            `after_quantity` INT NOT NULL,
            `reference_no` VARCHAR(128) NULL,
            `remark` VARCHAR(1000) NULL,
            `operator_id` BIGINT NULL,
            `operator_name` VARCHAR(128) NULL,
            `occurred_at` DATETIME NOT NULL,
            `created_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            KEY `idx_inventory_movement_time` (`occurred_at`, `id`),
            KEY `idx_inventory_movement_stock` (`stock_id`, `id`),
            KEY `idx_inventory_movement_order` (`order_id`),
            KEY `idx_inventory_movement_type` (`movement_type`, `occurred_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def _table_exists(cursor: Any, table_name: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table_name,))
    return bool(cursor.fetchone())


def _normalize_shelf_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    code = str(data.get("code") or "").strip().upper()
    name = str(data.get("name") or "").strip()
    if not code or len(code) > 64:
        raise ValueError("货架编码不能为空且最多 64 个字符")
    if not re.fullmatch(r"[A-Z0-9._\-/]+", code):
        raise ValueError("货架编码仅支持字母、数字、点、横线、斜杠和下划线")
    if not name or len(name) > 128:
        raise ValueError("货架名称不能为空且最多 128 个字符")
    return {
        "code": code,
        "name": name,
        "warehouse": str(data.get("warehouse") or "").strip()[:128],
        "location": str(data.get("location") or "").strip()[:255],
        "capacity": _optional_capacity(data.get("capacity")),
        "remark": str(data.get("remark") or "").strip()[:1000],
        "is_active": 1 if bool(data.get("is_active", True)) else 0,
    }


def list_inventory_shelves(*, include_inactive: bool = True) -> dict[str, Any]:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            where = "" if include_inactive else "WHERE shelves.`is_active` = 1"
            cursor.execute(
                f"""
                SELECT shelves.*,
                       COALESCE(SUM(stocks.`quantity`), 0) AS `stock_quantity`,
                       COALESCE(SUM(CASE WHEN stocks.`quantity` > 0 THEN 1 ELSE 0 END), 0)
                           AS `stock_lots`
                FROM `{SHELF_TABLE}` AS shelves
                LEFT JOIN `{STOCK_TABLE}` AS stocks ON stocks.`shelf_id` = shelves.`id`
                {where}
                GROUP BY shelves.`id`
                ORDER BY shelves.`is_active` DESC, shelves.`code` ASC
                """
            )
            rows = [_json_row(row) for row in (cursor.fetchall() or [])]
        connection.commit()
        return {"rows": rows, "total": len(rows)}
    finally:
        connection.close()


def create_inventory_shelf(data: Mapping[str, Any]) -> dict[str, Any]:
    import pymysql

    payload = _normalize_shelf_payload(data or {})
    now = _now()
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            try:
                cursor.execute(
                    f"""
                    INSERT INTO `{SHELF_TABLE}`
                        (`code`, `name`, `warehouse`, `location`, `capacity`, `remark`,
                         `is_active`, `created_at`, `updated_at`)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (*payload.values(), now, now),
                )
            except pymysql.err.IntegrityError as exc:
                raise ValueError("货架编码已存在") from exc
            shelf_id = int(cursor.lastrowid)
        connection.commit()
        return {"id": shelf_id, **payload, "stock_quantity": 0, "stock_lots": 0}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_inventory_shelf(shelf_id: Any, data: Mapping[str, Any]) -> dict[str, Any]:
    import pymysql

    normalized_id = _positive_int(shelf_id, "货架编号")
    payload = _normalize_shelf_payload(data or {})
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{SHELF_TABLE}` WHERE `id` = %s FOR UPDATE",
                (normalized_id,),
            )
            if not cursor.fetchone():
                raise KeyError("货架不存在")
            cursor.execute(
                f"SELECT COALESCE(SUM(`quantity`), 0) AS `quantity` "
                f"FROM `{STOCK_TABLE}` WHERE `shelf_id` = %s",
                (normalized_id,),
            )
            stock_quantity = int((cursor.fetchone() or {}).get("quantity") or 0)
            if payload["capacity"] is not None and payload["capacity"] < stock_quantity:
                raise ValueError(f"货架容量不能小于当前库存 {stock_quantity}")
            if not payload["is_active"] and stock_quantity > 0:
                raise ValueError("货架仍有库存，清空后才能停用")
            try:
                cursor.execute(
                    f"""
                    UPDATE `{SHELF_TABLE}`
                    SET `code` = %s, `name` = %s, `warehouse` = %s,
                        `location` = %s, `capacity` = %s, `remark` = %s,
                        `is_active` = %s, `updated_at` = %s
                    WHERE `id` = %s
                    """,
                    (*payload.values(), _now(), normalized_id),
                )
            except pymysql.err.IntegrityError as exc:
                raise ValueError("货架编码已存在") from exc
        connection.commit()
        return {"id": normalized_id, **payload, "stock_quantity": stock_quantity}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_inventory_stock(
    *,
    search: str = "",
    shelf_id: Any = None,
    stock_status: str = "positive",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    page = max(1, int(page))
    page_size = max(10, min(int(page_size), 200))
    where: list[str] = []
    params: list[Any] = []
    search = str(search or "").strip()
    if search:
        pattern = f"%{search}%"
        where.append(
            "(stocks.`order_id` LIKE %s OR stocks.`product_id` LIKE %s OR "
            "stocks.`product_name` LIKE %s OR shelves.`code` LIKE %s OR shelves.`name` LIKE %s)"
        )
        params.extend([pattern] * 5)
    if shelf_id not in (None, ""):
        where.append("stocks.`shelf_id` = %s")
        params.append(_positive_int(shelf_id, "货架编号"))
    status = str(stock_status or "positive").strip().lower()
    if status == "positive":
        where.append("stocks.`quantity` > 0")
    elif status == "empty":
        where.append("stocks.`quantity` = 0")
    elif status != "all":
        raise ValueError("库存状态筛选无效")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            source_sql = f"""
                FROM `{STOCK_TABLE}` AS stocks
                INNER JOIN `{SHELF_TABLE}` AS shelves ON shelves.`id` = stocks.`shelf_id`
                {where_sql}
            """
            cursor.execute(f"SELECT COUNT(*) AS `total` {source_sql}", tuple(params))
            total = int((cursor.fetchone() or {}).get("total") or 0)
            cursor.execute(
                f"""
                SELECT COALESCE(SUM(stocks.`quantity`), 0) AS `quantity`,
                       COALESCE(SUM(stocks.`quantity` * stocks.`unit_cost`), 0) AS `cost`,
                       COALESCE(SUM(CASE WHEN stocks.`quantity` > 0 THEN 1 ELSE 0 END), 0)
                           AS `stock_lots`,
                       COUNT(DISTINCT CASE WHEN stocks.`quantity` > 0 THEN stocks.`shelf_id` END)
                           AS `occupied_shelves`
                {source_sql}
                """,
                tuple(params),
            )
            summary = _json_row(cursor.fetchone())
            cursor.execute(
                f"""
                SELECT stocks.*, shelves.`code` AS `shelf_code`, shelves.`name` AS `shelf_name`,
                       shelves.`warehouse`, shelves.`location`, shelves.`capacity`,
                       shelves.`is_active` AS `shelf_active`,
                       stocks.`quantity` * stocks.`unit_cost` AS `total_cost`
                {source_sql}
                ORDER BY stocks.`quantity` > 0 DESC, stocks.`last_inbound_at` DESC, stocks.`id` DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [page_size, (page - 1) * page_size]),
            )
            rows = [_json_row(row) for row in (cursor.fetchall() or [])]
        connection.commit()
        return {
            "rows": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
            "summary": summary,
        }
    finally:
        connection.close()


def _order_items(raw_json: Any, fallback: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    from bit.bit_mysql import _mercado_order_sku_items

    fallback = dict(fallback or {})
    items = _mercado_order_sku_items(raw_json, fallback.get("image_url") or "")
    if items:
        return items
    product_id = str(fallback.get("product_id") or "").strip()
    if not product_id:
        return []
    return [{
        "product_id": product_id,
        "title": str(fallback.get("title") or product_id),
        "image_url": str(fallback.get("image_url") or ""),
        "quantity": 1,
        "variation": "",
        "seller_sku": "",
    }]


def _suggested_unit_cost(
    purchase_cost: Any,
    items: list[Mapping[str, Any]],
    product_id: str,
) -> Decimal | None:
    """Use an order-level purchase cost only when allocation is unambiguous."""

    if purchase_cost in (None, ""):
        return None
    try:
        total_cost = _nonnegative_decimal(purchase_cost, "采购成本")
    except ValueError:
        return None
    product_ids = {
        str(item.get("product_id") or "").strip()
        for item in items
        if str(item.get("product_id") or "").strip()
    }
    if product_ids != {str(product_id or "").strip()}:
        return None
    quantity = sum(max(0, int(item.get("quantity") or 0)) for item in items)
    if quantity <= 0:
        return None
    return (total_cost / quantity).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def list_inventory_matches(*, search: str = "", limit: int = 30) -> dict[str, Any]:
    search = str(search or "").strip()
    limit = max(1, min(int(limit), 100))
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            order_rows: list[dict[str, Any]] = []
            if _table_exists(cursor, ORDER_TABLE):
                params: list[Any] = []
                where = ""
                if search:
                    pattern = f"%{search}%"
                    where = (
                        "WHERE (`order_id` LIKE %s OR `product_id` LIKE %s OR "
                        "`title` LIKE %s OR `shop_name` LIKE %s OR `raw_json` LIKE %s)"
                    )
                    params.extend([pattern] * 5)
                cursor.execute(
                    f"""
                    SELECT `order_id`, `product_id`, `title`, `image_url`, `raw_json`,
                           `shop_name`, `purchase_cost`,
                           DATE_ADD(`date_created`, INTERVAL 8 HOUR) AS `ordered_at`
                    FROM `{ORDER_TABLE}`
                    {where}
                    ORDER BY `date_created` DESC
                    LIMIT %s
                    """,
                    tuple(params + [limit]),
                )
                for order in cursor.fetchall() or []:
                    items = _order_items(order.get("raw_json"), order)
                    for item in items:
                        haystack = " ".join((
                            str(order.get("order_id") or ""),
                            str(item.get("product_id") or ""),
                            str(item.get("seller_sku") or ""),
                            str(item.get("title") or ""),
                        )).casefold()
                        if search and search.casefold() not in haystack:
                            continue
                        order_rows.append({
                            "order_id": str(order.get("order_id") or ""),
                            "product_id": str(item.get("product_id") or ""),
                            "product_name": str(item.get("title") or order.get("title") or ""),
                            "image_url": str(item.get("image_url") or order.get("image_url") or ""),
                            "order_quantity": int(item.get("quantity") or 0),
                            "variation": str(item.get("variation") or ""),
                            "seller_sku": str(item.get("seller_sku") or ""),
                            "shop_name": str(order.get("shop_name") or ""),
                            "ordered_at": _json_value(order.get("ordered_at")),
                            "suggested_unit_cost": _json_value(
                                _suggested_unit_cost(
                                    order.get("purchase_cost"),
                                    items,
                                    str(item.get("product_id") or ""),
                                )
                            ),
                        })
                        if len(order_rows) >= limit:
                            break
                    if len(order_rows) >= limit:
                        break

            product_rows: list[dict[str, Any]] = []
            if _table_exists(cursor, PRODUCT_TABLE):
                product_params: list[Any] = []
                product_where = ""
                if search:
                    pattern = f"%{search}%"
                    product_where = "WHERE (`source_item_id` LIKE %s OR `title` LIKE %s)"
                    product_params.extend((pattern, pattern))
                cursor.execute(
                    f"""
                    SELECT `id`, `source_item_id`, `title`, `main_image_url`, `price`,
                           `currency_id`, `source_type`, `added_at`
                    FROM `{PRODUCT_TABLE}`
                    {product_where}
                    ORDER BY `id` DESC
                    LIMIT %s
                    """,
                    tuple(product_params + [limit]),
                )
                product_rows = [
                    {
                        "id": int(row.get("id") or 0),
                        "product_id": str(row.get("source_item_id") or ""),
                        "product_name": str(row.get("title") or ""),
                        "image_url": str(row.get("main_image_url") or ""),
                        "price": _json_value(row.get("price")),
                        "currency_id": str(row.get("currency_id") or ""),
                        "source_type": str(row.get("source_type") or ""),
                        "added_at": _json_value(row.get("added_at")),
                    }
                    for row in (cursor.fetchall() or [])
                ]
        connection.commit()
        return {"orders": order_rows, "products": product_rows}
    finally:
        connection.close()


def _matched_order_product(cursor: Any, order_id: str, product_id: str) -> dict[str, Any]:
    if not _table_exists(cursor, ORDER_TABLE):
        raise ValueError("订单同步表不存在，请先拉取订单")
    cursor.execute(
        f"""
        SELECT `order_id`, `product_id`, `title`, `image_url`, `raw_json`, `purchase_cost`
        FROM `{ORDER_TABLE}` WHERE `order_id` = %s LIMIT 1
        """,
        (order_id,),
    )
    order = cursor.fetchone()
    if not order:
        raise ValueError("匹配的订单不存在，请重新搜索选择")
    items = _order_items(order.get("raw_json"), order)
    for item in items:
        if str(item.get("product_id") or "").strip() == product_id:
            return {
                "product_name": str(item.get("title") or order.get("title") or product_id)[:255],
                "image_url": str(item.get("image_url") or order.get("image_url") or "")[:1500],
                "suggested_unit_cost": _suggested_unit_cost(
                    order.get("purchase_cost"), items, product_id
                ),
            }
    raise ValueError("所选产品不属于该订单，请重新匹配")


def create_inventory_movement(data: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(data or {})
    movement_type = str(payload.get("movement_type") or "").strip().lower()
    if movement_type not in MOVEMENT_TYPES:
        raise ValueError("操作类型只能是入库或出库")
    quantity = _positive_int(payload.get("quantity"), "数量")
    occurred_at = _parse_datetime(payload.get("occurred_at"))
    operator_id = payload.get("operator_id")
    operator_id = int(operator_id) if str(operator_id or "").strip() else None
    operator_name = str(payload.get("operator_name") or "").strip()[:128]
    reference_no = str(payload.get("reference_no") or "").strip()[:128]
    remark = str(payload.get("remark") or "").strip()[:1000]
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            now = _now()
            if movement_type == "inbound":
                shelf_id = _positive_int(payload.get("shelf_id"), "货架编号")
                order_id = str(payload.get("order_id") or "").strip()[:64]
                product_id = str(payload.get("product_id") or "").strip()[:64]
                if not order_id:
                    raise ValueError("入库必须匹配订单")
                if not product_id:
                    raise ValueError("入库必须匹配产品")
                context = _matched_order_product(cursor, order_id, product_id)
                cursor.execute(
                    f"SELECT * FROM `{SHELF_TABLE}` WHERE `id` = %s FOR UPDATE",
                    (shelf_id,),
                )
                shelf = cursor.fetchone()
                if not shelf:
                    raise ValueError("入库货架不存在")
                if not shelf.get("is_active"):
                    raise ValueError("入库货架已停用")
                cursor.execute(
                    f"SELECT COALESCE(SUM(`quantity`), 0) AS `quantity` "
                    f"FROM `{STOCK_TABLE}` WHERE `shelf_id` = %s",
                    (shelf_id,),
                )
                shelf_quantity = int((cursor.fetchone() or {}).get("quantity") or 0)
                if shelf.get("capacity") is not None and shelf_quantity + quantity > int(shelf["capacity"]):
                    raise ValueError(
                        f"入库后将超过货架容量 {int(shelf['capacity'])}，当前库存 {shelf_quantity}"
                    )
                cursor.execute(
                    f"""
                    SELECT * FROM `{STOCK_TABLE}`
                    WHERE `shelf_id` = %s AND `order_id` = %s AND `product_id` = %s
                    FOR UPDATE
                    """,
                    (shelf_id, order_id, product_id),
                )
                stock = cursor.fetchone()
                inbound_cost = payload.get("unit_cost")
                if inbound_cost in (None, ""):
                    inbound_cost = context.get("suggested_unit_cost")
                if inbound_cost in (None, ""):
                    raise ValueError("请输入单位成本")
                effect = movement_effect(
                    (stock or {}).get("quantity", 0),
                    (stock or {}).get("unit_cost", 0),
                    movement_type,
                    quantity,
                    inbound_cost,
                )
                if stock:
                    stock_id = int(stock["id"])
                    cursor.execute(
                        f"""
                        UPDATE `{STOCK_TABLE}`
                        SET `product_name` = %s, `image_url` = %s, `quantity` = %s,
                            `unit_cost` = %s, `last_inbound_at` = %s, `updated_at` = %s
                        WHERE `id` = %s
                        """,
                        (
                            context["product_name"], context["image_url"],
                            effect["after_quantity"], effect["unit_cost"],
                            occurred_at, now, stock_id,
                        ),
                    )
                else:
                    cursor.execute(
                        f"""
                        INSERT INTO `{STOCK_TABLE}`
                            (`shelf_id`, `order_id`, `product_id`, `product_name`, `image_url`,
                             `quantity`, `unit_cost`, `first_inbound_at`, `last_inbound_at`,
                             `created_at`, `updated_at`)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            shelf_id, order_id, product_id, context["product_name"],
                            context["image_url"], effect["after_quantity"], effect["unit_cost"],
                            occurred_at, occurred_at, now, now,
                        ),
                    )
                    stock_id = int(cursor.lastrowid)
                stock_snapshot = {
                    "id": stock_id,
                    "shelf_id": shelf_id,
                    "order_id": order_id,
                    "product_id": product_id,
                    "product_name": context["product_name"],
                    "image_url": context["image_url"],
                }
            else:
                stock_id = _positive_int(payload.get("stock_id"), "库存记录编号")
                cursor.execute(
                    f"SELECT * FROM `{STOCK_TABLE}` WHERE `id` = %s FOR UPDATE",
                    (stock_id,),
                )
                stock_snapshot = cursor.fetchone()
                if not stock_snapshot:
                    raise ValueError("库存记录不存在")
                effect = movement_effect(
                    stock_snapshot.get("quantity"), stock_snapshot.get("unit_cost"),
                    movement_type, quantity,
                )
                cursor.execute(
                    f"UPDATE `{STOCK_TABLE}` SET `quantity` = %s, `updated_at` = %s WHERE `id` = %s",
                    (effect["after_quantity"], now, stock_id),
                )
                shelf_id = int(stock_snapshot["shelf_id"])
                order_id = str(stock_snapshot["order_id"])
                product_id = str(stock_snapshot["product_id"])

            total_cost = (
                effect["movement_unit_cost"] * quantity
            ).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
            cursor.execute(
                f"""
                INSERT INTO `{MOVEMENT_TABLE}`
                    (`stock_id`, `shelf_id`, `movement_type`, `order_id`, `product_id`,
                     `product_name`, `image_url`, `quantity`, `unit_cost`, `total_cost`,
                     `before_quantity`, `after_quantity`, `reference_no`, `remark`,
                     `operator_id`, `operator_name`, `occurred_at`, `created_at`)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s)
                """,
                (
                    stock_id, shelf_id, movement_type, order_id, product_id,
                    stock_snapshot.get("product_name"), stock_snapshot.get("image_url"),
                    quantity, effect["movement_unit_cost"], total_cost,
                    effect["before_quantity"], effect["after_quantity"], reference_no,
                    remark, operator_id, operator_name, occurred_at, now,
                ),
            )
            movement_id = int(cursor.lastrowid)
        connection.commit()
        return {
            "movement_id": movement_id,
            "stock_id": stock_id,
            "movement_type": movement_type,
            "quantity": quantity,
            "before_quantity": effect["before_quantity"],
            "after_quantity": effect["after_quantity"],
            "unit_cost": float(effect["movement_unit_cost"]),
            "total_cost": float(total_cost),
        }
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_inventory_movements(
    *,
    search: str = "",
    movement_type: str = "",
    shelf_id: Any = None,
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    page = max(1, int(page))
    page_size = max(10, min(int(page_size), 200))
    where: list[str] = []
    params: list[Any] = []
    search = str(search or "").strip()
    if search:
        pattern = f"%{search}%"
        where.append(
            "(movements.`order_id` LIKE %s OR movements.`product_id` LIKE %s OR "
            "movements.`product_name` LIKE %s OR movements.`reference_no` LIKE %s OR "
            "movements.`operator_name` LIKE %s OR shelves.`code` LIKE %s)"
        )
        params.extend([pattern] * 6)
    kind = str(movement_type or "").strip().lower()
    if kind:
        if kind not in MOVEMENT_TYPES:
            raise ValueError("日志类型筛选无效")
        where.append("movements.`movement_type` = %s")
        params.append(kind)
    if shelf_id not in (None, ""):
        where.append("movements.`shelf_id` = %s")
        params.append(_positive_int(shelf_id, "货架编号"))
    if str(date_from or "").strip():
        where.append("movements.`occurred_at` >= %s")
        params.append(_parse_datetime(date_from, "开始时间"))
    if str(date_to or "").strip():
        end = datetime.strptime(_parse_datetime(date_to, "结束时间"), "%Y-%m-%d %H:%M:%S")
        where.append("movements.`occurred_at` < %s")
        params.append((end + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            source_sql = f"""
                FROM `{MOVEMENT_TABLE}` AS movements
                INNER JOIN `{SHELF_TABLE}` AS shelves ON shelves.`id` = movements.`shelf_id`
                {where_sql}
            """
            cursor.execute(f"SELECT COUNT(*) AS `total` {source_sql}", tuple(params))
            total = int((cursor.fetchone() or {}).get("total") or 0)
            cursor.execute(
                f"""
                SELECT movements.*, shelves.`code` AS `shelf_code`,
                       shelves.`name` AS `shelf_name`, shelves.`warehouse`
                {source_sql}
                ORDER BY movements.`occurred_at` DESC, movements.`id` DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [page_size, (page - 1) * page_size]),
            )
            rows = [_json_row(row) for row in (cursor.fetchall() or [])]
        connection.commit()
        return {
            "rows": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
        }
    finally:
        connection.close()
