"""MySQL persistence for workbench Mercado Libre collection and product lists."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping


TASK_TABLE = "erp_mercadolibre_collection_tasks"
COLLECTION_TABLE = "erp_mercadolibre_collection_items"
PRODUCT_TABLE = "erp_mercadolibre_products"
PUBLISH_RECORD_TABLE = "erp_mercadolibre_publish_records"

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
PRODUCT_PUBLISH_RECORD_STATUSES = {"pending", "publishing", "published", "failed"}
PRODUCT_PUBLISH_RETRYABLE_STATUSES = {"pending", "publishing", "failed"}
PRODUCT_PUBLISH_FILTER_STATUSES = PRODUCT_PUBLISH_RECORD_STATUSES | {"unpublished"}
PRODUCT_WORKFLOW_COLUMN_DEFINITIONS = (
    ("source_type", "VARCHAR(32) NOT NULL DEFAULT 'collected' AFTER `collection_item_id`"),
    ("review_status", "VARCHAR(32) NOT NULL DEFAULT 'unreviewed' AFTER `source_type`"),
    ("description_text", "LONGTEXT NULL AFTER `title`"),
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


def _ensure_collection_task_unique_index(cursor: Any) -> bool:
    """Keep collected items unique inside a task, not across all task history.

    The former source-item-only index caused an item already collected by an
    older task to remain attached to that task.  A new 200-item run could then
    report 200 processed rows while its task detail contained fewer than 200.
    """
    index_name = "uniq_erp_meli_collection_item"
    cursor.execute(
        f"SHOW INDEX FROM `{COLLECTION_TABLE}` WHERE `Key_name` = %s ",
        (index_name,),
    )
    rows = list(cursor.fetchall() or [])
    columns = [
        str(row.get("Column_name") or "")
        for row in sorted(rows, key=lambda row: int(row.get("Seq_in_index") or 0))
        if isinstance(row, Mapping)
    ]
    if columns == ["task_id", "source_item_id"]:
        return False
    if rows:
        cursor.execute(
            f"ALTER TABLE `{COLLECTION_TABLE}` DROP INDEX `{index_name}`"
        )
    cursor.execute(
        f"ALTER TABLE `{COLLECTION_TABLE}` "
        f"ADD UNIQUE KEY `{index_name}` (`task_id`, `source_item_id`)"
    )
    return True


def ensure_collection_tables(cursor: Any) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{TASK_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `source_url` VARCHAR(1500) NOT NULL,
            `requested_count` INT NOT NULL,
            `worker_count` INT NOT NULL DEFAULT 0,
            `collected_count` INT NOT NULL DEFAULT 0,
            `completed_count` INT NOT NULL DEFAULT 0,
            `failed_count` INT NOT NULL DEFAULT 0,
            `elapsed_seconds` INT NOT NULL DEFAULT 0,
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
            `description_text` LONGTEXT NULL,
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
            UNIQUE KEY `uniq_erp_meli_collection_item` (`task_id`, `source_item_id`),
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
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{PUBLISH_RECORD_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `batch_id` VARCHAR(64) NOT NULL,
            `product_item_id` BIGINT NULL,
            `source_item_id` VARCHAR(32) NOT NULL,
            `source_url` VARCHAR(1500) NULL,
            `main_image_url` VARCHAR(1500) NULL,
            `title` VARCHAR(255) NULL,
            `token_id` BIGINT NOT NULL,
            `store_name` VARCHAR(100) NOT NULL,
            `site_id` VARCHAR(16) NOT NULL,
            `site_name` VARCHAR(64) NULL,
            `quantity` INT NOT NULL DEFAULT 1,
            `status` VARCHAR(32) NOT NULL DEFAULT 'pending',
            `published_item_id` VARCHAR(64) NULL,
            `failure_reason` TEXT NULL,
            `result_json` LONGTEXT NULL,
            `created_by` VARCHAR(128) NULL,
            `started_at` DATETIME NULL,
            `finished_at` DATETIME NULL,
            `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_publish_batch_product` (`batch_id`, `product_item_id`),
            KEY `idx_erp_meli_publish_product` (`product_item_id`, `created_at`),
            KEY `idx_erp_meli_publish_status` (`status`, `created_at`),
            KEY `idx_erp_meli_publish_store` (`token_id`, `created_at`)
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
    _ensure_column(
        cursor, TASK_TABLE, "worker_count", "INT NOT NULL DEFAULT 0 AFTER `requested_count`"
    )
    _ensure_column(
        cursor, TASK_TABLE, "elapsed_seconds", "INT NOT NULL DEFAULT 0 AFTER `failed_count`"
    )
    for table in (COLLECTION_TABLE, PRODUCT_TABLE):
        for column, definition in PROFITABILITY_COLUMN_DEFINITIONS:
            _ensure_column(cursor, table, column, definition)
    for column, definition in PRODUCT_PUBLISH_COLUMN_DEFINITIONS:
        _ensure_column(cursor, PRODUCT_TABLE, column, definition)
    for column, definition in PRODUCT_WORKFLOW_COLUMN_DEFINITIONS:
        _ensure_column(cursor, PRODUCT_TABLE, column, definition)
    _ensure_collection_task_unique_index(cursor)
    _ensure_index(
        cursor, PRODUCT_TABLE, "idx_erp_meli_product_source", "(`source_type`, `id`)"
    )
    _ensure_index(
        cursor, PRODUCT_TABLE, "idx_erp_meli_product_review", "(`review_status`, `id`)"
    )
    _ensure_index(
        cursor, COLLECTION_TABLE, "idx_erp_meli_collection_added", "(`added_to_products`, `id`)"
    )
    _ensure_index(
        cursor, PRODUCT_TABLE, "idx_erp_meli_product_publish", "(`last_publish_status`, `id`)"
    )
    _ensure_index(cursor, PRODUCT_TABLE, "idx_erp_meli_product_weight", "(`weight_g`)")
    _ensure_index(cursor, PRODUCT_TABLE, "idx_erp_meli_product_price", "(`price`)")
    _ensure_index(
        cursor, PRODUCT_TABLE, "idx_erp_meli_product_net", "(`net_proceeds_usd`)"
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
    worker_count: int = 0,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    source_url = str(source_url or "").strip()
    if not source_url.startswith(("https://", "http://")):
        raise ValueError("请输入有效的 Mercado Libre 列表链接")
    requested_count = max(1, min(int(requested_count), 1000))
    worker_count = max(0, min(int(worker_count or 0), 10))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"""
                INSERT INTO `{TASK_TABLE}`
                    (`source_url`, `requested_count`, `worker_count`, `status`, `created_by`, `created_at`)
                VALUES (%s, %s, %s, 'pending', %s, %s)
                """,
                (
                    source_url,
                    requested_count,
                    worker_count,
                    str(created_by or "")[:128],
                    _now(),
                ),
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
    worker_count: int | None = None,
    elapsed_seconds: int | None = None,
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
        ("worker_count", worker_count),
        ("elapsed_seconds", elapsed_seconds),
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
    publish_status: str = "",
    weight_min: Any = None,
    weight_max: Any = None,
    price_min: Any = None,
    price_max: Any = None,
    net_proceeds_min: Any = None,
    net_proceeds_max: Any = None,
    date_from: str = "",
    date_to: str = "",
    exclude_added: bool = False,
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
    if table == COLLECTION_TABLE and exclude_added:
        where.append("`added_to_products` = 0")
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
        publish_status = str(publish_status or "").strip().lower()
        if publish_status:
            if publish_status not in PRODUCT_PUBLISH_FILTER_STATUSES:
                raise ValueError(f"不支持的上架状态: {publish_status}")
            if publish_status == "unpublished":
                where.append("(`last_publish_status` IS NULL OR `last_publish_status` = '')")
            else:
                where.append("`last_publish_status` = %s")
                params.append(publish_status)

        def optional_decimal(value: Any, name: str, *, nonnegative: bool) -> Decimal | None:
            if value in (None, ""):
                return None
            try:
                number = Decimal(str(value))
            except Exception as exc:
                raise ValueError(f"{name}必须是数字") from exc
            if not number.is_finite() or (nonnegative and number < 0):
                raise ValueError(f"{name}必须是{'非负' if nonnegative else '有效'}数字")
            return number

        ranges = (
            ("weight_g", "重量", weight_min, weight_max, True),
            ("price", "售价", price_min, price_max, True),
            (
                "net_proceeds_usd", "净收益", net_proceeds_min,
                net_proceeds_max, False,
            ),
        )
        for column, label, raw_min, raw_max, nonnegative in ranges:
            minimum = optional_decimal(raw_min, f"最低{label}", nonnegative=nonnegative)
            maximum = optional_decimal(raw_max, f"最高{label}", nonnegative=nonnegative)
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError(f"最低{label}不能大于最高{label}")
            if minimum is not None:
                where.append(f"`{column}` >= %s")
                params.append(minimum)
            if maximum is not None:
                where.append(f"`{column}` <= %s")
                params.append(maximum)

        def parsed_datetime(value: Any, name: str) -> tuple[datetime | None, str]:
            text = str(value or "").strip()
            if not text:
                return None, ""
            normalized = text.replace("T", " ")
            for date_format, precision in (
                ("%Y-%m-%d", "day"),
                ("%Y-%m-%d %H:%M", "minute"),
                ("%Y-%m-%d %H:%M:%S", "minute"),
            ):
                try:
                    return datetime.strptime(normalized, date_format), precision
                except ValueError:
                    continue
            raise ValueError(f"{name}格式必须为 YYYY-MM-DD HH:MM")

        start_date, _ = parsed_datetime(date_from, "开始时间")
        end_date, end_precision = parsed_datetime(date_to, "结束时间")
        if start_date and end_date and start_date > end_date:
            raise ValueError("开始时间不能晚于结束时间")
        if start_date:
            where.append("`added_at` >= %s")
            params.append(start_date.strftime("%Y-%m-%d %H:%M:%S"))
        if end_date:
            where.append("`added_at` < %s")
            end_exclusive = end_date + (
                timedelta(days=1) if end_precision == "day" else timedelta(minutes=1)
            )
            params.append(end_exclusive.strftime("%Y-%m-%d %H:%M:%S"))
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


def create_product_publish_records(
    product_rows: Iterable[Mapping[str, Any]],
    *,
    batch_id: str,
    token_id: int,
    store_name: str,
    site_id: str,
    site_name: str = "",
    quantity: int = 1,
    created_by: str = "",
    connection_factory: Callable[[], Any] | None = None,
) -> dict[int, int]:
    """Create one immutable attempt row for every selected product."""

    rows = [dict(row) for row in product_rows or []]
    if not rows:
        raise ValueError("请至少勾选一个产品")
    normalized_batch_id = str(batch_id or "").strip()[:64]
    if not normalized_batch_id:
        raise ValueError("上架批次编号不能为空")
    quantity = int(quantity)
    if quantity < 1 or quantity > 9999:
        raise ValueError("上架库存必须在 1-9999 之间")
    normalized_token_id = int(token_id)
    if normalized_token_id <= 0:
        raise ValueError("上架店铺编号无效")

    connection = (connection_factory or _connect)()
    record_ids: dict[int, int] = {}
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            for row in rows:
                product_item_id = int(row.get("id") or 0)
                if product_item_id <= 0:
                    raise ValueError("产品记录编号无效")
                source_item_id = str(row.get("source_item_id") or "").strip().upper()
                if not source_item_id:
                    raise ValueError(f"产品 {product_item_id} 缺少商品编号")
                cursor.execute(
                    f"""
                    INSERT INTO `{PUBLISH_RECORD_TABLE}` (
                        `batch_id`, `product_item_id`, `source_item_id`, `source_url`,
                        `main_image_url`, `title`, `token_id`, `store_name`,
                        `site_id`, `site_name`, `quantity`, `status`, `created_by`,
                        `created_at`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                    ON DUPLICATE KEY UPDATE `id` = LAST_INSERT_ID(`id`)
                    """,
                    (
                        normalized_batch_id,
                        product_item_id,
                        source_item_id,
                        str(row.get("source_url") or "")[:1500],
                        str(row.get("main_image_url") or "")[:1500],
                        str(row.get("title") or "")[:255],
                        normalized_token_id,
                        str(store_name or normalized_token_id)[:100],
                        str(site_id or "")[:16].upper(),
                        str(site_name or "")[:64],
                        quantity,
                        str(created_by or "")[:128],
                        _now(),
                    ),
                )
                record_ids[product_item_id] = int(cursor.lastrowid)
        connection.commit()
        return record_ids
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_published_product_item_ids(
    product_item_ids: Iterable[int],
    *,
    token_id: int,
    site_id: str,
    connection_factory: Callable[[], Any] | None = None,
) -> list[int]:
    """Return products already published for the same seller and destination site."""

    item_ids = list(dict.fromkeys(
        int(value) for value in product_item_ids or [] if int(value) > 0
    ))
    if not item_ids:
        return []
    normalized_token_id = int(token_id)
    normalized_site_id = str(site_id or "").strip().upper()
    if normalized_token_id <= 0 or not normalized_site_id:
        raise ValueError("查询历史上架记录时账号或站点无效")
    placeholders = ", ".join(["%s"] * len(item_ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT DISTINCT `product_item_id` FROM `{PUBLISH_RECORD_TABLE}` "
                f"WHERE `product_item_id` IN ({placeholders}) "
                "AND `token_id` = %s AND `site_id` = %s AND `status` = 'published'",
                tuple(item_ids + [normalized_token_id, normalized_site_id]),
            )
            rows = cursor.fetchall()
        connection.commit()
        return [int(row.get("product_item_id") or 0) for row in rows]
    finally:
        connection.close()


def update_product_publish_record(
    record_id: int,
    *,
    status: str | None = None,
    published_item_id: str | None = None,
    failure_reason: str | None = None,
    result: Mapping[str, Any] | None = None,
    started: bool = False,
    finished: bool = False,
    connection_factory: Callable[[], Any] | None = None,
) -> None:
    assignments: list[str] = []
    values: list[Any] = []
    if status is not None:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in PRODUCT_PUBLISH_RECORD_STATUSES:
            raise ValueError(f"不支持的上架记录状态: {normalized_status}")
        assignments.append("`status` = %s")
        values.append(normalized_status)
    if published_item_id is not None:
        assignments.append("`published_item_id` = %s")
        values.append(str(published_item_id or "")[:64])
    if failure_reason is not None:
        assignments.append("`failure_reason` = %s")
        values.append(str(failure_reason or "")[:4000])
    if result is not None:
        assignments.append("`result_json` = %s")
        values.append(_dumps(result))
    if started:
        assignments.append("`started_at` = COALESCE(`started_at`, %s)")
        values.append(_now())
    if finished:
        assignments.append("`finished_at` = %s")
        values.append(_now())
    if not assignments:
        return

    values.append(int(record_id))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"UPDATE `{PUBLISH_RECORD_TABLE}` SET {', '.join(assignments)} WHERE `id` = %s",
                tuple(values),
            )
            if int(cursor.rowcount or 0) == 0:
                cursor.execute(
                    f"SELECT 1 FROM `{PUBLISH_RECORD_TABLE}` WHERE `id` = %s",
                    (int(record_id),),
                )
                if not cursor.fetchone():
                    raise KeyError("产品上架记录不存在")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_product_publish_records(
    *,
    search: str = "",
    status: str = "",
    store_name: str = "",
    site_id: str = "",
    limit: int = 500,
    offset: int = 0,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    base_where: list[str] = []
    base_params: list[Any] = []
    search = str(search or "").strip()
    if search:
        pattern = f"%{search}%"
        base_where.append(
            "(`source_item_id` LIKE %s OR `title` LIKE %s OR "
            "`published_item_id` LIKE %s OR `batch_id` LIKE %s)"
        )
        base_params.extend((pattern, pattern, pattern, pattern))
    store_name = str(store_name or "").strip()
    if store_name:
        base_where.append("`store_name` LIKE %s")
        base_params.append(f"%{store_name}%")
    site_id = str(site_id or "").strip().upper()
    if site_id:
        base_where.append("`site_id` = %s")
        base_params.append(site_id)
    where = list(base_where)
    params = list(base_params)
    status = str(status or "").strip().lower()
    if status:
        if status not in PRODUCT_PUBLISH_RECORD_STATUSES:
            raise ValueError(f"不支持的上架记录状态: {status}")
        where.append("`status` = %s")
        params.append(status)
    base_where_sql = f"WHERE {' AND '.join(base_where)}" if base_where else ""
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT `status`, COUNT(*) AS total FROM `{PUBLISH_RECORD_TABLE}` "
                f"{base_where_sql} GROUP BY `status`",
                tuple(base_params),
            )
            counts = {key: 0 for key in PRODUCT_PUBLISH_RECORD_STATUSES}
            for count_row in cursor.fetchall():
                count_status = str(count_row.get("status") or "")
                if count_status in counts:
                    counts[count_status] = int(count_row.get("total") or 0)
            counts["all"] = sum(counts.values())
            cursor.execute(
                f"SELECT COUNT(*) AS total FROM `{PUBLISH_RECORD_TABLE}` {where_sql}",
                tuple(params),
            )
            total = int((cursor.fetchone() or {}).get("total") or 0)
            cursor.execute(
                f"SELECT * FROM `{PUBLISH_RECORD_TABLE}` {where_sql} "
                "ORDER BY `id` DESC LIMIT %s OFFSET %s",
                tuple(params + [limit, offset]),
            )
            rows = [_json_safe_row(row) for row in cursor.fetchall()]
        connection.commit()
        return {"total": total, "counts": counts, "rows": rows}
    finally:
        connection.close()


def get_product_publish_records_by_ids(
    record_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    ids: list[int] = []
    for value in record_ids or []:
        try:
            record_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"上架记录编号无效: {value!r}") from exc
        if record_id > 0 and record_id not in ids:
            ids.append(record_id)
    if not ids:
        raise ValueError("请至少勾选一条可重新上架的记录")
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{PUBLISH_RECORD_TABLE}` "
                f"WHERE `id` IN ({placeholders}) ORDER BY `id` DESC",
                tuple(ids),
            )
            rows = [_json_safe_row(row) for row in cursor.fetchall()]
        connection.commit()
        return rows
    finally:
        connection.close()


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


def update_product_item(
    product_item_id: int,
    changes: Mapping[str, Any],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Update user-editable product content and invalidate derived costs."""

    try:
        row_id = int(product_item_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("产品记录编号无效") from exc
    if row_id <= 0:
        raise ValueError("产品记录编号无效")
    if not isinstance(changes, Mapping):
        raise ValueError("产品内容必须是对象")

    normalized: dict[str, Any] = {}
    text_rules = {
        "title": ("标题", 255, False),
        "description_text": ("产品描述", 60000, True),
        "main_image_url": ("主图链接", 1500, False),
        "category_id": ("分类编号", 64, True),
    }
    for field, (label, max_length, allow_empty) in text_rules.items():
        if field not in changes:
            continue
        value = str(changes.get(field) or "").strip()
        if not allow_empty and not value:
            raise ValueError(f"{label}不能为空")
        if len(value) > max_length:
            raise ValueError(f"{label}不能超过 {max_length} 个字符")
        if field == "main_image_url" and not value.lower().startswith(("http://", "https://")):
            raise ValueError("主图链接必须以 http:// 或 https:// 开头")
        normalized[field] = value

    numeric_rules = {
        "price": "原价",
        "weight_g": "实际重量",
        "package_length_cm": "包装长度",
        "package_width_cm": "包装宽度",
        "package_height_cm": "包装高度",
    }
    for field, label in numeric_rules.items():
        if field not in changes:
            continue
        raw_value = changes.get(field)
        try:
            value = Decimal(str(raw_value))
        except Exception as exc:
            raise ValueError(f"{label}必须是数字") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{label}必须大于 0")
        normalized[field] = value

    if not normalized:
        raise ValueError("没有可保存的产品内容")

    assignments = [f"`{field}` = %s" for field in normalized]
    values = list(normalized.values())
    metric_fields = set(numeric_rules) | {"category_id"}
    profitability_stale = bool(metric_fields.intersection(normalized))
    if "weight_g" in normalized:
        assignments.append("`weight_basis` = 'manual_edit'")
    if set(normalized).intersection({
        "package_length_cm", "package_width_cm", "package_height_cm",
    }):
        assignments.append(
            "`volumetric_weight_kg` = CASE "
            "WHEN `package_length_cm` > 0 AND `package_width_cm` > 0 "
            "AND `package_height_cm` > 0 THEN ROUND("
            "`package_length_cm` * `package_width_cm` * `package_height_cm` / 6000, 4) "
            "ELSE NULL END"
        )
    if profitability_stale:
        assignments.extend((
            "`sale_price_usd` = NULL",
            "`commission_amount_local` = NULL",
            "`commission_amount_usd` = NULL",
            "`shipping_fee_local` = NULL",
            "`shipping_fee_usd` = NULL",
            "`billable_weight_g` = NULL",
            "`shipping_api_billable_weight_g` = NULL",
            "`net_proceeds_usd` = NULL",
            "`profitability_updated_at` = NULL",
            "`profitability_source` = 'manual_edit_pending'",
            "`profitability_error` = ''",
        ))
        if "category_id" in normalized:
            assignments.extend((
                "`category_name` = NULL",
                "`commission_rate` = NULL",
            ))

    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` SET {', '.join(assignments)} WHERE `id` = %s",
                tuple(values + [row_id]),
            )
            changed = int(cursor.rowcount or 0)
            if changed == 0:
                cursor.execute(
                    f"SELECT 1 FROM `{PRODUCT_TABLE}` WHERE `id` = %s",
                    (row_id,),
                )
                if not cursor.fetchone():
                    raise KeyError("产品记录不存在")

            # The daily profitability worker reads collection rows, so mirror
            # edited pricing/weight data back there and mark the snapshot stale.
            mirrored_fields = [
                field for field in normalized
                if field in {
                    "title", "main_image_url", "price", "weight_g", "category_id",
                    "package_length_cm", "package_width_cm", "package_height_cm",
                }
            ]
            if mirrored_fields:
                collection_assignments = [
                    f"c.`{field}` = p.`{field}`" for field in mirrored_fields
                ]
                if "weight_g" in normalized:
                    collection_assignments.append("c.`weight_basis` = 'manual_edit'")
                if set(normalized).intersection({
                    "package_length_cm", "package_width_cm", "package_height_cm",
                }):
                    collection_assignments.append(
                        "c.`volumetric_weight_kg` = p.`volumetric_weight_kg`"
                    )
                if profitability_stale:
                    collection_assignments.extend((
                        "c.`sale_price_usd` = NULL",
                        "c.`commission_amount_local` = NULL",
                        "c.`commission_amount_usd` = NULL",
                        "c.`shipping_fee_local` = NULL",
                        "c.`shipping_fee_usd` = NULL",
                        "c.`billable_weight_g` = NULL",
                        "c.`shipping_api_billable_weight_g` = NULL",
                        "c.`net_proceeds_usd` = NULL",
                        "c.`profitability_updated_at` = NULL",
                        "c.`profitability_source` = 'manual_edit_pending'",
                        "c.`profitability_error` = ''",
                    ))
                    if "category_id" in normalized:
                        collection_assignments.extend((
                            "c.`category_name` = NULL",
                            "c.`commission_rate` = NULL",
                        ))
                cursor.execute(
                    f"UPDATE `{COLLECTION_TABLE}` AS c "
                    f"INNER JOIN `{PRODUCT_TABLE}` AS p "
                    "ON c.`source_item_id` = p.`source_item_id` "
                    f"SET {', '.join(collection_assignments)} WHERE p.`id` = %s",
                    (row_id,),
                )
        connection.commit()
        return {
            "product_item_id": row_id,
            "changed": changed,
            "profitability_refresh_pending": profitability_stale,
        }
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


def move_product_items_to_collection(
    product_item_ids: Iterable[int],
    *,
    reason: str = "不可上架",
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Move products out of the product list and restore/create collection rows."""

    ids = _normalize_row_ids(product_item_ids, empty_message="请至少勾选一个产品")
    placeholders = ", ".join(["%s"] * len(ids))
    reason_text = str(reason or "不可上架").strip()[:1000]
    connection = (connection_factory or _connect)()
    moved = 0
    created = 0
    deleted = 0
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` IN ({placeholders})",
                tuple(ids),
            )
            product_rows = [dict(row) for row in cursor.fetchall()]
            profitability_columns_sql = ", ".join(
                f"`{column}`" for column in PROFITABILITY_COLUMNS
            )
            for row in product_rows:
                cursor.execute(
                    f"SELECT `id` FROM `{COLLECTION_TABLE}` "
                    "WHERE `id` = %s OR `source_item_id` = %s "
                    "ORDER BY (`id` = %s) DESC, `id` DESC LIMIT 1",
                    (
                        int(row.get("collection_item_id") or 0),
                        str(row.get("source_item_id") or ""),
                        int(row.get("collection_item_id") or 0),
                    ),
                )
                existing = cursor.fetchone() or {}
                if existing.get("id"):
                    cursor.execute(
                        f"""
                        UPDATE `{COLLECTION_TABLE}`
                        SET `main_image_url` = COALESCE(NULLIF(%s, ''), `main_image_url`),
                            `title` = COALESCE(NULLIF(%s, ''), `title`),
                            `price` = COALESCE(%s, `price`),
                            `currency_id` = COALESCE(NULLIF(%s, ''), `currency_id`),
                            `weight_g` = COALESCE(%s, `weight_g`),
                            `volumetric_weight_kg` = COALESCE(%s, `volumetric_weight_kg`),
                            `package_length_cm` = COALESCE(%s, `package_length_cm`),
                            `package_width_cm` = COALESCE(%s, `package_width_cm`),
                            `package_height_cm` = COALESCE(%s, `package_height_cm`),
                            `weight_basis` = COALESCE(NULLIF(%s, ''), `weight_basis`),
                            `added_to_products` = 0,
                            `error_message` = %s
                        WHERE `id` = %s
                        """,
                        (
                            str(row.get("main_image_url") or ""),
                            str(row.get("title") or ""),
                            row.get("price"),
                            str(row.get("currency_id") or ""),
                            row.get("weight_g"),
                            row.get("volumetric_weight_kg"),
                            row.get("package_length_cm"),
                            row.get("package_width_cm"),
                            row.get("package_height_cm"),
                            str(row.get("weight_basis") or ""),
                            f"产品列表自动移回：{reason_text}",
                            int(existing["id"]),
                        ),
                    )
                else:
                    snapshot = _loads(row.get("source_snapshot_json"), {})
                    values = (
                        0,
                        str(row.get("source_item_id") or ""),
                        str(row.get("source_url") or ""),
                        str(row.get("source_url") or ""),
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
                        "partial",
                        f"产品列表自动移回：{reason_text}",
                        _dumps(snapshot.get("source") or {}),
                        _dumps(snapshot.get("description") or {}),
                        _dumps(snapshot.get("page_snapshot") or {}),
                        _dumps(snapshot.get("plugin_snapshot") or {}),
                        _now(),
                    )
                    cursor.execute(
                        f"""
                        INSERT INTO `{COLLECTION_TABLE}` (
                            `task_id`, `source_item_id`, `source_url`, `final_url`,
                            `main_image_url`, `title`, `price`, `currency_id`,
                            `weight_g`, `volumetric_weight_kg`, `package_length_cm`,
                            `package_width_cm`, `package_height_cm`, `weight_basis`,
                            {profitability_columns_sql}, `scrape_status`, `error_message`,
                            `source_json`, `description_json`, `page_snapshot_json`,
                            `plugin_snapshot_json`, `collected_at`
                        ) VALUES ({", ".join(["%s"] * len(values))})
                        ON DUPLICATE KEY UPDATE
                            `added_to_products` = 0,
                            `error_message` = VALUES(`error_message`),
                            `updated_at` = CURRENT_TIMESTAMP
                        """,
                        values,
                    )
                    created += 1
                moved += 1
            cursor.execute(
                f"DELETE FROM `{PRODUCT_TABLE}` WHERE `id` IN ({placeholders})",
                tuple(ids),
            )
            deleted = int(cursor.rowcount or 0)
        connection.commit()
        return {
            "requested": len(ids),
            "moved": moved,
            "created_collection_rows": created,
            "deleted": deleted,
        }
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
    """Return collected and pulled rows whose cost snapshot needs refreshing."""

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
            remaining = limit - len(rows)
            if remaining > 0:
                cursor.execute(
                    f"""
                    SELECT * FROM `{PRODUCT_TABLE}`
                    WHERE `source_type` = 'pulled'
                      AND (`profitability_updated_at` IS NULL
                           OR `profitability_updated_at` < %s)
                      AND `price` IS NOT NULL
                      AND `price` > 0
                      AND `title` IS NOT NULL
                      AND `title` <> ''
                      AND `weight_g` IS NOT NULL
                      AND `weight_g` > 0
                    ORDER BY COALESCE(`profitability_updated_at`, '1970-01-01') ASC,
                             `id` ASC
                    LIMIT %s
                    """,
                    (stale_before, remaining),
                )
                rows.extend(_json_safe_row(row) for row in cursor.fetchall())
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


def backfill_item_exchange_prices(
    exchange_rates: Mapping[str, Any],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Fill local-to-USD rates and USD sale prices independently of fee quotes."""

    normalized: dict[str, tuple[Decimal, str]] = {
        "USD": (Decimal("1"), _now()),
    }
    for raw_currency, raw_snapshot in dict(exchange_rates or {}).items():
        currency_id = str(raw_currency or "").strip().upper()
        if not currency_id:
            continue
        snapshot = raw_snapshot if isinstance(raw_snapshot, Mapping) else {}
        raw_rate = snapshot.get("ratio") if snapshot else raw_snapshot
        try:
            rate = Decimal(str(raw_rate))
        except Exception as exc:
            raise ValueError(f"{currency_id} 到 USD 的汇率不是有效数字") from exc
        if not rate.is_finite() or rate <= 0:
            raise ValueError(f"{currency_id} 到 USD 的汇率必须大于 0")
        updated_at = str(
            snapshot.get("creation_date")
            or snapshot.get("refreshed_at")
            or _now()
        )[:64]
        normalized[currency_id] = (rate, updated_at)

    connection = (connection_factory or _connect)()
    updated = 0
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            for table in (COLLECTION_TABLE, PRODUCT_TABLE):
                for currency_id, (rate, updated_at) in normalized.items():
                    cursor.execute(
                        f"""
                        UPDATE `{table}`
                        SET `sale_price_usd` = ROUND(`price` * %s, 2),
                            `exchange_rate_to_usd` = %s,
                            `exchange_rate_updated_at` = %s
                        WHERE UPPER(COALESCE(`currency_id`, '')) = %s
                          AND `price` IS NOT NULL
                          AND `price` > 0
                        """,
                        (rate, rate, updated_at, currency_id),
                    )
                    updated += max(0, int(cursor.rowcount or 0))
        connection.commit()
        return {
            "updated": updated,
            "currencies": sorted(normalized),
        }
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
            incomplete_rows: list[dict[str, Any]] = []
            complete_rows: list[dict[str, Any]] = []
            for row in selected_rows:
                try:
                    weight = Decimal(str(row.get("weight_g")))
                except Exception:
                    weight = None
                if weight is None or not weight.is_finite() or weight <= 0:
                    incomplete_rows.append(row)
                else:
                    complete_rows.append(row)
            selected_rows = complete_rows
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
                description_text = str(
                    (snapshot.get("description") or {}).get("plain_text")
                    or (snapshot.get("description") or {}).get("text")
                    or ""
                ).strip()
                values = (
                    row["id"], "collected", "unreviewed",
                    row["source_item_id"], row["source_url"],
                    row.get("main_image_url"), row.get("title"), description_text,
                    row.get("price"),
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
                        `main_image_url`, `title`, `description_text`, `price`,
                        `currency_id`, `weight_g`,
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
                        `description_text` = VALUES(`description_text`),
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
            complete_ids = [int(row["id"]) for row in complete_rows]
            if complete_ids:
                complete_placeholders = ", ".join(["%s"] * len(complete_ids))
                cursor.execute(
                    f"UPDATE `{COLLECTION_TABLE}` SET `added_to_products` = 1 "
                    f"WHERE `id` IN ({complete_placeholders})",
                    tuple(complete_ids),
                )
            incomplete_ids = [int(row["id"]) for row in incomplete_rows]
            if incomplete_ids:
                incomplete_placeholders = ", ".join(["%s"] * len(incomplete_ids))
                cursor.execute(
                    f"UPDATE `{COLLECTION_TABLE}` SET `added_to_products` = 0 "
                    f"WHERE `id` IN ({incomplete_placeholders})",
                    tuple(incomplete_ids),
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
        "requested": len(ids),
        "skipped_incomplete": len(incomplete_rows),
        "skipped_incomplete_item_ids": [
            str(row.get("source_item_id") or "") for row in incomplete_rows
        ],
        "mirrored": mirrored,
        "mirror_errors": mirror_errors[:10],
    }
