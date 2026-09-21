"""MySQL persistence for workbench Mercado Libre collection and product lists."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping


TASK_TABLE = "erp_mercadolibre_collection_tasks"
COLLECTION_TABLE = "erp_mercadolibre_collection_items"
PRODUCT_TABLE = "erp_mercadolibre_products"
PUBLISH_RECORD_TABLE = "erp_mercadolibre_publish_records"
MANAGEMENT_CATEGORY_TABLE = "erp_mercadolibre_management_categories"
EXCHANGE_RATE_TABLE = "erp_mercadolibre_exchange_rates"

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
PRODUCT_SOURCE_TYPES = {"collected", "pulled", "zying", "ai_original"}
PRODUCT_REVIEW_STATUSES = {
    "unreviewed", "approved", "suspected", "infringing", "risk",
}
PRODUCT_PUBLISH_RECORD_STATUSES = {"pending", "publishing", "published", "failed"}
PRODUCT_PUBLISH_RETRYABLE_STATUSES = {"pending", "publishing", "failed"}
PRODUCT_PUBLISH_FILTER_STATUSES = PRODUCT_PUBLISH_RECORD_STATUSES | {"unpublished"}
ZYING_PROFITABILITY_SOURCE = "zying_collection"
COLLECTION_WORKFLOW_COLUMN_DEFINITIONS = (
    ("review_status", "VARCHAR(32) NOT NULL DEFAULT 'unreviewed' AFTER `added_to_products`"),
    ("last_publish_status", "VARCHAR(32) NULL AFTER `review_status`"),
)
PRODUCT_WORKFLOW_COLUMN_DEFINITIONS = (
    ("source_type", "VARCHAR(32) NOT NULL DEFAULT 'collected' AFTER `collection_item_id`"),
    ("review_status", "VARCHAR(32) NOT NULL DEFAULT 'unreviewed' AFTER `source_type`"),
    ("description_text", "LONGTEXT NULL AFTER `title`"),
    ("product_developer_id", "VARCHAR(64) NULL AFTER `source_type`"),
    ("product_developer_name", "VARCHAR(255) NULL AFTER `product_developer_id`"),
)
INFRINGEMENT_RESULT_COLUMN_DEFINITIONS = (
    ("infringement_risk_level", "TINYINT NULL"),
    ("infringement_keywords", "VARCHAR(1024) NULL"),
    ("infringement_reason", "VARCHAR(1000) NULL"),
    ("infringement_checked_at", "DATETIME NULL"),
)

_schema_lock = threading.Lock()
_schema_ready = False
PROFITABILITY_INVALIDATION_BATCH_SIZE = 1000
RETRYABLE_TRANSACTION_ERROR_CODES = {1205, 1213}


def _connect() -> Any:
    from bit.bit_mysql import config, pymysql

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


def _is_retryable_transaction_error(exc: BaseException) -> bool:
    """Return whether MySQL rolled back/waited out a transient transaction."""

    code = exc.args[0] if getattr(exc, "args", ()) else None
    try:
        return int(code) in RETRYABLE_TRANSACTION_ERROR_CODES
    except (TypeError, ValueError):
        return False


def _decimal_from_text(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        number = Decimal(match.group(0).replace(",", "."))
    except Exception:
        return None
    return number if number.is_finite() else None


def has_complete_weight_dimensions(row: Mapping[str, Any]) -> bool:
    """Return whether all four publish/shipping measurements are usable.

    ``scrape_status`` is historical workflow state and can become stale after a
    product is edited or moved between lists.  Weight completeness must always
    be derived from the current values instead of that status flag.
    """

    if str(row.get("weight_basis") or "").strip().lower() in {
        "calculated_volumetric",
        "legacy_unknown",
        "plugin_volumetric_fallback",
    }:
        return False
    for key in (
        "weight_g",
        "package_length_cm",
        "package_width_cm",
        "package_height_cm",
    ):
        value = _decimal_from_text(row.get(key))
        if value is None or value <= 0:
            return False
    return True


def has_valid_actual_weight(row: Mapping[str, Any]) -> bool:
    """Return whether ``weight_g`` is a positive, genuine actual weight."""

    if str(row.get("weight_basis") or "").strip().lower() in {
        "calculated_volumetric",
        "legacy_unknown",
        "plugin_volumetric_fallback",
    }:
        return False
    value = _decimal_from_text(row.get("weight_g"))
    return value is not None and value > 0


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
    result["actual_weight_complete"] = has_valid_actual_weight(result)
    result["weight_dimensions_complete"] = has_complete_weight_dimensions(result)
    return result


def _mirror_zying_snapshot_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """Expose workflow metadata stored in the source snapshot as list fields."""
    result = dict(row)
    source_type = str(result.get("source_type") or "").strip().lower()
    if source_type == "ai_original":
        snapshot = _loads(result.get("source_snapshot_json"), {})
        original = snapshot.get("original_1688") if isinstance(snapshot, dict) else {}
        prepared = snapshot.get("ai_original") if isinstance(snapshot, dict) else {}
        result["original_1688"] = original if isinstance(original, dict) else {}
        result["ai_original"] = prepared if isinstance(prepared, dict) else {}
        result["ai_status"] = str(result["ai_original"].get("status") or "pending")
        result["ai_error"] = str(result["ai_original"].get("error") or "")
        result["title_es"] = str(result["ai_original"].get("title_es") or "")
        result["title_pt"] = str(result["ai_original"].get("title_pt") or "")
        result["description_es"] = str(result["ai_original"].get("description_es") or "")
        result["description_pt"] = str(result["ai_original"].get("description_pt") or "")
        return result
    if source_type != "zying":
        return result
    snapshot = _loads(result.get("source_snapshot_json"), {})
    plugin_snapshot = (
        snapshot.get("plugin_snapshot") if isinstance(snapshot, dict) else {}
    )
    if not isinstance(plugin_snapshot, dict):
        plugin_snapshot = {}
    for field in (
        "zying_category_id",
        "zying_category",
        "product_developer_id",
        "product_developer_name",
        "zying_status",
    ):
        if (
            result.get(field) in (None, "")
            and plugin_snapshot.get(field) not in (None, "")
        ):
            result[field] = plugin_snapshot.get(field)
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


def _migrate_collection_tables(cursor: Any) -> None:
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
            `management_category_id` BIGINT NULL,
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
            `review_status` VARCHAR(32) NOT NULL DEFAULT 'unreviewed',
            `last_publish_status` VARCHAR(32) NULL,
            `collected_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_collection_item` (`task_id`, `source_item_id`),
            KEY `idx_erp_meli_collection_task` (`task_id`, `collected_at`),
            KEY `idx_erp_meli_collection_status` (`scrape_status`, `collected_at`),
            KEY `idx_erp_meli_collection_management_category` (`management_category_id`, `id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{PRODUCT_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `collection_item_id` BIGINT NOT NULL,
            `source_type` VARCHAR(32) NOT NULL DEFAULT 'collected',
            `product_developer_id` VARCHAR(64) NULL,
            `product_developer_name` VARCHAR(255) NULL,
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
            `management_category_id` BIGINT NULL,
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
            KEY `idx_erp_meli_product_review` (`review_status`, `id`),
            KEY `idx_erp_meli_product_management_category` (`management_category_id`, `id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{MANAGEMENT_CATEGORY_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `name` VARCHAR(64) NOT NULL,
            `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_management_category_name` (`name`)
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
    for column, definition in COLLECTION_WORKFLOW_COLUMN_DEFINITIONS:
        _ensure_column(cursor, COLLECTION_TABLE, column, definition)
    for column, definition in PRODUCT_WORKFLOW_COLUMN_DEFINITIONS:
        _ensure_column(cursor, PRODUCT_TABLE, column, definition)
    for table in (COLLECTION_TABLE, PRODUCT_TABLE):
        for column, definition in INFRINGEMENT_RESULT_COLUMN_DEFINITIONS:
            _ensure_column(cursor, table, column, definition)
        _ensure_column(
            cursor,
            table,
            "management_category_id",
            "BIGINT NULL AFTER `category_name`",
        )
    cursor.execute("SHOW TABLES LIKE 'infringement_risk_checks'")
    if cursor.fetchone():
        risk_assignments = (
            "target.`infringement_risk_level` = risks.`risk_level`, "
            "target.`infringement_keywords` = risks.`keywords`, "
            "target.`infringement_reason` = risks.`reason`, "
            "target.`infringement_checked_at` = risks.`checked_at`"
        )
        cursor.execute(
            f"UPDATE `{COLLECTION_TABLE}` AS target "
            "INNER JOIN `infringement_risk_checks` AS risks "
            "ON risks.`source_type` = 'collection_list' "
            "AND risks.`source_row_id` = target.`id` "
            f"SET {risk_assignments}"
        )
        cursor.execute(
            f"UPDATE `{PRODUCT_TABLE}` AS target "
            "INNER JOIN `infringement_risk_checks` AS risks "
            "ON risks.`source_type` = 'product_list' "
            "AND risks.`source_row_id` = target.`id` "
            f"SET {risk_assignments}"
        )
        for source_type in ("pulled", "zying"):
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` AS target "
                "INNER JOIN `infringement_risk_checks` AS risks "
                f"ON risks.`source_type` = '{source_type}' "
                "AND risks.`product_id` = target.`source_item_id` "
                f"SET {risk_assignments} "
                "WHERE target.`source_type` = %s",
                (source_type,),
            )
        # Older deployments may already have moved a checked collection item
        # into products. Backfill and re-key those decisions during migration.
        cursor.execute(
            f"UPDATE `{PRODUCT_TABLE}` AS target "
            "INNER JOIN `infringement_risk_checks` AS risks "
            "ON risks.`source_type` = 'collection_list' "
            "AND risks.`source_row_id` = target.`collection_item_id` "
            f"SET {risk_assignments} "
            "WHERE target.`source_type` = 'collected'"
        )
        cursor.execute(
            f"""
            INSERT INTO `infringement_risk_checks` (
                `source_type`, `source_row_id`, `product_id`, `title`,
                `main_image_url`, `product_category`, `zying_category_id`,
                `zying_category`, `salesperson`, `group_name`, `token_id`,
                `account_name`, `risk_level`, `keywords`, `reason`, `checked_at`
            )
            SELECT 'product_list', products.`id`, products.`source_item_id`,
                   products.`title`, products.`main_image_url`, products.`category_name`,
                   '', '', '', '', NULL, '', risks.`risk_level`, risks.`keywords`,
                   risks.`reason`, risks.`checked_at`
            FROM `{PRODUCT_TABLE}` AS products
            INNER JOIN `infringement_risk_checks` AS risks
              ON risks.`source_type` = 'collection_list'
             AND risks.`source_row_id` = products.`collection_item_id`
            WHERE products.`source_type` = 'collected'
            ON DUPLICATE KEY UPDATE
                `risk_level` = VALUES(`risk_level`), `keywords` = VALUES(`keywords`),
                `reason` = VALUES(`reason`), `checked_at` = VALUES(`checked_at`)
            """
        )
        cursor.execute(
            f"DELETE risks FROM `infringement_risk_checks` AS risks "
            f"INNER JOIN `{PRODUCT_TABLE}` AS products "
            "ON products.`collection_item_id` = risks.`source_row_id` "
            "WHERE risks.`source_type` = 'collection_list' "
            "AND products.`source_type` = 'collected'"
        )
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
        cursor, COLLECTION_TABLE, "idx_erp_meli_collection_review", "(`review_status`, `id`)"
    )
    _ensure_index(
        cursor, COLLECTION_TABLE, "idx_erp_meli_collection_publish", "(`last_publish_status`, `id`)"
    )
    for table in (COLLECTION_TABLE, PRODUCT_TABLE):
        _ensure_index(
            cursor, table, "idx_erp_meli_profit_refresh", "(`profitability_updated_at`, `id`)"
        )
    _ensure_index(
        cursor, PRODUCT_TABLE, "idx_erp_meli_product_publish", "(`last_publish_status`, `id`)"
    )
    _ensure_index(cursor, PRODUCT_TABLE, "idx_erp_meli_product_weight", "(`weight_g`)")
    _ensure_index(cursor, PRODUCT_TABLE, "idx_erp_meli_product_price", "(`price`)")
    _ensure_index(
        cursor, PRODUCT_TABLE, "idx_erp_meli_product_net", "(`net_proceeds_usd`)"
    )
    _ensure_index(
        cursor,
        COLLECTION_TABLE,
        "idx_erp_meli_collection_management_category",
        "(`management_category_id`, `id`)",
    )
    _ensure_index(
        cursor,
        PRODUCT_TABLE,
        "idx_erp_meli_product_management_category",
        "(`management_category_id`, `id`)",
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


def _collection_schema_is_current(cursor: Any) -> bool:
    required = {
        (TASK_TABLE, "worker_count"),
        (TASK_TABLE, "elapsed_seconds"),
        (COLLECTION_TABLE, "volumetric_weight_kg"),
        (COLLECTION_TABLE, "profitability_error"),
        (COLLECTION_TABLE, "added_to_products"),
        (COLLECTION_TABLE, "review_status"),
        (COLLECTION_TABLE, "last_publish_status"),
        (COLLECTION_TABLE, "management_category_id"),
        (COLLECTION_TABLE, "infringement_risk_level"),
        (COLLECTION_TABLE, "infringement_keywords"),
        (COLLECTION_TABLE, "infringement_reason"),
        (COLLECTION_TABLE, "infringement_checked_at"),
        (PRODUCT_TABLE, "source_type"),
        (PRODUCT_TABLE, "review_status"),
        (PRODUCT_TABLE, "description_text"),
        (PRODUCT_TABLE, "product_developer_id"),
        (PRODUCT_TABLE, "product_developer_name"),
        (PRODUCT_TABLE, "profitability_error"),
        (PRODUCT_TABLE, "last_published_at"),
        (PRODUCT_TABLE, "management_category_id"),
        (PRODUCT_TABLE, "infringement_risk_level"),
        (PRODUCT_TABLE, "infringement_keywords"),
        (PRODUCT_TABLE, "infringement_reason"),
        (PRODUCT_TABLE, "infringement_checked_at"),
        (MANAGEMENT_CATEGORY_TABLE, "name"),
        (PUBLISH_RECORD_TABLE, "failure_reason"),
        (PUBLISH_RECORD_TABLE, "result_json"),
    }
    cursor.execute(
        """
        SELECT `TABLE_NAME`, `COLUMN_NAME`
        FROM `information_schema`.`COLUMNS`
        WHERE `TABLE_SCHEMA` = DATABASE()
          AND `TABLE_NAME` IN (%s, %s, %s, %s, %s)
        """,
        (
            TASK_TABLE,
            COLLECTION_TABLE,
            PRODUCT_TABLE,
            PUBLISH_RECORD_TABLE,
            MANAGEMENT_CATEGORY_TABLE,
        ),
    )
    existing = {
        (str(row.get("TABLE_NAME") or row.get("Table_name") or ""),
         str(row.get("COLUMN_NAME") or row.get("Column_name") or ""))
        for row in cursor.fetchall() or []
        if isinstance(row, Mapping)
    }
    return required.issubset(existing)


def ensure_collection_tables(cursor: Any) -> None:
    """Run migrations once, avoiding repeated DDL on the large product table."""

    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        if not _collection_schema_is_current(cursor):
            _migrate_collection_tables(cursor)
        else:
            # Column-only schema checks must not skip a newly added queue index.
            for table in (COLLECTION_TABLE, PRODUCT_TABLE):
                _ensure_index(
                    cursor, table, "idx_erp_meli_profit_refresh",
                    "(`profitability_updated_at`, `id`)",
                )
        _schema_ready = True


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
            # USD display price is cheap reference-data work. Resolve it from
            # the one stored rate per currency while the batch is being saved,
            # instead of leaving it behind slower category/commission API work.
            fixed_exchange_rates: dict[str, Mapping[str, Any]] = {}
            try:
                cursor.execute(
                    f"""
                    SELECT `from_currency_id`, `rate`, `source_created_at`,
                           `refreshed_at`
                    FROM `{EXCHANGE_RATE_TABLE}`
                    WHERE `to_currency_id` = 'USD'
                    """
                )
                fixed_exchange_rates = {
                    str(rate_row.get("from_currency_id") or "").upper(): rate_row
                    for rate_row in (cursor.fetchall() or [])
                    if isinstance(rate_row, Mapping)
                }
            except Exception:
                # A brand-new installation can receive its first collection
                # before the background worker creates the rate table. Keep
                # collection available; that worker will bootstrap and backfill.
                fixed_exchange_rates = {}

            for row in records:
                currency_id = str(row.get("currency_id") or "").strip().upper()
                rate_row: Mapping[str, Any]
                if currency_id == "USD":
                    rate_row = {
                        "rate": Decimal("1"),
                        "source_created_at": _now(),
                    }
                else:
                    rate_row = fixed_exchange_rates.get(currency_id) or {}
                try:
                    price = Decimal(str(row.get("price")))
                    rate = Decimal(str(rate_row.get("rate")))
                except Exception:
                    continue
                if not price.is_finite() or price <= 0 or not rate.is_finite() or rate <= 0:
                    continue
                row["sale_price_usd"] = (price * rate).quantize(Decimal("0.01"))
                row["exchange_rate_to_usd"] = rate
                row["exchange_rate_updated_at"] = str(
                    rate_row.get("source_created_at")
                    or rate_row.get("refreshed_at")
                    or _now()
                )[:64]

            profitability_columns_sql = ", ".join(
                f"`{column}`" for column in PROFITABILITY_COLUMNS
            )
            protect_existing_complete_sql = (
                "`scrape_status` = 'ok' "
                "AND VALUES(`scrape_status`) <> 'ok' "
                "AND (VALUES(`weight_g`) IS NULL OR VALUES(`weight_g`) <= 0 "
                "OR LOWER(COALESCE(VALUES(`weight_basis`), '')) IN ("
                "'calculated_volumetric', 'legacy_unknown', "
                "'plugin_volumetric_fallback'))"
            )
            for row in records:
                replace_profitability = bool(
                    row.pop("_replace_profitability_snapshot", False)
                )
                profitability_updates_sql = ",\n                        ".join(
                    (
                        f"`{column}` = IF("
                        f"{protect_existing_complete_sql}, "
                        f"`{column}`, "
                        + (
                            f"VALUES(`{column}`)"
                            if replace_profitability and column not in {
                                "category_id",
                                "category_name",
                                "listing_type_id",
                                "listing_type_name",
                            }
                            else f"COALESCE(VALUES(`{column}`), `{column}`)"
                        )
                        + ")"
                    )
                    for column in PROFITABILITY_COLUMNS
                )
                if has_complete_weight_dimensions(row):
                    # A previous failed/partial pass must not keep a complete
                    # item permanently labelled as waiting for measurements.
                    row["scrape_status"] = "ok"
                elif not has_valid_actual_weight(row) and str(
                    row.get("scrape_status") or ""
                ).lower() == "ok":
                    # Never let a stale workflow flag turn volumetric fallback
                    # data (or zero/missing weight) into a publishable weight.
                    row["scrape_status"] = "partial"
                values = (
                    int(task_id),
                    str(row.get("source_item_id") or row.get("item_id") or "")[:64],
                    str(row.get("source_url") or "")[:1500],
                    str(row.get("final_url") or "")[:1500],
                    str(row.get("main_image_url") or "")[:1500],
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
                            {protect_existing_complete_sql},
                            `task_id`, VALUES(`task_id`)
                        ),
                        `source_url` = COALESCE(NULLIF(VALUES(`source_url`), ''), `source_url`),
                        `final_url` = COALESCE(NULLIF(VALUES(`final_url`), ''), `final_url`),
                        `main_image_url` = COALESCE(NULLIF(VALUES(`main_image_url`), ''), `main_image_url`),
                        `title` = COALESCE(NULLIF(VALUES(`title`), ''), `title`),
                        `price` = COALESCE(VALUES(`price`), `price`),
                        `currency_id` = COALESCE(NULLIF(VALUES(`currency_id`), ''), `currency_id`),
                        `weight_g` = IF(
                            {protect_existing_complete_sql}, `weight_g`, CASE
                            WHEN LOWER(COALESCE(VALUES(`weight_basis`), '')) IN (
                                'calculated_volumetric', 'legacy_unknown',
                                'plugin_volumetric_fallback'
                            ) THEN NULL
                            WHEN VALUES(`weight_g`) IS NOT NULL
                                 AND VALUES(`weight_g`) > 0
                            THEN VALUES(`weight_g`)
                            ELSE `weight_g`
                        END),
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
                        `weight_basis` = IF(
                            {protect_existing_complete_sql}, `weight_basis`, CASE
                            WHEN LOWER(COALESCE(VALUES(`weight_basis`), '')) IN (
                                'calculated_volumetric', 'legacy_unknown',
                                'plugin_volumetric_fallback'
                            ) THEN VALUES(`weight_basis`)
                            WHEN VALUES(`weight_g`) IS NOT NULL
                                 AND VALUES(`weight_g`) > 0
                            THEN COALESCE(NULLIF(VALUES(`weight_basis`), ''), `weight_basis`)
                            ELSE `weight_basis`
                        END),
                        {profitability_updates_sql},
                        `error_message` = IF(
                            {protect_existing_complete_sql},
                            `error_message`, VALUES(`error_message`)
                        ),
                        `source_json` = IF(
                            {protect_existing_complete_sql},
                            `source_json`, VALUES(`source_json`)
                        ),
                        `description_json` = IF(
                            {protect_existing_complete_sql},
                            `description_json`, VALUES(`description_json`)
                        ),
                        `page_snapshot_json` = IF(
                            {protect_existing_complete_sql},
                            `page_snapshot_json`, VALUES(`page_snapshot_json`)
                        ),
                        `plugin_snapshot_json` = IF(
                            {protect_existing_complete_sql},
                            `plugin_snapshot_json`, VALUES(`plugin_snapshot_json`)
                        ),
                        `collected_at` = IF(
                            {protect_existing_complete_sql},
                            `collected_at`, VALUES(`collected_at`)
                        ),
                        `scrape_status` = IF(
                            {protect_existing_complete_sql},
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
    weight_status: str = "",
    management_category_id: Any = None,
    mercado_category: str = "",
    zying_category: str = "",
    product_developer_id: str = "",
    weight_min: Any = None,
    weight_max: Any = None,
    price_min: Any = None,
    price_max: Any = None,
    net_proceeds_min: Any = None,
    net_proceeds_max: Any = None,
    date_from: str = "",
    date_to: str = "",
    exclude_added: bool = False,
    token_ids: Iterable[int] | None = None,
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
    category_filter = str(management_category_id or "").strip().lower()
    if category_filter in {"uncategorized", "unclassified", "none"}:
        where.append("`management_category_id` IS NULL")
    elif category_filter:
        try:
            normalized_category_id = int(category_filter)
        except (TypeError, ValueError) as exc:
            raise ValueError("运营分类编号无效") from exc
        if normalized_category_id <= 0:
            raise ValueError("运营分类编号无效")
        where.append("`management_category_id` = %s")
        params.append(normalized_category_id)
    mercado_category = str(mercado_category or "").strip()[:255]
    if mercado_category:
        where.append("(`category_id` = %s OR `category_name` LIKE %s)")
        params.extend((mercado_category, f"%{mercado_category}%"))
    if table == PRODUCT_TABLE:
        if token_ids is not None:
            scoped_token_ids = sorted({
                int(value) for value in token_ids or () if int(value or 0) > 0
            })
            if not scoped_token_ids:
                where.append("1 = 0")
            else:
                placeholders = ", ".join(["%s"] * len(scoped_token_ids))
                where.append(
                    f"(`last_publish_token_id` IN ({placeholders}) OR EXISTS ("
                    f"SELECT 1 FROM `{PUBLISH_RECORD_TABLE}` AS own_records "
                    f"WHERE own_records.`product_item_id` = `{PRODUCT_TABLE}`.`id` "
                    f"AND own_records.`token_id` IN ({placeholders})) OR EXISTS ("
                    "SELECT 1 FROM `erp_mercadolibre_store_links` AS own_links "
                    f"WHERE own_links.`item_id` = `{PRODUCT_TABLE}`.`source_item_id` "
                    f"AND own_links.`token_id` IN ({placeholders})))"
                )
                params.extend(scoped_token_ids)
                params.extend(scoped_token_ids)
                params.extend(scoped_token_ids)
        source_type = str(source_type or "").strip().lower()
        if source_type:
            if source_type not in PRODUCT_SOURCE_TYPES:
                raise ValueError(f"不支持的产品来源: {source_type}")
            where.append("`source_type` = %s")
            params.append(source_type)
        zying_category = str(zying_category or "").strip()[:255]
        if zying_category:
            zying_name_expr = (
                "JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(`source_snapshot_json`), "
                "`source_snapshot_json`, '{}'), "
                "'$.plugin_snapshot.zying_category'))"
            )
            zying_id_expr = (
                "JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(`source_snapshot_json`), "
                "`source_snapshot_json`, '{}'), "
                "'$.plugin_snapshot.zying_category_id'))"
            )
            where.append(
                f"({zying_name_expr} = %s OR {zying_name_expr} LIKE %s "
                f"OR {zying_id_expr} = %s)"
            )
            params.extend((zying_category, f"%{zying_category}%", zying_category))
        product_developer_id = str(product_developer_id or "").strip()[:64]
        if product_developer_id:
            where.append(
                "(`product_developer_id` = %s OR `product_developer_name` = %s)"
            )
            params.extend((product_developer_id, product_developer_id))
    review_status = str(review_status or "").strip().lower()
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
    weight_status = str(weight_status or "").strip().lower()
    if weight_status:
        if weight_status not in {"available", "missing"}:
            raise ValueError(f"不支持的实重状态: {weight_status}")
        usable_weight_sql = (
            "`weight_g` IS NOT NULL AND `weight_g` > 0 "
            "AND LOWER(COALESCE(`weight_basis`, '')) NOT IN ("
            "'calculated_volumetric', 'legacy_unknown', "
            "'plugin_volumetric_fallback')"
        )
        where.append(
            f"({usable_weight_sql})"
            if weight_status == "available"
            else f"NOT ({usable_weight_sql})"
        )

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
    time_column = "collected_at" if table == COLLECTION_TABLE else "added_at"
    if start_date:
        where.append(f"`{time_column}` >= %s")
        params.append(start_date.strftime("%Y-%m-%d %H:%M:%S"))
    if end_date:
        where.append(f"`{time_column}` < %s")
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
                f"SELECT `{table}`.*, category.`name` AS `management_category_name` "
                f"FROM `{table}` LEFT JOIN `{MANAGEMENT_CATEGORY_TABLE}` AS category "
                f"ON category.`id` = `{table}`.`management_category_id` "
                f"{where_sql} ORDER BY `{table}`.`id` DESC LIMIT %s OFFSET %s",
                tuple(params + [limit, offset]),
            )
            rows = [
                _mirror_zying_snapshot_fields(_json_safe_row(row))
                for row in cursor.fetchall()
            ]
        connection.commit()
        return {"total": total, "rows": rows}
    finally:
        connection.close()


def list_collection_items(**kwargs: Any) -> dict[str, Any]:
    return _list_rows(COLLECTION_TABLE, **kwargs)


def list_product_items(**kwargs: Any) -> dict[str, Any]:
    kwargs.pop("task_id", None)
    return _list_rows(PRODUCT_TABLE, **kwargs)


def _normalize_management_category_name(value: Any) -> str:
    name = re.sub(r"\s+", " ", str(value or "").strip())
    if not name:
        raise ValueError("请输入分类名称")
    if len(name) > 64:
        raise ValueError("分类名称不能超过 64 个字符")
    return name


def list_management_categories(
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Return user-managed product categories with list usage counts."""

    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"""
                SELECT category.`id`, category.`name`, category.`created_at`,
                       category.`updated_at`,
                       (SELECT COUNT(*) FROM `{COLLECTION_TABLE}` AS collection_item
                        WHERE collection_item.`management_category_id` = category.`id`)
                           AS `collection_count`,
                       (SELECT COUNT(*) FROM `{PRODUCT_TABLE}` AS product_item
                        WHERE product_item.`management_category_id` = category.`id`)
                           AS `product_count`
                FROM `{MANAGEMENT_CATEGORY_TABLE}` AS category
                ORDER BY category.`name` ASC, category.`id` ASC
                """
            )
            rows = []
            for raw_row in cursor.fetchall():
                row = _json_safe_row(raw_row)
                row.pop("added_to_products", None)
                row.pop("weight_dimensions_complete", None)
                rows.append(row)
        connection.commit()
        return {"total": len(rows), "rows": rows}
    finally:
        connection.close()


def create_management_category(
    name: str,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    normalized_name = _normalize_management_category_name(name)
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT `id` FROM `{MANAGEMENT_CATEGORY_TABLE}` WHERE `name` = %s",
                (normalized_name,),
            )
            if cursor.fetchone():
                raise ValueError("分类名称已存在")
            cursor.execute(
                f"INSERT INTO `{MANAGEMENT_CATEGORY_TABLE}` (`name`) VALUES (%s)",
                (normalized_name,),
            )
            category_id = int(cursor.lastrowid)
        connection.commit()
        return {"id": category_id, "name": normalized_name}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_management_category(
    category_id: int,
    name: str,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    try:
        normalized_id = int(category_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("运营分类编号无效") from exc
    if normalized_id <= 0:
        raise ValueError("运营分类编号无效")
    normalized_name = _normalize_management_category_name(name)
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT `id` FROM `{MANAGEMENT_CATEGORY_TABLE}` "
                "WHERE `name` = %s AND `id` <> %s",
                (normalized_name, normalized_id),
            )
            if cursor.fetchone():
                raise ValueError("分类名称已存在")
            cursor.execute(
                f"UPDATE `{MANAGEMENT_CATEGORY_TABLE}` SET `name` = %s WHERE `id` = %s",
                (normalized_name, normalized_id),
            )
            changed = int(cursor.rowcount or 0)
            if changed == 0:
                cursor.execute(
                    f"SELECT 1 FROM `{MANAGEMENT_CATEGORY_TABLE}` WHERE `id` = %s",
                    (normalized_id,),
                )
                if not cursor.fetchone():
                    raise KeyError("运营分类不存在")
        connection.commit()
        return {"id": normalized_id, "name": normalized_name, "changed": changed}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_management_category(
    category_id: int,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    try:
        normalized_id = int(category_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("运营分类编号无效") from exc
    if normalized_id <= 0:
        raise ValueError("运营分类编号无效")
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT `id` FROM `{MANAGEMENT_CATEGORY_TABLE}` WHERE `id` = %s",
                (normalized_id,),
            )
            if not cursor.fetchone():
                raise KeyError("运营分类不存在")
            cleared = 0
            for table in (COLLECTION_TABLE, PRODUCT_TABLE):
                cursor.execute(
                    f"UPDATE `{table}` SET `management_category_id` = NULL "
                    "WHERE `management_category_id` = %s",
                    (normalized_id,),
                )
                cleared += max(0, int(cursor.rowcount or 0))
            cursor.execute(
                f"DELETE FROM `{MANAGEMENT_CATEGORY_TABLE}` WHERE `id` = %s",
                (normalized_id,),
            )
        connection.commit()
        return {"id": normalized_id, "deleted": 1, "cleared_items": cleared}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def assign_management_category(
    item_type: str,
    item_ids: Iterable[int],
    category_id: int | None,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    normalized_type = str(item_type or "").strip().lower()
    table_by_type = {"collection": COLLECTION_TABLE, "products": PRODUCT_TABLE}
    if normalized_type not in table_by_type:
        raise ValueError("商品列表类型无效")
    ids = _normalize_row_ids(item_ids, empty_message="请至少勾选一个商品")
    normalized_category_id: int | None = None
    if category_id not in (None, ""):
        try:
            normalized_category_id = int(category_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("运营分类编号无效") from exc
        if normalized_category_id <= 0:
            raise ValueError("运营分类编号无效")
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            if normalized_category_id is not None:
                cursor.execute(
                    f"SELECT 1 FROM `{MANAGEMENT_CATEGORY_TABLE}` WHERE `id` = %s",
                    (normalized_category_id,),
                )
                if not cursor.fetchone():
                    raise KeyError("运营分类不存在")
            target_table = table_by_type[normalized_type]
            cursor.execute(
                f"UPDATE `{target_table}` SET `management_category_id` = %s "
                f"WHERE `id` IN ({placeholders})",
                tuple([normalized_category_id] + ids),
            )
            changed = int(cursor.rowcount or 0)
            if normalized_type == "collection":
                cursor.execute(
                    f"UPDATE `{PRODUCT_TABLE}` AS product "
                    f"INNER JOIN `{COLLECTION_TABLE}` AS collection_item "
                    "ON collection_item.`source_item_id` = product.`source_item_id` "
                    "SET product.`management_category_id` = %s "
                    f"WHERE collection_item.`id` IN ({placeholders})",
                    tuple([normalized_category_id] + ids),
                )
            else:
                cursor.execute(
                    f"UPDATE `{COLLECTION_TABLE}` AS collection_item "
                    f"INNER JOIN `{PRODUCT_TABLE}` AS product "
                    "ON collection_item.`source_item_id` = product.`source_item_id` "
                    "SET collection_item.`management_category_id` = %s "
                    f"WHERE product.`id` IN ({placeholders})",
                    tuple([normalized_category_id] + ids),
                )
        connection.commit()
        return {
            "item_type": normalized_type,
            "requested": len(ids),
            "changed": changed,
            "category_id": normalized_category_id,
        }
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


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


def get_published_product_account_ids(
    product_item_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[int, int]:
    """Return the latest successful account owner for each product.

    A product may be published to several sites under one account, but assigning
    it to another account would create a duplicate seller product.
    """

    item_ids = list(dict.fromkeys(
        int(value) for value in product_item_ids or [] if int(value) > 0
    ))
    if not item_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(item_ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT `product_item_id`, `token_id`, `id` "
                f"FROM `{PUBLISH_RECORD_TABLE}` "
                f"WHERE `product_item_id` IN ({placeholders}) "
                "AND `status` = 'published' ORDER BY `id` DESC",
                tuple(item_ids),
            )
            rows = cursor.fetchall()
        connection.commit()
        result: dict[int, int] = {}
        for row in rows:
            product_item_id = int(row.get("product_item_id") or 0)
            token_id = int(row.get("token_id") or 0)
            if product_item_id > 0 and token_id > 0 and product_item_id not in result:
                result[product_item_id] = token_id
        return result
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
    group_name: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 500,
    offset: int = 0,
    token_ids: Iterable[int] | None = None,
    filter_token_ids: Iterable[int] | None = None,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    base_where: list[str] = []
    base_params: list[Any] = []
    if token_ids is not None:
        scoped_token_ids = sorted({
            int(value) for value in token_ids or () if int(value or 0) > 0
        })
        if not scoped_token_ids:
            base_where.append("1 = 0")
        else:
            placeholders = ", ".join(["%s"] * len(scoped_token_ids))
            base_where.append(f"records.`token_id` IN ({placeholders})")
            base_params.extend(scoped_token_ids)
    selected_token_ids = sorted({
        int(value) for value in (filter_token_ids or ()) if int(value or 0) > 0
    })
    if selected_token_ids:
        placeholders = ", ".join(["%s"] * len(selected_token_ids))
        base_where.append(f"records.`token_id` IN ({placeholders})")
        base_params.extend(selected_token_ids)
    search = str(search or "").strip()
    if search:
        pattern = f"%{search}%"
        base_where.append(
            "(records.`source_item_id` LIKE %s OR records.`title` LIKE %s OR "
            "records.`published_item_id` LIKE %s OR records.`batch_id` LIKE %s)"
        )
        base_params.extend((pattern, pattern, pattern, pattern))
    store_name = str(store_name or "").strip()
    if store_name:
        base_where.append("records.`store_name` LIKE %s")
        base_params.append(f"%{store_name}%")
    site_id = str(site_id or "").strip().upper()
    if site_id:
        base_where.append("records.`site_id` = %s")
        base_params.append(site_id)
    group_name = str(group_name or "").strip()
    if len(group_name) > 100:
        raise ValueError("店铺组名称不能超过 100 个字符")
    if group_name == "__ungrouped__":
        base_where.append("COALESCE(settings.`group_name`, '') = ''")
    elif group_name:
        base_where.append("settings.`group_name` = %s")
        base_params.append(group_name)

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

    start_at, _ = parsed_datetime(start_date, "开始时间")
    end_at, end_precision = parsed_datetime(end_date, "结束时间")
    if start_at and end_at and start_at > end_at:
        raise ValueError("开始时间不能晚于结束时间")
    if start_at:
        base_where.append("records.`created_at` >= %s")
        base_params.append(start_at.strftime("%Y-%m-%d %H:%M:%S"))
    if end_at:
        end_exclusive = end_at + (
            timedelta(days=1) if end_precision == "day" else timedelta(minutes=1)
        )
        base_where.append("records.`created_at` < %s")
        base_params.append(end_exclusive.strftime("%Y-%m-%d %H:%M:%S"))

    where = list(base_where)
    params = list(base_params)
    status = str(status or "").strip().lower()
    if status:
        if status not in PRODUCT_PUBLISH_RECORD_STATUSES:
            raise ValueError(f"不支持的上架记录状态: {status}")
        where.append("records.`status` = %s")
        params.append(status)
    base_where_sql = f"WHERE {' AND '.join(base_where)}" if base_where else ""
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    from_sql = (
        f"`{PUBLISH_RECORD_TABLE}` AS records "
        "LEFT JOIN `mercado_store_site_settings` AS settings "
        "ON settings.`token_id` = records.`token_id` "
        "AND settings.`site_id` = records.`site_id`"
    )

    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT records.`status`, COUNT(*) AS total FROM {from_sql} "
                f"{base_where_sql} GROUP BY records.`status`",
                tuple(base_params),
            )
            counts = {key: 0 for key in PRODUCT_PUBLISH_RECORD_STATUSES}
            for count_row in cursor.fetchall():
                count_status = str(count_row.get("status") or "")
                if count_status in counts:
                    counts[count_status] = int(count_row.get("total") or 0)
            counts["all"] = sum(counts.values())
            cursor.execute(
                f"SELECT COUNT(*) AS total FROM {from_sql} {where_sql}",
                tuple(params),
            )
            total = int((cursor.fetchone() or {}).get("total") or 0)
            cursor.execute(
                f"SELECT records.*, COALESCE(settings.`group_name`, '') AS `group_name` "
                f"FROM {from_sql} {where_sql} "
                "ORDER BY records.`id` DESC LIMIT %s OFFSET %s",
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


def _normalize_product_content_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
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
    return normalized


def _product_content_update_plan(
    normalized: Mapping[str, Any],
    *,
    preserve_zying_net_proceeds: bool = False,
) -> tuple[list[str], list[Any], bool]:
    assignments = [f"`{field}` = %s" for field in normalized]
    values = list(normalized.values())
    # Dimensions remain useful display/reference data, but shipping now uses
    # actual weight only. Editing length/width/height must not erase or queue a
    # perfectly valid commission/freight snapshot.
    profitability_stale = bool({"price", "weight_g", "category_id"}.intersection(normalized))
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
        net_proceeds_assignment = "`net_proceeds_usd` = NULL"
        updated_at_assignment = "`profitability_updated_at` = NULL"
        source_assignment = "`profitability_source` = 'manual_edit_pending'"
        if preserve_zying_net_proceeds:
            # ZYing's net proceeds come directly from its product detail page.
            # Editing a derived-cost input must not turn that source value into
            # a pending Mercado profitability calculation.
            net_proceeds_assignment = (
                "`net_proceeds_usd` = IF(`source_type` = 'zying', "
                "`net_proceeds_usd`, NULL)"
            )
            updated_at_assignment = (
                "`profitability_updated_at` = IF(`source_type` = 'zying', "
                "`profitability_updated_at`, NULL)"
            )
            source_assignment = (
                "`profitability_source` = IF(`source_type` = 'zying', "
                f"'{ZYING_PROFITABILITY_SOURCE}', 'manual_edit_pending')"
            )
        assignments.extend((
            "`sale_price_usd` = NULL",
            "`commission_amount_local` = NULL",
            "`commission_amount_usd` = NULL",
            "`shipping_fee_local` = NULL",
            "`shipping_fee_usd` = NULL",
            "`billable_weight_g` = NULL",
            "`shipping_api_billable_weight_g` = NULL",
            net_proceeds_assignment,
            updated_at_assignment,
            source_assignment,
            "`profitability_error` = ''",
        ))
        if "category_id" in normalized:
            assignments.extend((
                "`category_name` = NULL",
                "`commission_rate` = NULL",
            ))
    return assignments, values, profitability_stale


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
    normalized = _normalize_product_content_changes(changes)
    assignments, values, profitability_stale = _product_content_update_plan(
        normalized, preserve_zying_net_proceeds=True
    )

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


def update_product_items(
    product_item_ids: Iterable[int],
    changes: Mapping[str, Any],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Apply explicitly selected content fields to multiple products atomically."""

    ids = _normalize_row_ids(product_item_ids, empty_message="请至少勾选一个产品")
    normalized = _normalize_product_content_changes(changes)
    assignments, values, profitability_stale = _product_content_update_plan(
        normalized, preserve_zying_net_proceeds=True
    )
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` SET {', '.join(assignments)} "
                f"WHERE `id` IN ({placeholders})",
                tuple(values + ids),
            )
            changed = int(cursor.rowcount or 0)

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
                    f"SET {', '.join(collection_assignments)} "
                    f"WHERE p.`id` IN ({placeholders})",
                    tuple(ids),
                )
        connection.commit()
        return {
            "requested": len(ids),
            "changed": changed,
            "updated_fields": list(normalized),
            "profitability_refresh_pending": profitability_stale,
        }
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_existing_user_product_ids(
    product_item_ids: Iterable[int],
    *,
    token_id: int,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[int, str]:
    """Return the latest reusable UP Siteless ID for each product/account."""
    item_ids = list(dict.fromkeys(
        int(value) for value in product_item_ids or [] if int(value) > 0
    ))
    if not item_ids:
        return {}
    normalized_token_id = int(token_id)
    if normalized_token_id <= 0:
        raise ValueError("查询历史 User Product 时账号无效")
    placeholders = ", ".join(["%s"] * len(item_ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT `product_item_id`, `published_item_id`, `id` "
                f"FROM `{PUBLISH_RECORD_TABLE}` "
                f"WHERE `product_item_id` IN ({placeholders}) "
                "AND `token_id` = %s AND `status` = 'published' "
                "AND (`published_item_id` LIKE 'CBTU%%' "
                "OR `published_item_id` LIKE 'U%%') "
                "ORDER BY `id` DESC",
                tuple(item_ids + [normalized_token_id]),
            )
            rows = cursor.fetchall()
        connection.commit()
        result: dict[int, str] = {}
        for row in rows:
            product_item_id = int(row.get("product_item_id") or 0)
            published_item_id = str(row.get("published_item_id") or "").strip().upper()
            if product_item_id > 0 and product_item_id not in result:
                result[product_item_id] = published_item_id
        return result
    finally:
        connection.close()


def update_collection_items(
    collection_item_ids: Iterable[int],
    changes: Mapping[str, Any],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Update weight/dimensions for selected collection candidates atomically."""

    ids = _normalize_row_ids(
        collection_item_ids, empty_message="请至少勾选一个采集商品"
    )
    allowed = {
        "weight_g", "package_length_cm", "package_width_cm", "package_height_cm",
    }
    unsupported = set(changes or {}).difference(allowed)
    if unsupported:
        raise ValueError("采集列表只支持批量修改实际重量和包装尺寸")
    normalized = _normalize_product_content_changes(changes)
    assignments, values, profitability_stale = _product_content_update_plan(normalized)
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"UPDATE `{COLLECTION_TABLE}` SET {', '.join(assignments)} "
                f"WHERE `id` IN ({placeholders})",
                tuple(values + ids),
            )
            changed = int(cursor.rowcount or 0)

            product_assignments = [
                f"p.`{field}` = c.`{field}`" for field in normalized
            ]
            if "weight_g" in normalized:
                product_assignments.append("p.`weight_basis` = 'manual_edit'")
            if set(normalized).intersection({
                "package_length_cm", "package_width_cm", "package_height_cm",
            }):
                product_assignments.append(
                    "p.`volumetric_weight_kg` = c.`volumetric_weight_kg`"
                )
            if profitability_stale:
                product_assignments.extend((
                    "p.`sale_price_usd` = NULL",
                    "p.`commission_amount_local` = NULL",
                    "p.`commission_amount_usd` = NULL",
                    "p.`shipping_fee_local` = NULL",
                    "p.`shipping_fee_usd` = NULL",
                    "p.`billable_weight_g` = NULL",
                    "p.`shipping_api_billable_weight_g` = NULL",
                    "p.`net_proceeds_usd` = NULL",
                    "p.`profitability_updated_at` = NULL",
                    "p.`profitability_source` = 'manual_edit_pending'",
                    "p.`profitability_error` = ''",
                ))
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` AS p "
                f"INNER JOIN `{COLLECTION_TABLE}` AS c "
                "ON c.`source_item_id` = p.`source_item_id` "
                f"SET {', '.join(product_assignments)} "
                f"WHERE c.`id` IN ({placeholders})",
                tuple(ids),
            )
        connection.commit()
        return {
            "requested": len(ids),
            "changed": changed,
            "updated_fields": list(normalized),
            "profitability_refresh_pending": profitability_stale,
        }
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def upsert_ai_original_product(
    raw_product: Mapping[str, Any],
    *,
    created_by: str = "",
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Insert or refresh one 1688 source snapshot in the unified product list."""
    from erp.ai_original_products import normalize_1688_product

    product = normalize_1688_product(raw_product)
    dimensions = (
        _decimal_from_text(product.get("package_length_cm")),
        _decimal_from_text(product.get("package_width_cm")),
        _decimal_from_text(product.get("package_height_cm")),
    )
    weight_g = _decimal_from_text(product.get("weight_g"))
    source_snapshot = {
        "original_1688": product,
        "ai_original": {"status": "pending", "error": ""},
        "source": {
            "id": f"CBT{product['source_1688_item_id']}",
            "site_id": "CBT",
            "title": product["title"],
            "price": product.get("price"),
            "currency_id": "CNY",
            "permalink": product["source_url"],
            "pictures": [{"source": url} for url in product.get("images") or []],
            "attributes": [],
            "variations": list(product.get("variations") or []),
        },
        "description": {"plain_text": product.get("description_text") or ""},
        "page_snapshot": {},
        "plugin_snapshot": {
            "source_type": "ai_original",
            "source_platform": "1688",
            "created_by": str(created_by or "")[:128],
        },
    }
    now = _now()
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"""
                INSERT INTO `{PRODUCT_TABLE}` (
                    `collection_item_id`, `source_type`, `review_status`,
                    `source_item_id`, `source_url`, `main_image_url`, `title`,
                    `description_text`, `price`, `currency_id`, `weight_g`,
                    `package_length_cm`, `package_width_cm`, `package_height_cm`,
                    `weight_basis`, `source_snapshot_json`, `added_at`
                ) VALUES (0, 'ai_original', 'unreviewed', %s, %s, %s, %s, %s,
                          %s, 'CNY', %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    `source_url` = IF(`source_type` = 'ai_original', VALUES(`source_url`), `source_url`),
                    `main_image_url` = IF(`source_type` = 'ai_original' AND
                        JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(`source_snapshot_json`),
                        `source_snapshot_json`, '{{}}'), '$.ai_original.status')) <> 'completed',
                        VALUES(`main_image_url`), `main_image_url`),
                    `title` = IF(`source_type` = 'ai_original' AND
                        JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(`source_snapshot_json`),
                        `source_snapshot_json`, '{{}}'), '$.ai_original.status')) <> 'completed',
                        VALUES(`title`), `title`),
                    `price` = IF(`source_type` = 'ai_original', VALUES(`price`), `price`),
                    `weight_g` = IF(`source_type` = 'ai_original', COALESCE(VALUES(`weight_g`), `weight_g`), `weight_g`),
                    `package_length_cm` = IF(`source_type` = 'ai_original', COALESCE(VALUES(`package_length_cm`), `package_length_cm`), `package_length_cm`),
                    `package_width_cm` = IF(`source_type` = 'ai_original', COALESCE(VALUES(`package_width_cm`), `package_width_cm`), `package_width_cm`),
                    `package_height_cm` = IF(`source_type` = 'ai_original', COALESCE(VALUES(`package_height_cm`), `package_height_cm`), `package_height_cm`),
                    `source_snapshot_json` = IF(`source_type` = 'ai_original' AND
                        JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(`source_snapshot_json`),
                        `source_snapshot_json`, '{{}}'), '$.ai_original.status')) <> 'completed',
                        VALUES(`source_snapshot_json`), `source_snapshot_json`),
                    `updated_at` = CURRENT_TIMESTAMP
                """,
                (
                    product["source_item_id"], product["source_url"],
                    product.get("main_image_url") or "", product["title"],
                    product.get("description_text") or "", _decimal_from_text(product.get("price")),
                    weight_g, dimensions[0], dimensions[1], dimensions[2],
                    "1688_source" if weight_g else "", _dumps(source_snapshot), now,
                ),
            )
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `source_item_id` = %s",
                (product["source_item_id"],),
            )
            row = cursor.fetchone()
        connection.commit()
        return _mirror_zying_snapshot_fields(_json_safe_row(row))
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_ai_original_product(
    product_item_id: int,
    changes: Mapping[str, Any],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Persist AI output or explicitly entered listing economics."""
    row_id = int(product_item_id)
    allowed = {
        "title", "description_text", "main_image_url", "source_snapshot_json",
        "weight_g", "package_length_cm", "package_width_cm", "package_height_cm",
        "category_id", "net_proceeds_usd", "review_status", "currency_id",
    }
    normalized = {key: value for key, value in dict(changes or {}).items() if key in allowed}
    if not normalized:
        raise ValueError("没有可保存的 AI 原创产品字段")
    if "review_status" in normalized and normalized["review_status"] not in {
        "unreviewed", "approved"
    }:
        raise ValueError("AI 原创产品审核状态无效")
    decimal_fields = {
        "weight_g", "package_length_cm", "package_width_cm", "package_height_cm",
        "net_proceeds_usd",
    }
    assignments, values = [], []
    for key, value in normalized.items():
        if key in decimal_fields:
            value = _decimal_from_text(value)
            if value is not None and value <= 0:
                raise ValueError(f"{key} 必须大于 0")
        if key == "source_snapshot_json" and isinstance(value, Mapping):
            value = _dumps(value)
        assignments.append(f"`{key}` = %s")
        values.append(value)
    if "weight_g" in normalized:
        assignments.append("`weight_basis` = 'ai_original_manual'")
    if set(normalized).intersection({
        "package_length_cm", "package_width_cm", "package_height_cm",
    }):
        assignments.append(
            "`volumetric_weight_kg` = CASE WHEN `package_length_cm` > 0 "
            "AND `package_width_cm` > 0 AND `package_height_cm` > 0 THEN ROUND("
            "`package_length_cm` * `package_width_cm` * `package_height_cm` / 6000, 4) "
            "ELSE NULL END"
        )
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            if normalized.get("review_status") == "approved":
                cursor.execute(
                    f"SELECT `weight_g`, `net_proceeds_usd`, `source_snapshot_json` "
                    f"FROM `{PRODUCT_TABLE}` WHERE `id` = %s "
                    "AND `source_type` = 'ai_original'",
                    (row_id,),
                )
                current = cursor.fetchone()
                if not current:
                    raise KeyError("AI 原创产品不存在")
                snapshot = _loads(current.get("source_snapshot_json"), {})
                prepared = snapshot.get("ai_original") if isinstance(snapshot, dict) else {}
                if not isinstance(prepared, dict) or prepared.get("status") != "completed":
                    raise ValueError("AI 原创任务完成后才能审核通过")
                approved_weight = _decimal_from_text(
                    normalized.get("weight_g", current.get("weight_g"))
                )
                approved_net = _decimal_from_text(
                    normalized.get("net_proceeds_usd", current.get("net_proceeds_usd"))
                )
                if approved_weight is None or approved_weight <= 0:
                    raise ValueError("审核通过前必须填写有效实重")
                if approved_net is None or approved_net <= 0:
                    raise ValueError("审核通过前必须填写有效净收益 USD")
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` SET {', '.join(assignments)} "
                "WHERE `id` = %s AND `source_type` = 'ai_original'",
                tuple(values + [row_id]),
            )
            if int(cursor.rowcount or 0) == 0:
                cursor.execute(
                    f"SELECT 1 FROM `{PRODUCT_TABLE}` WHERE `id` = %s "
                    "AND `source_type` = 'ai_original'",
                    (row_id,),
                )
                if not cursor.fetchone():
                    raise KeyError("AI 原创产品不存在")
            cursor.execute(f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s", (row_id,))
            row = cursor.fetchone()
        connection.commit()
        return _mirror_zying_snapshot_fields(_json_safe_row(row))
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _editor_text(value: Any, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def _editor_attributes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for raw in value[:200]:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, Any] = {}
        for key in (
            "id", "name", "name_es", "name_pt", "value_id", "value_name",
            "value_name_es", "value_name_pt", "value_struct", "values",
        ):
            if raw.get(key) not in (None, ""):
                item[key] = raw.get(key)
        if item.get("id") or item.get("name"):
            result.append(item)
    return result


def _editor_variations(value: Any) -> list[dict[str, Any]]:
    """Round-trip a bounded SKU matrix while keeping prices and stock numeric."""
    if not isinstance(value, list):
        return []
    result = []
    for raw in value[:200]:
        if not isinstance(raw, Mapping):
            continue
        # JSON round-tripping is deliberate: it removes request-only objects
        # and guarantees the snapshot can be written to LONGTEXT safely.
        try:
            item = json.loads(json.dumps(dict(raw), ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def update_ai_original_listing(
    product_item_id: int,
    listing: Mapping[str, Any],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Save the editable bilingual listing, category schema values and SKUs."""
    row_id = int(product_item_id)
    if row_id <= 0:
        raise ValueError("产品记录编号无效")
    data = dict(listing or {})
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s "
                "AND `source_type` = 'ai_original'",
                (row_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise KeyError("AI 原创产品不存在")
            snapshot = _loads(current.get("source_snapshot_json"), {})
            if not isinstance(snapshot, dict):
                snapshot = {}
            original = dict(snapshot.get("original_1688") or {})
            prepared = dict(snapshot.get("ai_original") or {})
            source = dict(snapshot.get("source") or {})

            source_title = _editor_text(data.get("source_title"), 255)
            source_description = _editor_text(data.get("source_description"), 50000)
            category_id = _editor_text(data.get("category_id"), 64)
            category_name = _editor_text(data.get("category_name"), 255)
            title_es = _editor_text(data.get("title_es"), 255)
            title_pt = _editor_text(data.get("title_pt"), 255)
            description_es = _editor_text(data.get("description_es"), 50000)
            description_pt = _editor_text(data.get("description_pt"), 50000)
            attributes = _editor_attributes(data.get("attributes"))
            variations = _editor_variations(data.get("variations"))

            if "source_title" in data and source_title:
                original["title"] = source_title
            if "source_description" in data:
                original["description_text"] = source_description
            if "category_id" in data:
                original["category_id"] = category_id
                source["category_id"] = category_id
            if "category_name" in data:
                original["category_name"] = category_name
                source["category_name"] = category_name
            if "title_es" in data:
                prepared["title_es"] = title_es
            if "title_pt" in data:
                prepared["title_pt"] = title_pt
            if "description_es" in data:
                prepared["description_es"] = description_es
            if "description_pt" in data:
                prepared["description_pt"] = description_pt
            if "attributes" in data:
                prepared["attributes"] = attributes
                source["attributes"] = attributes
            if "variations" in data:
                prepared["variations"] = variations
                source["variations"] = variations
            if "source_title" in data or "source_description" in data:
                snapshot["original_1688"] = original
            snapshot["ai_original"] = prepared
            snapshot["source"] = source
            description_for_row = description_es or source_description
            if description_for_row:
                snapshot["description"] = {"plain_text": description_for_row}

            row_title = title_es or title_pt or str(current.get("title") or "")
            row_description = description_for_row or str(current.get("description_text") or "")
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` SET `title` = %s, `description_text` = %s, "
                "`category_id` = %s, `category_name` = %s, `source_snapshot_json` = %s "
                "WHERE `id` = %s AND `source_type` = 'ai_original'",
                (
                    row_title[:255], row_description[:50000], category_id,
                    category_name, _dumps(snapshot), row_id,
                ),
            )
            cursor.execute(f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s", (row_id,))
            row = cursor.fetchone()
        connection.commit()
        return _mirror_zying_snapshot_fields(_json_safe_row(row))
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def translate_ai_original_listing(
    product_item_id: int,
    language: str,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Translate Chinese source content and SKU labels into one target language."""
    from erp.mercadolibre_translation import translate_texts

    language_key = {"es": "es", "es-419": "es", "pt": "pt-BR", "pt-br": "pt-BR"}.get(
        str(language or "").strip().lower()
    )
    if language_key is None:
        raise ValueError("翻译语言仅支持 es 或 pt-BR")
    row_id = int(product_item_id)
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s "
                "AND `source_type` = 'ai_original'",
                (row_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise KeyError("AI 原创产品不存在")
            snapshot = _loads(current.get("source_snapshot_json"), {})
            if not isinstance(snapshot, dict):
                snapshot = {}
            original = dict(snapshot.get("original_1688") or {})
            prepared = dict(snapshot.get("ai_original") or {})
            texts: list[str] = []
            setters: list[Callable[[str], None]] = []
            suffix = "es" if language_key == "es" else "pt"

            def add(value: Any, setter: Callable[[str], None]) -> None:
                text = str(value or "").strip()
                if text and len(texts) < 100:
                    texts.append(text)
                    setters.append(setter)

            add(original.get("title"), lambda value: prepared.__setitem__("title_es" if suffix == "es" else "title_pt", value))
            add(original.get("description_text"), lambda value: prepared.__setitem__("description_es" if suffix == "es" else "description_pt", value))

            attributes = _editor_attributes(prepared.get("attributes"))
            if not attributes:
                attributes = _editor_attributes([
                    {
                        "name": prop.get("name") or prop.get("key"),
                        "value_name": prop.get("value") or prop.get("value_name"),
                    }
                    for prop in original.get("properties") or []
                    if isinstance(prop, Mapping)
                ])
            for attribute in attributes:
                add(attribute.get("name"), lambda value, target=attribute: target.__setitem__("name_" + suffix, value))
                add(attribute.get("value_name"), lambda value, target=attribute: target.__setitem__("value_name_" + suffix, value))

            variations = _editor_variations(prepared.get("variations") or original.get("variations"))
            translatable_keys = ("name", "label", "value_name", "value", "text", "title", "sku_name")
            for variation in variations:
                for container_key in ("attribute_combinations", "attributes", "properties"):
                    for attribute in variation.get(container_key) or []:
                        if not isinstance(attribute, dict):
                            continue
                        for key in translatable_keys:
                            if key in attribute:
                                add(attribute.get(key), lambda value, target=attribute, key=key: target.__setitem__(f"{key}_{suffix}", value))
                for key in ("name", "label", "sku_name", "title"):
                    if key in variation:
                        add(variation.get(key), lambda value, target=variation, key=key: target.__setitem__(f"{key}_{suffix}", value))
            if not texts:
                raise ValueError("没有可翻译的标题、描述或变体文本")
            translated = translate_texts(texts, "zh-CN", language_key)
            for setter, value in zip(setters, translated):
                setter(value)
            prepared["attributes"] = attributes
            prepared["variations"] = variations
            snapshot["ai_original"] = prepared
            source = dict(snapshot.get("source") or {})
            source["attributes"] = attributes
            source["variations"] = variations
            snapshot["source"] = source
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` SET `source_snapshot_json` = %s WHERE `id` = %s",
                (_dumps(snapshot), row_id),
            )
            cursor.execute(f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s", (row_id,))
            row = cursor.fetchone()
        connection.commit()
        return _mirror_zying_snapshot_fields(_json_safe_row(row))
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def upsert_zying_products_to_products(
    records: Iterable[Mapping[str, Any]],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, int]:
    """将智赢二级详情快照写入统一产品列表，供审核和上架流程复用。"""

    values = []
    skipped = 0
    now = _now()
    for raw_record in records or []:
        record = dict(raw_record or {})
        product_id = str(record.get("product_id") or "").strip()[:32]
        snapshot = dict(record.get("listing_snapshot") or {})
        source = dict(snapshot.get("source") or {})
        description = dict(snapshot.get("description") or {})
        title = str(
            snapshot.get("title") or source.get("title") or record.get("title") or ""
        ).strip()[:255]
        if not product_id or not title:
            skipped += 1
            continue
        plugin_snapshot = dict(snapshot.get("plugin_snapshot") or {})
        plugin_snapshot.update(
            {
                "source_type": "zying",
                "zying_product_id": product_id,
                "zying_category_id": record.get("zying_category_id") or "",
                "zying_category": record.get("zying_category") or "",
                "product_developer_id": record.get("product_developer_id") or "",
                "product_developer_name": record.get("product_developer_name") or "",
                "zying_status": record.get("zying_status") or "",
            }
        )
        snapshot["plugin_snapshot"] = plugin_snapshot
        source_url = str(
            snapshot.get("final_url")
            or snapshot.get("source_url")
            or "https://meli.zying.net/#/product"
        ).strip()[:1500]
        main_image_url = str(
            snapshot.get("main_image_url") or record.get("main_image_url") or ""
        ).strip()[:1500]
        price = _decimal_from_text(
            snapshot.get("price") if snapshot.get("price") not in (None, "")
            else record.get("sale_price")
        )
        currency_id = str(
            snapshot.get("currency_id") or source.get("currency_id") or "USD"
        ).strip().upper()[:16]
        if currency_id in {"$", "US$", "USD$"}:
            currency_id = "USD"
        weight_g = _decimal_from_text(
            snapshot.get("weight_g") if snapshot.get("weight_g") not in (None, "")
            else record.get("package_gross_weight")
        )
        dimensions = [
            _decimal_from_text(snapshot.get("package_length_cm")),
            _decimal_from_text(snapshot.get("package_width_cm")),
            _decimal_from_text(snapshot.get("package_height_cm")),
        ]
        if any(value is None for value in dimensions):
            parsed_dimensions = re.findall(
                r"\d+(?:[.,]\d+)?",
                str(record.get("package_dimensions") or ""),
            )[:3]
            if len(parsed_dimensions) == 3:
                dimensions = [_decimal_from_text(value) for value in parsed_dimensions]
        volumetric_weight = (
            dimensions[0] * dimensions[1] * dimensions[2] / Decimal("6000")
            if all(value is not None for value in dimensions)
            else None
        )
        category_id = str(
            snapshot.get("category_id")
            or source.get("category_id")
            or record.get("product_category_id")
            or ""
        ).strip()[:64]
        category_name = str(record.get("product_category") or "").strip()[:255]
        sale_price_usd = price if currency_id == "USD" else None
        net_proceeds = _decimal_from_text(record.get("net_income"))
        if currency_id != "USD":
            net_proceeds = None
        # Preserve the direct ZYing value inside the source snapshot as an
        # auditable recovery source; it must never be derived from our fees.
        snapshot["zying_net_proceeds_usd"] = net_proceeds
        description_text = str(
            description.get("plain_text")
            or description.get("text")
            or record.get("description_text")
            or ""
        ).strip()
        values.append((
            0, "zying", "unreviewed", product_id, source_url,
            main_image_url, title, description_text, price, currency_id,
            weight_g, volumetric_weight, dimensions[0], dimensions[1], dimensions[2],
            "zying_detail", sale_price_usd, category_id, category_name,
            net_proceeds, now, ZYING_PROFITABILITY_SOURCE, _dumps(snapshot), now,
            str(record.get("product_developer_id") or "").strip()[:64],
            str(record.get("product_developer_name") or "").strip()[:255],
        ))
    if not values:
        return {"count": 0, "skipped": skipped}

    mirrored_fields = (
        "source_url", "main_image_url", "title", "description_text", "price",
        "currency_id", "weight_g", "volumetric_weight_kg", "package_length_cm",
        "package_width_cm", "package_height_cm", "weight_basis", "sale_price_usd",
        "category_id", "category_name", "net_proceeds_usd",
        "profitability_updated_at", "profitability_source", "source_snapshot_json",
        "product_developer_id", "product_developer_name",
    )
    updates = ",\n                    ".join(
        f"`{field}` = IF(`source_type` = 'zying', VALUES(`{field}`), `{field}`)"
        for field in mirrored_fields
    )
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.executemany(
                f"""
                INSERT INTO `{PRODUCT_TABLE}` (
                    `collection_item_id`, `source_type`, `review_status`, `source_item_id`,
                    `source_url`, `main_image_url`, `title`, `description_text`, `price`,
                    `currency_id`, `weight_g`, `volumetric_weight_kg`,
                    `package_length_cm`, `package_width_cm`, `package_height_cm`,
                    `weight_basis`, `sale_price_usd`, `category_id`, `category_name`,
                    `net_proceeds_usd`, `profitability_updated_at`, `profitability_source`,
                    `source_snapshot_json`, `added_at`, `product_developer_id`,
                    `product_developer_name`
                ) VALUES ({", ".join(["%s"] * 26)})
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


def sync_zying_product_developers(
    developers: Iterable[Mapping[str, Any]],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    """补全统一产品列表中历史智赢快照的产品开发姓名。"""
    values = []
    for developer in developers or ():
        developer_id = str(developer.get("id") or "").strip()[:64]
        developer_name = str(developer.get("name") or "").strip()[:255]
        if developer_id and developer_name:
            values.append((developer_id, developer_name, developer_id, developer_id, developer_id))
    if not values:
        return 0
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.executemany(
                f"""
                UPDATE `{PRODUCT_TABLE}`
                SET `product_developer_id` = %s, `product_developer_name` = %s
                WHERE `source_type` = 'zying'
                  AND (
                    `product_developer_id` = %s
                    OR JSON_UNQUOTE(JSON_EXTRACT(
                        IF(JSON_VALID(`source_snapshot_json`), `source_snapshot_json`, '{{}}'),
                        '$.plugin_snapshot.product_developer_id'
                    )) = %s
                    OR JSON_UNQUOTE(JSON_EXTRACT(
                        IF(JSON_VALID(`source_snapshot_json`), `source_snapshot_json`, '{{}}'),
                        '$.page_snapshot.zying_detail.sale_loginid'
                    )) = %s
                  )
                """,
                values,
            )
            changed = int(cursor.rowcount or 0)
        connection.commit()
        return changed
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


def sync_pulled_product_fields_from_store_links(
    link_ids: Iterable[int],
    fields: Iterable[str],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    """Mirror confirmed remote listing edits into pulled product rows."""

    ids = sorted({int(value) for value in link_ids or [] if int(value) > 0})
    if not ids:
        return 0
    allowed = {
        "price", "weight_g", "package_length_cm", "package_width_cm",
        "package_height_cm", "net_proceeds_usd",
    }
    selected = [field for field in fields or [] if field in allowed]
    if not selected:
        return 0
    profitability_stale = bool({"price", "weight_g"}.intersection(selected))
    assignments: list[str] = []
    if "price" in selected:
        assignments.append("products.`price` = links.`price`")
    if "weight_g" in selected:
        assignments.append("products.`weight_g` = links.`weight_g`")
    for field in ("package_length_cm", "package_width_cm", "package_height_cm"):
        if field in selected:
            assignments.append(f"products.`{field}` = links.`{field}`")
    if {"package_length_cm", "package_width_cm", "package_height_cm"}.intersection(selected):
        assignments.append("products.`volumetric_weight_kg` = links.`volumetric_weight_kg`")
    if "weight_g" in selected:
        assignments.append("products.`weight_basis` = 'mercado_remote_update'")
    if profitability_stale:
        assignments.extend([
            "products.`sale_price_usd` = NULL",
            "products.`commission_amount_local` = NULL",
            "products.`commission_amount_usd` = NULL",
            "products.`shipping_fee_local` = NULL",
            "products.`shipping_fee_usd` = NULL",
            "products.`billable_weight_g` = NULL",
            "products.`shipping_api_billable_weight_g` = NULL",
            "products.`shipping_weight_rule` = NULL",
            "products.`net_proceeds_usd` = NULL",
            "products.`profitability_updated_at` = NULL",
            "products.`profitability_source` = 'mercado_remote_edit_pending'",
            "products.`profitability_error` = ''",
        ])
    elif "net_proceeds_usd" in selected:
        assignments.extend([
            "products.`net_proceeds_usd` = links.`net_proceeds_usd`",
            "products.`profitability_source` = 'mercado_remote_update'",
            "products.`profitability_updated_at` = CURRENT_TIMESTAMP",
            "products.`profitability_error` = NULL",
        ])
    assignments.append("products.`updated_at` = CURRENT_TIMESTAMP")
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"""
                UPDATE `{PRODUCT_TABLE}` AS products
                INNER JOIN `erp_mercadolibre_store_links` AS links
                    ON links.`item_id` = products.`source_item_id`
                SET {", ".join(assignments)}
                WHERE links.`id` IN ({placeholders})
                  AND products.`source_type` = 'pulled'
                """,
                tuple(ids),
            )
            changed = int(cursor.rowcount or 0)
        connection.commit()
        return changed
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
            cursor.execute("SHOW TABLES LIKE 'infringement_risk_checks'")
            if cursor.fetchone():
                cursor.execute(
                    f"DELETE FROM `infringement_risk_checks` "
                    f"WHERE `source_type` = 'collection_list' "
                    f"AND `source_row_id` IN ({placeholders})",
                    tuple(ids),
                )
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
                f"SELECT `id`, `source_item_id`, `review_status`, `last_publish_status`, "
                "`infringement_risk_level`, `infringement_keywords`, "
                "`infringement_reason`, `infringement_checked_at` "
                f"FROM `{PRODUCT_TABLE}` WHERE `id` IN ({placeholders})",
                tuple(ids),
            )
            product_states = [
                dict(row) for row in cursor.fetchall() if row.get("source_item_id")
            ]
            cursor.execute("SHOW TABLES LIKE 'infringement_risk_checks'")
            if product_states and cursor.fetchone():
                cursor.execute(
                    f"""
                    INSERT INTO `infringement_risk_checks` (
                        `source_type`, `source_row_id`, `product_id`, `title`,
                        `main_image_url`, `product_category`, `zying_category_id`,
                        `zying_category`, `salesperson`, `group_name`, `token_id`,
                        `account_name`, `risk_level`, `keywords`, `reason`, `checked_at`
                    )
                    SELECT 'collection_list', collection_items.`id`,
                           collection_items.`source_item_id`, collection_items.`title`,
                           collection_items.`main_image_url`, collection_items.`category_name`,
                           '', '', '', '', NULL, '', risks.`risk_level`, risks.`keywords`,
                           risks.`reason`, risks.`checked_at`
                    FROM `infringement_risk_checks` AS risks
                    INNER JOIN `{PRODUCT_TABLE}` AS products
                      ON products.`id` = risks.`source_row_id`
                    INNER JOIN `{COLLECTION_TABLE}` AS collection_items
                      ON collection_items.`source_item_id` = products.`source_item_id`
                    WHERE risks.`source_type` = 'product_list'
                      AND risks.`source_row_id` IN ({placeholders})
                    ON DUPLICATE KEY UPDATE
                        `product_id` = VALUES(`product_id`), `title` = VALUES(`title`),
                        `main_image_url` = VALUES(`main_image_url`),
                        `product_category` = VALUES(`product_category`),
                        `risk_level` = VALUES(`risk_level`),
                        `keywords` = VALUES(`keywords`), `reason` = VALUES(`reason`),
                        `checked_at` = VALUES(`checked_at`)
                    """,
                    tuple(ids),
                )
                cursor.execute(
                    f"DELETE FROM `infringement_risk_checks` "
                    f"WHERE `source_type` = 'product_list' "
                    f"AND `source_row_id` IN ({placeholders})",
                    tuple(ids),
                )
            cursor.execute(
                f"DELETE FROM `{PRODUCT_TABLE}` WHERE `id` IN ({placeholders})",
                tuple(ids),
            )
            deleted = int(cursor.rowcount or 0)
            for row in product_states:
                cursor.execute(
                    f"UPDATE `{COLLECTION_TABLE}` SET `added_to_products` = 0, "
                    "`review_status` = %s, `last_publish_status` = %s, "
                    "`infringement_risk_level` = %s, `infringement_keywords` = %s, "
                    "`infringement_reason` = %s, `infringement_checked_at` = %s "
                    "WHERE `source_item_id` = %s",
                    (
                        str(row.get("review_status") or "unreviewed"),
                        row.get("last_publish_status"),
                        row.get("infringement_risk_level"),
                        row.get("infringement_keywords"),
                        row.get("infringement_reason"),
                        row.get("infringement_checked_at"),
                        str(row.get("source_item_id") or ""),
                    ),
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
    moved_pairs: list[tuple[int, int]] = []
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
                    f"SELECT * FROM `{COLLECTION_TABLE}` "
                    "WHERE `id` = %s OR `source_item_id` = %s "
                    "ORDER BY (`id` = %s) DESC, `id` DESC LIMIT 1",
                    (
                        int(row.get("collection_item_id") or 0),
                        str(row.get("source_item_id") or ""),
                        int(row.get("collection_item_id") or 0),
                    ),
                )
                existing = cursor.fetchone() or {}
                merged_metrics = {
                    key: row.get(key) if row.get(key) not in (None, "") else existing.get(key)
                    for key in (
                        "weight_g",
                        "package_length_cm",
                        "package_width_cm",
                        "package_height_cm",
                        "weight_basis",
                    )
                }
                scrape_status = "ok" if has_complete_weight_dimensions(merged_metrics) else "partial"
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
                            `management_category_id` = COALESCE(%s, `management_category_id`),
                            `infringement_risk_level` = %s,
                            `infringement_keywords` = %s,
                            `infringement_reason` = %s,
                            `infringement_checked_at` = %s,
                            `added_to_products` = 0,
                            `review_status` = %s,
                            `last_publish_status` = %s,
                            `scrape_status` = %s,
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
                            row.get("management_category_id"),
                            row.get("infringement_risk_level"),
                            row.get("infringement_keywords"),
                            row.get("infringement_reason"),
                            row.get("infringement_checked_at"),
                            str(row.get("review_status") or "unreviewed"),
                            row.get("last_publish_status"),
                            scrape_status,
                            f"产品列表自动移回：{reason_text}",
                            int(existing["id"]),
                        ),
                    )
                    collection_row_id = int(existing["id"])
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
                        row.get("management_category_id"),
                        row.get("infringement_risk_level"),
                        row.get("infringement_keywords"),
                        row.get("infringement_reason"),
                        row.get("infringement_checked_at"),
                        *(row.get(column) for column in PROFITABILITY_COLUMNS),
                        str(row.get("review_status") or "unreviewed"),
                        row.get("last_publish_status"),
                        scrape_status,
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
                            `management_category_id`, `infringement_risk_level`,
                            `infringement_keywords`, `infringement_reason`,
                            `infringement_checked_at`, {profitability_columns_sql},
                            `review_status`, `last_publish_status`,
                            `scrape_status`, `error_message`,
                            `source_json`, `description_json`, `page_snapshot_json`,
                            `plugin_snapshot_json`, `collected_at`
                        ) VALUES ({", ".join(["%s"] * len(values))})
                        ON DUPLICATE KEY UPDATE
                            `added_to_products` = 0,
                            `review_status` = VALUES(`review_status`),
                            `last_publish_status` = VALUES(`last_publish_status`),
                            `infringement_risk_level` = VALUES(`infringement_risk_level`),
                            `infringement_keywords` = VALUES(`infringement_keywords`),
                            `infringement_reason` = VALUES(`infringement_reason`),
                            `infringement_checked_at` = VALUES(`infringement_checked_at`),
                            `error_message` = VALUES(`error_message`),
                            `updated_at` = CURRENT_TIMESTAMP
                        """,
                        values,
                    )
                    collection_row_id = int(cursor.lastrowid or 0)
                    if not collection_row_id:
                        cursor.execute(
                            f"SELECT `id` FROM `{COLLECTION_TABLE}` "
                            "WHERE `source_item_id` = %s ORDER BY `id` DESC LIMIT 1",
                            (str(row.get("source_item_id") or ""),),
                        )
                        collection_row_id = int((cursor.fetchone() or {}).get("id") or 0)
                    created += 1
                if collection_row_id:
                    moved_pairs.append((int(row["id"]), collection_row_id))
                moved += 1
            if moved_pairs:
                cursor.execute("SHOW TABLES LIKE 'infringement_risk_checks'")
                if cursor.fetchone():
                    for product_row_id, collection_row_id in moved_pairs:
                        cursor.execute(
                            f"""
                            INSERT INTO `infringement_risk_checks` (
                                `source_type`, `source_row_id`, `product_id`, `title`,
                                `main_image_url`, `product_category`, `zying_category_id`,
                                `zying_category`, `salesperson`, `group_name`, `token_id`,
                                `account_name`, `risk_level`, `keywords`, `reason`, `checked_at`
                            )
                            SELECT 'collection_list', collection_items.`id`,
                                   collection_items.`source_item_id`, collection_items.`title`,
                                   collection_items.`main_image_url`, collection_items.`category_name`,
                                   '', '', '', '', NULL, '', risks.`risk_level`, risks.`keywords`,
                                   risks.`reason`, risks.`checked_at`
                            FROM `infringement_risk_checks` AS risks
                            INNER JOIN `{COLLECTION_TABLE}` AS collection_items
                              ON collection_items.`id` = %s
                            WHERE risks.`source_type` = 'product_list'
                              AND risks.`source_row_id` = %s
                            ON DUPLICATE KEY UPDATE
                                `product_id` = VALUES(`product_id`), `title` = VALUES(`title`),
                                `main_image_url` = VALUES(`main_image_url`),
                                `product_category` = VALUES(`product_category`),
                                `risk_level` = VALUES(`risk_level`),
                                `keywords` = VALUES(`keywords`), `reason` = VALUES(`reason`),
                                `checked_at` = VALUES(`checked_at`)
                            """,
                            (collection_row_id, product_row_id),
                        )
                    cursor.execute(
                        f"DELETE FROM `infringement_risk_checks` "
                        f"WHERE `source_type` = 'product_list' "
                        f"AND `source_row_id` IN ({placeholders})",
                        tuple(ids),
                    )
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
    retry_before: str | None = None,
    limit: int = 50,
    connection_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """Refresh every product source and retry incomplete snapshots after a delay."""

    limit = max(1, min(int(limit), 500))
    retry_before = retry_before or (datetime.now() - timedelta(minutes=5)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    connection = (connection_factory or _connect)()
    try:
        rows = []
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            # Reserve space for both lists. Pending rows are read separately in
            # newest-first order so a rate-card refresh or legacy NULL backlog
            # cannot hide a just-collected high-id item for hours.
            for table, batch_limit in (
                (PRODUCT_TABLE, max(1, limit // 2)),
                (COLLECTION_TABLE, None),
            ):
                batch_limit = batch_limit or (limit - len(rows))
                if batch_limit <= 0:
                    continue
                eligibility_sql = f"""
                    `price` IS NOT NULL AND `price` > 0
                    AND `weight_g` IS NOT NULL AND `weight_g` > 0
                    AND LOWER(COALESCE(`weight_basis`, '')) NOT IN (
                        'calculated_volumetric', 'legacy_unknown',
                        'plugin_volumetric_fallback'
                    )
                    AND (
                        NULLIF(TRIM(COALESCE(`category_id`, '')), '') IS NOT NULL
                        OR NULLIF(TRIM(COALESCE(`title`, '')), '') IS NOT NULL
                    )
                    {"AND `source_type` <> 'zying'" if table == PRODUCT_TABLE else ""}
                """
                cursor.execute(
                    f"""
                    SELECT * FROM `{table}`
                    WHERE {eligibility_sql}
                      AND `profitability_updated_at` IS NULL
                    ORDER BY `id` DESC
                    LIMIT %s
                    """,
                    (batch_limit,),
                )
                pending_rows = list(cursor.fetchall())
                rows.extend(_json_safe_row(row) for row in pending_rows)
                remaining = batch_limit - len(pending_rows)
                if remaining <= 0:
                    continue
                cursor.execute(
                    f"""
                    SELECT * FROM `{table}`
                    WHERE {eligibility_sql}
                      AND `profitability_updated_at` IS NOT NULL
                      AND (
                          `profitability_updated_at` < %s
                          OR (`profitability_updated_at` < %s AND (
                              `commission_amount_usd` IS NULL
                              OR `shipping_fee_usd` IS NULL
                              OR `net_proceeds_usd` IS NULL
                              OR COALESCE(`profitability_error`, '') <> ''
                          ))
                      )
                    ORDER BY `profitability_updated_at` ASC, `id` ASC
                    LIMIT %s
                    """,
                    (stale_before, retry_before, remaining),
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
) -> bool:
    """Update a cost snapshot only while its calculation inputs are unchanged."""

    item_id = str(source_item_id or "").strip().upper()
    if not item_id:
        raise ValueError("商品编号不能为空")
    if str(snapshot.get("source_type") or "").strip().lower() == "zying":
        # Protect direct ZYing proceeds even when a row was already fetched by
        # an estimator worker before the queue exclusion took effect.
        return False
    values = [snapshot.get(column) for column in PROFITABILITY_COLUMNS]
    assignments = ", ".join(f"`{column}` = %s" for column in PROFITABILITY_COLUMNS)
    expected_inputs = snapshot.get("_expected_profitability_inputs")
    expected_inputs = (
        dict(expected_inputs) if isinstance(expected_inputs, Mapping) else None
    )

    def cas(
        table: str,
        *,
        include_updated_at: bool = True,
        include_snapshots: bool = True,
    ) -> tuple[str, list[Any]]:
        if expected_inputs is None:
            return "", []
        columns = [
            "price", "currency_id", "weight_g", "weight_basis",
            "category_id", "title",
        ]
        if include_updated_at:
            columns.insert(0, "updated_at")
        if include_snapshots:
            columns.extend(
                ["source_json", "description_json", "page_snapshot_json"]
                if table == COLLECTION_TABLE
                else ["source_snapshot_json", "description_text"]
            )
        return (
            "".join(f" AND `{column}` <=> %s" for column in columns),
            [expected_inputs.get(column) for column in columns],
        )
    collection_item_id = None
    if snapshot.get("task_id") is not None:
        try:
            collection_item_id = int(snapshot.get("id") or 0) or None
        except (TypeError, ValueError):
            collection_item_id = None
    connection = (connection_factory or _connect)()
    applied = False
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            if snapshot.get("source_type") and snapshot.get("id") and collection_item_id is None:
                # A product may have a different price/category from historical
                # captures of the same listing. Persist its own quote only.
                cas_sql, cas_values = cas(PRODUCT_TABLE)
                cursor.execute(
                    f"UPDATE `{PRODUCT_TABLE}` SET {assignments} "
                    f"WHERE `id` = %s AND `source_item_id` = %s{cas_sql}",
                    tuple(values + [int(snapshot["id"]), item_id] + cas_values),
                )
                applied = int(cursor.rowcount or 0) > 0
            elif collection_item_id is not None:
                collection_cas_sql, collection_cas_values = cas(COLLECTION_TABLE)
                cursor.execute(
                    f"UPDATE `{COLLECTION_TABLE}` SET {assignments} "
                    f"WHERE `id` = %s AND `source_item_id` = %s"
                    f"{collection_cas_sql}",
                    tuple(
                        values
                        + [collection_item_id, item_id]
                        + collection_cas_values
                    ),
                )
                applied = int(cursor.rowcount or 0) > 0
                # The product list mirrors the newest captured snapshot only;
                # an older duplicate task must never overwrite a newer price.
                if applied:
                    product_cas_sql, product_cas_values = cas(
                        PRODUCT_TABLE,
                        include_updated_at=False,
                        include_snapshots=False,
                    )
                    cursor.execute(
                        f"""
                        UPDATE `{PRODUCT_TABLE}`
                        SET {assignments}
                        WHERE `source_item_id` = %s
                          AND `collection_item_id` = %s
                          AND `source_type` = 'collected'
                          {product_cas_sql}
                          AND NOT EXISTS (
                              SELECT 1 FROM `{COLLECTION_TABLE}` AS newer
                              WHERE newer.`source_item_id` = %s AND newer.`id` > %s
                          )
                        """,
                        tuple(
                            values
                            + [item_id, collection_item_id]
                            + product_cas_values
                            + [item_id, collection_item_id]
                        ),
                    )
            else:
                for table in (COLLECTION_TABLE, PRODUCT_TABLE):
                    cursor.execute(
                        f"UPDATE `{table}` SET {assignments} WHERE `source_item_id` = %s",
                        tuple(values + [item_id]),
                    )
                    applied = applied or int(cursor.rowcount or 0) > 0
        connection.commit()
        return applied
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_all_profitability_stale(
    *,
    reason: str = "official_shipping_rate_card_refresh_pending",
    site_ids: Iterable[str] | None = None,
    batch_size: int = PROFITABILITY_INVALIDATION_BATCH_SIZE,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    """Invalidate freight/net in short batches while preserving commission.

    A single UPDATE over the product and collection tables can hold hundreds of
    thousands of row locks until commit. Select primary keys in bounded batches
    and commit every batch so foreground writes can proceed.
    """

    try:
        normalized_batch_size = int(batch_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("利润失效批次大小必须是整数") from exc
    if normalized_batch_size <= 0:
        raise ValueError("利润失效批次大小必须大于 0")
    connection = (connection_factory or _connect)()
    changed = 0
    normalized_sites = sorted({
        str(site_id or "").strip().upper()
        for site_id in (site_ids or [])
        if str(site_id or "").strip()
    })
    site_filter = ""
    site_values: list[Any] = []
    if normalized_sites:
        site_filter = (
            " AND LEFT(`source_item_id`, 3) IN ("
            + ", ".join(["%s"] * len(normalized_sites))
            + ")"
        )
        site_values.extend(normalized_sites)
    stale_filter = """
        AND (
            `shipping_fee_local` IS NOT NULL
            OR `shipping_currency_id` IS NOT NULL
            OR `shipping_fee_usd` IS NOT NULL
            OR `billable_weight_g` IS NOT NULL
            OR `shipping_api_billable_weight_g` IS NOT NULL
            OR `shipping_weight_rule` IS NOT NULL
            OR `net_proceeds_usd` IS NOT NULL
            OR `profitability_updated_at` IS NOT NULL
            OR COALESCE(`profitability_error`, '') <> ''
        )
    """
    reason_text = str(reason or "")[:128]
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            for table in (COLLECTION_TABLE, PRODUCT_TABLE):
                source_filter = (
                    "AND `source_type` <> 'zying'"
                    if table == PRODUCT_TABLE else ""
                )
                last_id = 0
                while True:
                    cursor.execute(
                        f"""
                        SELECT `id`
                        FROM `{table}`
                        WHERE `id` > %s
                          AND `price` IS NOT NULL AND `price` > 0
                          {source_filter}
                          {site_filter}
                          {stale_filter}
                        ORDER BY `id` ASC
                        LIMIT %s
                        """,
                        tuple([last_id] + site_values + [normalized_batch_size]),
                    )
                    batch_ids = sorted({
                        int(row.get("id") if isinstance(row, Mapping) else row[0])
                        for row in (cursor.fetchall() or [])
                    })
                    if not batch_ids:
                        break
                    last_id = batch_ids[-1]
                    placeholders = ", ".join(["%s"] * len(batch_ids))
                    cursor.execute(
                        f"""
                        UPDATE `{table}`
                        SET `shipping_fee_local` = NULL,
                            `shipping_currency_id` = NULL,
                            `shipping_fee_usd` = NULL,
                            `billable_weight_g` = NULL,
                            `shipping_api_billable_weight_g` = NULL,
                            `shipping_weight_rule` = NULL,
                            `net_proceeds_usd` = NULL,
                            `profitability_updated_at` = NULL,
                            `profitability_source` = %s,
                            `profitability_error` = ''
                        WHERE `id` IN ({placeholders})
                        """,
                        tuple([reason_text] + batch_ids),
                    )
                    changed += max(0, int(cursor.rowcount or 0))
                    connection.commit()
        return changed
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
                product_scope = (
                    " AND `source_type` = 'collected'"
                    if table == PRODUCT_TABLE else ""
                )
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
                          {product_scope}
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


def _add_collection_items_to_products_once(
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
                weight_basis = str(row.get("weight_basis") or "").strip().lower()
                if (
                    weight is None
                    or not weight.is_finite()
                    or weight <= 0
                    or weight_basis in {
                        "calculated_volumetric",
                        "legacy_unknown",
                        "plugin_volumetric_fallback",
                    }
                ):
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
                collection_review_status = str(
                    row.get("review_status") or "unreviewed"
                ).strip().lower()
                if collection_review_status not in PRODUCT_REVIEW_STATUSES:
                    collection_review_status = "unreviewed"
                values = (
                    row["id"], "collected", collection_review_status,
                    row["source_item_id"], row["source_url"],
                    row.get("main_image_url"), row.get("title"), description_text,
                    row.get("price"),
                    row.get("currency_id"), row.get("weight_g"),
                    row.get("volumetric_weight_kg"),
                    row.get("package_length_cm"), row.get("package_width_cm"),
                    row.get("package_height_cm"), row.get("weight_basis"),
                    row.get("management_category_id"),
                    row.get("infringement_risk_level"),
                    row.get("infringement_keywords"),
                    row.get("infringement_reason"),
                    row.get("infringement_checked_at"),
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
                        `weight_basis`, `management_category_id`,
                        `infringement_risk_level`, `infringement_keywords`,
                        `infringement_reason`, `infringement_checked_at`,
                        {profitability_columns_sql},
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
                        `management_category_id` = COALESCE(
                            VALUES(`management_category_id`), `management_category_id`
                        ),
                        `infringement_risk_level` = VALUES(`infringement_risk_level`),
                        `infringement_keywords` = VALUES(`infringement_keywords`),
                        `infringement_reason` = VALUES(`infringement_reason`),
                        `infringement_checked_at` = VALUES(`infringement_checked_at`),
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
                # The risk result follows the same logical item when it moves
                # from the collection queue into the product library. Keeping
                # only the product-list key avoids a duplicate result and also
                # prevents an unnecessary second AI call.
                cursor.execute("SHOW TABLES LIKE 'infringement_risk_checks'")
                if cursor.fetchone():
                    cursor.execute(
                        f"""
                        INSERT INTO `infringement_risk_checks` (
                            `source_type`, `source_row_id`, `product_id`, `title`,
                            `main_image_url`, `product_category`, `zying_category_id`,
                            `zying_category`, `salesperson`, `group_name`, `token_id`,
                            `account_name`, `risk_level`, `keywords`, `reason`, `checked_at`
                        )
                        SELECT 'product_list', products.`id`, products.`source_item_id`,
                               products.`title`, products.`main_image_url`,
                               products.`category_name`, '', '', '', '', NULL, '',
                               risks.`risk_level`, risks.`keywords`, risks.`reason`,
                               risks.`checked_at`
                        FROM `infringement_risk_checks` AS risks
                        INNER JOIN `{PRODUCT_TABLE}` AS products
                          ON products.`collection_item_id` = risks.`source_row_id`
                        WHERE risks.`source_type` = 'collection_list'
                          AND risks.`source_row_id` IN ({complete_placeholders})
                        ON DUPLICATE KEY UPDATE
                            `product_id` = VALUES(`product_id`),
                            `title` = VALUES(`title`),
                            `main_image_url` = VALUES(`main_image_url`),
                            `product_category` = VALUES(`product_category`),
                            `risk_level` = VALUES(`risk_level`),
                            `keywords` = VALUES(`keywords`),
                            `reason` = VALUES(`reason`),
                            `checked_at` = VALUES(`checked_at`)
                        """,
                        tuple(complete_ids),
                    )
                    cursor.execute(
                        f"DELETE FROM `infringement_risk_checks` "
                        f"WHERE `source_type` = 'collection_list' "
                        f"AND `source_row_id` IN ({complete_placeholders})",
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


def add_collection_items_to_products(
    collection_item_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Move collection rows to products, retrying transient MySQL lock races."""

    attempts = 3
    for attempt in range(attempts):
        try:
            return _add_collection_items_to_products_once(
                collection_item_ids,
                connection_factory=connection_factory,
            )
        except Exception as exc:
            if not _is_retryable_transaction_error(exc) or attempt >= attempts - 1:
                raise
            time.sleep(0.15 * (2**attempt))
    raise RuntimeError("加入产品列表重试结束但没有返回结果")
