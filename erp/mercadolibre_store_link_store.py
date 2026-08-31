"""MySQL persistence for listings synchronized from authorized stores."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping


STORE_LINK_TABLE = "erp_mercadolibre_store_links"
STORE_LINK_SYNC_STATE_TABLE = "erp_mercadolibre_store_link_sync_state"
STORE_LINK_SALES_PAGE_INDEX = "idx_erp_meli_store_link_sales_page"
STORE_LINK_SITE_PAGE_INDEX = "idx_erp_meli_store_link_site_page"

_schema_lock = threading.RLock()
_store_link_schema_ready = False
_sync_state_schema_ready = False


def _connect() -> Any:
    import pymysql
    from bit.bit_mysql import config

    return pymysql.connect(**config)


def _now() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_safe_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, value in tuple(result.items()):
        if isinstance(value, datetime):
            result[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(value, Decimal):
            result[key] = float(value)
        elif isinstance(value, bytes):
            result[key] = value.decode("utf-8", errors="replace")
    result["is_current"] = bool(result.get("is_current"))
    return result


def _ensure_column(cursor: Any, column: str, definition: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM `{STORE_LINK_TABLE}` LIKE %s", (column,))
    if cursor.fetchone():
        return False
    cursor.execute(
        f"ALTER TABLE `{STORE_LINK_TABLE}` ADD COLUMN `{column}` {definition}"
    )
    return True


def _migrate_store_link_table(cursor: Any) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{STORE_LINK_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `token_id` BIGINT NOT NULL,
            `store_name` VARCHAR(128) NOT NULL,
            `seller_id` VARCHAR(64) NOT NULL,
            `site_id` VARCHAR(16) NULL,
            `item_id` VARCHAR(64) NOT NULL,
            `title` VARCHAR(512) NULL,
            `permalink` VARCHAR(1500) NULL,
            `thumbnail_url` VARCHAR(1500) NULL,
            `status` VARCHAR(64) NULL,
            `price` DECIMAL(20,4) NULL,
            `currency_id` VARCHAR(16) NULL,
            `available_quantity` INT NULL,
            `sold_quantity` INT NULL,
            `seller_sku` VARCHAR(255) NULL,
            `category_id` VARCHAR(64) NULL,
            `listing_type_id` VARCHAR(64) NULL,
            `weight_g` DECIMAL(20,4) NULL,
            `volumetric_weight_kg` DECIMAL(20,4) NULL,
            `package_length_cm` DECIMAL(20,4) NULL,
            `package_width_cm` DECIMAL(20,4) NULL,
            `package_height_cm` DECIMAL(20,4) NULL,
            `net_proceeds_usd` DECIMAL(20,4) NULL,
            `price_manual` TINYINT(1) NOT NULL DEFAULT 0,
            `weight_manual` TINYINT(1) NOT NULL DEFAULT 0,
            `dimensions_manual` TINYINT(1) NOT NULL DEFAULT 0,
            `net_proceeds_manual` TINYINT(1) NOT NULL DEFAULT 0,
            `sync_marker` VARCHAR(64) NULL,
            `remote_json` LONGTEXT NULL,
            `is_current` TINYINT(1) NOT NULL DEFAULT 1,
            `last_synced_at` DATETIME NOT NULL,
            `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_store_link` (`token_id`, `item_id`),
            KEY `idx_erp_meli_store_link_current` (`is_current`, `status`, `last_synced_at`),
            KEY `idx_erp_meli_store_link_store` (`token_id`, `is_current`, `item_id`),
            KEY `idx_erp_meli_store_link_sales_page`
                (`is_current`, `sold_quantity`, `last_synced_at`, `id`),
            KEY `idx_erp_meli_store_link_site_page` (`is_current`, `site_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    _ensure_column(cursor, "sync_marker", "VARCHAR(64) NULL AFTER `dimensions_manual`")
    net_manual_added = _ensure_column(
        cursor,
        "net_proceeds_manual",
        "TINYINT(1) NOT NULL DEFAULT 0 AFTER `dimensions_manual`",
    )
    if net_manual_added:
        cursor.execute(
            f"UPDATE `{STORE_LINK_TABLE}` SET `net_proceeds_manual` = 1 "
            "WHERE `net_proceeds_usd` IS NOT NULL"
        )
        cursor.execute(
            f"""
            UPDATE `{STORE_LINK_TABLE}`
            SET `net_proceeds_usd` = CAST(
                JSON_UNQUOTE(JSON_EXTRACT(`remote_json`, '$.net_proceeds.amount'))
                AS DECIMAL(20,4)
            )
            WHERE `net_proceeds_manual` = 0
              AND JSON_UNQUOTE(
                    JSON_EXTRACT(`remote_json`, '$.net_proceeds.currency_id')
                  ) = 'USD'
              AND JSON_EXTRACT(`remote_json`, '$.net_proceeds.amount') IS NOT NULL
            """
        )


def ensure_store_link_table(cursor: Any) -> None:
    """Avoid repeated DDL against the large synchronized-listing table."""

    global _store_link_schema_ready
    if _store_link_schema_ready:
        return
    with _schema_lock:
        if _store_link_schema_ready:
            return
        cursor.execute(
            """
            SELECT `COLUMN_NAME`
            FROM `information_schema`.`COLUMNS`
            WHERE `TABLE_SCHEMA` = DATABASE() AND `TABLE_NAME` = %s
            """,
            (STORE_LINK_TABLE,),
        )
        schema_rows = cursor.fetchall() if hasattr(cursor, "fetchall") else []
        columns = {
            str(row.get("COLUMN_NAME") or row.get("Column_name") or "")
            for row in schema_rows or []
            if isinstance(row, Mapping)
        }
        if not {"sync_marker", "net_proceeds_manual", "remote_json"}.issubset(columns):
            _migrate_store_link_table(cursor)
        required_indexes = {
            STORE_LINK_SALES_PAGE_INDEX:
                "(`is_current`, `sold_quantity`, `last_synced_at`, `id`)",
            STORE_LINK_SITE_PAGE_INDEX: "(`is_current`, `site_id`)",
        }
        for index_name, columns_sql in required_indexes.items():
            cursor.execute(
                """
                SELECT 1
                FROM `information_schema`.`STATISTICS`
                WHERE `TABLE_SCHEMA` = DATABASE() AND `TABLE_NAME` = %s
                  AND `INDEX_NAME` = %s
                LIMIT 1
                """,
                (STORE_LINK_TABLE, index_name),
            )
            if not cursor.fetchone():
                try:
                    cursor.execute(
                        f"ALTER TABLE `{STORE_LINK_TABLE}` ADD INDEX "
                        f"`{index_name}` {columns_sql}"
                    )
                except Exception as exc:
                    # Another hot-reload process may finish the same online index first.
                    if not exc.args or exc.args[0] != 1061:
                        raise
        _store_link_schema_ready = True


def _migrate_store_link_sync_state_table(cursor: Any) -> None:
    """Keep automatic sync requests durable across service restarts."""

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{STORE_LINK_SYNC_STATE_TABLE}` (
            `token_id` BIGINT NOT NULL,
            `requested_at` DATETIME NULL,
            `last_started_at` DATETIME NULL,
            `last_completed_at` DATETIME NULL,
            `last_status` VARCHAR(32) NOT NULL DEFAULT 'pending',
            `last_error` TEXT NULL,
            `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`token_id`),
            KEY `idx_erp_meli_store_link_sync_due` (`requested_at`, `last_completed_at`),
            KEY `idx_erp_meli_store_link_sync_retry` (`last_started_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    # Do not backfill this state by scanning the million-row listing table.
    # The scheduler's LEFT JOIN already treats a missing state row as due, and
    # creates the compact state row when that store is actually synchronized.


def ensure_store_link_sync_state_table(cursor: Any) -> None:
    """Create/seed scheduler state once per process, then use a fast path."""

    global _sync_state_schema_ready
    if _sync_state_schema_ready:
        return
    with _schema_lock:
        if _sync_state_schema_ready:
            return
        cursor.execute(
            """
            SELECT `COLUMN_NAME`
            FROM `information_schema`.`COLUMNS`
            WHERE `TABLE_SCHEMA` = DATABASE() AND `TABLE_NAME` = %s
            """,
            (STORE_LINK_SYNC_STATE_TABLE,),
        )
        schema_rows = cursor.fetchall() if hasattr(cursor, "fetchall") else []
        columns = {
            str(row.get("COLUMN_NAME") or row.get("Column_name") or "")
            for row in schema_rows or []
            if isinstance(row, Mapping)
        }
        if not {"token_id", "last_completed_at", "last_error"}.issubset(columns):
            _migrate_store_link_sync_state_table(cursor)
        _sync_state_schema_ready = True


def request_store_link_sync(
    token_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    """Persist an immediate sync request for one or more authorized stores."""

    ids: list[int] = []
    for value in token_ids or ():
        try:
            token_id = int(value)
        except (TypeError, ValueError):
            continue
        if token_id > 0 and token_id not in ids:
            ids.append(token_id)
    if not ids:
        return 0
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_store_link_table(cursor)
            ensure_store_link_sync_state_table(cursor)
            cursor.executemany(
                f"""
                INSERT INTO `{STORE_LINK_SYNC_STATE_TABLE}` (
                    `token_id`, `requested_at`, `last_status`, `last_error`
                ) VALUES (%s, %s, 'queued', NULL)
                ON DUPLICATE KEY UPDATE
                    `requested_at` = VALUES(`requested_at`),
                    `last_status` = 'queued',
                    `last_error` = NULL
                """,
                [(token_id, _now()) for token_id in ids],
            )
        connection.commit()
        return len(ids)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def order_store_link_token_ids_for_full_sync(
    token_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> list[int]:
    """Put never-completed stores first, then the least recently synced stores."""

    ids: list[int] = []
    for value in token_ids or ():
        try:
            token_id = int(value)
        except (TypeError, ValueError):
            continue
        if token_id > 0 and token_id not in ids:
            ids.append(token_id)
    if len(ids) < 2:
        return ids

    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_store_link_table(cursor)
            ensure_store_link_sync_state_table(cursor)
            placeholders = ", ".join(["%s"] * len(ids))
            cursor.execute(
                f"""
                SELECT `token_id`, `last_completed_at`
                FROM `{STORE_LINK_SYNC_STATE_TABLE}`
                WHERE `token_id` IN ({placeholders})
                """,
                tuple(ids),
            )
            rows = cursor.fetchall()
        connection.commit()
    finally:
        connection.close()

    completed_at_by_id = {
        int(row["token_id"]): row.get("last_completed_at")
        for row in rows
        if row.get("token_id") is not None
    }
    original_positions = {token_id: index for index, token_id in enumerate(ids)}

    def priority(token_id: int) -> tuple[bool, str, int]:
        completed_at = completed_at_by_id.get(token_id)
        return (
            completed_at is not None,
            str(completed_at or ""),
            original_positions[token_id],
        )

    return sorted(ids, key=priority)


def mark_store_link_sync_started(
    token_id: int,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> None:
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_store_link_table(cursor)
            ensure_store_link_sync_state_table(cursor)
            cursor.execute(
                f"""
                INSERT INTO `{STORE_LINK_SYNC_STATE_TABLE}` (
                    `token_id`, `last_started_at`, `last_status`, `last_error`
                ) VALUES (%s, %s, 'running', NULL)
                ON DUPLICATE KEY UPDATE
                    `last_started_at` = VALUES(`last_started_at`),
                    `last_status` = 'running',
                    `last_error` = NULL
                """,
                (int(token_id), _now()),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_store_link_sync_finished(
    token_id: int,
    status: str,
    error: str = "",
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> None:
    """Save a result; only completed scans advance the three-day clock."""

    status = str(status or "error").strip().lower()[:32]
    successful = status in {"success", "partial", "completed"}
    finished_at = _now()
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_store_link_table(cursor)
            ensure_store_link_sync_state_table(cursor)
            if successful:
                cursor.execute(
                    f"""
                    INSERT INTO `{STORE_LINK_SYNC_STATE_TABLE}` (
                        `token_id`, `last_started_at`, `last_completed_at`,
                        `last_status`, `last_error`
                    ) VALUES (%s, %s, %s, %s, NULL)
                    ON DUPLICATE KEY UPDATE
                        `last_completed_at` = VALUES(`last_completed_at`),
                        `last_status` = VALUES(`last_status`),
                        `last_error` = NULL,
                        `requested_at` = CASE
                            WHEN `requested_at` IS NULL
                              OR `last_started_at` IS NULL
                              OR `requested_at` <= `last_started_at`
                            THEN NULL ELSE `requested_at` END
                    """,
                    (int(token_id), finished_at, finished_at, status),
                )
            else:
                cursor.execute(
                    f"""
                    INSERT INTO `{STORE_LINK_SYNC_STATE_TABLE}` (
                        `token_id`, `last_started_at`, `last_status`, `last_error`
                    ) VALUES (%s, %s, 'error', %s)
                    ON DUPLICATE KEY UPDATE
                        `last_status` = 'error', `last_error` = VALUES(`last_error`)
                    """,
                    (int(token_id), finished_at, str(error or "")[:4000]),
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_due_store_link_token_ids(
    *,
    interval_days: int = 3,
    retry_minutes: int = 60,
    limit: int = 1000,
    connection_factory: Callable[[], Any] | None = None,
) -> list[int]:
    """Return authorized stores with a queued request or an expired sync clock."""

    due_before = (
        datetime.now().replace(microsecond=0) - timedelta(days=max(1, int(interval_days)))
    ).strftime("%Y-%m-%d %H:%M:%S")
    retry_before = (
        datetime.now().replace(microsecond=0) - timedelta(minutes=max(1, int(retry_minutes)))
    ).strftime("%Y-%m-%d %H:%M:%S")
    limit = max(1, min(int(limit or 1000), 1000))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_store_link_table(cursor)
            ensure_store_link_sync_state_table(cursor)
            cursor.execute(
                f"""
                SELECT tokens.`id`
                FROM `mercado_store_tokens` AS tokens
                LEFT JOIN `{STORE_LINK_SYNC_STATE_TABLE}` AS sync_state
                  ON sync_state.`token_id` = tokens.`id`
                WHERE (
                    sync_state.`requested_at` IS NOT NULL
                    OR sync_state.`last_completed_at` IS NULL
                    OR sync_state.`last_completed_at` <= %s
                )
                  AND (
                    sync_state.`last_started_at` IS NULL
                    OR sync_state.`last_started_at` <= %s
                  )
                ORDER BY
                    CASE WHEN sync_state.`requested_at` IS NOT NULL THEN 0 ELSE 1 END,
                    COALESCE(sync_state.`requested_at`, sync_state.`last_completed_at`) ASC,
                    tokens.`id` ASC
                LIMIT %s
                """,
                (due_before, retry_before, limit),
            )
            rows = cursor.fetchall()
        connection.commit()
        return [int(row["id"]) for row in rows]
    finally:
        connection.close()


def _value_struct_number(value: Any, *, weight: bool = False) -> Decimal | None:
    if isinstance(value, Mapping):
        number = value.get("number")
        unit = str(value.get("unit") or "").strip().lower()
    else:
        text = str(value or "").replace(",", "").strip().lower()
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return None
        number = match.group(0)
        unit = text
    try:
        result = Decimal(str(number))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if weight:
        if "kg" in unit or "kilogram" in unit:
            result *= 1000
        elif "mg" in unit:
            result /= 1000
        elif "lb" in unit:
            result *= Decimal("453.59237")
    elif "mm" in unit:
        result /= 10
    elif re.search(r"(^|\W)m($|\W)", unit) and "cm" not in unit:
        result *= 100
    return result


def _attribute_number(item: Mapping[str, Any], ids: set[str], *, weight: bool = False) -> Decimal | None:
    for attribute in item.get("attributes") or []:
        if not isinstance(attribute, Mapping) or str(attribute.get("id") or "").upper() not in ids:
            continue
        value = _value_struct_number(attribute.get("value_struct"), weight=weight)
        if value is None:
            value = _value_struct_number(attribute.get("value_name"), weight=weight)
        if value is not None:
            return value
    return None


def _shipping_dimensions(item: Mapping[str, Any]) -> dict[str, Decimal | None]:
    text = str((item.get("shipping") or {}).get("dimensions") or "").strip()
    match = re.search(
        r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return {"height": None, "width": None, "length": None, "weight": None}
    height, width, length, weight = (Decimal(value) for value in match.groups())
    return {"height": height, "width": width, "length": length, "weight": weight}


def _seller_sku(item: Mapping[str, Any]) -> str:
    direct = item.get("seller_sku") or item.get("seller_custom_field")
    if direct:
        return str(direct)
    for attribute in item.get("attributes") or []:
        if isinstance(attribute, Mapping) and str(attribute.get("id") or "").upper() == "SELLER_SKU":
            return str(attribute.get("value_name") or "")
    return ""


def _thumbnail(item: Mapping[str, Any]) -> str:
    pictures = item.get("pictures") or []
    if pictures and isinstance(pictures[0], Mapping):
        return str(pictures[0].get("secure_url") or pictures[0].get("url") or "")
    return str(item.get("secure_thumbnail") or item.get("thumbnail") or "")


def _net_proceeds_usd(item: Mapping[str, Any]) -> Any:
    values = item.get("net_proceeds")
    candidates = values if isinstance(values, list) else [values]
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        if str(candidate.get("currency_id") or "").strip().upper() != "USD":
            continue
        amount = candidate.get("amount")
        if amount in (None, ""):
            continue
        try:
            return Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError):
            continue
    return None


def listing_record(token: Mapping[str, Any], item: Mapping[str, Any], synced_at: str) -> dict[str, Any]:
    """Normalize one official API listing while keeping package values editable."""

    shipping = _shipping_dimensions(item)
    weight = _attribute_number(item, {"PACKAGE_WEIGHT", "WEIGHT", "NET_WEIGHT"}, weight=True)
    length = _attribute_number(item, {"PACKAGE_LENGTH", "LENGTH"})
    width = _attribute_number(item, {"PACKAGE_WIDTH", "WIDTH"})
    height = _attribute_number(item, {"PACKAGE_HEIGHT", "HEIGHT"})
    weight = weight if weight is not None else shipping["weight"]
    length = length if length is not None else shipping["length"]
    width = width if width is not None else shipping["width"]
    height = height if height is not None else shipping["height"]
    volumetric = None
    if all(value is not None for value in (length, width, height)):
        volumetric = (length * width * height / Decimal("6000")).quantize(Decimal("0.0001"))
    return {
        "token_id": int(token["id"]),
        "store_name": str(token.get("display_name") or token.get("nickname") or token["id"])[:128],
        "seller_id": str(item.get("seller_id") or token.get("meli_user_id") or "")[:64],
        "site_id": str(item.get("site_id") or token.get("site_id") or "")[:16],
        "item_id": str(item.get("id") or "")[:64],
        "title": str(item.get("title") or "")[:512],
        "permalink": str(item.get("permalink") or "")[:1500],
        "thumbnail_url": _thumbnail(item)[:1500],
        "status": str(item.get("status") or "")[:64],
        "price": item.get("price"),
        "currency_id": str(item.get("currency_id") or "")[:16],
        "available_quantity": item.get("available_quantity"),
        "sold_quantity": item.get("sold_quantity"),
        "seller_sku": _seller_sku(item)[:255],
        "category_id": str(item.get("category_id") or "")[:64],
        "listing_type_id": str(item.get("listing_type_id") or "")[:64],
        "weight_g": weight,
        "volumetric_weight_kg": volumetric,
        "package_length_cm": length,
        "package_width_cm": width,
        "package_height_cm": height,
        "net_proceeds_usd": _net_proceeds_usd(item),
        "remote_json": _dumps(item),
        "is_current": 1,
        "last_synced_at": synced_at,
    }


def replace_store_snapshot(
    token: Mapping[str, Any],
    items: Iterable[Mapping[str, Any]],
    *,
    current_item_ids: Iterable[str] | None = None,
    sync_marker: str = "",
    finalize: bool = True,
    synced_at: str | None = None,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, int]:
    synced_at = synced_at or _now()
    rows = [listing_record(token, item, synced_at) for item in items]
    rows = [row for row in rows if row["item_id"]]
    discovered_ids = []
    current_values = current_item_ids if current_item_ids is not None else (row["item_id"] for row in rows)
    for value in current_values:
        item_id = str(value or "").strip()[:64]
        if item_id and item_id not in discovered_ids:
            discovered_ids.append(item_id)
    token_id = int(token["id"])
    marker = str(sync_marker or "").strip()[:64]
    for row in rows:
        row["sync_marker"] = marker
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_store_link_table(cursor)
            item_ids = [row["item_id"] for row in rows]
            existing: set[str] = set()
            if item_ids:
                placeholders = ", ".join(["%s"] * len(item_ids))
                cursor.execute(
                    f"SELECT `item_id` FROM `{STORE_LINK_TABLE}` "
                    f"WHERE `token_id` = %s AND `item_id` IN ({placeholders})",
                    tuple([token_id] + item_ids),
                )
                existing = {str(row["item_id"]) for row in cursor.fetchall()}
            if finalize:
                cursor.execute(
                    f"UPDATE `{STORE_LINK_TABLE}` SET `is_current` = 0 WHERE `token_id` = %s",
                    (token_id,),
                )
            for start in range(0, len(discovered_ids), 500):
                batch = discovered_ids[start : start + 500]
                placeholders = ", ".join(["%s"] * len(batch))
                cursor.execute(
                    f"UPDATE `{STORE_LINK_TABLE}` SET `is_current` = 1, `sync_marker` = %s "
                    f"WHERE `token_id` = %s AND `item_id` IN ({placeholders})",
                    tuple([marker, token_id] + batch),
                )
            sql = f"""
                INSERT INTO `{STORE_LINK_TABLE}` (
                    `token_id`, `store_name`, `seller_id`, `site_id`, `item_id`, `title`,
                    `permalink`, `thumbnail_url`, `status`, `price`, `currency_id`,
                    `available_quantity`, `sold_quantity`, `seller_sku`, `category_id`,
                    `listing_type_id`, `weight_g`, `volumetric_weight_kg`,
                    `package_length_cm`, `package_width_cm`, `package_height_cm`,
                    `net_proceeds_usd`, `sync_marker`, `remote_json`, `is_current`,
                    `last_synced_at`
                ) VALUES ({", ".join(["%s"] * 26)})
                ON DUPLICATE KEY UPDATE
                    `store_name` = VALUES(`store_name`), `seller_id` = VALUES(`seller_id`),
                    `site_id` = VALUES(`site_id`),
                    `title` = COALESCE(NULLIF(VALUES(`title`), ''), `title`),
                    `permalink` = COALESCE(NULLIF(VALUES(`permalink`), ''), `permalink`),
                    `thumbnail_url` = COALESCE(NULLIF(VALUES(`thumbnail_url`), ''), `thumbnail_url`),
                    `status` = VALUES(`status`),
                    `price` = IF(`price_manual` = 1, `price`, COALESCE(VALUES(`price`), `price`)),
                    `currency_id` = COALESCE(NULLIF(VALUES(`currency_id`), ''), `currency_id`),
                    `available_quantity` = COALESCE(VALUES(`available_quantity`), `available_quantity`),
                    `sold_quantity` = COALESCE(VALUES(`sold_quantity`), `sold_quantity`),
                    `seller_sku` = COALESCE(NULLIF(VALUES(`seller_sku`), ''), `seller_sku`),
                    `category_id` = COALESCE(NULLIF(VALUES(`category_id`), ''), `category_id`),
                    `listing_type_id` = COALESCE(NULLIF(VALUES(`listing_type_id`), ''), `listing_type_id`),
                    `weight_g` = IF(`weight_manual` = 1, `weight_g`, COALESCE(VALUES(`weight_g`), `weight_g`)),
                    `volumetric_weight_kg` = IF(`dimensions_manual` = 1, `volumetric_weight_kg`, COALESCE(VALUES(`volumetric_weight_kg`), `volumetric_weight_kg`)),
                    `package_length_cm` = IF(`dimensions_manual` = 1, `package_length_cm`, COALESCE(VALUES(`package_length_cm`), `package_length_cm`)),
                    `package_width_cm` = IF(`dimensions_manual` = 1, `package_width_cm`, COALESCE(VALUES(`package_width_cm`), `package_width_cm`)),
                    `package_height_cm` = IF(`dimensions_manual` = 1, `package_height_cm`, COALESCE(VALUES(`package_height_cm`), `package_height_cm`)),
                    `net_proceeds_usd` = IF(`net_proceeds_manual` = 1, `net_proceeds_usd`, COALESCE(VALUES(`net_proceeds_usd`), `net_proceeds_usd`)),
                    `sync_marker` = VALUES(`sync_marker`),
                    `remote_json` = VALUES(`remote_json`), `is_current` = 1,
                    `last_synced_at` = VALUES(`last_synced_at`)
            """
            values = []
            for row in rows:
                values.append(
                    tuple(
                        row[key]
                        for key in (
                            "token_id", "store_name", "seller_id", "site_id", "item_id", "title",
                            "permalink", "thumbnail_url", "status", "price", "currency_id",
                            "available_quantity", "sold_quantity", "seller_sku", "category_id",
                            "listing_type_id", "weight_g", "volumetric_weight_kg",
                            "package_length_cm", "package_width_cm", "package_height_cm",
                            "net_proceeds_usd", "sync_marker", "remote_json", "is_current",
                            "last_synced_at",
                        )
                    )
                )
            if values:
                cursor.executemany(sql, values)
        connection.commit()
        updated = len(existing)
        return {"total": len(rows), "inserted": len(rows) - updated, "updated": updated}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def finalize_store_snapshot(
    token_id: int,
    sync_marker: str,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    """Hide links absent from a fully completed incremental scan."""

    token_id = int(token_id)
    marker = str(sync_marker or "").strip()[:64]
    if token_id <= 0 or not marker:
        raise ValueError("完成店铺链接同步时缺少有效的店铺或同步标记")
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_store_link_table(cursor)
            cursor.execute(
                f"UPDATE `{STORE_LINK_TABLE}` SET `is_current` = 0 "
                "WHERE `token_id` = %s AND (`sync_marker` IS NULL OR `sync_marker` <> %s)",
                (token_id, marker),
            )
            changed = int(cursor.rowcount or 0)
        connection.commit()
        return changed
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_store_links(
    *,
    search: str = "",
    token_id: int | None = None,
    site_id: str = "",
    group_name: str = "",
    status: str = "",
    sales_sort: str = "desc",
    current_only: bool = True,
    page: int = 1,
    page_size: int = 1000,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = 1000
    conditions: list[str] = []
    values: list[Any] = []
    if current_only:
        conditions.append("links.`is_current` = 1")
    if token_id not in (None, ""):
        conditions.append("links.`token_id` = %s")
        values.append(int(token_id))
    site_id = str(site_id or "").strip().upper()[:16]
    if site_id:
        conditions.append("links.`site_id` = %s")
        values.append(site_id)
    group_name = str(group_name or "").strip()[:100]
    status = str(status or "").strip()
    if status:
        conditions.append("links.`status` = %s")
        values.append(status)
    search = str(search or "").strip()
    if search:
        pattern = f"%{search}%"
        conditions.append(
            "(links.`item_id` LIKE %s OR links.`title` LIKE %s OR "
            "links.`seller_sku` LIKE %s OR links.`store_name` LIKE %s)"
        )
        values.extend([pattern] * 4)
    links_from_sql = f" FROM `{STORE_LINK_TABLE}` AS links"
    sales_direction = "ASC" if str(sales_sort or "").strip().lower() == "asc" else "DESC"
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_store_link_table(cursor)
            cursor.execute(
                """
                SELECT `token_id`, `site_id`, COALESCE(`group_name`, '') AS `group_name`
                FROM `mercado_store_site_settings`
                """
            )
            site_settings = [dict(row) for row in cursor.fetchall()]
            group_map = {
                (int(row.get("token_id") or 0), str(row.get("site_id") or "").upper()):
                str(row.get("group_name") or "")
                for row in site_settings
            }
            groups = [
                {"group_name": value}
                for value in sorted(
                    {name for name in group_map.values() if name},
                    key=lambda name: name.casefold(),
                )
            ]
            groups.append({"group_name": "__ungrouped__"})

            filtered_conditions = list(conditions)
            filtered_values = list(values)
            if group_name:
                if group_name == "__ungrouped__":
                    grouped_pairs = [pair for pair, name in group_map.items() if name]
                    if grouped_pairs:
                        pair_sql = " OR ".join(
                            "(links.`token_id` = %s AND links.`site_id` = %s)"
                            for _pair in grouped_pairs
                        )
                        filtered_conditions.append(f"NOT ({pair_sql})")
                        for token_value, site_value in grouped_pairs:
                            filtered_values.extend([token_value, site_value])
                else:
                    matching_pairs = [
                        pair for pair, name in group_map.items() if name == group_name
                    ]
                    if matching_pairs:
                        pair_sql = " OR ".join(
                            "(links.`token_id` = %s AND links.`site_id` = %s)"
                            for _pair in matching_pairs
                        )
                        filtered_conditions.append(f"({pair_sql})")
                        for token_value, site_value in matching_pairs:
                            filtered_values.extend([token_value, site_value])
                    else:
                        filtered_conditions.append("1 = 0")
            where_sql = (
                " WHERE " + " AND ".join(filtered_conditions)
                if filtered_conditions else ""
            )
            cursor.execute(
                f"SELECT COUNT(*) AS `total`{links_from_sql}{where_sql}",
                tuple(filtered_values),
            )
            total = int((cursor.fetchone() or {}).get("total") or 0)
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            page_order_sql = (
                f"links.`sold_quantity` {sales_direction}, "
                f"links.`last_synced_at` {sales_direction}, links.`id` {sales_direction}"
            )
            outer_order_sql = (
                f"full_links.`sold_quantity` {sales_direction}, "
                f"full_links.`last_synced_at` {sales_direction}, "
                f"full_links.`id` {sales_direction}"
            )
            cursor.execute(
                f"""
                SELECT full_links.`id`, full_links.`token_id`, full_links.`store_name`,
                       full_links.`seller_id`, full_links.`site_id`, full_links.`item_id`,
                       full_links.`title`, full_links.`permalink`, full_links.`thumbnail_url`,
                       full_links.`status`, full_links.`price`, full_links.`currency_id`,
                       full_links.`available_quantity`, full_links.`sold_quantity`,
                       full_links.`seller_sku`, full_links.`category_id`,
                       full_links.`listing_type_id`, full_links.`weight_g`,
                       full_links.`volumetric_weight_kg`, full_links.`package_length_cm`,
                       full_links.`package_width_cm`, full_links.`package_height_cm`,
                       full_links.`net_proceeds_usd`, full_links.`price_manual`,
                       full_links.`weight_manual`, full_links.`dimensions_manual`,
                       full_links.`net_proceeds_manual`, full_links.`is_current`,
                       full_links.`last_synced_at`, full_links.`created_at`,
                       full_links.`updated_at`
                FROM `{STORE_LINK_TABLE}` AS full_links
                INNER JOIN (
                    SELECT links.`id`{links_from_sql}{where_sql}
                    ORDER BY {page_order_sql} LIMIT %s OFFSET %s
                ) AS page_ids ON page_ids.`id` = full_links.`id`
                ORDER BY {outer_order_sql}
                """,
                tuple(filtered_values + [page_size, (page - 1) * page_size]),
            )
            rows = [_json_safe_row(row) for row in cursor.fetchall()]
            for row in rows:
                row["group_name"] = group_map.get(
                    (int(row.get("token_id") or 0), str(row.get("site_id") or "").upper()),
                    "",
                )
            cursor.execute(
                """
                SELECT tokens.`id` AS `token_id`,
                       tokens.`display_name` AS `store_name`,
                       NULL AS `link_count`, NULL AS `last_synced_at`
                FROM `mercado_store_tokens` AS tokens
                ORDER BY tokens.`display_name`, tokens.`id`
                """
            )
            stores = [_json_safe_row(row) for row in cursor.fetchall()]
            cursor.execute(
                f"""
                SELECT links.`site_id`, COUNT(*) AS `link_count`
                FROM `{STORE_LINK_TABLE}` AS links
                WHERE links.`is_current` = 1
                  AND links.`site_id` IS NOT NULL AND links.`site_id` <> ''
                GROUP BY links.`site_id` ORDER BY links.`site_id`
                """
            )
            sites = [_json_safe_row(row) for row in cursor.fetchall()]
            cursor.execute(
                f"SELECT COUNT(*) AS `all_count` FROM `{STORE_LINK_TABLE}`"
            )
            summary = dict(cursor.fetchone() or {})
            cursor.execute(
                f"SELECT COUNT(*) AS `current_count` FROM `{STORE_LINK_TABLE}` "
                "WHERE `is_current` = 1"
            )
            summary.update(cursor.fetchone() or {})
            cursor.execute(
                f"SELECT COUNT(DISTINCT `token_id`) AS `store_count` "
                f"FROM `{STORE_LINK_TABLE}` WHERE `is_current` = 1"
            )
            summary.update(cursor.fetchone() or {})
            cursor.execute(
                f"SELECT MAX(`last_synced_at`) AS `last_synced_at` "
                f"FROM `{STORE_LINK_TABLE}` WHERE `is_current` = 1"
            )
            summary.update(cursor.fetchone() or {})
            summary = _json_safe_row(summary)
        connection.commit()
        return {
            "rows": rows,
            "stores": stores,
            "sites": sites,
            "groups": groups,
            "summary": summary,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }
    finally:
        connection.close()


def _decimal_change(field: str, value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有效数字") from exc
    if field != "net_proceeds_usd" and number < 0:
        raise ValueError(f"{field} 不能小于 0")
    return number


def get_store_links_by_ids(
    link_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """Load the fields required to update the corresponding remote listings."""

    ids: list[int] = []
    for value in link_ids or []:
        try:
            link_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"店铺链接编号无效：{value!r}") from exc
        if link_id > 0 and link_id not in ids:
            ids.append(link_id)
    ids.sort()
    if not ids:
        raise ValueError("请至少勾选一条店铺链接")
    if len(ids) > 1000:
        raise ValueError("每次最多更新 1000 条店铺链接")
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_store_link_table(cursor)
            cursor.execute(
                f"""
                SELECT `id`, `token_id`, `store_name`, `item_id`, `site_id`, `status`,
                       `currency_id`, `price`, `weight_g`, `package_length_cm`,
                       `package_width_cm`, `package_height_cm`, `net_proceeds_usd`
                FROM `{STORE_LINK_TABLE}`
                WHERE `id` IN ({placeholders})
                ORDER BY `id`
                """,
                tuple(ids),
            )
            rows = [_json_safe_row(row) for row in cursor.fetchall()]
        missing = sorted(set(ids).difference(int(row["id"]) for row in rows))
        if missing:
            raise ValueError(f"店铺链接不存在：{', '.join(map(str, missing))}")
        return rows
    finally:
        connection.close()


def bulk_update_store_links(
    link_ids: Iterable[int],
    changes: Mapping[str, Any],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, int]:
    ids = []
    for value in link_ids or []:
        try:
            link_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"店铺链接编号无效：{value!r}") from exc
        if link_id > 0 and link_id not in ids:
            ids.append(link_id)
    ids.sort()
    if not ids:
        raise ValueError("请至少勾选一条店铺链接")
    allowed = (
        "price", "weight_g", "package_length_cm", "package_width_cm",
        "package_height_cm", "net_proceeds_usd",
    )
    clean_changes = {
        field: _decimal_change(field, changes[field])
        for field in allowed
        if field in changes and changes[field] not in (None, "")
    }
    if not clean_changes:
        raise ValueError("请至少填写一个需要批量更新的字段")
    assignments = [f"`{field}` = %s" for field in clean_changes]
    values: list[Any] = list(clean_changes.values())
    if "price" in clean_changes:
        assignments.append("`price_manual` = 1")
    if "weight_g" in clean_changes:
        assignments.append("`weight_manual` = 1")
    dimension_fields = {"package_length_cm", "package_width_cm", "package_height_cm"}
    if dimension_fields.intersection(clean_changes):
        assignments.append("`dimensions_manual` = 1")
        assignments.append(
            "`volumetric_weight_kg` = CASE WHEN `package_length_cm` IS NOT NULL "
            "AND `package_width_cm` IS NOT NULL AND `package_height_cm` IS NOT NULL "
            "THEN ROUND(`package_length_cm` * `package_width_cm` * `package_height_cm` / 6000, 4) "
            "ELSE NULL END"
        )
    if "net_proceeds_usd" in clean_changes:
        assignments.append("`net_proceeds_manual` = 1")
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_store_link_table(cursor)
            cursor.execute(
                f"SELECT COUNT(*) AS `total` FROM `{STORE_LINK_TABLE}` WHERE `id` IN ({placeholders})",
                tuple(ids),
            )
            matched = int((cursor.fetchone() or {}).get("total") or 0)
            cursor.execute(
                f"UPDATE `{STORE_LINK_TABLE}` SET {', '.join(assignments)} WHERE `id` IN ({placeholders})",
                tuple(values + ids),
            )
            changed = int(cursor.rowcount or 0)
        connection.commit()
        return {"matched": matched, "changed": changed}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "STORE_LINK_TABLE",
    "STORE_LINK_SYNC_STATE_TABLE",
    "bulk_update_store_links",
    "ensure_store_link_table",
    "ensure_store_link_sync_state_table",
    "finalize_store_snapshot",
    "get_store_links_by_ids",
    "list_store_links",
    "list_due_store_link_token_ids",
    "listing_record",
    "mark_store_link_sync_finished",
    "mark_store_link_sync_started",
    "order_store_link_token_ids_for_full_sync",
    "request_store_link_sync",
    "replace_store_snapshot",
]
