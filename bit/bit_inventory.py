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
SKU_TABLE = "inventory_skus"
SKU_MAPPING_TABLE = "inventory_sku_mappings"
RESERVATION_TABLE = "inventory_order_reservations"
PRODUCT_TABLE = "erp_mercadolibre_products"
ORDER_TABLE = "mercado_synced_orders"
STORE_LINK_TABLE = "erp_mercadolibre_store_links"

MONEY_QUANTUM = Decimal("0.0001")
MOVEMENT_TYPES = {"inbound", "outbound"}
STORE_SITE_SETTINGS_TABLE = "mercado_store_site_settings"


def _connect():
    from bit.bit_mysql import config, pymysql

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
            `order_remark` VARCHAR(1000) NULL,
            `salesperson` VARCHAR(128) NULL,
            `sku_id` BIGINT NOT NULL DEFAULT 0,
            `quantity` INT NOT NULL DEFAULT 0,
            `unit_cost` DECIMAL(20,4) NOT NULL DEFAULT 0,
            `first_inbound_at` DATETIME NOT NULL,
            `last_inbound_at` DATETIME NOT NULL,
            `created_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_inventory_stock_lot` (`shelf_id`, `order_id`, `product_id`, `sku_id`),
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
            `order_remark` VARCHAR(1000) NULL,
            `salesperson` VARCHAR(128) NULL,
            `sku_id` BIGINT NULL,
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
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{SKU_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `sku` VARCHAR(64) NOT NULL,
            `name` VARCHAR(255) NOT NULL,
            `product_item_id` BIGINT NULL,
            `safety_stock` INT NOT NULL DEFAULT 0,
            `is_active` TINYINT(1) NOT NULL DEFAULT 1,
            `created_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_inventory_sku_code` (`sku`),
            KEY `idx_inventory_sku_active` (`is_active`, `sku`),
            KEY `idx_inventory_sku_product` (`product_item_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{SKU_MAPPING_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `sku_id` BIGINT NOT NULL,
            `token_id` BIGINT NOT NULL,
            `store_name` VARCHAR(128) NOT NULL DEFAULT '',
            `site_id` VARCHAR(16) NOT NULL,
            `item_id` VARCHAR(64) NOT NULL,
            `variation_id` VARCHAR(64) NOT NULL DEFAULT '',
            `seller_sku` VARCHAR(255) NULL,
            `created_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_inventory_sku_listing_variant`
                (`token_id`, `site_id`, `item_id`, `variation_id`),
            UNIQUE KEY `uniq_inventory_sku_seller_sku`
                (`token_id`, `site_id`, `seller_sku`),
            KEY `idx_inventory_sku_mapping_sku` (`sku_id`, `id`),
            KEY `idx_inventory_sku_mapping_item` (`item_id`, `variation_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{RESERVATION_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `order_id` VARCHAR(64) NOT NULL,
            `line_key` VARCHAR(255) NOT NULL,
            `token_id` BIGINT NOT NULL,
            `site_id` VARCHAR(16) NOT NULL DEFAULT '',
            `item_id` VARCHAR(64) NOT NULL,
            `variation_id` VARCHAR(64) NOT NULL DEFAULT '',
            `seller_sku` VARCHAR(255) NULL,
            `product_name` VARCHAR(255) NOT NULL DEFAULT '',
            `sku_id` BIGINT NULL,
            `quantity` INT NOT NULL DEFAULT 0,
            `order_status` VARCHAR(64) NOT NULL DEFAULT '',
            `reservation_status` VARCHAR(16) NOT NULL DEFAULT 'unmapped',
            `created_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            `released_at` DATETIME NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_inventory_reservation_order_line` (`order_id`, `line_key`),
            KEY `idx_inventory_reservation_sku_status` (`sku_id`, `reservation_status`),
            KEY `idx_inventory_reservation_order` (`order_id`, `reservation_status`),
            KEY `idx_inventory_reservation_mapping` (`token_id`, `site_id`, `item_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    # Existing workbench databases predate the order-context fields above.  Keep
    # their schema forward-compatible without requiring a separate migration.
    for table_name, columns in (
        (STOCK_TABLE, (
            ("order_remark", "VARCHAR(1000) NULL"),
            ("salesperson", "VARCHAR(128) NULL"),
            ("sku_id", "BIGINT NOT NULL DEFAULT 0"),
        )),
        (MOVEMENT_TABLE, (
            ("order_remark", "VARCHAR(1000) NULL"),
            ("salesperson", "VARCHAR(128) NULL"),
            ("sku_id", "BIGINT NULL"),
        )),
    ):
        for column_name, definition in columns:
            cursor.execute(
                f"SHOW COLUMNS FROM `{table_name}` LIKE %s", (column_name,)
            )
            if not cursor.fetchone():
                cursor.execute(
                    f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {definition}"
                )
    cursor.execute(f"SHOW INDEX FROM `{STOCK_TABLE}` WHERE `Key_name` = %s", ("uniq_inventory_stock_lot",))
    stock_index_rows = cursor.fetchall() or []
    if stock_index_rows:
        indexed_columns = [str(row.get("Column_name") or "") for row in stock_index_rows]
        if indexed_columns == ["shelf_id", "order_id", "product_id"]:
            cursor.execute(f"ALTER TABLE `{STOCK_TABLE}` DROP INDEX `uniq_inventory_stock_lot`")
    cursor.execute(f"SHOW INDEX FROM `{STOCK_TABLE}` WHERE `Key_name` = %s", ("uniq_inventory_stock_lot",))
    if not cursor.fetchone():
        cursor.execute(
            f"ALTER TABLE `{STOCK_TABLE}` ADD UNIQUE KEY `uniq_inventory_stock_lot` "
            "(`shelf_id`, `order_id`, `product_id`, `sku_id`)"
        )


def _table_exists(cursor: Any, table_name: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table_name,))
    return bool(cursor.fetchone())


def _normalize_sku_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    sku = str(data.get("sku") or "").strip().upper()
    name = str(data.get("name") or "").strip()
    if not sku or len(sku) > 64 or not re.fullmatch(r"[A-Z0-9._/-]+", sku):
        raise ValueError("内部 SKU 不能为空，且仅支持字母、数字、点、横线、斜杠和下划线")
    if not name or len(name) > 255:
        raise ValueError("SKU 名称不能为空且最多 255 个字符")
    safety_stock = int(data.get("safety_stock") or 0)
    if safety_stock < 0 or safety_stock > 100_000_000:
        raise ValueError("安全库存必须在 0–100000000 之间")
    product_item_id = data.get("product_item_id")
    product_item_id = (
        _positive_int(product_item_id, "产品库编号")
        if product_item_id not in (None, "") else None
    )
    return {
        "sku": sku,
        "name": name,
        "product_item_id": product_item_id,
        "safety_stock": safety_stock,
        "is_active": 1 if bool(data.get("is_active", True)) else 0,
    }


def _validate_master_product(cursor: Any, product_item_id: int | None) -> None:
    if product_item_id is None:
        return
    if not _table_exists(cursor, PRODUCT_TABLE):
        raise ValueError("产品库尚未初始化，暂时不能关联 User Products 产品")
    cursor.execute(
        f"SELECT 1 FROM `{PRODUCT_TABLE}` WHERE `id` = %s LIMIT 1",
        (product_item_id,),
    )
    if not cursor.fetchone():
        raise ValueError("关联的 User Products 产品不存在")


def list_inventory_skus(*, include_inactive: bool = True) -> dict[str, Any]:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            active_filter = "" if include_inactive else "WHERE sku.`is_active` = 1"
            cursor.execute(
                f"""
                SELECT sku.*,
                       COALESCE(stock.`on_hand`, 0) AS `on_hand`,
                       COALESCE(reservation.`reserved`, 0) AS `reserved`,
                       COALESCE(mapping.`mapping_count`, 0) AS `mapping_count`
                FROM `{SKU_TABLE}` AS sku
                LEFT JOIN (
                    SELECT `sku_id`, SUM(`quantity`) AS `on_hand`
                    FROM `{STOCK_TABLE}` WHERE `sku_id` > 0 GROUP BY `sku_id`
                ) AS stock ON stock.`sku_id` = sku.`id`
                LEFT JOIN (
                    SELECT `sku_id`, SUM(`quantity`) AS `reserved`
                    FROM `{RESERVATION_TABLE}`
                    WHERE `reservation_status` = 'reserved' AND `sku_id` IS NOT NULL
                    GROUP BY `sku_id`
                ) AS reservation ON reservation.`sku_id` = sku.`id`
                LEFT JOIN (
                    SELECT `sku_id`, COUNT(*) AS `mapping_count`
                    FROM `{SKU_MAPPING_TABLE}` GROUP BY `sku_id`
                ) AS mapping ON mapping.`sku_id` = sku.`id`
                {active_filter}
                ORDER BY sku.`is_active` DESC, sku.`sku` ASC
                """
            )
            rows = []
            for raw_row in cursor.fetchall() or []:
                row = _json_row(raw_row)
                row["available"] = max(
                    0,
                    int(row.get("on_hand") or 0)
                    - int(row.get("reserved") or 0)
                    - int(row.get("safety_stock") or 0),
                )
                rows.append(row)
        connection.commit()
        return {"rows": rows, "total": len(rows)}
    finally:
        connection.close()


def create_inventory_sku(data: Mapping[str, Any]) -> dict[str, Any]:
    import pymysql

    payload = _normalize_sku_payload(data or {})
    now = _now()
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            _validate_master_product(cursor, payload["product_item_id"])
            try:
                cursor.execute(
                    f"""
                    INSERT INTO `{SKU_TABLE}`
                        (`sku`, `name`, `product_item_id`, `safety_stock`, `is_active`,
                         `created_at`, `updated_at`)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        payload["sku"], payload["name"], payload["product_item_id"],
                        payload["safety_stock"], payload["is_active"], now, now,
                    ),
                )
            except pymysql.err.IntegrityError as exc:
                raise ValueError("内部 SKU 编码已存在") from exc
            sku_id = int(cursor.lastrowid)
        connection.commit()
        return {"id": sku_id, **payload, "on_hand": 0, "reserved": 0, "available": 0}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_inventory_sku(sku_id: Any, data: Mapping[str, Any]) -> dict[str, Any]:
    import pymysql

    normalized_id = _positive_int(sku_id, "SKU 编号")
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            cursor.execute(f"SELECT * FROM `{SKU_TABLE}` WHERE `id` = %s FOR UPDATE", (normalized_id,))
            current = cursor.fetchone()
            if not current:
                raise KeyError("内部 SKU 不存在")
            payload = _normalize_sku_payload({**current, **dict(data or {})})
            _validate_master_product(cursor, payload["product_item_id"])
            try:
                cursor.execute(
                    f"""
                    UPDATE `{SKU_TABLE}`
                    SET `sku` = %s, `name` = %s, `product_item_id` = %s,
                        `safety_stock` = %s, `is_active` = %s, `updated_at` = %s
                    WHERE `id` = %s
                    """,
                    (
                        payload["sku"], payload["name"], payload["product_item_id"],
                        payload["safety_stock"], payload["is_active"], _now(), normalized_id,
                    ),
                )
            except pymysql.err.IntegrityError as exc:
                raise ValueError("内部 SKU 编码已存在") from exc
            cursor.execute(
                f"SELECT COALESCE(SUM(`quantity`), 0) AS `on_hand` FROM `{STOCK_TABLE}` WHERE `sku_id` = %s",
                (normalized_id,),
            )
            on_hand = int((cursor.fetchone() or {}).get("on_hand") or 0)
            cursor.execute(
                f"SELECT COALESCE(SUM(`quantity`), 0) AS `reserved` FROM `{RESERVATION_TABLE}` "
                "WHERE `sku_id` = %s AND `reservation_status` = 'reserved'",
                (normalized_id,),
            )
            reserved = int((cursor.fetchone() or {}).get("reserved") or 0)
        connection.commit()
        return {
            "id": normalized_id, **payload, "on_hand": on_hand, "reserved": reserved,
            "available": max(0, on_hand - reserved - payload["safety_stock"]),
        }
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _normalize_sku_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    sku_id = _positive_int(data.get("sku_id"), "内部 SKU 编号")
    token_id = _positive_int(data.get("token_id"), "店铺编号")
    site_id = str(data.get("site_id") or "").strip().upper()[:16]
    item_id = str(data.get("item_id") or "").strip().upper()[:64]
    variation_id = str(data.get("variation_id") or "").strip()[:64]
    seller_sku = str(data.get("seller_sku") or "").strip()[:255] or None
    store_name = str(data.get("store_name") or "").strip()[:128]
    if not site_id:
        raise ValueError("站点不能为空")
    if not item_id:
        raise ValueError("平台商品 ID 不能为空")
    return {
        "sku_id": sku_id,
        "token_id": token_id,
        "store_name": store_name,
        "site_id": site_id,
        "item_id": item_id,
        "variation_id": variation_id,
        "seller_sku": seller_sku,
    }


def list_inventory_sku_store_sites(*, token_ids: list[int] | None = None) -> dict[str, Any]:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            if not _table_exists(cursor, STORE_LINK_TABLE):
                return {"rows": []}
            where = ["`is_current` = 1", "COALESCE(`site_id`, '') <> ''"]
            params: list[Any] = []
            if token_ids is not None:
                allowed = [int(value) for value in token_ids if int(value or 0) > 0]
                if not allowed:
                    return {"rows": []}
                where.append(f"`token_id` IN ({', '.join(['%s'] * len(allowed))})")
                params.extend(allowed)
            cursor.execute(
                f"SELECT DISTINCT `token_id`, `store_name`, `site_id` FROM `{STORE_LINK_TABLE}` "
                f"WHERE {' AND '.join(where)} ORDER BY `store_name`, `site_id`",
                tuple(params),
            )
            rows = [_json_row(row) for row in cursor.fetchall() or []]
        connection.commit()
        return {"rows": rows}
    finally:
        connection.close()


def list_inventory_sku_mappings(*, token_ids: list[int] | None = None) -> dict[str, Any]:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            where = ""
            params: list[Any] = []
            if token_ids is not None:
                allowed = [int(value) for value in token_ids if int(value or 0) > 0]
                if not allowed:
                    return {"rows": [], "total": 0}
                where = f"WHERE mapping.`token_id` IN ({', '.join(['%s'] * len(allowed))})"
                params.extend(allowed)
            cursor.execute(
                f"""
                SELECT mapping.*, sku.`sku`, sku.`name` AS `sku_name`
                FROM `{SKU_MAPPING_TABLE}` AS mapping
                INNER JOIN `{SKU_TABLE}` AS sku ON sku.`id` = mapping.`sku_id`
                {where}
                ORDER BY sku.`sku`, mapping.`store_name`, mapping.`site_id`, mapping.`item_id`, mapping.`variation_id`
                """,
                tuple(params),
            )
            rows = [_json_row(row) for row in cursor.fetchall() or []]
        connection.commit()
        return {"rows": rows, "total": len(rows)}
    finally:
        connection.close()


def _backfill_inventory_stock_sku(
    cursor: Any, mapping: Mapping[str, Any], *, previous_sku_id: int | None = None
) -> int:
    if not _table_exists(cursor, ORDER_TABLE):
        return 0
    cursor.execute(
        f"""
        SELECT stock.`id`, stock.`sku_id`, stock.`order_id`, orders.`raw_json`
        FROM `{STOCK_TABLE}` AS stock
        INNER JOIN `{ORDER_TABLE}` AS orders ON orders.`order_id` = stock.`order_id`
        WHERE stock.`product_id` = %s AND orders.`token_id` = %s AND orders.`site_id` = %s
          AND stock.`sku_id` IN (%s, %s)
        """,
        (
            mapping["item_id"], mapping["token_id"], mapping["site_id"],
            0, int(previous_sku_id or 0),
        ),
    )
    stock_rows = cursor.fetchall() or []
    from bit.bit_mysql import _mercado_order_sku_items

    changed = 0
    for stock in stock_rows:
        items = _mercado_order_sku_items(stock.get("raw_json"))
        matching = [
            item for item in items
            if str(item.get("product_id") or "").strip().upper() == mapping["item_id"]
            and (
                not mapping["variation_id"]
                or str(item.get("variation_id") or "").strip() == mapping["variation_id"]
            )
            and (
                not mapping.get("seller_sku")
                or str(item.get("seller_sku") or "").strip().casefold()
                == str(mapping["seller_sku"]).casefold()
            )
        ]
        if matching:
            cursor.execute(
                f"UPDATE `{STOCK_TABLE}` SET `sku_id` = %s WHERE `id` = %s",
                (mapping["sku_id"], stock["id"]),
            )
            changed += int(cursor.rowcount or 0)
    return changed


def _reconcile_active_orders_for_mapping(cursor: Any, mapping: Mapping[str, Any]) -> None:
    if not _table_exists(cursor, ORDER_TABLE):
        return
    cursor.execute(
        f"""
        SELECT `order_id` FROM `{ORDER_TABLE}`
        WHERE `token_id` = %s AND `site_id` = %s AND `raw_json` LIKE %s
          AND LOWER(COALESCE(`status`, '')) IN
              ('paid', 'confirmed', 'payment_in_process', 'handling', 'ready_to_ship', 'shipped', 'delivered', 'not_delivered')
        ORDER BY `date_created` DESC LIMIT 5000
        """,
        (mapping["token_id"], mapping["site_id"], f'%{mapping["item_id"]}%'),
    )
    order_ids = [str(row.get("order_id") or "") for row in cursor.fetchall() or []]
    if order_ids:
        reconcile_inventory_order_reservations(cursor, order_ids, ensure_tables=False)


def create_inventory_sku_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    import pymysql

    payload = _normalize_sku_mapping(data or {})
    now = _now()
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            cursor.execute(f"SELECT 1 FROM `{SKU_TABLE}` WHERE `id` = %s", (payload["sku_id"],))
            if not cursor.fetchone():
                raise KeyError("内部 SKU 不存在")
            try:
                cursor.execute(
                    f"""
                    INSERT INTO `{SKU_MAPPING_TABLE}`
                        (`sku_id`, `token_id`, `store_name`, `site_id`, `item_id`,
                         `variation_id`, `seller_sku`, `created_at`, `updated_at`)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        payload["sku_id"], payload["token_id"], payload["store_name"],
                        payload["site_id"], payload["item_id"], payload["variation_id"],
                        payload["seller_sku"], now, now,
                    ),
                )
            except pymysql.err.IntegrityError as exc:
                raise ValueError("该店铺商品/变体或卖家 SKU 已映射，请编辑现有映射") from exc
            mapping_id = int(cursor.lastrowid)
            _backfill_inventory_stock_sku(cursor, payload)
            _reconcile_active_orders_for_mapping(cursor, payload)
        connection.commit()
        return {"id": mapping_id, **payload}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_inventory_sku_mapping(mapping_id: Any, data: Mapping[str, Any]) -> dict[str, Any]:
    import pymysql

    normalized_id = _positive_int(mapping_id, "映射编号")
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{SKU_MAPPING_TABLE}` WHERE `id` = %s FOR UPDATE",
                (normalized_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise KeyError("SKU 映射不存在")
            payload = _normalize_sku_mapping({**current, **dict(data or {})})
            old_sku_id = int(current.get("sku_id") or 0)
            cursor.execute(f"SELECT 1 FROM `{SKU_TABLE}` WHERE `id` = %s", (payload["sku_id"],))
            if not cursor.fetchone():
                raise KeyError("内部 SKU 不存在")
            try:
                cursor.execute(
                    f"""
                    UPDATE `{SKU_MAPPING_TABLE}` SET `sku_id` = %s, `token_id` = %s,
                        `store_name` = %s, `site_id` = %s, `item_id` = %s,
                        `variation_id` = %s, `seller_sku` = %s, `updated_at` = %s
                    WHERE `id` = %s
                    """,
                    (
                        payload["sku_id"], payload["token_id"], payload["store_name"],
                        payload["site_id"], payload["item_id"], payload["variation_id"],
                        payload["seller_sku"], _now(), normalized_id,
                    ),
                )
            except pymysql.err.IntegrityError as exc:
                raise ValueError("该店铺商品/变体或卖家 SKU 已映射") from exc
            _backfill_inventory_stock_sku(cursor, payload, previous_sku_id=old_sku_id)
            _reconcile_active_orders_for_mapping(cursor, current)
            _reconcile_active_orders_for_mapping(cursor, payload)
        connection.commit()
        return {"id": normalized_id, **payload}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_inventory_sku_mapping(mapping_id: Any) -> dict[str, Any]:
    normalized_id = _positive_int(mapping_id, "映射编号")
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{SKU_MAPPING_TABLE}` WHERE `id` = %s FOR UPDATE",
                (normalized_id,),
            )
            mapping = cursor.fetchone()
            if not mapping:
                raise KeyError("SKU 映射不存在")
            cursor.execute(f"DELETE FROM `{SKU_MAPPING_TABLE}` WHERE `id` = %s", (normalized_id,))
            _reconcile_active_orders_for_mapping(cursor, mapping)
        connection.commit()
        return {"id": normalized_id, "deleted": 1}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_inventory_reconciliation(*, token_ids: list[int] | None = None) -> dict[str, Any]:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            ensure_inventory_tables(cursor)
            store_links_exist = _table_exists(cursor, STORE_LINK_TABLE)
            links_join = (
                f"LEFT JOIN `{STORE_LINK_TABLE}` AS links "
                "ON links.`token_id` = mapping.`token_id` AND links.`item_id` = mapping.`item_id` "
                "AND links.`site_id` = mapping.`site_id` AND links.`is_current` = 1"
                if store_links_exist else ""
            )
            link_fields = (
                "links.`available_quantity` AS `item_available_quantity`, "
                "links.`remote_json`, links.`last_synced_at`, "
                "links.`store_name` AS `current_store_name`"
                if store_links_exist else
                "NULL AS `item_available_quantity`, NULL AS `remote_json`, "
                "NULL AS `last_synced_at`, mapping.`store_name` AS `current_store_name`"
            )
            where = ""
            params: list[Any] = []
            if token_ids is not None:
                allowed = [int(value) for value in token_ids if int(value or 0) > 0]
                if not allowed:
                    return {"rows": [], "total": 0, "unmapped_orders": []}
                where = f"AND mapping.`token_id` IN ({', '.join(['%s'] * len(allowed))})"
                params.extend(allowed)
            cursor.execute(
                f"""
                SELECT mapping.*, sku.`sku`, sku.`name` AS `sku_name`, sku.`safety_stock`,
                       COALESCE(stock.`on_hand`, 0) AS `on_hand`,
                       COALESCE(reservation.`reserved`, 0) AS `reserved`,
                       {link_fields}
                FROM `{SKU_MAPPING_TABLE}` AS mapping
                INNER JOIN `{SKU_TABLE}` AS sku ON sku.`id` = mapping.`sku_id`
                LEFT JOIN (
                    SELECT `sku_id`, SUM(`quantity`) AS `on_hand` FROM `{STOCK_TABLE}`
                    WHERE `sku_id` > 0 GROUP BY `sku_id`
                ) AS stock ON stock.`sku_id` = sku.`id`
                LEFT JOIN (
                    SELECT `sku_id`, SUM(`quantity`) AS `reserved` FROM `{RESERVATION_TABLE}`
                    WHERE `reservation_status` = 'reserved' AND `sku_id` IS NOT NULL
                    GROUP BY `sku_id`
                ) AS reservation ON reservation.`sku_id` = sku.`id`
                {links_join}
                WHERE 1 = 1 {where}
                ORDER BY sku.`sku`, mapping.`store_name`, mapping.`site_id`, mapping.`item_id`
                """,
                tuple(params),
            )
            rows = []
            for raw_row in cursor.fetchall() or []:
                row = _json_row(raw_row)
                expected = max(
                    0,
                    int(row.get("on_hand") or 0)
                    - int(row.get("reserved") or 0)
                    - int(row.get("safety_stock") or 0),
                )
                actual = _platform_mapping_quantity(
                    row.get("remote_json"),
                    row.get("item_available_quantity"),
                    row.get("variation_id"),
                )
                row["expected_quantity"] = expected
                row["platform_quantity"] = actual
                row["difference"] = (
                    int(actual) - expected if actual is not None else None
                )
                row["has_difference"] = actual is None or int(actual) != expected
                row["store_name"] = row.get("current_store_name") or row.get("store_name")
                row.pop("remote_json", None)
                row.pop("item_available_quantity", None)
                rows.append(row)
            reservation_where = "reservation.`reservation_status` = 'unmapped'"
            reservation_params: list[Any] = []
            if token_ids is not None:
                allowed = [int(value) for value in token_ids if int(value or 0) > 0]
                if not allowed:
                    return {"rows": rows, "total": len(rows), "unmapped_orders": []}
                reservation_where += f" AND reservation.`token_id` IN ({', '.join(['%s'] * len(allowed))})"
                reservation_params.extend(allowed)
            cursor.execute(
                f"""
                SELECT reservation.`order_id`, reservation.`site_id`, reservation.`item_id`,
                       reservation.`variation_id`, reservation.`seller_sku`,
                       reservation.`product_name`, reservation.`quantity`, reservation.`order_status`,
                       reservation.`token_id`, reservation.`updated_at`
                FROM `{RESERVATION_TABLE}` AS reservation
                WHERE {reservation_where}
                ORDER BY reservation.`updated_at` DESC LIMIT 500
                """,
                tuple(reservation_params),
            )
            unmapped = [_json_row(row) for row in cursor.fetchall() or []]
            cursor.execute(
                f"SELECT COUNT(*) AS `total` FROM `{RESERVATION_TABLE}` AS reservation "
                f"WHERE {reservation_where}",
                tuple(reservation_params),
            )
            unmapped_total = int((cursor.fetchone() or {}).get("total") or 0)
            active_where = "reservation.`reservation_status` = 'reserved'"
            active_params: list[Any] = []
            if token_ids is not None:
                allowed = [int(value) for value in token_ids if int(value or 0) > 0]
                if not allowed:
                    active_where += " AND 1 = 0"
                else:
                    active_where += f" AND reservation.`token_id` IN ({', '.join(['%s'] * len(allowed))})"
                    active_params.extend(allowed)
            cursor.execute(
                f"""
                SELECT reservation.`order_id`, reservation.`site_id`, reservation.`item_id`,
                       reservation.`variation_id`, reservation.`product_name`,
                       reservation.`quantity`, reservation.`order_status`, reservation.`updated_at`,
                       reservation.`token_id`, sku.`sku`, sku.`name` AS `sku_name`
                FROM `{RESERVATION_TABLE}` AS reservation
                INNER JOIN `{SKU_TABLE}` AS sku ON sku.`id` = reservation.`sku_id`
                WHERE {active_where}
                ORDER BY reservation.`updated_at` DESC LIMIT 500
                """,
                tuple(active_params),
            )
            active_reservations = [_json_row(row) for row in cursor.fetchall() or []]
            cursor.execute(
                f"SELECT COUNT(*) AS `total` FROM `{RESERVATION_TABLE}` AS reservation "
                f"WHERE {active_where}",
                tuple(active_params),
            )
            active_reservation_total = int((cursor.fetchone() or {}).get("total") or 0)
        connection.commit()
        return {
            "rows": rows,
            "total": len(rows),
            "difference_count": sum(1 for row in rows if row["has_difference"]),
            "unmapped_orders": unmapped,
            "unmapped_total": unmapped_total,
            "active_reservations": active_reservations,
            "active_reservation_total": active_reservation_total,
        }
    finally:
        connection.close()


def _platform_mapping_quantity(remote_json: Any, item_quantity: Any, variation_id: Any) -> int | None:
    variation_id = str(variation_id or "").strip()
    raw = remote_json
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    if variation_id:
        variations = raw.get("variations") if isinstance(raw, dict) else []
        for variation in variations or []:
            if not isinstance(variation, Mapping):
                continue
            if str(variation.get("id") or "").strip() == variation_id:
                value = variation.get("available_quantity")
                if value in (None, ""):
                    value = variation.get("stock")
                try:
                    return max(0, int(value)) if value not in (None, "") else None
                except (TypeError, ValueError):
                    return None
        return None
    try:
        return max(0, int(item_quantity)) if item_quantity not in (None, "") else None
    except (TypeError, ValueError):
        return None


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
            "stocks.`product_name` LIKE %s OR stocks.`salesperson` LIKE %s OR "
            "stocks.`order_remark` LIKE %s OR shelves.`code` LIKE %s OR shelves.`name` LIKE %s OR "
            "sku.`sku` LIKE %s OR sku.`name` LIKE %s)"
        )
        params.extend([pattern] * 9)
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
                LEFT JOIN `{SKU_TABLE}` AS sku ON sku.`id` = stocks.`sku_id`
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
                       sku.`sku` AS `internal_sku`, sku.`name` AS `internal_sku_name`,
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
        "variation_id": "",
        "seller_sku": "",
    }]


def _resolve_inventory_sku(
    cursor: Any,
    *,
    token_id: Any,
    site_id: Any,
    item_id: Any,
    variation_id: Any = "",
    seller_sku: Any = "",
) -> int | None:
    normalized_item_id = str(item_id or "").strip().upper()
    normalized_site_id = str(site_id or "").strip().upper()
    normalized_variation = str(variation_id or "").strip()
    normalized_seller_sku = str(seller_sku or "").strip()
    if not normalized_item_id or not normalized_site_id:
        return None
    if normalized_variation:
        cursor.execute(
            f"SELECT `sku_id` FROM `{SKU_MAPPING_TABLE}` "
            "WHERE `token_id` = %s AND `site_id` = %s AND `item_id` = %s "
            "AND `variation_id` = %s LIMIT 1",
            (int(token_id), normalized_site_id, normalized_item_id, normalized_variation),
        )
        row = cursor.fetchone()
        if row:
            return int(row.get("sku_id") or 0) or None
    cursor.execute(
        f"SELECT `sku_id` FROM `{SKU_MAPPING_TABLE}` "
        "WHERE `token_id` = %s AND `site_id` = %s AND `item_id` = %s "
        "AND `variation_id` = '' LIMIT 1",
        (int(token_id), normalized_site_id, normalized_item_id),
    )
    row = cursor.fetchone()
    if row:
        return int(row.get("sku_id") or 0) or None
    if normalized_seller_sku:
        cursor.execute(
            f"SELECT `sku_id` FROM `{SKU_MAPPING_TABLE}` "
            "WHERE `token_id` = %s AND `site_id` = %s AND `seller_sku` = %s LIMIT 1",
            (int(token_id), normalized_site_id, normalized_seller_sku),
        )
        row = cursor.fetchone()
        if row:
            return int(row.get("sku_id") or 0) or None
    return None


INVENTORY_RESERVABLE_ORDER_STATUSES = {
    "paid", "confirmed", "payment_in_process", "handling", "ready_to_ship",
    "shipped", "delivered", "not_delivered",
}
INVENTORY_RELEASE_ORDER_STATUSES = {
    "cancelled", "invalid", "partially_refunded", "refunded",
}


def reconcile_inventory_order_reservations(
    cursor: Any, order_ids: list[str], *, ensure_tables: bool = True
) -> int:
    """Refresh reservations for synchronized orders, releasing cancelled lines."""
    normalized_ids = list(dict.fromkeys(
        str(value or "").strip() for value in order_ids or [] if str(value or "").strip()
    ))
    if not normalized_ids:
        return 0
    if ensure_tables:
        ensure_inventory_tables(cursor)
    placeholders = ", ".join(["%s"] * len(normalized_ids))
    cursor.execute(
        f"""
        SELECT `order_id`, `token_id`, `site_id`, `status`, `raw_json`
        FROM `{ORDER_TABLE}` WHERE `order_id` IN ({placeholders})
        """,
        tuple(normalized_ids),
    )
    orders = cursor.fetchall() or []
    now = _now()
    changed = 0
    from bit.bit_mysql import _mercado_order_sku_items

    for order in orders:
        order_id = str(order.get("order_id") or "")
        status = str(order.get("status") or "").strip().lower()
        cursor.execute(
            f"""
            UPDATE `{RESERVATION_TABLE}`
            SET `reservation_status` = 'released', `released_at` = %s, `updated_at` = %s,
                `order_status` = %s
            WHERE `order_id` = %s AND `reservation_status` IN ('reserved', 'unmapped')
            """,
            (now, now, status, order_id),
        )
        if status not in INVENTORY_RESERVABLE_ORDER_STATUSES:
            continue
        raw_items = _mercado_order_sku_items(order.get("raw_json"))
        lines: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item in raw_items:
            item_id = str(item.get("product_id") or "").strip().upper()
            if not item_id:
                continue
            variation_id = str(item.get("variation_id") or "").strip()
            seller_sku = str(item.get("seller_sku") or "").strip()
            key = (item_id, variation_id, seller_sku.casefold())
            line = lines.setdefault(key, {
                "item_id": item_id,
                "variation_id": variation_id,
                "seller_sku": seller_sku,
                "product_name": str(item.get("title") or item_id)[:255],
                "quantity": 0,
            })
            try:
                line["quantity"] += max(0, int(item.get("quantity") or 0))
            except (TypeError, ValueError):
                continue
        for line in lines.values():
            quantity = int(line["quantity"])
            if quantity <= 0:
                continue
            sku_id = _resolve_inventory_sku(
                cursor,
                token_id=order.get("token_id"),
                site_id=order.get("site_id"),
                item_id=line["item_id"],
                variation_id=line["variation_id"],
                seller_sku=line["seller_sku"],
            )
            line_key = __import__("hashlib").sha256(
                "|".join((line["item_id"], line["variation_id"], line["seller_sku"])).encode("utf-8")
            ).hexdigest()
            reservation_status = "reserved" if sku_id else "unmapped"
            cursor.execute(
                f"""
                INSERT INTO `{RESERVATION_TABLE}` (
                    `order_id`, `line_key`, `token_id`, `site_id`, `item_id`, `variation_id`,
                    `seller_sku`, `product_name`, `sku_id`, `quantity`, `order_status`,
                    `reservation_status`, `created_at`, `updated_at`, `released_at`
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                ON DUPLICATE KEY UPDATE
                    `token_id` = VALUES(`token_id`), `site_id` = VALUES(`site_id`),
                    `item_id` = VALUES(`item_id`), `variation_id` = VALUES(`variation_id`),
                    `seller_sku` = VALUES(`seller_sku`), `product_name` = VALUES(`product_name`),
                    `sku_id` = VALUES(`sku_id`), `quantity` = VALUES(`quantity`),
                    `order_status` = VALUES(`order_status`),
                    `reservation_status` = VALUES(`reservation_status`),
                    `updated_at` = VALUES(`updated_at`), `released_at` = NULL
                """,
                (
                    order_id, line_key, int(order.get("token_id") or 0),
                    str(order.get("site_id") or "").upper()[:16], line["item_id"],
                    line["variation_id"], line["seller_sku"] or None, line["product_name"],
                    sku_id, quantity, status, reservation_status, now, now,
                ),
            )
            changed += 1
    return changed


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
                           `shop_name`, `purchase_cost`, `purchase_order`,
                           `purchase_remark`, `status_detail`, `token_id`, `site_id`,
                           DATE_ADD(`date_created`, INTERVAL 8 HOUR) AS `ordered_at`
                    FROM `{ORDER_TABLE}`
                    {where}
                    ORDER BY `date_created` DESC
                    LIMIT %s
                    """,
                    tuple(params + [limit]),
                )
                orders = cursor.fetchall() or []
                settings_exists = _table_exists(cursor, STORE_SITE_SETTINGS_TABLE)
                for order in orders:
                    salesperson = ""
                    if settings_exists:
                        cursor.execute(
                            f"SELECT `salesperson` FROM `{STORE_SITE_SETTINGS_TABLE}` "
                            "WHERE `token_id` = %s AND `site_id` = %s LIMIT 1",
                            (order.get("token_id"), order.get("site_id")),
                        )
                        salesperson = str(
                            (cursor.fetchone() or {}).get("salesperson") or ""
                        )
                    order_remark = _combined_order_remark(order)
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
                        mapped_sku_id = _resolve_inventory_sku(
                            cursor,
                            token_id=order.get("token_id"),
                            site_id=order.get("site_id"),
                            item_id=item.get("product_id"),
                            variation_id=item.get("variation_id"),
                            seller_sku=item.get("seller_sku"),
                        )
                        order_rows.append({
                            "order_id": str(order.get("order_id") or ""),
                            "product_id": str(item.get("product_id") or ""),
                            "variation_id": str(item.get("variation_id") or ""),
                            "product_name": str(item.get("title") or order.get("title") or ""),
                            "image_url": str(item.get("image_url") or order.get("image_url") or ""),
                            "order_quantity": int(item.get("quantity") or 0),
                            "variation": str(item.get("variation") or ""),
                            "seller_sku": str(item.get("seller_sku") or ""),
                            "sku_id": mapped_sku_id,
                            "token_id": int(order.get("token_id") or 0),
                            "site_id": str(order.get("site_id") or ""),
                            "shop_name": str(order.get("shop_name") or ""),
                            "salesperson": salesperson,
                            "order_remark": order_remark,
                            "reference_no": str(order.get("purchase_order") or ""),
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


def _combined_order_remark(order: Mapping[str, Any]) -> str:
    parts = []
    for label, key in (("采购备注", "purchase_remark"), ("订单备注", "status_detail")):
        value = str(order.get(key) or "").strip()
        if value:
            parts.append(f"{label}：{value}")
    return "；".join(parts)[:1000]


def _matched_order_product(
    cursor: Any,
    order_id: str,
    product_id: str,
    variation_id: str = "",
    seller_sku: str = "",
) -> dict[str, Any]:
    if not _table_exists(cursor, ORDER_TABLE):
        raise ValueError("订单同步表不存在，请先拉取订单")
    cursor.execute(
        f"""
        SELECT `order_id`, `product_id`, `title`, `image_url`, `raw_json`,
               `purchase_cost`, `purchase_order`, `purchase_remark`, `status_detail`,
               `token_id`, `site_id`
        FROM `{ORDER_TABLE}` WHERE `order_id` = %s LIMIT 1
        """,
        (order_id,),
    )
    order = cursor.fetchone()
    if not order:
        raise ValueError("匹配的订单不存在，请重新搜索选择")
    salesperson = ""
    if _table_exists(cursor, STORE_SITE_SETTINGS_TABLE):
        cursor.execute(
            f"SELECT `salesperson` FROM `{STORE_SITE_SETTINGS_TABLE}` "
            "WHERE `token_id` = %s AND `site_id` = %s LIMIT 1",
            (order.get("token_id"), order.get("site_id")),
        )
        salesperson = str((cursor.fetchone() or {}).get("salesperson") or "")[:128]
    items = _order_items(order.get("raw_json"), order)
    matches = [
        item for item in items
        if str(item.get("product_id") or "").strip() == product_id
        and (not variation_id or str(item.get("variation_id") or "").strip() == variation_id)
        and (not seller_sku or str(item.get("seller_sku") or "").strip().casefold() == seller_sku.casefold())
    ]
    if len(matches) > 1 and not variation_id and not seller_sku:
        raise ValueError("订单中同一商品包含多个变体，请重新选择具体变体")
    for item in matches:
        sku_id = _resolve_inventory_sku(
            cursor,
            token_id=order.get("token_id"),
            site_id=order.get("site_id"),
            item_id=item.get("product_id"),
            variation_id=item.get("variation_id"),
            seller_sku=item.get("seller_sku"),
        )
        return {
            "product_name": str(item.get("title") or order.get("title") or product_id)[:255],
            "image_url": str(item.get("image_url") or order.get("image_url") or "")[:1500],
            "variation_id": str(item.get("variation_id") or ""),
            "seller_sku": str(item.get("seller_sku") or ""),
            "sku_id": sku_id,
            "suggested_unit_cost": _suggested_unit_cost(
                order.get("purchase_cost"), items, product_id
            ),
            "order_remark": _combined_order_remark(order),
            "salesperson": salesperson,
            "reference_no": str(order.get("purchase_order") or "")[:128],
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
                variation_id = str(payload.get("variation_id") or "").strip()[:64]
                seller_sku = str(payload.get("seller_sku") or "").strip()[:255]
                if not order_id:
                    raise ValueError("入库必须匹配订单")
                if not product_id:
                    raise ValueError("入库必须匹配产品")
                context = _matched_order_product(
                    cursor, order_id, product_id, variation_id, seller_sku
                )
                selected_sku_id = payload.get("sku_id")
                sku_id = (
                    _positive_int(selected_sku_id, "内部 SKU 编号")
                    if selected_sku_id not in (None, "", 0, "0")
                    else int(context.get("sku_id") or 0)
                )
                if sku_id:
                    cursor.execute(
                        f"SELECT 1 FROM `{SKU_TABLE}` WHERE `id` = %s AND `is_active` = 1",
                        (sku_id,),
                    )
                    if not cursor.fetchone():
                        raise ValueError("所选内部 SKU 不存在或已停用")
                order_remark = str(context.get("order_remark") or "")[:1000]
                salesperson = str(context.get("salesperson") or "")[:128]
                if not reference_no:
                    reference_no = str(context.get("reference_no") or "")[:128]
                if order_remark and not remark:
                    remark = order_remark
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
                      AND `sku_id` = %s
                    FOR UPDATE
                    """,
                    (shelf_id, order_id, product_id, sku_id),
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
                        SET `product_name` = %s, `image_url` = %s,
                            `order_remark` = %s, `salesperson` = %s, `quantity` = %s,
                            `sku_id` = %s, `unit_cost` = %s, `last_inbound_at` = %s, `updated_at` = %s
                        WHERE `id` = %s
                        """,
                        (
                            context["product_name"], context["image_url"],
                            order_remark, salesperson,
                            effect["after_quantity"], sku_id, effect["unit_cost"],
                            occurred_at, now, stock_id,
                        ),
                    )
                else:
                    cursor.execute(
                        f"""
                        INSERT INTO `{STOCK_TABLE}`
                            (`shelf_id`, `order_id`, `product_id`, `product_name`, `image_url`,
                             `order_remark`, `salesperson`, `sku_id`,
                             `quantity`, `unit_cost`, `first_inbound_at`, `last_inbound_at`,
                             `created_at`, `updated_at`)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            shelf_id, order_id, product_id, context["product_name"],
                            context["image_url"], order_remark, salesperson,
                            sku_id, effect["after_quantity"], effect["unit_cost"],
                            occurred_at, occurred_at, now, now,
                        ),
                    )
                    stock_id = int(cursor.lastrowid)
                stock_snapshot = {
                    "id": stock_id,
                    "shelf_id": shelf_id,
                    "order_id": order_id,
                    "product_id": product_id,
                    "sku_id": sku_id,
                    "product_name": context["product_name"],
                    "image_url": context["image_url"],
                    "order_remark": order_remark,
                    "salesperson": salesperson,
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
                sku_id = int(stock_snapshot.get("sku_id") or 0)

            total_cost = (
                effect["movement_unit_cost"] * quantity
            ).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
            cursor.execute(
                f"""
                INSERT INTO `{MOVEMENT_TABLE}`
                    (`stock_id`, `shelf_id`, `movement_type`, `order_id`, `product_id`,
                     `product_name`, `image_url`, `order_remark`, `salesperson`, `sku_id`,
                     `quantity`, `unit_cost`, `total_cost`,
                     `before_quantity`, `after_quantity`, `reference_no`, `remark`,
                     `operator_id`, `operator_name`, `occurred_at`, `created_at`)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    stock_id, shelf_id, movement_type, order_id, product_id,
                    stock_snapshot.get("product_name"), stock_snapshot.get("image_url"),
                    stock_snapshot.get("order_remark"), stock_snapshot.get("salesperson"),
                    int(stock_snapshot.get("sku_id") or 0),
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
            "movements.`operator_name` LIKE %s OR movements.`salesperson` LIKE %s OR "
            "movements.`order_remark` LIKE %s OR shelves.`code` LIKE %s)"
        )
        params.extend([pattern] * 8)
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
