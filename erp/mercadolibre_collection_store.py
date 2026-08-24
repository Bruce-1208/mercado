"""MySQL persistence for workbench Mercado Libre collection and product lists."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping


TASK_TABLE = "erp_mercadolibre_collection_tasks"
COLLECTION_TABLE = "erp_mercadolibre_collection_items"
PRODUCT_TABLE = "erp_mercadolibre_products"

PROFITABILITY_COLUMN_DEFINITIONS = (
    ("sale_price_usd", "DECIMAL(20,4) NULL"),
    ("exchange_rate_to_usd", "DECIMAL(20,8) NULL"),
    ("exchange_rate_updated_at", "VARCHAR(64) NULL"),
    ("category_id", "VARCHAR(64) NULL"),
    ("category_name", "VARCHAR(255) NULL"),
    ("listing_type_id", "VARCHAR(64) NULL"),
    ("listing_type_name", "VARCHAR(128) NULL"),
    ("commission_rate", "DECIMAL(10,4) NULL"),
    ("commission_amount_local", "DECIMAL(20,4) NULL"),
    ("commission_currency_id", "VARCHAR(16) NULL"),
    ("commission_amount_usd", "DECIMAL(20,4) NULL"),
    ("shipping_fee_local", "DECIMAL(20,4) NULL"),
    ("shipping_currency_id", "VARCHAR(16) NULL"),
    ("shipping_fee_usd", "DECIMAL(20,4) NULL"),
    ("billable_weight_g", "DECIMAL(20,4) NULL"),
    ("shipping_api_billable_weight_g", "DECIMAL(20,4) NULL"),
    ("shipping_weight_rule", "VARCHAR(128) NULL"),
    ("net_proceeds_usd", "DECIMAL(20,4) NULL"),
    ("profitability_updated_at", "DATETIME NULL"),
    ("profitability_source", "VARCHAR(128) NULL"),
    ("profitability_error", "TEXT NULL"),
)
PROFITABILITY_COLUMNS = tuple(column for column, _ in PROFITABILITY_COLUMN_DEFINITIONS)
PRODUCT_PUBLISH_COLUMN_DEFINITIONS = (
    ("last_publish_status", "VARCHAR(32) NULL"),
    ("last_publish_store_name", "VARCHAR(100) NULL"),
    ("last_publish_token_id", "BIGINT NULL"),
    ("last_published_item_id", "VARCHAR(64) NULL"),
    ("last_publish_error", "TEXT NULL"),
    ("last_publish_result_json", "LONGTEXT NULL"),
    ("last_published_at", "DATETIME NULL"),
)
PRODUCT_SOURCE_TYPES = {"collected", "pulled"}
PRODUCT_REVIEW_STATUSES = {
    "unreviewed", "approved", "suspected", "infringing", "risk",
}
PRODUCT_WORKFLOW_COLUMN_DEFINITIONS = (
    ("source_type", "VARCHAR(32) NOT NULL DEFAULT 'collected' AFTER `collection_item_id`"),
    ("review_status", "VARCHAR(32) NOT NULL DEFAULT 'unreviewed' AFTER `source_type`"),
)


def _connect() -> Any:
    import pymysql
    from bit.bit_mysql import config

    return pymysql.connect(**config)


def _now() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _json_safe_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, value in tuple(result.items()):
        if isinstance(value, datetime):
            result[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(value, Decimal):
            result[key] = float(value)
        elif isinstance(value, bytes):
            result[key] = value.decode("utf-8", errors="replace")
    result["added_to_products"] = bool(result.get("added_to_products"))
    return result


def _ensure_column(cursor: Any, table: str, column: str, definition: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM `{table}` LIKE %s", (column,))
    if cursor.fetchone():
        return False
    cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")
    return True


def _ensure_index(cursor: Any, table: str, index_name: str, definition: str) -> bool:
    cursor.execute(f"SHOW INDEX FROM `{table}` WHERE `Key_name` = %s", (index_name,))
    if cursor.fetchone():
        return False
    cursor.execute(f"ALTER TABLE `{table}` ADD KEY `{index_name}` {definition}")
    return True


def ensure_collection_tables(cursor: Any) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{TASK_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `source_url` VARCHAR(1500) NOT NULL,
            `requested_count` INT NOT NULL,
            `collected_count` INT NOT NULL DEFAULT 0,
            `completed_count` INT NOT NULL DEFAULT 0,
            `failed_count` INT NOT NULL DEFAULT 0,
            `current_page` INT NOT NULL DEFAULT 0,
            `status` VARCHAR(32) NOT NULL DEFAULT 'pending',
            `message` TEXT NULL,
            `created_by` VARCHAR(128) NULL,
            `started_at` DATETIME NULL,
            `finished_at` DATETIME NULL,
            `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            KEY `idx_erp_meli_collection_task_status` (`status`, `created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{COLLECTION_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `task_id` BIGINT NOT NULL,
            `source_item_id` VARCHAR(32) NOT NULL,
            `source_url` VARCHAR(1500) NOT NULL,
            `final_url` VARCHAR(1500) NULL,
            `main_image_url` VARCHAR(1500) NULL,
            `title` VARCHAR(255) NULL,
            `price` DECIMAL(20,4) NULL,
            `currency_id` VARCHAR(16) NULL,
            `weight_g` DECIMAL(20,4) NULL,
            `volumetric_weight_kg` DECIMAL(20,4) NULL,
            `package_length_cm` DECIMAL(20,4) NULL,
            `package_width_cm` DECIMAL(20,4) NULL,
            `package_height_cm` DECIMAL(20,4) NULL,
            `weight_basis` VARCHAR(64) NULL,
            `sale_price_usd` DECIMAL(20,4) NULL,
            `exchange_rate_to_usd` DECIMAL(20,8) NULL,
            `exchange_rate_updated_at` VARCHAR(64) NULL,
            `category_id` VARCHAR(64) NULL,
            `category_name` VARCHAR(255) NULL,
            `listing_type_id` VARCHAR(64) NULL,
            `listing_type_name` VARCHAR(128) NULL,
            `commission_rate` DECIMAL(10,4) NULL,
            `commission_amount_local` DECIMAL(20,4) NULL,
            `commission_currency_id` VARCHAR(16) NULL,
            `commission_amount_usd` DECIMAL(20,4) NULL,
            `shipping_fee_local` DECIMAL(20,4) NULL,
            `shipping_currency_id` VARCHAR(16) NULL,
            `shipping_fee_usd` DECIMAL(20,4) NULL,
            `billable_weight_g` DECIMAL(20,4) NULL,
            `shipping_api_billable_weight_g` DECIMAL(20,4) NULL,
            `shipping_weight_rule` VARCHAR(128) NULL,
            `net_proceeds_usd` DECIMAL(20,4) NULL,
            `profitability_updated_at` DATETIME NULL,
            `profitability_source` VARCHAR(128) NULL,
            `profitability_error` TEXT NULL,
            `scrape_status` VARCHAR(32) NOT NULL DEFAULT 'pending',
            `error_message` TEXT NULL,
            `source_json` LONGTEXT NULL,
            `description_json` LONGTEXT NULL,
            `page_snapshot_json` LONGTEXT NULL,
            `plugin_snapshot_json` LONGTEXT NULL,
            `added_to_products` TINYINT(1) NOT NULL DEFAULT 0,
            `collected_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_collection_item` (`source_item_id`),
            KEY `idx_erp_meli_collection_task` (`task_id`, `collected_at`),
            KEY `idx_erp_meli_collection_status` (`scrape_status`, `collected_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{PRODUCT_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `collection_item_id` BIGINT NOT NULL,
            `source_type` VARCHAR(32) NOT NULL DEFAULT 'collected',
            `review_status` VARCHAR(32) NOT NULL DEFAULT 'unreviewed',
            `source_item_id` VARCHAR(32) NOT NULL,
            `source_url` VARCHAR(1500) NOT NULL,
            `main_image_url` VARCHAR(1500) NULL,
            `title` VARCHAR(255) NULL,
            `price` DECIMAL(20,4) NULL,
            `currency_id` VARCHAR(16) NULL,
            `weight_g` DECIMAL(20,4) NULL,
            `volumetric_weight_kg` DECIMAL(20,4) NULL,
            `package_length_cm` DECIMAL(20,4) NULL,
            `package_width_cm` DECIMAL(20,4) NULL,
            `package_height_cm` DECIMAL(20,4) NULL,
            `weight_basis` VARCHAR(64) NULL,
            `sale_price_usd` DECIMAL(20,4) NULL,
            `exchange_rate_to_usd` DECIMAL(20,8) NULL,
            `exchange_rate_updated_at` VARCHAR(64) NULL,
            `category_id` VARCHAR(64) NULL,
            `category_name` VARCHAR(255) NULL,
            `listing_type_id` VARCHAR(64) NULL,
            `listing_type_name` VARCHAR(128) NULL,
            `commission_rate` DECIMAL(10,4) NULL,
            `commission_amount_local` DECIMAL(20,4) NULL,
            `commission_currency_id` VARCHAR(16) NULL,
            `commission_amount_usd` DECIMAL(20,4) NULL,
            `shipping_fee_local` DECIMAL(20,4) NULL,
            `shipping_currency_id` VARCHAR(16) NULL,
            `shipping_fee_usd` DECIMAL(20,4) NULL,
            `billable_weight_g` DECIMAL(20,4) NULL,
            `shipping_api_billable_weight_g` DECIMAL(20,4) NULL,
            `shipping_weight_rule` VARCHAR(128) NULL,
            `net_proceeds_usd` DECIMAL(20,4) NULL,
            `profitability_updated_at` DATETIME NULL,
            `profitability_source` VARCHAR(128) NULL,
            `profitability_error` TEXT NULL,
            `last_publish_status` VARCHAR(32) NULL,
            `last_publish_store_name` VARCHAR(100) NULL,
            `last_publish_token_id` BIGINT NULL,
            `last_published_item_id` VARCHAR(64) NULL,
            `last_publish_error` TEXT NULL,
            `last_publish_result_json` LONGTEXT NULL,
            `last_published_at` DATETIME NULL,
            `source_snapshot_json` LONGTEXT NULL,
            `added_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_product_item` (`source_item_id`),
            KEY `idx_erp_meli_product_added` (`added_at`),
            KEY `idx_erp_meli_product_source` (`source_type`, `id`),
            KEY `idx_erp_meli_product_review` (`review_status`, `id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    collection_volumetric_added = _ensure_column(
        cursor, COLLECTION_TABLE, "volumetric_weight_kg", "DECIMAL(20,4) NULL AFTER `weight_g`"
    )
    product_volumetric_added = _ensure_column(
        cursor, PRODUCT_TABLE, "volumetric_weight_kg", "DECIMAL(20,4) NULL AFTER `weight_g`"
    )
    product_basis_added = _ensure_column(
        cursor, PRODUCT_TABLE, "weight_basis", "VARCHAR(64) NULL AFTER `package_height_cm`"
    )
    for table in (COLLECTION_TABLE, PRODUCT_TABLE):
        for column, definition in PROFITABILITY_COLUMN_DEFINITIONS:
            _ensure_column(cursor, table, column, definition)
    for column, definition in PRODUCT_PUBLISH_COLUMN_DEFINITIONS:
        _ensure_column(cursor, PRODUCT_TABLE, column, definition)
    for column, definition in PRODUCT_WORKFLOW_COLUMN_DEFINITIONS:
        _ensure_column(cursor, PRODUCT_TABLE, column, definition)
    _ensure_index(
        cursor, PRODUCT_TABLE, "idx_erp_meli_product_source", "(`source_type`, `id`)"
    )
    _ensure_index(
        cursor, PRODUCT_TABLE, "idx_erp_meli_product_review", "(`review_status`, `id`)"
    )
    if collection_volumetric_added:
        cursor.execute(
            f"""
            UPDATE `{COLLECTION_TABLE}`
            SET `volumetric_weight_kg` = ROUND(
                `package_length_cm` * `package_width_cm` * `package_height_cm` / 6000, 4
            )
            WHERE `package_length_cm` IS NOT NULL
              AND `package_width_cm` IS NOT NULL
              AND `package_height_cm` IS NOT NULL
            """
        )
    if product_volumetric_added:
        cursor.execute(
            f"""
            UPDATE `{PRODUCT_TABLE}`
            SET `volumetric_weight_kg` = ROUND(
                `package_length_cm` * `package_width_cm` * `package_height_cm` / 6000, 4
            )
            WHERE `package_length_cm` IS NOT NULL
              AND `package_width_cm` IS NOT NULL
              AND `package_height_cm` IS NOT NULL
            """
        )
    if product_basis_added:
        cursor.execute(
            f"UPDATE `{PRODUCT_TABLE}` SET `weight_basis` = 'legacy_unknown' "
            "WHERE `weight_g` IS NOT NULL AND (`weight_basis` IS NULL OR `weight_basis` = '')"
        )


def create_collection_task(
    source_url: str,
    requested_count: int,
    created_by: str = "",
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    source_url = str(source_url or "").strip()
    if not source_url.startswith(("https://", "http://")):
        raise ValueError("请输入有效的 Mercado Libre 列表链接")
    requested_count = max(1, min(int(requested_count), 1000))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"""
                INSERT INTO `{TASK_TABLE}`
                    (`source_url`, `requested_count`, `status`, `created_by`, `created_at`)
                VALUES (%s, %s, 'pending', %s, %s)
                """,
                (source_url, requested_count, str(created_by or "")[:128], _now()),
            )
            task_id = int(cursor.lastrowid)
        connection.commit()
        return task_id
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_collection_task(
    task_id: int,
    *,
    status: str | None = None,
    message: str | None = None,
    collected_count: int | None = None,
    completed_count: int | None = None,
    failed_count: int | None = None,
    current_page: int | None = None,
    started: bool = False,
    finished: bool = False,
    connection_factory: Callable[[], Any] | None = None,
) -> None:
    values: list[Any] = []
    assignments: list[str] = []
    for column, value in (
        ("status", status),
        ("message", message),
        ("collected_count", collected_count),
        ("completed_count", completed_count),
        ("failed_count", failed_count),
        ("current_page", current_page),
    ):
        if value is not None:
            assignments.append(f"`{column}` = %s")
            values.append(value)
    if started:
        assignments.append("`started_at` = COALESCE(`started_at`, %s)")
        values.append(_now())
    if finished:
        assignments.append("`finished_at` = %s")
        values.append(_now())
    if not assignments:
        return
    values.append(int(task_id))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"UPDATE `{TASK_TABLE}` SET {', '.join(assignments)} WHERE `id` = %s",
                tuple(values),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_collection_task(
    task_id: int,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any] | None:
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(f"SELECT * FROM `{TASK_TABLE}` WHERE `id` = %s", (int(task_id),))
            row = cursor.fetchone()
        connection.commit()
        return _json_safe_row(row) if row else None
    finally:
        connection.close()


def recover_interrupted_collection_tasks(
    *,
    cutoff: str | None = None,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    """Finish tasks left active by a previous workbench process."""
    cutoff = str(cutoff or _now())
    message = "任务因服务重启或采集浏览器异常退出而中断，请重新采集"
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"""
                UPDATE `{TASK_TABLE}`
                SET `status` = 'error',
                    `message` = %s,
                    `finished_at` = COALESCE(`finished_at`, %s)
                WHERE `status` IN ('pending', 'starting', 'running')
                  AND `updated_at` < %s
                """,
                (message, cutoff, cutoff),
            )
            updated = int(cursor.rowcount or 0)
        connection.commit()
        return updated
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def upsert_collection_items(
    task_id: int,
    rows: Iterable[Mapping[str, Any]],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    records = [dict(row) for row in rows or []]
    if not records:
        return 0
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            profitability_columns_sql = ", ".join(
                f"`{column}`" for column in PROFITABILITY_COLUMNS
            )
            profitability_updates_sql = ",\n                        ".join(
                (
                    f"`{column}` = IF("
                    "`scrape_status` = 'ok' AND VALUES(`scrape_status`) <> 'ok', "
                    f"`{column}`, COALESCE(VALUES(`{column}`), `{column}`))"
                )
                for column in PROFITABILITY_COLUMNS
            )
            for row in records:
                values = (
                    int(task_id),
                    str(row.get("source_item_id") or row.get("item_id") or ""),
                    str(row.get("source_url") or ""),
                    str(row.get("final_url") or ""),
                    str(row.get("main_image_url") or ""),
                    str(row.get("title") or "")[:255],
                    row.get("price"),
                    str(row.get("currency_id") or ""),
                    row.get("weight_g"),
                    row.get("volumetric_weight_kg"),
                    row.get("package_length_cm"),
                    row.get("package_width_cm"),
                    row.get("package_height_cm"),
                    str(row.get("weight_basis") or ""),
                    *(row.get(column) for column in PROFITABILITY_COLUMNS),
                    str(row.get("scrape_status") or "partial")[:32],
                    str(row.get("error_message") or "")[:4000],
                    _dumps(row.get("source") or {}),
                    _dumps(row.get("description") or {}),
                    _dumps(row.get("page_snapshot") or {}),
                    _dumps(row.get("plugin_snapshot") or {}),
                    str(row.get("collected_at") or _now()),
                )
                cursor.execute(
                    f"""
                    INSERT INTO `{COLLECTION_TABLE}` (
                        `task_id`, `source_item_id`, `source_url`, `final_url`,
                        `main_image_url`, `title`, `price`, `currency_id`, `weight_g`,
                        `volumetric_weight_kg`,
                        `package_length_cm`, `package_width_cm`, `package_height_cm`,
                        `weight_basis`, {profitability_columns_sql},
                        `scrape_status`, `error_message`, `source_json`,
                        `description_json`, `page_snapshot_json`, `plugin_snapshot_json`,
                        `collected_at`
                    ) VALUES ({", ".join(["%s"] * len(values))})
                    ON DUPLICATE KEY UPDATE
                        `task_id` = IF(
                            `scrape_status` = 'ok' AND VALUES(`scrape_status`) <> 'ok',
                            `task_id`, VALUES(`task_id`)
                        ),
                        `source_url` = COALESCE(NULLIF(VALUES(`source_url`), ''), `source_url`),
                        `final_url` = COALESCE(NULLIF(VALUES(`final_url`), ''), `final_url`),
                        `main_image_url` = COALESCE(NULLIF(VALUES(`main_image_url`), ''), `main_image_url`),
                        `title` = COALESCE(NULLIF(VALUES(`title`), ''), `title`),
                        `price` = COALESCE(VALUES(`price`), `price`),
                        `currency_id` = COALESCE(NULLIF(VALUES(`currency_id`), ''), `currency_id`),
                        `weight_g` = COALESCE(VALUES(`weight_g`), `weight_g`),
                        `volumetric_weight_kg` = COALESCE(
                            VALUES(`volumetric_weight_kg`), `volumetric_weight_kg`
                        ),
                        `package_length_cm` = COALESCE(
                            VALUES(`package_length_cm`), `package_length_cm`
                        ),
                        `package_width_cm` = COALESCE(
                            VALUES(`package_width_cm`), `package_width_cm`
                        ),
                        `package_height_cm` = COALESCE(
                            VALUES(`package_height_cm`), `package_height_cm`
                        ),
                        `weight_basis` = COALESCE(
                            NULLIF(VALUES(`weight_basis`), ''), `weight_basis`
                        ),
                        {profitability_updates_sql},
                        `error_message` = IF(
                            `scrape_status` = 'ok' AND VALUES(`scrape_status`) <> 'ok',
                            `error_message`, VALUES(`error_message`)
                        ),
                        `source_json` = IF(
                            `scrape_status` = 'ok' AND VALUES(`scrape_status`) <> 'ok',
                            `source_json`, VALUES(`source_json`)
                        ),
                        `description_json` = IF(
                            `scrape_status` = 'ok' AND VALUES(`scrape_status`) <> 'ok',
                            `description_json`, VALUES(`description_json`)
                        ),
                        `page_snapshot_json` = IF(
                            `scrape_status` = 'ok' AND VALUES(`scrape_status`) <> 'ok',
                            `page_snapshot_json`, VALUES(`page_snapshot_json`)
                        ),
                        `plugin_snapshot_json` = IF(
                            `scrape_status` = 'ok' AND VALUES(`scrape_status`) <> 'ok',
                            `plugin_snapshot_json`, VALUES(`plugin_snapshot_json`)
                        ),
                        `collected_at` = IF(
                            `scrape_status` = 'ok' AND VALUES(`scrape_status`) <> 'ok',
                            `collected_at`, VALUES(`collected_at`)
                        ),
                        `scrape_status` = IF(
                            `scrape_status` = 'ok' AND VALUES(`scrape_status`) <> 'ok',
                            'ok', VALUES(`scrape_status`)
                        )
                    """,
                    values,
                )
        connection.commit()
        return len(records)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _list_rows(
    table: str,
    *,
    search: str = "",
    limit: int = 500,
    offset: int = 0,
    task_id: int | None = None,
    source_type: str = "",
    review_status: str = "",
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    where: list[str] = []
    params: list[Any] = []
    search = str(search or "").strip()
    if search:
        where.append("(`source_item_id` LIKE %s OR `title` LIKE %s)")
        pattern = f"%{search}%"
        params.extend((pattern, pattern))
    if task_id is not None and table == COLLECTION_TABLE:
        where.append("`task_id` = %s")
        params.append(int(task_id))
    if table == PRODUCT_TABLE:
        source_type = str(source_type or "").strip().lower()
        review_status = str(review_status or "").strip().lower()
        if source_type:
            if source_type not in PRODUCT_SOURCE_TYPES:
                raise ValueError(f"不支持的产品来源: {source_type}")
            where.append("`source_type` = %s")
            params.append(source_type)
        if review_status:
            if review_status not in PRODUCT_REVIEW_STATUSES:
                raise ValueError(f"不支持的审核状态: {review_status}")
            where.append("`review_status` = %s")
            params.append(review_status)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(f"SELECT COUNT(*) AS total FROM `{table}` {where_sql}", tuple(params))
            total = int((cursor.fetchone() or {}).get("total") or 0)
            cursor.execute(
                f"SELECT * FROM `{table}` {where_sql} ORDER BY `id` DESC LIMIT %s OFFSET %s",
                tuple(params + [limit, offset]),
            )
            rows = [_json_safe_row(row) for row in cursor.fetchall()]
        connection.commit()
        return {"total": total, "rows": rows}
    finally:
        connection.close()


def list_collection_items(**kwargs: Any) -> dict[str, Any]:
    return _list_rows(COLLECTION_TABLE, **kwargs)


def list_product_items(**kwargs: Any) -> dict[str, Any]:
    kwargs.pop("task_id", None)
    return _list_rows(PRODUCT_TABLE, **kwargs)


def update_product_review_status(
    product_item_ids: Iterable[int],
    review_status: str,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, int]:
    ids = _normalize_row_ids(product_item_ids, empty_message="请至少勾选一个产品")
    status = str(review_status or "").strip().lower()
    if status not in PRODUCT_REVIEW_STATUSES:
        raise ValueError(f"不支持的审核状态: {status}")
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` SET `review_status` = %s "
                f"WHERE `id` IN ({placeholders})",
                tuple([status] + ids),
            )
            changed = int(cursor.rowcount or 0)
        connection.commit()
        return {"requested": len(ids), "changed": changed}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def upsert_pulled_store_links_to_products(
    token: Mapping[str, Any],
    items: Iterable[Mapping[str, Any]],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, int]:
    """Mirror detailed authorized-store listings into publish-ready products."""

    from erp.mercadolibre_store_link_store import listing_record

    now = _now()
    values = []
    skipped = 0
    for item in items or []:
        source = dict(item or {})
        record = listing_record(token, source, now)
        if not all((
            record.get("item_id"), record.get("title"), record.get("permalink"),
            record.get("thumbnail_url"), record.get("category_id"),
            record.get("price") is not None,
        )):
            skipped += 1
            continue
        snapshot = {
            "source": source,
            "description": {},
            "page_snapshot": {},
            "plugin_snapshot": {
                "source_type": "pulled",
                "store_name": record.get("store_name"),
                "site_id": record.get("site_id"),
            },
        }
        net_proceeds = source.get("net_proceeds") or {}
        net_amount = (
            net_proceeds.get("amount")
            if isinstance(net_proceeds, Mapping)
            and str(net_proceeds.get("currency_id") or record.get("currency_id")).upper() == "USD"
            else None
        )
        sale_price_usd = (
            record.get("price")
            if str(record.get("currency_id") or "").upper() == "USD"
            else None
        )
        values.append((
            0, "pulled", "unreviewed", record["item_id"], record["permalink"],
            record["thumbnail_url"], record["title"], record.get("price"),
            record.get("currency_id"), record.get("weight_g"),
            record.get("volumetric_weight_kg"), record.get("package_length_cm"),
            record.get("package_width_cm"), record.get("package_height_cm"),
            "official_api" if record.get("weight_g") is not None else "official_missing",
            sale_price_usd, record.get("category_id"), record.get("listing_type_id"),
            net_amount, _dumps(snapshot), now,
        ))
    if not values:
        return {"count": 0, "skipped": skipped}

    pulled_fields = (
        "source_url", "main_image_url", "title", "price", "currency_id",
        "weight_g", "volumetric_weight_kg", "package_length_cm",
        "package_width_cm", "package_height_cm", "weight_basis",
        "sale_price_usd", "category_id", "listing_type_id", "net_proceeds_usd",
        "source_snapshot_json",
    )
    updates = ",\n                    ".join(
        f"`{field}` = IF(`source_type` = 'pulled', VALUES(`{field}`), `{field}`)"
        for field in pulled_fields
    )
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.executemany(
                f"""
                INSERT INTO `{PRODUCT_TABLE}` (
                    `collection_item_id`, `source_type`, `review_status`, `source_item_id`,
                    `source_url`, `main_image_url`, `title`, `price`, `currency_id`,
                    `weight_g`, `volumetric_weight_kg`, `package_length_cm`,
                    `package_width_cm`, `package_height_cm`, `weight_basis`,
                    `sale_price_usd`, `category_id`, `listing_type_id`,
                    `net_proceeds_usd`, `source_snapshot_json`, `added_at`
                ) VALUES ({", ".join(["%s"] * 21)})
                ON DUPLICATE KEY UPDATE
                    {updates},
                    `updated_at` = CURRENT_TIMESTAMP
                """,
                values,
            )
        connection.commit()
        return {"count": len(values), "skipped": skipped}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _normalize_row_ids(values: Iterable[int], *, empty_message: str) -> list[int]:
    ids: list[int] = []
    for value in values or []:
        try:
            row_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"商品记录编号无效: {value!r}") from exc
        if row_id > 0 and row_id not in ids:
            ids.append(row_id)
    ids.sort()
    if not ids:
        raise ValueError(empty_message)
    if len(ids) > 500:
        raise ValueError("每次最多处理 500 件商品")
    return ids


def get_product_items_by_ids(
    product_item_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    ids = _normalize_row_ids(product_item_ids, empty_message="请至少勾选一个产品")
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` IN ({placeholders}) ORDER BY `id` ASC",
                tuple(ids),
            )
            rows = [_json_safe_row(row) for row in cursor.fetchall()]
        connection.commit()
        return rows
    finally:
        connection.close()


def delete_collection_items(
    collection_item_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    ids = _normalize_row_ids(collection_item_ids, empty_message="请至少勾选一个采集商品")
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"DELETE FROM `{COLLECTION_TABLE}` WHERE `id` IN ({placeholders})",
                tuple(ids),
            )
            deleted = int(cursor.rowcount or 0)
        connection.commit()
        return {"requested": len(ids), "deleted": deleted}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_product_items(
    product_item_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    ids = _normalize_row_ids(product_item_ids, empty_message="请至少勾选一个产品")
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT `source_item_id` FROM `{PRODUCT_TABLE}` WHERE `id` IN ({placeholders})",
                tuple(ids),
            )
            source_ids = [
                str(row.get("source_item_id") or "")
                for row in cursor.fetchall()
                if row.get("source_item_id")
            ]
            cursor.execute(
                f"DELETE FROM `{PRODUCT_TABLE}` WHERE `id` IN ({placeholders})",
                tuple(ids),
            )
            deleted = int(cursor.rowcount or 0)
            if source_ids:
                source_placeholders = ", ".join(["%s"] * len(source_ids))
                cursor.execute(
                    f"UPDATE `{COLLECTION_TABLE}` SET `added_to_products` = 0 "
                    f"WHERE `source_item_id` IN ({source_placeholders})",
                    tuple(source_ids),
                )
        connection.commit()
        return {"requested": len(ids), "deleted": deleted}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_product_publish_state(
    product_item_id: int,
    *,
    status: str,
    store_name: str,
    token_id: int,
    published_item_id: str = "",
    error_message: str = "",
    result: Mapping[str, Any] | None = None,
    finished: bool = False,
    connection_factory: Callable[[], Any] | None = None,
) -> None:
    status = str(status or "").strip()[:32]
    if status not in {"pending", "publishing", "published", "failed"}:
        raise ValueError(f"不支持的上架状态: {status}")
    assignments = [
        "`last_publish_status` = %s",
        "`last_publish_store_name` = %s",
        "`last_publish_token_id` = %s",
        "`last_published_item_id` = %s",
        "`last_publish_error` = %s",
        "`last_publish_result_json` = %s",
    ]
    values: list[Any] = [
        status,
        str(store_name or "")[:100],
        int(token_id),
        str(published_item_id or "")[:64],
        str(error_message or "")[:4000],
        _dumps(result or {}),
    ]
    if finished:
        assignments.append("`last_published_at` = %s")
        values.append(_now())
    values.append(int(product_item_id))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` SET {', '.join(assignments)} WHERE `id` = %s",
                tuple(values),
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    f"SELECT 1 FROM `{PRODUCT_TABLE}` WHERE `id` = %s",
                    (int(product_item_id),),
                )
                if not cursor.fetchone():
                    raise KeyError("产品记录不存在")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_stale_profitability_items(
    *,
    stale_before: str,
    limit: int = 50,
    connection_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """Return collected rows whose official cost snapshot needs refreshing."""

    limit = max(1, min(int(limit), 500))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"""
                SELECT * FROM `{COLLECTION_TABLE}`
                WHERE (`profitability_updated_at` IS NULL
                       OR `profitability_updated_at` < %s)
                  AND `price` IS NOT NULL
                  AND `price` > 0
                  AND `title` IS NOT NULL
                  AND `title` <> ''
                  AND `weight_g` IS NOT NULL
                  AND `weight_g` > 0
                ORDER BY COALESCE(`profitability_updated_at`, '1970-01-01') ASC, `id` ASC
                LIMIT %s
                """,
                (stale_before, limit),
            )
            rows = [_json_safe_row(row) for row in cursor.fetchall()]
        connection.commit()
        return rows
    finally:
        connection.close()


def update_item_profitability(
    source_item_id: str,
    snapshot: Mapping[str, Any],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> None:
    """Update the official cost snapshot in collection and product lists."""

    item_id = str(source_item_id or "").strip().upper()
    if not item_id:
        raise ValueError("商品编号不能为空")
    values = [snapshot.get(column) for column in PROFITABILITY_COLUMNS]
    assignments = ", ".join(f"`{column}` = %s" for column in PROFITABILITY_COLUMNS)
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            for table in (COLLECTION_TABLE, PRODUCT_TABLE):
                cursor.execute(
                    f"UPDATE `{table}` SET {assignments} WHERE `source_item_id` = %s",
                    tuple(values + [item_id]),
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def add_collection_items_to_products(
    collection_item_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    ids: list[int] = []
    for value in collection_item_ids or []:
        try:
            item_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"采集商品编号无效: {value!r}") from exc
        if item_id > 0 and item_id not in ids:
            ids.append(item_id)
    ids.sort()
    if not ids:
        raise ValueError("请至少勾选一个采集商品")
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    selected_rows: list[dict[str, Any]] = []
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{COLLECTION_TABLE}` WHERE `id` IN ({placeholders})",
                tuple(ids),
            )
            selected_rows = [dict(row) for row in cursor.fetchall()]
            profitability_columns_sql = ", ".join(
                f"`{column}`" for column in PROFITABILITY_COLUMNS
            )
            profitability_updates_sql = ",\n                        ".join(
                f"`{column}` = VALUES(`{column}`)" for column in PROFITABILITY_COLUMNS
            )
            for row in selected_rows:
                snapshot = {
                    "source": _loads(row.get("source_json"), {}),
                    "description": _loads(row.get("description_json"), {}),
                    "page_snapshot": _loads(row.get("page_snapshot_json"), {}),
                    "plugin_snapshot": _loads(row.get("plugin_snapshot_json"), {}),
                }
                values = (
                    row["id"], "collected", "unreviewed",
                    row["source_item_id"], row["source_url"],
                    row.get("main_image_url"), row.get("title"), row.get("price"),
                    row.get("currency_id"), row.get("weight_g"),
                    row.get("volumetric_weight_kg"),
                    row.get("package_length_cm"), row.get("package_width_cm"),
                    row.get("package_height_cm"), row.get("weight_basis"),
                    *(row.get(column) for column in PROFITABILITY_COLUMNS),
                    _dumps(snapshot), _now(),
                )
                cursor.execute(
                    f"""
                    INSERT INTO `{PRODUCT_TABLE}` (
                        `collection_item_id`, `source_type`, `review_status`,
                        `source_item_id`, `source_url`,
                        `main_image_url`, `title`, `price`, `currency_id`, `weight_g`,
                        `volumetric_weight_kg`,
                        `package_length_cm`, `package_width_cm`, `package_height_cm`,
                        `weight_basis`, {profitability_columns_sql},
                        `source_snapshot_json`, `added_at`
                    ) VALUES ({", ".join(["%s"] * len(values))})
                    ON DUPLICATE KEY UPDATE
                        `collection_item_id` = VALUES(`collection_item_id`),
                        `source_type` = 'collected',
                        `source_url` = VALUES(`source_url`),
                        `main_image_url` = VALUES(`main_image_url`),
                        `title` = VALUES(`title`),
                        `price` = VALUES(`price`),
                        `currency_id` = VALUES(`currency_id`),
                        `weight_g` = VALUES(`weight_g`),
                        `volumetric_weight_kg` = VALUES(`volumetric_weight_kg`),
                        `package_length_cm` = VALUES(`package_length_cm`),
                        `package_width_cm` = VALUES(`package_width_cm`),
                        `package_height_cm` = VALUES(`package_height_cm`),
                        `weight_basis` = VALUES(`weight_basis`),
                        {profitability_updates_sql},
                        `source_snapshot_json` = VALUES(`source_snapshot_json`),
                        `updated_at` = CURRENT_TIMESTAMP
                    """,
                    values,
                )
            cursor.execute(
                f"UPDATE `{COLLECTION_TABLE}` SET `added_to_products` = 1 WHERE `id` IN ({placeholders})",
                tuple(ids),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    # Keep the publication source table in sync.  A failed mirror does not undo
    # the user's explicit move into the product list.
    mirrored = 0
    mirror_errors: list[str] = []
    try:
        from erp.mercadolibre_source_store import upsert_source_snapshot

        for row in selected_rows:
            try:
                upsert_source_snapshot(
                    {
                        "item_id": row["source_item_id"],
                        "source_url": row["source_url"],
                        "final_url": row.get("final_url"),
                        "main_image_url": row.get("main_image_url"),
                        "title": row.get("title"),
                        "price": row.get("price"),
                        "currency_id": row.get("currency_id"),
                        "category_id": row.get("category_id"),
                        "source": _loads(row.get("source_json"), {}),
                        "description": _loads(row.get("description_json"), {}),
                        "page_snapshot": _loads(row.get("page_snapshot_json"), {}),
                        "plugin_snapshot": _loads(row.get("plugin_snapshot_json"), {}),
                        "weight_g": row.get("weight_g"),
                        "volumetric_weight_kg": row.get("volumetric_weight_kg"),
                        "package_length_cm": row.get("package_length_cm"),
                        "package_width_cm": row.get("package_width_cm"),
                        "package_height_cm": row.get("package_height_cm"),
                        "scrape_status": row.get("scrape_status") or "partial",
                        "error_message": row.get("error_message") or "",
                        "scraped_at": row.get("collected_at") or _now(),
                    }
                )
                mirrored += 1
            except Exception as exc:
                mirror_errors.append(f"{row.get('source_item_id')}: {exc}")
    except Exception as exc:
        mirror_errors.append(str(exc))
    return {
        "count": len(selected_rows),
        "mirrored": mirrored,
        "mirror_errors": mirror_errors[:10],
    }
