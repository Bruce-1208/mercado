"""MySQL persistence for workbench Mercado Libre collection and product lists."""

from __future__ import annotations

import json
import logging
import hashlib
import re
import threading
import time
from bit import product_access
from erp.mercadolibre_page_facts import repair_source_facts
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Mapping


TASK_TABLE = "erp_mercadolibre_collection_tasks"
COLLECTION_TABLE = "erp_mercadolibre_collection_items"
PRODUCT_TABLE = "erp_mercadolibre_products"
PUBLISH_RECORD_TABLE = "erp_mercadolibre_publish_records"
PRODUCT_PUBLISH_SCHEDULE_TABLE = "erp_mercadolibre_publish_schedules"
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
SOURCE_1688_NUMERIC_FIELDS = {
    "weight_g", "package_length_cm", "package_width_cm", "package_height_cm",
    "net_proceeds_usd", "price",
}
PRODUCT_REVIEW_STATUSES = {
    "unreviewed", "approved", "failed", "suspected", "infringing", "risk",
}
PRODUCT_PUBLISH_RECORD_STATUSES = {"pending", "publishing", "published", "failed", "pending_review"}
# A lingering ``publishing`` record has an unknown remote outcome. Retrying it
# can create a duplicate listing, so only attempts known not to be in flight are
# eligible for another submission.
PRODUCT_PUBLISH_RETRYABLE_STATUSES = {"pending", "failed"}
PRODUCT_PUBLISH_FILTER_STATUSES = PRODUCT_PUBLISH_RECORD_STATUSES | {"unpublished"}
PRODUCT_PUBLISH_RATE_LIMIT_RETRY_HOURS = 2
ZYING_PROFITABILITY_SOURCE = "zying_collection"
PURCHASE_PROFITABILITY_SOURCE = "purchase_cost_ratio"
MANUAL_PRODUCT_PROFITABILITY_SOURCE = "manual_product_edit"
COLLECTION_WORKFLOW_COLUMN_DEFINITIONS = (
    ("review_status", "VARCHAR(32) NOT NULL DEFAULT 'unreviewed' AFTER `added_to_products`"),
    ("last_publish_status", "VARCHAR(32) NULL AFTER `review_status`"),
    ("ai_copy_json", "LONGTEXT NULL AFTER `description_json`"),
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
    ("infringement_check_json", "LONGTEXT NULL"),
)


def _copy_list_infringement_metadata(cursor, source, target, ids=None):
    """Re-key complete screening and human decisions before deleting the old key."""
    from bit.bit_mysql import INFRINGEMENT_CHECK_METADATA_COLUMNS
    if (source, target) not in {("collection_list", "product_list"), ("product_list", "collection_list")}:
        raise ValueError("侵权结果迁移方向无效")
    if source == "collection_list":
        source_id, target_id = "collections.`id`", "products.`id`"
        joined = f"`{PRODUCT_TABLE}` products INNER JOIN `{COLLECTION_TABLE}` collections ON products.`collection_item_id`=collections.`id`"
    else:
        source_id, target_id = "products.`id`", "collections.`id`"
        joined = f"`{PRODUCT_TABLE}` products INNER JOIN `{COLLECTION_TABLE}` collections ON products.`source_item_id`=collections.`source_item_id` AND products.`organization_key`=collections.`organization_key`"
    assignments = ", ".join(f"target.`{name}`=source.`{name}`" for name in INFRINGEMENT_CHECK_METADATA_COLUMNS)
    clause = ""
    values = ()
    if ids is not None:
        if not ids:
            return
        clause = f" AND {source_id} IN ({', '.join(['%s'] * len(ids))})"
        values = tuple(ids)
    cursor.execute(
        f"UPDATE {joined} INNER JOIN `infringement_risk_checks` source "
        f"ON source.`source_type`='{source}' AND source.`source_row_id`={source_id} "
        f"INNER JOIN `infringement_risk_checks` target "
        f"ON target.`source_type`='{target}' AND target.`source_row_id`={target_id} "
        f"SET {assignments} WHERE 1=1{clause}", values)

_schema_lock = threading.Lock()
_schema_ready = False
PROFITABILITY_INVALIDATION_BATCH_SIZE = 1000
RETRYABLE_TRANSACTION_ERROR_CODES = {1205, 1213}


def _connect() -> Any:
    from bit.bit_mysql import config, pymysql

    return pymysql.connect(**config)


def _connect_list() -> Any:
    """Separate bounded read connections; do not change write job timeouts."""
    from bit.bit_mysql import config, pymysql

    return pymysql.connect(**dict(
        config, connect_timeout=3, read_timeout=10, write_timeout=5,
    ))


def _now() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _schedule_utc_iso(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return str(value or "")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def product_number(product_id: int) -> str:
    """Stable nine-digit identifier backed by the product table's unique PK.

    Existing and newly inserted products use the same mapping without a backfill
    or a second sequence. The 9xxxxxxxx range is reserved for collection items.
    Never truncate/wrap IDs, which would cause collisions.
    """
    product_id = int(product_id)
    if not 1 <= product_id <= 799_999_999:
        raise ValueError("产品 ID 超出 9 位产品编号范围")
    return str(100_000_000 + product_id)


def collection_product_number(collection_id: int) -> str:
    """Use the persisted collection identity before and after claiming a product."""
    collection_id = int(collection_id)
    if not 1 <= collection_id <= 99_999_999:
        raise ValueError("采集 ID 超出 9 位产品编号范围")
    return str(900_000_000 + collection_id)


def _json_safe_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    if "collection_item_id" in result and "source_type" in result:
        if result["source_type"] == "collected" and result.get("collection_item_id"):
            result["product_number"] = collection_product_number(result["collection_item_id"])
        else:
            result["product_number"] = product_number(result["id"])
    elif "task_id" in result and result.get("id"):
        result["product_number"] = collection_product_number(result["id"])
    for key, value in tuple(result.items()):
        if isinstance(value, datetime):
            result[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(value, Decimal):
            result[key] = float(value)
        elif isinstance(value, bytes):
            result[key] = value.decode("utf-8", errors="replace")
    from erp.sku_matrix import sku_count
    result['sku_count'] = sku_count(_loads(result.get('source_snapshot_json') or result.get('source_json'), {}) or {})
    result["ai_weight_price"] = _loads(result.get("ai_weight_price_json"), {})
    collection_copy = _loads(result.get("ai_copy_json"), {})
    if not collection_copy:
        snapshot = _loads(result.get("source_snapshot_json"), {})
        collection_copy = snapshot.get("ai_copy") if isinstance(snapshot, Mapping) else {}
    result["ai_copy"] = collection_copy if isinstance(collection_copy, Mapping) else {}
    result["added_to_products"] = bool(result.get("added_to_products"))
    result["actual_weight_complete"] = has_valid_actual_weight(result)
    result["weight_dimensions_complete"] = has_complete_weight_dimensions(result)
    if "task_id" in result:
        from erp.mercadolibre_collection_attributes import collection_category_id
        result["category_id"] = collection_category_id(result)
        result["collector_salesperson"] = str(result.get("owner_name") or result.get("salesperson") or "").strip()
        actor = product_access.actor()
        result["can_claim"] = bool(actor and product_access.can_operate(result, actor))
        from erp.mercadolibre_collection_quality import collection_quality_issues
        result["collection_quality_issues"] = collection_quality_issues(result)
    user = product_access.actor()
    if user:
        result["can_operate"] = product_access.can_operate(result, user)
        result["can_manage_visibility"] = result["can_operate"]
    return result


def _collection_personnel_predicate(name_expression, organization_expression):
    """Task creator strings are not a personnel directory."""
    return (
        "EXISTS (SELECT 1 FROM `workbench_users` AS collection_person "
        "WHERE collection_person.`is_active` = 1 "
        f"AND collection_person.`organization_key` = {organization_expression} "
        f"AND ({name_expression} = TRIM(collection_person.`username`) "
        f"OR {name_expression} = TRIM(collection_person.`display_name`)))"
    )


def _sync_original_package_attributes(prepared, row):
    """Column measurements are authoritative, including for legacy draft attributes."""
    fields = {"LENGTH": "package_length_cm", "WIDTH": "package_width_cm",
              "HEIGHT": "package_height_cm", "WEIGHT": "weight_g"}
    attributes = [dict(item) for item in prepared.get("attributes") or [] if isinstance(item, Mapping)]
    for suffix, field in fields.items():
        if field not in row:
            continue
        ids = {"PACKAGE_" + suffix, "SELLER_PACKAGE_" + suffix}
        targets = [item for item in attributes if str(item.get("id", "")).upper() in ids]
        value = row.get(field)
        if value in (None, ""):
            attributes = [item for item in attributes if str(item.get("id", "")).upper() not in ids]
            continue
        if not targets:
            targets = [{"id": "PACKAGE_" + suffix}]
            attributes.extend(targets)
        for item in targets:
            for key in ("value_id", "values", "value_struct", "value_name_es", "value_name_pt"):
                item.pop(key, None)
            item["value_name"] = f"{format(Decimal(str(value)), 'f')} {'g' if suffix == 'WEIGHT' else 'cm'}"
    prepared["attributes"] = attributes
    for field in (*fields.values(), "net_proceeds_usd"):
        if field in row:
            prepared[field] = row[field]


def _mirror_zying_snapshot_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """Expose workflow metadata stored in the source snapshot as list fields."""
    result = dict(row)
    source_type = str(result.get("source_type") or "").strip().lower()
    if source_type == "ai_original":
        snapshot = _loads(result.get("source_snapshot_json"), {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        from copy import deepcopy
        snapshot = deepcopy(snapshot)
        from erp.sku_matrix import sync_single_sku_cover
        for product in [snapshot.get('original_1688') or {}, snapshot.get('ai_original') or {},
                        snapshot.get('source') or {}, *((snapshot.get('marketplace_listings') or {}).values())]:
            sync_single_sku_cover(product)
        cover = (snapshot.get('ai_original') or {}).get('main_image_url')
        if cover and len((snapshot.get("ai_original") or {}).get("variations") or []) == 1:
            result['main_image_url'] = cover
        for draft in [snapshot.get("ai_original"), snapshot.get("source"), (snapshot.get("marketplace_listings") or {}).get("mercadolibre")]:
            if isinstance(draft, dict):
                _sync_original_package_attributes(draft, result)
        result['source_snapshot_json'] = _dumps(snapshot)
        original = snapshot.get("original_1688") if isinstance(snapshot, dict) else {}
        prepared = snapshot.get("ai_original") if isinstance(snapshot, dict) else {}
        from erp.ai_original_state import category_failure_state, source_input_state
        prepared = category_failure_state(result, prepared if isinstance(prepared, dict) else {})
        prepared = source_input_state(prepared, original if isinstance(original, dict) else {})
        result["original_1688"] = original if isinstance(original, dict) else {}
        result["ai_original"] = prepared if isinstance(prepared, dict) else {}
        drafts = snapshot.get("marketplace_listings")
        result["marketplace_listings"] = dict(drafts) if isinstance(drafts, Mapping) else {}
        # The legacy Mercado listing remains authoritative after manual edits.
        result["marketplace_listings"]["mercadolibre"] = result["ai_original"]
        overrides = snapshot.get("source_1688_overrides")
        result["source_1688_overrides"] = (
            dict(overrides) if isinstance(overrides, dict) else {}
        )
        overrides = result["source_1688_overrides"]
        for field in (
            "weight_g", "package_length_cm", "package_width_cm", "package_height_cm",
        ):
            effective = overrides[field] if field in overrides else result["original_1688"].get(field)
            if field not in overrides and effective in (None, ""):
                effective = result.get(field)
            number = _decimal_from_text(effective)
            result[f"source_{field}"] = float(number) if number is not None else None
        result["ai_status"] = str(result["ai_original"].get("status") or "pending")
        result["ai_error"] = str(result["ai_original"].get("error") or "")
        result["title_es"] = str(result["ai_original"].get("title_es") or "")
        result["title_pt"] = str(result["ai_original"].get("title_pt") or "")
        result["description_es"] = str(result["ai_original"].get("description_es") or "")
        result["description_pt"] = str(result["ai_original"].get("description_pt") or "")
        from erp.ai_original_products import (
            ai_original_price_bounds_cny,
            suggested_ai_original_net_proceeds,
        )

        variations = result["original_1688"].get("variations")
        variation_min, variation_max = ai_original_price_bounds_cny(variations)
        raw_product_price = result["original_1688"].get("price")
        product_min, _ = ai_original_price_bounds_cny([], fallback_price=raw_product_price)
        if product_min is None and "price" not in result["original_1688"]:
            raw_product_price = result.get("price")
            product_min, _ = ai_original_price_bounds_cny([], fallback_price=raw_product_price)
        result["source_purchase_price_cny"] = (
            product_min if product_min is not None else variation_min
        )
        if "price" in overrides:
            raw_product_price = overrides["price"]
            result["source_purchase_price_cny"] = raw_product_price
        max_price, suggested_net = suggested_ai_original_net_proceeds(
            [] if "price" in overrides else variations, fallback_price=raw_product_price
        )
        result["source_variation_max_price_cny"] = variation_max
        result["source_net_calculation_price_cny"] = max_price
        result["suggested_net_price_basis"] = (
            "manual_purchase_price" if "price" in overrides and max_price is not None
            else "" if "price" in overrides
            else "sku" if variation_max is not None
            else "product_price" if max_price is not None else ""
        )
        result["suggested_net_proceeds_usd"] = suggested_net
        result["source_net_proceeds_usd"] = (
            overrides["net_proceeds_usd"] if "net_proceeds_usd" in overrides else suggested_net
        )
        plugin = snapshot.get("plugin_snapshot") or {}
        result["collector_salesperson"] = str(plugin.get("created_by") or "").removeprefix("浏览器插件：").strip()
        result["generator_salesperson"] = str(result["ai_original"].get("generated_by") or "").strip()
        result["salesperson"] = str(
            result.get("salesperson") or plugin.get("salesperson")
            or result["collector_salesperson"] or result["generator_salesperson"] or ""
        ).strip()
        return result
    snapshot = _loads(result.get("source_snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    plugin_snapshot = snapshot.get("plugin_snapshot")
    if not isinstance(plugin_snapshot, dict):
        plugin_snapshot = {}
    result["salesperson"] = str(
        result.get("salesperson") or snapshot.get("salesperson")
        or plugin_snapshot.get("salesperson") or plugin_snapshot.get("created_by") or ""
    ).removeprefix("浏览器插件：").strip()
    if source_type != "zying":
        return result
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
    if columns == ["organization_key", "task_id", "source_item_id"]:
        return False
    if rows:
        cursor.execute(
            f"ALTER TABLE `{COLLECTION_TABLE}` DROP INDEX `{index_name}`"
        )
    cursor.execute(
        f"ALTER TABLE `{COLLECTION_TABLE}` "
        f"ADD UNIQUE KEY `{index_name}` (`organization_key`, `task_id`, `source_item_id`)"
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
            `ai_copy_json` LONGTEXT NULL,
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
            `listing_ratio_percent` DECIMAL(10,4) NOT NULL DEFAULT 100.0000,
            `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_management_category_name` (`name`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    _ensure_column(
        cursor, MANAGEMENT_CATEGORY_TABLE, "listing_ratio_percent",
        "DECIMAL(10,4) NOT NULL DEFAULT 100.0000 AFTER `name`",
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
            `auto_retry_started_at` DATETIME NULL,
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
    for access_table in (COLLECTION_TABLE, PRODUCT_TABLE):
        for column, definition in (
            ("organization_key", "VARCHAR(64) NOT NULL DEFAULT 'wuhan-zeshun'"),
            ("owner_user_id", "BIGINT NULL"),
            ("owner_name", "VARCHAR(128) NULL"),
            ("visibility", "VARCHAR(16) NOT NULL DEFAULT 'enterprise'"),
        ):
            added = _ensure_column(cursor, access_table, column, definition)
            if added and column == "owner_name":
                if access_table == COLLECTION_TABLE:
                    cursor.execute(f"UPDATE `{COLLECTION_TABLE}` AS items "
                        f"JOIN `{TASK_TABLE}` AS tasks ON tasks.`id` = items.`task_id` "
                        "SET items.`owner_name` = TRIM(REPLACE(COALESCE(tasks.`created_by`, ''), '浏览器插件：', ''))")
                else:
                    cursor.execute(f"UPDATE `{PRODUCT_TABLE}` AS products "
                        f"LEFT JOIN `{COLLECTION_TABLE}` AS items ON items.`id` = products.`collection_item_id` "
                        "SET products.`owner_name` = COALESCE(NULLIF(items.`owner_name`, ''), "
                        "TRIM(REPLACE(COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT("
                        "IF(JSON_VALID(products.`source_snapshot_json`), products.`source_snapshot_json`, '{}'), "
                        "'$.plugin_snapshot.created_by')), 'null'), ''), '浏览器插件：', '')))")
        _ensure_index(cursor, access_table, "idx_product_enterprise_visibility", "(`organization_key`, `visibility`, `id`)")
    cursor.execute("SHOW TABLES LIKE 'mercado_store_tokens'")
    if cursor.fetchone():
        cursor.execute(f"UPDATE `{PRODUCT_TABLE}` AS products "
                       "JOIN `mercado_store_tokens` AS tokens ON tokens.`id` = products.`last_publish_token_id` "
                       "SET products.`organization_key` = CASE WHEN tokens.`organization_key` IN ('', 'default') "
                       "THEN 'wuhan-zeshun' ELSE tokens.`organization_key` END "
                       "WHERE products.`owner_user_id` IS NULL AND tokens.`organization_key` IS NOT NULL")
    cursor.execute("SHOW TABLES LIKE 'workbench_users'")
    if cursor.fetchone():
        # Global uniqueness is required before changing the historical company.
        owners_sql = """(SELECT alias_name, MIN(id) AS user_id, MIN(organization_key) AS organization_key
            FROM (SELECT id, display_name AS alias_name, organization_key FROM workbench_users
                  UNION SELECT id, username AS alias_name, organization_key FROM workbench_users) AS aliases
            WHERE alias_name IS NOT NULL AND alias_name <> ''
            GROUP BY alias_name HAVING COUNT(DISTINCT id) = 1)"""
        for access_table in (COLLECTION_TABLE, PRODUCT_TABLE):
            cursor.execute(f"UPDATE `{access_table}` AS items JOIN {owners_sql} AS owners "
                           "ON owners.alias_name = items.`owner_name` "
                           "SET items.`owner_user_id` = owners.user_id, items.`organization_key` = "
                           "CASE WHEN owners.organization_key IN ('', 'default') THEN 'wuhan-zeshun' "
                           "ELSE owners.organization_key END WHERE items.`owner_user_id` IS NULL "
                           "AND owners.organization_key IS NOT NULL")

    cursor.execute(f"SHOW INDEX FROM `{PRODUCT_TABLE}` WHERE Key_name = 'uniq_erp_meli_product_item'")
    unique_columns = [row.get("Column_name") for row in sorted(cursor.fetchall() or [], key=lambda row: int(row.get("Seq_in_index") or 0))]
    if unique_columns == ["source_item_id"]:
        cursor.execute(f"ALTER TABLE `{PRODUCT_TABLE}` DROP INDEX `uniq_erp_meli_product_item`, "
                       "ADD UNIQUE KEY `uniq_erp_meli_product_item` (`organization_key`, `source_item_id`)")

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
        cursor, PUBLISH_RECORD_TABLE, "auto_retry_started_at",
        "DATETIME NULL AFTER `result_json`",
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
        _ensure_column(cursor, table, "ai_weight_price_json", "LONGTEXT NULL")
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
        from bit.bit_mysql import _ensure_infringement_risk_checks_table
        _ensure_infringement_risk_checks_table(cursor)
        risk_assignments = (
            "target.`infringement_risk_level` = risks.`risk_level`, "
            "target.`infringement_keywords` = risks.`keywords`, "
            "target.`infringement_reason` = risks.`reason`, "
            "target.`infringement_checked_at` = risks.`checked_at`, "
            "target.`infringement_check_json` = COALESCE(target.`infringement_check_json`, risks.`details_json`)"
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
        _copy_list_infringement_metadata(cursor, "collection_list", "product_list")
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
        (COLLECTION_TABLE, "ai_weight_price_json"),
        (PRODUCT_TABLE, "ai_weight_price_json"),
        (TASK_TABLE, "worker_count"),
        (TASK_TABLE, "elapsed_seconds"),
        (COLLECTION_TABLE, "volumetric_weight_kg"),
        (COLLECTION_TABLE, "profitability_error"),
        (COLLECTION_TABLE, "added_to_products"),
        (COLLECTION_TABLE, "review_status"),
        (COLLECTION_TABLE, "last_publish_status"),
        (COLLECTION_TABLE, "ai_copy_json"),
        (COLLECTION_TABLE, "management_category_id"),
        (PRODUCT_TABLE, "source_type"),
        (PRODUCT_TABLE, "review_status"),
        (PRODUCT_TABLE, "description_text"),
        (PRODUCT_TABLE, "product_developer_id"),
        (PRODUCT_TABLE, "product_developer_name"),
        (PRODUCT_TABLE, "profitability_error"),
        (PRODUCT_TABLE, "last_published_at"),
        (PRODUCT_TABLE, "management_category_id"),
        (MANAGEMENT_CATEGORY_TABLE, "name"),
        (MANAGEMENT_CATEGORY_TABLE, "listing_ratio_percent"),
        (PUBLISH_RECORD_TABLE, "failure_reason"),
        (PUBLISH_RECORD_TABLE, "result_json"),
        (PUBLISH_RECORD_TABLE, "auto_retry_started_at"),
    }
    required.update((table, column) for table in (COLLECTION_TABLE, PRODUCT_TABLE)
                    for column in ("organization_key", "owner_user_id", "owner_name", "visibility"))
    required.update(
        (table, column)
        for table in (COLLECTION_TABLE, PRODUCT_TABLE)
        for column, _definition in INFRINGEMENT_RESULT_COLUMN_DEFINITIONS
    )
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
        cursor.execute(f"SHOW INDEX FROM `{PRODUCT_TABLE}` WHERE Key_name = 'uniq_erp_meli_product_item'")
        unique_columns = [row.get("Column_name") for row in sorted(cursor.fetchall() or [], key=lambda row: int(row.get("Seq_in_index") or 0))]
        if unique_columns == ["source_item_id"]:
            cursor.execute(f"ALTER TABLE `{PRODUCT_TABLE}` DROP INDEX `uniq_erp_meli_product_item`, "
                           "ADD UNIQUE KEY `uniq_erp_meli_product_item` (`organization_key`, `source_item_id`)")
        from erp.product_sku_groups import ensure_table as ensure_sku_groups
        ensure_sku_groups(cursor)
        from erp.product_sku_materialize import ensure_table as ensure_sku_members
        ensure_sku_members(cursor)
        _ensure_collection_task_unique_index(cursor)
        _ensure_index(cursor, PRODUCT_TABLE, "idx_product_source_enterprise", "(`source_item_id`, `organization_key`)" )
        _ensure_index(
            cursor, PRODUCT_TABLE, "idx_erp_meli_product_collection",
            "(`collection_item_id`)",
        )
        _ensure_index(
            cursor, PRODUCT_TABLE, "idx_erp_meli_product_owner",
            "(`last_publish_token_id`)",
        )
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{PRODUCT_PUBLISH_SCHEDULE_TABLE}` (
                `id` BIGINT NOT NULL AUTO_INCREMENT,
                `batch_id` VARCHAR(64) NOT NULL,
                `scheduled_at` DATETIME NOT NULL,
                `status` VARCHAR(32) NOT NULL DEFAULT 'scheduled',
                `payload_json` LONGTEXT NOT NULL,
                `metadata_json` LONGTEXT NULL,
                `result_json` LONGTEXT NULL,
                `message` TEXT NULL,
                `created_by` VARCHAR(128) NULL,
                `started_at` DATETIME NULL,
                `finished_at` DATETIME NULL,
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (`id`),
                UNIQUE KEY `uniq_erp_meli_publish_schedule_batch` (`batch_id`),
                KEY `idx_erp_meli_publish_schedule_due` (`status`, `scheduled_at`, `id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        _schema_ready = True


def create_product_publish_schedule(
    scheduled_at: str,
    payload: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any] | None = None,
    batch_id: str,
    created_by: str = "",
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    """Persist a prepared product publish run for the central scheduler."""
    run_at = str(scheduled_at or "").strip()
    batch = str(batch_id or "").strip()
    if not run_at or not batch:
        raise ValueError("定时上架缺少触发时间或任务编号")
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"""
                INSERT INTO `{PRODUCT_PUBLISH_SCHEDULE_TABLE}`
                    (`batch_id`, `scheduled_at`, `status`, `payload_json`,
                     `metadata_json`, `message`, `created_by`)
                VALUES (%s, %s, 'scheduled', %s, %s, %s, %s)
                """,
                (
                    batch,
                    run_at,
                    _dumps(dict(payload or {})),
                    _dumps(dict(metadata or {})),
                    "等待定时触发",
                    str(created_by or "")[:128],
                ),
            )
            schedule_id = int(cursor.lastrowid)
        connection.commit()
        return schedule_id
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_latest_product_publish_schedule(
    *, connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any] | None:
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_PUBLISH_SCHEDULE_TABLE}` "
                "ORDER BY `id` DESC LIMIT 1"
            )
            row = cursor.fetchone()
        connection.commit()
        if not row:
            return None
        result = _json_safe_row(row)
        result["scheduled_at"] = _schedule_utc_iso(result.get("scheduled_at"))
        result["payload"] = _loads(result.pop("payload_json", None), {})
        result["metadata"] = _loads(result.pop("metadata_json", None), {})
        result["result"] = _loads(result.pop("result_json", None), {})
        return result
    finally:
        connection.close()


def claim_due_product_publish_schedule(
    now: str,
    *, connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any] | None:
    """Atomically claim the earliest due schedule so it cannot fire twice."""
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_PUBLISH_SCHEDULE_TABLE}` "
                "WHERE `status` = 'scheduled' AND `scheduled_at` <= %s "
                "ORDER BY `scheduled_at`, `id` LIMIT 1 FOR UPDATE",
                (str(now),),
            )
            row = cursor.fetchone()
            if not row:
                connection.commit()
                return None
            schedule_id = int(row.get("id") or 0)
            cursor.execute(
                f"UPDATE `{PRODUCT_PUBLISH_SCHEDULE_TABLE}` "
                "SET `status` = 'running', `message` = %s, "
                "`started_at` = COALESCE(`started_at`, %s) WHERE `id` = %s "
                "AND `status` = 'scheduled'",
                ("定时上架已触发", _now(), schedule_id),
            )
            if int(cursor.rowcount or 0) != 1:
                connection.rollback()
                return None
        connection.commit()
        result = _json_safe_row(row)
        result["scheduled_at"] = _schedule_utc_iso(result.get("scheduled_at"))
        result["payload"] = _loads(result.pop("payload_json", None), {})
        result["metadata"] = _loads(result.pop("metadata_json", None), {})
        result["result"] = _loads(result.pop("result_json", None), {})
        result["status"] = "running"
        result["message"] = "定时上架已触发"
        result["started_at"] = _now()
        return result
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def recover_interrupted_product_publish_schedules(
    *, connection_factory: Callable[[], Any] | None = None,
) -> int:
    """Put claimed schedules back in the durable queue after a service restart."""
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"UPDATE `{PRODUCT_PUBLISH_SCHEDULE_TABLE}` "
                "SET `status` = 'scheduled', `message` = %s, `started_at` = NULL "
                "WHERE `status` = 'running'",
                ("服务重启后已恢复，等待触发",),
            )
            recovered = int(cursor.rowcount or 0)
        connection.commit()
        return recovered
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_product_publish_schedule(
    schedule_id: int,
    *,
    status: str,
    message: str = "",
    result: Mapping[str, Any] | None = None,
    finished: bool = False,
    connection_factory: Callable[[], Any] | None = None,
) -> None:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"scheduled", "running", "completed", "partial", "error"}:
        raise ValueError("定时上架任务状态无效")
    assignments = ["`status` = %s", "`message` = %s"]
    values: list[Any] = [normalized_status, str(message or "")[:2000]]
    if result is not None:
        assignments.append("`result_json` = %s")
        values.append(_dumps(dict(result)))
    if finished:
        assignments.append("`finished_at` = %s")
        values.append(_now())
    values.append(int(schedule_id))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"UPDATE `{PRODUCT_PUBLISH_SCHEDULE_TABLE}` "
                f"SET {', '.join(assignments)} WHERE `id` = %s",
                tuple(values),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


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


def normalize_collection_management_category_id(value: Any) -> int | None:
    """Parse an optional destination category without accepting lossy numbers."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d{0,18}", text):
        raise ValueError("泽顺产品分类编号必须是正整数")
    category_id = int(text)
    if category_id > 9223372036854775807:
        raise ValueError("泽顺产品分类编号无效")
    return category_id


def _validate_collection_management_categories(cursor, category_ids) -> None:
    # Hold the category row until the product write commits so a concurrent
    # category deletion cannot leave a successful collection pointing nowhere.
    for category_id in sorted(set(category_ids)):
        cursor.execute(
            f"SELECT `id` FROM `{MANAGEMENT_CATEGORY_TABLE}` WHERE `id` = %s FOR UPDATE",
            (category_id,),
        )
        if not cursor.fetchone():
            raise ValueError("所选泽顺产品分类不存在，请刷新分类后重试")


def _execute_access_insert(cursor, sql, values, *, many=False, inherited=None):
    """Attach enterprise ownership to INSERT values in the same transaction."""
    user = product_access.actor() or {}
    inherited = inherited or {}
    access_values = (
        inherited.get("organization_key") or product_access.organization(user),
        inherited["owner_user_id"] if "owner_user_id" in inherited else user.get("id"),
        inherited["owner_name"] if "owner_name" in inherited else (user.get("display_name") or user.get("username") or ""),
        inherited.get("visibility") or "enterprise",
    )
    if user.get("access_version") == 1:
        table = re.search(r"INSERT INTO `([^`]+)`", sql).group(1)
        columns = re.search(r"INSERT INTO `[^`]+`\s*\((.*?)\)\s*VALUES", sql, re.S).group(1)
        column_names = re.findall(r"`([^`]+)`", columns)
        expressions = re.search(r"VALUES\s*\((.*?)\)", sql, re.S).group(1).split(',')
        source_position = column_names.index("source_item_id")
        param_index = sum(expression.count('%s') for expression in expressions[:source_position])
        batches = values if many else [values]
        sources = sorted({str(record[param_index]) for record in batches})
        cursor.execute(f"SELECT `id`, `organization_key`, `owner_user_id`, `owner_name`, `visibility` "
                       f"FROM `{table}` WHERE `organization_key` = %s AND `source_item_id` IN "
                       f"({', '.join(['%s'] * len(sources))}) FOR UPDATE", (access_values[0], *sources))
        if any(not product_access.can_operate(row, user) for row in cursor.fetchall()):
            raise PermissionError("只能重新采集自己的产品")
    sql = re.sub(r"(\)\s*VALUES\s*\()", r", `organization_key`, `owner_user_id`, `owner_name`, `visibility`\1", sql, count=1, flags=re.I)
    sql = re.sub(r"(VALUES\s*\([^)]*)(\))", r"\1, %s, %s, %s, %s\2", sql, count=1, flags=re.I)
    if many:
        return cursor.executemany(sql, [tuple(row) + access_values for row in values])
    return cursor.execute(sql, tuple(values) + access_values)


def _execute_source_scoped(cursor, sql, values):
    """Source identifiers are unique within an enterprise."""
    sql = re.sub(r"\s+(ORDER BY|LIMIT|FOR UPDATE)\b", r" AND `organization_key` = %s \1", sql, count=1, flags=re.I) if re.search(r"\s+(ORDER BY|LIMIT|FOR UPDATE)\b", sql, re.I) else sql + " AND `organization_key` = %s"
    return cursor.execute(sql, tuple(values) + (product_access.organization(),))


def upsert_collection_items(
    task_id: int,
    rows: Iterable[Mapping[str, Any]],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    records = [dict(row) for row in rows or []]
    if not records:
        return 0
    category_ids = set()
    from erp.mercadolibre_collection_attributes import collection_category_id
    for row in records:
        row["category_id"] = collection_category_id(row) or None
        row["management_category_id"] = normalize_collection_management_category_id(
            row.get("management_category_id")
        )
        if row["management_category_id"] is not None:
            category_ids.add(row["management_category_id"])
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            _validate_collection_management_categories(cursor, category_ids)
            # Every ingestion path (including the browser extension and DB
            # API) must save freight and proceeds together. Use this cursor:
            # opening a second connection here can exhaust small worker pools.
            from erp.mercadolibre_profitability import (
                estimate_collection_profitability_from_rate_card,
                FIXED_COLLECTION_PROFITABILITY_SOURCE,
            )
            from erp.mercadolibre_shipping_rate_cards import (
                OFFICIAL_SHIPPING_RATE_CARD_TABLE,
            )
            try:
                cursor.execute(
                    f"SELECT * FROM `{OFFICIAL_SHIPPING_RATE_CARD_TABLE}`"
                )
                rate_rows = tuple(cursor.fetchall() or [])
            except Exception:
                # Collection remains available before rate-card bootstrap.
                rate_rows = ()
            for row in records:
                if row.get("profitability_source") != FIXED_COLLECTION_PROFITABILITY_SOURCE:
                    # The plugin's reference net may use volumetric freight
                    # and a minimum revenue floor. Never persist that as this
                    # workflow's actual-weight-only calculation, or keep an
                    # old net after a price/weight change.
                    for column in PROFITABILITY_COLUMNS:
                        if column not in {
                            "category_id", "category_name",
                            "listing_type_id", "listing_type_name",
                        }:
                            row[column] = None
                    row["profitability_error"] = ""
                    row["_replace_profitability_snapshot"] = True
                estimate = estimate_collection_profitability_from_rate_card(
                    row, rate_rows=rate_rows,
                )
                if estimate:
                    row.update(estimate)
                    row["_replace_profitability_snapshot"] = True
            # Resolve the USD display price for rows without a complete
            # rate-card snapshot from the one stored rate per currency.
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
                # A rate-card estimate already carries a coherent sale price,
                # commission, freight and net value from the same daily FX
                # snapshot. Do not replace only its sale-price FX here.
                if (
                    row.get("net_proceeds_usd") is not None
                    and row.get("commission_amount_usd") is not None
                    and row.get("shipping_fee_usd") is not None
                ):
                    continue
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
                # 美客多跟卖采集使用固定 15% 佣金；官方表命中时采集阶段已
                # 按当前实重运费和同一汇率快照算出净收益。
                row["profitability_source"] = "fixed_commission_15_pct"
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
                    _dumps(repair_source_facts(row.get("source") or {})),
                    _dumps(row.get("description") or {}),
                    _dumps(row.get("page_snapshot") or {}),
                    _dumps(row.get("plugin_snapshot") or {}),
                    str(row.get("collected_at") or _now()),
                )
                category_column_sql = ""
                category_update_sql = ""
                if row["management_category_id"] is not None:
                    values += (row["management_category_id"],)
                    category_column_sql = ", `management_category_id`"
                    category_update_sql = (
                        "`management_category_id` = VALUES(`management_category_id`),"
                    )
                _execute_access_insert(cursor, f"""
                    INSERT INTO `{COLLECTION_TABLE}` (
                        `task_id`, `source_item_id`, `source_url`, `final_url`,
                        `main_image_url`, `title`, `price`, `currency_id`, `weight_g`,
                        `volumetric_weight_kg`,
                        `package_length_cm`, `package_width_cm`, `package_height_cm`,
                        `weight_basis`, {profitability_columns_sql},
                        `scrape_status`, `error_message`, `source_json`,
                        `description_json`, `page_snapshot_json`, `plugin_snapshot_json`,
                        `collected_at`{category_column_sql}
                    ) VALUES ({", ".join(["%s"] * len(values))})
                    ON DUPLICATE KEY UPDATE
                        {category_update_sql}
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
                    """, values)
                if row["management_category_id"] is not None:
                    _execute_source_scoped(cursor, f"UPDATE `{PRODUCT_TABLE}` SET `management_category_id` = %s "
                        "WHERE `source_item_id` = %s", (row["management_category_id"], values[1]))
        connection.commit()
        return len(records)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _product_token_scope(
    token_ids: Iterable[int] | None,
) -> tuple[str, str, list[int]]:
    """Share product ownership rules between rows and filter directories."""
    if token_ids is None:
        return "", "", []
    scoped_token_ids = sorted({
        int(value) for value in token_ids or () if int(value or 0) > 0
    })
    if not scoped_token_ids:
        return "", "1 = 0", []
    placeholders = ", ".join(["%s"] * len(scoped_token_ids))
    # Materialize the authorized IDs once. Correlated EXISTS under OR
    # otherwise probes both ownership tables for every product. UNION
    # deduplicates all three ownership paths before paging or aggregation.
    authorized_ids_sql = (
        f"SELECT `id` FROM `{PRODUCT_TABLE}` "
        f"WHERE `last_publish_token_id` IN ({placeholders}) UNION "
        f"SELECT own_records.`product_item_id` AS `id` FROM `{PUBLISH_RECORD_TABLE}` AS own_records "
        f"INNER JOIN `{PRODUCT_TABLE}` AS recorded_products "
        "ON recorded_products.`id` = own_records.`product_item_id` "
        f"WHERE own_records.`token_id` IN ({placeholders}) UNION "
        f"SELECT linked_products.`id` FROM `erp_mercadolibre_store_links` AS own_links "
        f"INNER JOIN `{PRODUCT_TABLE}` AS linked_products "
        "ON linked_products.`source_item_id` = own_links.`item_id` "
        f"WHERE own_links.`token_id` IN ({placeholders})"
    )
    predicate = (
        "`id` IN (SELECT authorized_products.`id` FROM ("
        + authorized_ids_sql + ") AS authorized_products)"
    )
    return authorized_ids_sql, predicate, scoped_token_ids * 3



def _product_salesperson_expression() -> str:
    """Zeshun staff ownership is persisted; never rescan source JSON per product."""
    return f"COALESCE(NULLIF(`{PRODUCT_TABLE}`.`owner_name`, ''), '未分配')"


def _read_product_salespeople(cursor, token_ids=None):
    if product_access.actor():
        token_ids = None
    access, access_params = product_access.predicate(PRODUCT_TABLE)
    _, scope, scope_params = _product_token_scope(token_ids)
    where = " AND ".join(filter(None, (access, scope))) or "1 = 1"
    # The covering index contains ownership and visibility. A hard server-side
    # deadline also protects older clients still requesting options with rows.
    cursor.execute(
        f"SELECT /*+ MAX_EXECUTION_TIME(2000) INDEX(`{PRODUCT_TABLE}` `idx_product_salesperson`) */ "
        f"`owner_name` AS salesperson "
        f"FROM `{PRODUCT_TABLE}` WHERE {where} "
        "GROUP BY `organization_key`, `owner_name` ORDER BY salesperson",
        tuple(access_params + scope_params),
    )
    return sorted({row["salesperson"] or "未分配" for row in cursor.fetchall()})


def list_product_salespeople(*, token_ids=None, connection_factory=None):
    connection = (connection_factory or _connect_list)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            return {"salespeople": _read_product_salespeople(cursor, token_ids)}
    finally:
        connection.close()


def _list_rows(
    table: str,
    *,
    search: str = "",
    limit: int = 500,
    offset: int = 0,
    include_total: bool = True,
    group_skus: bool = False,
    task_id: int | None = None,
    source_type: str = "",
    ai_status: str = "",
    target_platform: str = "mercadolibre",
    salesperson: str = "",
    include_salesperson_options: bool = False,
    collector_salesperson: str = "",
    collector_salesperson_exact: bool = False,
    generator_salesperson: str = "",
    include_generator_options: bool = False,
    review_status: str = "",
    publish_status: str = "",
    weight_status: str = "",
    management_category_id: Any = None,
    mercado_category: str = "",
    zying_category: str = "",
    zying_status: str = "",
    product_developer_id: str = "",
    weight_min: Any = None,
    weight_max: Any = None,
    price_min: Any = None,
    price_max: Any = None,
    sku_count_min: Any = None,
    sku_count_max: Any = None,
    net_proceeds_min: Any = None,
    net_proceeds_max: Any = None,
    date_from: str = "",
    date_to: str = "",
    exclude_added: bool = False,
    token_ids: Iterable[int] | None = None,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    # Only fixed JSON paths may participate in SQL; never interpolate a request path.
    platform_paths = {
        "mercadolibre": "$.ai_original",
        "yandex": "$.marketplace_listings.yandex",
        "amazon": "$.marketplace_listings.amazon",
    }
    target_platform = str(target_platform or "mercadolibre").strip().lower()
    if target_platform not in platform_paths:
        raise ValueError("不支持的目标平台，请选择美客多、Yandex 或亚马逊美国站")
    listing_path = platform_paths[target_platform]
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    where: list[str] = []
    params: list[Any] = []
    authorized_ids_sql = ""
    authorized_ids_predicate = ""
    access_sql, access_params = product_access.predicate(table)
    if access_sql:
        where.append(access_sql)
        params.extend(access_params)
    search = str(search or "").strip()
    search_predicate = "(`source_item_id` LIKE %s OR `title` LIKE %s)"
    if search:
        where.append(search_predicate)
        pattern = f"%{search}%"
        params.extend((pattern, pattern))
    if task_id is not None and table == COLLECTION_TABLE:
        where.append("`task_id` = %s")
        params.append(int(task_id))
    if table == COLLECTION_TABLE and exclude_added:
        where.append("`added_to_products` = 0")
    salesperson_expression = (
        "TRIM(REPLACE(COALESCE(NULLIF(collection_task.`created_by`, ''), "
        "'未分配'), '浏览器插件：', ''))"
    )
    if table == COLLECTION_TABLE and str(salesperson or "").strip():
        if str(salesperson).strip() == "未分配":
            personnel_predicate = _collection_personnel_predicate(
                salesperson_expression, f"`{COLLECTION_TABLE}`.`organization_key`",
            )
            where.append(
                f"NOT EXISTS (SELECT 1 FROM `{TASK_TABLE}` AS collection_task "
                f"WHERE collection_task.`id` = `{COLLECTION_TABLE}`.`task_id` "
                f"AND {personnel_predicate})"
            )
        else:
            where.append(
                f"EXISTS (SELECT 1 FROM `{TASK_TABLE}` AS collection_task "
                f"WHERE collection_task.`id` = `{COLLECTION_TABLE}`.`task_id` "
                f"AND {salesperson_expression} = %s)"
            )
            params.append(str(salesperson).strip())
    if table == PRODUCT_TABLE and str(salesperson or "").strip():
        if str(salesperson).strip() == "未分配":
            where.append("(`owner_name` IS NULL OR `owner_name` = '')")
        else:
            where.append("`owner_name` = %s")
            params.append(str(salesperson).strip())
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
    mercado_category = str(mercado_category or "").strip()
    if mercado_category.startswith("tree:"):
        from erp.mercadolibre_category_tree import category_filter_ids
        category_ids = category_filter_ids(mercado_category)
        where.append("`category_id` IN (" + ", ".join(["%s"] * len(category_ids)) + ")")
        params.extend(category_ids)
    elif mercado_category:
        mercado_category = mercado_category[:255]
        where.append("(`category_id` = %s OR `category_name` LIKE %s)")
        params.extend((mercado_category, f"%{mercado_category}%"))
    if table == PRODUCT_TABLE:
        if product_access.actor():
            token_ids = None
        authorized_ids_sql, authorized_ids_predicate, scoped_params = _product_token_scope(token_ids)
        if authorized_ids_predicate:
            where.append(authorized_ids_predicate)
            params.extend(scoped_params)
        source_type = str(source_type or "").strip().lower()
        if source_type:
            if source_type not in PRODUCT_SOURCE_TYPES:
                raise ValueError(f"不支持的产品来源: {source_type}")
            where.append("`source_type` = %s")
            params.append(source_type)
        generator_options_where = [clause for clause in where if clause != search_predicate]
        generator_options_params = list(access_params + params[len(access_params) + 2:] if search else params)
        ai_status = str(ai_status or "").strip().lower()
        if ai_status:
            if ai_status not in {"pending", "processing", "completed", "failed"}:
                raise ValueError(f"不支持的 AI 状态: {ai_status}")
            ai_status_expr = (
                "JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(`source_snapshot_json`), "
                f"`source_snapshot_json`, '{{}}'), '{listing_path}.status'))"
            )
            if target_platform == "mercadolibre":
                workflow_expr = (
                    "JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(`source_snapshot_json`), "
                    "`source_snapshot_json`, '{}'), '$.ai_original.workflow_status'))"
                )
                ai_status_expr = (
                    f"CASE WHEN {ai_status_expr} IN ('processing', 'waiting_off_peak', 'failed') "
                    f"THEN {ai_status_expr} WHEN {ai_status_expr} = 'needs_category' "
                    f"OR {workflow_expr} = 'needs_category' OR ({ai_status_expr} = 'completed' "
                    "AND NOT (COALESCE(`category_id`, '') REGEXP '^CBT[0-9]+$' "
                    "AND BINARY LEFT(COALESCE(`category_id`, ''), 3) = BINARY 'CBT')) "
                    f"THEN 'failed' ELSE {ai_status_expr} END"
                )
            where.append(f"COALESCE(NULLIF({ai_status_expr}, ''), 'pending') = %s")
            params.append(ai_status)
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
        zying_status = str(zying_status or "").strip()[:255]
        if zying_status:
            where.append(
                "JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(`source_snapshot_json`), "
                "`source_snapshot_json`, '{}'), '$.plugin_snapshot.zying_status')) = %s"
            )
            params.append(zying_status)
        product_developer_id = str(product_developer_id or "").strip()[:255]
        if product_developer_id:
            where.append(
                "(TRIM(COALESCE(`product_developer_id`, '')) = %s "
                "OR TRIM(COALESCE(`product_developer_name`, '')) = %s)"
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

    from erp.sku_matrix import sku_count_sql
    sku_bounds = [optional_decimal(v, 'SKU 个数', nonnegative=True) for v in (sku_count_min, sku_count_max)]
    if any(v is not None and v != v.to_integral_value() for v in sku_bounds):
        raise ValueError('SKU 个数必须是非负整数')
    if all(v is not None for v in sku_bounds) and sku_bounds[0] > sku_bounds[1]:
        raise ValueError('最少 SKU 个数不能大于最多 SKU 个数')
    for operator, value in zip(('>=', '<='), sku_bounds):
        if value is not None:
            where.append(f"{sku_count_sql('`source_json`' if table == COLLECTION_TABLE else '`source_snapshot_json`')} {operator} %s")
            params.append(int(value))

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
        expression = f"`{column}`"
        if table == PRODUCT_TABLE and source_type == "ai_original" and column == "weight_g":
            snapshot_sql = "IF(JSON_VALID(`source_snapshot_json`), `source_snapshot_json`, '{}')"
            override_sql = f"JSON_UNQUOTE(JSON_EXTRACT({snapshot_sql}, '$.source_1688_overrides.weight_g'))"
            original_sql = f"JSON_UNQUOTE(JSON_EXTRACT({snapshot_sql}, '$.original_1688.weight_g'))"
            expression = (
                f"(CASE WHEN JSON_CONTAINS_PATH({snapshot_sql}, 'one', "
                "'$.source_1688_overrides.weight_g') THEN "
                f"CAST(NULLIF({override_sql}, 'null') AS DECIMAL(20,4)) "
                f"ELSE COALESCE(CAST(NULLIF(NULLIF({original_sql}, 'null'), '') "
                "AS DECIMAL(20,4)), `weight_g`) END)"
            )
        if table == PRODUCT_TABLE and source_type == "ai_original" and column == "price":
            snapshot_sql = "IF(JSON_VALID(`source_snapshot_json`), `source_snapshot_json`, '{}')"
            override_sql = f"JSON_UNQUOTE(JSON_EXTRACT({snapshot_sql}, '$.source_1688_overrides.price'))"
            original_sql = f"JSON_UNQUOTE(JSON_EXTRACT({snapshot_sql}, '$.original_1688.price'))"
            original_number_sql = f"CAST(NULLIF({original_sql}, 'null') AS DECIMAL(20,4))"
            expression = (
                f"(CASE WHEN JSON_CONTAINS_PATH({snapshot_sql}, 'one', "
                "'$.source_1688_overrides.price') THEN "
                f"CAST(NULLIF({override_sql}, 'null') AS DECIMAL(20,4)) "
                f"ELSE CASE WHEN {original_number_sql} > 0 THEN {original_number_sql} "
                "WHEN `price` > 0 THEN `price` ELSE NULL END END)"
            )
        if minimum is not None:
            where.append(f"{expression} >= %s")
            params.append(minimum)
        if maximum is not None:
            where.append(f"{expression} <= %s")
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
    for owner, path, prefix in (
        (collector_salesperson, "$.plugin_snapshot.created_by", "浏览器插件："),
        (generator_salesperson, f"{listing_path}.generated_by", ""),
    ):
        if str(owner or "").strip():
            expression = (
                "TRIM(REPLACE(COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT("
                "IF(JSON_VALID(`source_snapshot_json`), `source_snapshot_json`, '{}'), "
                f"'{path}')), 'null'), ''), '{prefix}', ''))"
            )
            if (path == "$.plugin_snapshot.created_by" and collector_salesperson_exact) or path == f"{listing_path}.generated_by":
                where.append(f"{expression} = %s")
                params.append(str(owner).strip())
            else:
                where.append(f"{expression} LIKE %s")
                params.append("%" + str(owner).strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if table == PRODUCT_TABLE:
        where.append("NOT EXISTS (SELECT 1 FROM erp_mercadolibre_product_sku_members sku_member "
                     f"WHERE sku_member.product_id = `{PRODUCT_TABLE}`.id AND sku_member.parent_id <> `{PRODUCT_TABLE}`.id)")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    started = time.monotonic()
    timings = {}
    stage = "connect"
    stage_started = started
    connection = None

    def mark(next_stage):
        nonlocal stage, stage_started
        now = time.monotonic()
        timings[stage] = round((now - stage_started) * 1000, 1)
        stage, stage_started = next_stage, now

    try:
        connection = (connection_factory or _connect_list)()
        mark("schema")
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            salesperson_options = None
            if include_salesperson_options and table == COLLECTION_TABLE:
                # Read the directory independently of paging and active filters.
                added_predicate = " AND item.`added_to_products` = 0" if exclude_added else ""
                options_access, options_params = product_access.predicate("item")
                if options_access:
                    added_predicate += " AND " + options_access
                personnel_predicate = _collection_personnel_predicate(
                    salesperson_expression, "item.`organization_key`",
                )
                cursor.execute(
                    f"SELECT DISTINCT CASE WHEN {personnel_predicate} "
                    f"THEN {salesperson_expression} ELSE '未分配' END AS salesperson "
                    f"FROM `{COLLECTION_TABLE}` AS item "
                    f"LEFT JOIN `{TASK_TABLE}` AS collection_task "
                    f"ON item.`task_id` = collection_task.`id` WHERE 1 = 1{added_predicate} "
                    "ORDER BY salesperson", tuple(options_params) if options_params else None
                )
                salesperson_options = [row["salesperson"] for row in cursor.fetchall()]
            if include_salesperson_options and table == PRODUCT_TABLE:
                try:
                    salesperson_options = _read_product_salespeople(cursor, token_ids)
                except Exception as exc:
                    if not exc.args or exc.args[0] != 3024:
                        raise
                    logging.warning("Product salesperson directory timed out; returning product rows")
            generator_options = None
            if include_generator_options and table == PRODUCT_TABLE:
                options_where = " AND ".join(generator_options_where) or "1 = 1"
                cursor.execute(
                    "SELECT /*+ MAX_EXECUTION_TIME(2000) */ DISTINCT TRIM(COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT("
                    "IF(JSON_VALID(`source_snapshot_json`), `source_snapshot_json`, '{}'), "
                    f"'{listing_path}.generated_by')), 'null'), '')) AS generator_salesperson "
                    f"FROM `{table}` WHERE {options_where} "
                    "HAVING generator_salesperson <> '' ORDER BY generator_salesperson",
                    tuple(generator_options_params),
                )
                generator_options = [row["generator_salesperson"] for row in cursor.fetchall()]
            scoped_page = include_total and bool(authorized_ids_sql and where == [authorized_ids_predicate])
            keyword_only = table == PRODUCT_TABLE and bool(search) and all(
                clause in (search_predicate, access_sql) for clause in where
            )
            keyword_scope = (access_sql + " AND " if access_sql else "") + search_predicate
            keyword_index = "idx_product_scope_search" if access_sql else "idx_product_search_cover"
            # Optimizer hints are ignored (rather than failing requests) before
            # the offline index migration has been applied on another server.
            keyword_hint = f"/*+ INDEX(`{table}` `{keyword_index}`) */"
            keyword_from = ""
            keyword_where = ""
            if table == PRODUCT_TABLE and search and not keyword_only:
                # Drive permission/filter checks from matching IDs. A semijoin
                # lets ORDER BY/LIMIT reverse that order and scan the whole
                # tenant before discovering that the keyword has no matches.
                keyword_from = (
                    f"(SELECT {keyword_hint} `id` AS match_id "
                    f"FROM `{table}` WHERE {keyword_scope} "
                    "LIMIT 9223372036854775807) AS keyword_matches "
                    f"STRAIGHT_JOIN `{table}` ON `{table}`.`id` = keyword_matches.match_id "
                )
                remaining = [clause for clause in where if clause not in (search_predicate, access_sql)]
                keyword_where = " WHERE " + " AND ".join(remaining) if remaining else ""
            mark("count_page" if scoped_page else "count")
            # MySQL's unqualified InnoDB COUNT can take the parallel clustered
            # scan path despite EXPLAIN showing a secondary index. Pin the
            # narrow existing index for the unfiltered product total only;
            # filtered/permission-scoped queries need their own optimizer plan.
            count_index = (
                "FORCE INDEX (`idx_erp_meli_product_added`)"
                if table == PRODUCT_TABLE and not where else ""
            )
            if include_total and not scoped_page:
                count_from = f"`{table}` {count_index} {where_sql}"
                if keyword_only:
                    count_from = f"`{table}` WHERE {keyword_scope}"
                elif keyword_from:
                    count_from = keyword_from + keyword_where
                count_hint = f" INDEX(`{table}` `{keyword_index}`)" if keyword_only else ""
                try:
                    cursor.execute(f"SELECT /*+ MAX_EXECUTION_TIME(8000){count_hint} */ COUNT(*) AS total FROM {count_from}", tuple(params))
                    count_result = cursor.fetchone() or {}
                except Exception as exc:
                    # A statement deadline leaves the connection usable. Keep
                    # all filters and fetch one extra row to determine whether
                    # another page exists, without repeating the expensive count.
                    # Connection/authentication failures must still propagate.
                    if table != PRODUCT_TABLE or not exc.args or exc.args[0] != 3024:
                        raise
                    include_total = False
                    count_result = {}
                    logging.warning("mercado_list_count_timeout: returning product page without exact total")
                total = int(count_result.get("total") or 0)
                if search and count_result.get("total") == 0:
                    # An absent keyword must not scan the catalog a second time
                    # looking for page IDs. This scan can time out even though
                    # COUNT already established the exact empty result.
                    mark("commit")
                    connection.commit()
                    mark("complete")
                    return {"total": 0, "rows": [], **(
                        {"generator_salespeople": generator_options}
                        if generator_options is not None else {}
                    ), **(
                        {"salespeople": salesperson_options}
                        if salesperson_options is not None else {}
                    )}
                mark("page")
            selected_fields = (
                f"`{table}`.*, category.`name` AS `management_category_name`, "
                "category.`listing_ratio_percent` AS `management_category_listing_ratio_percent`"
            )
            page_from = (
                # Materialize only page IDs, not large JSON snapshots/descriptions.
                f"(SELECT `id` FROM `{table}` {where_sql} "
                f"ORDER BY `id` DESC LIMIT %s OFFSET %s) AS page_ids "
                f"INNER JOIN `{table}` ON `{table}`.`id` = page_ids.`id` "
            )
            query_prefix = ""
            if keyword_only:
                # Sort the covered IDs before fetching any large product row.
                page_from = (
                    f"(SELECT `id` FROM (SELECT {keyword_hint} `id` "
                    f"FROM `{table}` WHERE {keyword_scope} "
                    "LIMIT 9223372036854775807) AS keyword_matches "
                    "ORDER BY `id` DESC LIMIT %s OFFSET %s) AS page_ids "
                    f"INNER JOIN `{table}` ON `{table}`.`id` = page_ids.`id` "
                )
            elif keyword_from:
                page_from = (
                    f"(SELECT `{table}`.`id` FROM {keyword_from}{keyword_where} "
                    f"ORDER BY `{table}`.`id` DESC LIMIT %s OFFSET %s) AS page_ids "
                    f"INNER JOIN `{table}` ON `{table}`.`id` = page_ids.`id` "
                )
            if not include_total and authorized_ids_sql and where == [authorized_ids_predicate]:
                # For a single page, each ownership path only needs its newest
                # offset + limit + 1 distinct IDs. Bound before the UNION so a
                # large account does not materialize its entire product catalog.
                candidates = offset + limit + 1
                bounded_paths = []
                for index, path in enumerate(authorized_ids_sql.split(" UNION ")):
                    distinct_path = path.replace("SELECT ", "SELECT DISTINCT ", 1)
                    bounded_paths.append(
                        f"SELECT id FROM ({distinct_path} ORDER BY id DESC "
                        f"LIMIT {candidates}) AS ownership_{index}"
                    )
                page_from = (
                    "(SELECT id FROM (" + " UNION ".join(bounded_paths) + ") AS authorized_page "
                    "ORDER BY id DESC LIMIT %s OFFSET %s) AS page_ids "
                    f"INNER JOIN `{table}` ON `{table}`.`id` = page_ids.`id` "
                )
            if scoped_page:
                # UNION makes this CTE materialize once for both the exact total
                # and paging. Each ownership path already joins existing products,
                # so sorting its IDs needs no further product lookup until LIMIT.
                query_prefix = f"WITH authorized_products AS ({authorized_ids_sql}) "
                selected_fields += ", list_totals.`total` AS `__list_total`"
                page_from = (
                    "(SELECT COUNT(*) AS total FROM authorized_products) AS list_totals "
                    "LEFT JOIN (SELECT `id` FROM authorized_products "
                    "ORDER BY `id` DESC LIMIT %s OFFSET %s) AS page_ids ON 1 = 1 "
                    f"LEFT JOIN `{table}` ON `{table}`.`id` = page_ids.`id` "
                )
            from_sql = (
                page_from +
                f"LEFT JOIN `{MANAGEMENT_CATEGORY_TABLE}` AS category "
                f"ON category.`id` = `{table}`.`management_category_id`"
            )
            if table == COLLECTION_TABLE:
                personnel_predicate = _collection_personnel_predicate(
                    salesperson_expression, f"`{table}`.`organization_key`",
                )
                selected_fields += (
                    f", CASE WHEN {personnel_predicate} THEN {salesperson_expression} "
                    "ELSE '未分配' END AS `salesperson`"
                )
                from_sql += (
                    f" LEFT JOIN `{TASK_TABLE}` AS collection_task "
                    f"ON collection_task.`id` = `{table}`.`task_id`"
                )
            else:
                selected_fields += f", {_product_salesperson_expression()} AS `salesperson`"
            cursor.execute(
                query_prefix + f"SELECT /*+ MAX_EXECUTION_TIME(8000) */ {selected_fields} FROM {from_sql} "
                f"ORDER BY `{table}`.`id` DESC",
                tuple(params + [limit if include_total else limit + 1, offset]),
            )
            raw_rows = cursor.fetchall()
            if scoped_page:
                # The total survives an empty page (including an offset beyond
                # the last page); the LEFT JOIN's null row is not a product.
                total = int((raw_rows[0] if raw_rows else {}).get("__list_total") or 0)
                raw_rows = [row for row in raw_rows if row.get("id") is not None]
                for row in raw_rows:
                    row.pop("__list_total", None)
            has_more = len(raw_rows) > limit if not include_total else offset + len(raw_rows) < total
            if not include_total:
                raw_rows = raw_rows[:limit]
                total = None
        mark("commit")
        connection.commit()
        connection.close()
        connection = None
        # JSON decoding/copying can be expensive for supplier SKU snapshots.
        # No database work remains; release the slot before processing them.
        mark("decode")
        rows = [_mirror_zying_snapshot_fields(_json_safe_row(row)) for row in raw_rows]
        mark("complete")
        return {"total": total, "rows": rows, **(
            {"has_more": has_more, "total_exact": False} if not include_total else {}
        ), **(
            {"generator_salespeople": generator_options} if generator_options is not None else {}
        ), **(
            {"salespeople": salesperson_options} if salesperson_options is not None else {}
        )}
    finally:
        mark("cleanup")
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        logging.log(
            logging.WARNING if elapsed_ms >= 2000 or "complete" not in timings else logging.INFO,
            "mercado_list_query table=%s limit=%s offset=%s elapsed_ms=%s stages_ms=%s",
            table, limit, offset, elapsed_ms, timings,
        )
        if connection is not None:
            connection.close()


def list_collection_items(**kwargs: Any) -> dict[str, Any]:
    return _list_rows(COLLECTION_TABLE, **kwargs)


def get_collection_items_by_ids(
    collection_item_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    ids = _normalize_row_ids(
        collection_item_ids, empty_message="请至少勾选一个采集商品"
    )
    if len(ids) > 100:
        raise ValueError("每批最多生成 100 件采集商品文案")
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{COLLECTION_TABLE}` WHERE `id` IN ({placeholders})",
                tuple(ids),
            )
            rows = {int(row["id"]): _json_safe_row(row) for row in cursor.fetchall()}
        connection.commit()
        if len(rows) != len(ids):
            raise ValueError("部分采集商品已删除，请刷新列表后重试")
        return [rows[item_id] for item_id in ids]
    finally:
        connection.close()


def product_ai_copy_row(row):
    """Adapt a product snapshot to the shared bilingual-copy facts contract."""
    snapshot = _loads(row.get("source_snapshot_json"), {})
    return {**row, "_ai_copy_product": True,
            "source_json": _dumps(snapshot.get("source") or {}),
            "description_json": _dumps(snapshot.get("description") or {}),
            "ai_copy_json": _dumps(snapshot.get("ai_copy") or {})}


def save_collection_item_ai_copy(
    collection_item_id: int,
    expected: Mapping[str, Any],
    generated: Mapping[str, Any],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Persist validated bilingual copy only while its source facts are unchanged."""
    try:
        item_id = int(collection_item_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("采集商品编号无效") from exc
    if item_id <= 0:
        raise ValueError("采集商品编号无效")
    if not isinstance(expected, Mapping) or not isinstance(generated, Mapping):
        raise ValueError("文案保存数据格式无效")
    copy = {
        key: str(generated.get(key) or "").strip()
        for key in ("title_es", "title_pt", "description_es", "description_pt")
    }
    for language, key in (("西班牙语", "title_es"), ("葡萄牙语", "title_pt")):
        if not 50 <= len(copy[key]) <= 60:
            raise ValueError(f"{language}标题必须为 50–60 个字符")
    if not copy["description_es"] or not copy["description_pt"]:
        raise ValueError("西班牙语和葡萄牙语描述都不能为空")
    if len(copy["description_es"]) > 50000 or len(copy["description_pt"]) > 50000:
        raise ValueError("产品描述不能超过 50000 个字符")
    terms = generated.get("brand_terms") or []
    if isinstance(terms, str):
        terms = [terms]
    if not isinstance(terms, list):
        terms = []
    copy["brand_terms"] = [str(value).strip()[:120] for value in terms if str(value).strip()][:50]
    copy["generated_at"] = _now()

    product_mode = bool(expected.get("_ai_copy_product"))
    table = PRODUCT_TABLE if product_mode else COLLECTION_TABLE
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{table}` WHERE `id` = %s FOR UPDATE",
                (item_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise ValueError("采集商品已删除，不能保存文案")
            if product_mode:
                current = product_ai_copy_row(current)
            source_json = current.get("source_json") or ""
            if isinstance(source_json, bytes):
                source_json = source_json.decode("utf-8", errors="replace")
            description_json = current.get("description_json") or ""
            if isinstance(description_json, bytes):
                description_json = description_json.decode("utf-8", errors="replace")
            expected_source_hash = str(expected.get("source_json_sha256") or "").strip()
            current_source_hash = hashlib.sha256(str(source_json).encode("utf-8")).hexdigest()
            expected_description_hash = str(expected.get("description_json_sha256") or "").strip()
            current_description_hash = hashlib.sha256(
                str(description_json).encode("utf-8")
            ).hexdigest()
            numeric_facts = (
                "price", "weight_g", "package_length_cm", "package_width_cm",
                "package_height_cm",
            )
            numeric_facts_changed = any(
                _decimal_from_text(current.get(field))
                != _decimal_from_text(expected.get(field))
                for field in numeric_facts
            )
            text_facts = ("currency_id", "category_id", "category_name")
            text_facts_changed = any(
                str(current.get(field) or "") != str(expected.get(field) or "")
                for field in text_facts
            )
            is_stale = (
                (product_mode and current.get("source_snapshot_json") != expected.get("source_snapshot_json"))
                or str(current.get("title") or "") != str(expected.get("title") or "")
                or str(current.get("description_text") or "")
                != str(expected.get("description_text") or "")
                or numeric_facts_changed
                or text_facts_changed
                or (
                    current_source_hash != expected_source_hash
                    if expected_source_hash
                    else str(source_json) != str(expected.get("source_json") or "")
                )
                or (
                    current_description_hash != expected_description_hash
                    if expected_description_hash
                    else str(description_json) != str(expected.get("description_json") or "")
                )
            )
            if is_stale:
                connection.rollback()
                return {"saved": False, "status": "conflict", "collection_item_id": item_id}

            previous_copy = _loads(current.get("ai_copy_json"), {})
            previous_copy = previous_copy if isinstance(previous_copy, Mapping) else {}
            copy["source_title"] = str(
                previous_copy.get("source_title") or current.get("title") or ""
            )[:255]
            copy["source_description"] = str(
                previous_copy.get("source_description") or current.get("description_text") or ""
            )[:50000]

            source = _loads(source_json, {})
            if not isinstance(source, dict):
                source = {}
            source["title"] = copy["title_es"]
            description = {"plain_text": copy["description_es"]}
            if product_mode:
                snapshot = _loads(current.get("source_snapshot_json"), {})
                snapshot.update(source=source, description=description, ai_copy=copy)
                review_assignment = ""
                if str(current.get("source_type") or "").lower() == "ai_original":
                    _mark_ai_marketplace_drafts_unreviewed(snapshot)
                    review_assignment = ", `review_status`='unreviewed'"
                cursor.execute(
                    f"UPDATE `{PRODUCT_TABLE}` SET `title`=%s, `description_text`=%s, "
                    f"`source_snapshot_json`=%s{review_assignment} WHERE `id`=%s",
                    (copy["title_es"], copy["description_es"], _dumps(snapshot), item_id),
                )
                connection.commit()
                return {"saved": True, "status": "completed", "product_item_id": item_id}
            cursor.execute(
                f"UPDATE `{COLLECTION_TABLE}` SET `title` = %s, `description_text` = %s, "
                "`description_json` = %s, `source_json` = %s, `ai_copy_json` = %s "
                "WHERE `id` = %s",
                (
                    copy["title_es"], copy["description_es"], _dumps(description),
                    _dumps(source), _dumps(copy), item_id,
                ),
            )

            # Keep a product already created from this collection item in sync.
            cursor.execute(
                f"SELECT `id`, `source_type`, `source_snapshot_json` FROM `{PRODUCT_TABLE}` "
                "WHERE `collection_item_id` = %s FOR UPDATE",
                (item_id,),
            )
            product = cursor.fetchone()
            if product:
                snapshot = _loads(product.get("source_snapshot_json"), {})
                if not isinstance(snapshot, dict):
                    snapshot = {}
                product_source = dict(snapshot.get("source") or {})
                product_source["title"] = copy["title_es"]
                snapshot.update({
                    "source": product_source,
                    "description": description,
                    "ai_copy": copy,
                })
                review_assignment = ""
                if str(product.get("source_type") or "").lower() == "ai_original":
                    _mark_ai_marketplace_drafts_unreviewed(snapshot)
                    review_assignment = ", `review_status`='unreviewed'"
                cursor.execute(
                    f"UPDATE `{PRODUCT_TABLE}` SET `title` = %s, `description_text` = %s, "
                    f"`source_snapshot_json` = %s{review_assignment} WHERE `id` = %s",
                    (copy["title_es"], copy["description_es"], _dumps(snapshot), product["id"]),
                )
        connection.commit()
        return {"saved": True, "status": "completed", "collection_item_id": item_id,
                "product_mirrored": bool(product)}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_product_items(**kwargs: Any) -> dict[str, Any]:
    kwargs.pop("task_id", None)
    return _list_rows(PRODUCT_TABLE, **kwargs)


def list_product_mercado_categories(
    *, token_ids: Iterable[int] | None = None,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Category options from the entire visible library, independent of pagination."""
    if product_access.actor():
        token_ids = None
    _, scope, scope_params = _product_token_scope(token_ids)
    if scope == "1 = 0":
        return {"rows": []}
    access, access_params = product_access.predicate(PRODUCT_TABLE)
    where = ["TRIM(COALESCE(`category_id`, '')) <> ''"]
    params = []
    for predicate, values in ((scope, scope_params), (access, access_params)):
        if predicate:
            where.append(predicate)
            params.extend(values)
    connection = (connection_factory or _connect_list)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                "SELECT /*+ MAX_EXECUTION_TIME(8000) */ `category_id`, "
                "MAX(`category_name`) AS category_name, COUNT(*) AS product_count "
                f"FROM `{PRODUCT_TABLE}` WHERE {' AND '.join(where)} "
                "GROUP BY `category_id` ORDER BY `category_id`", tuple(params),
            )
            return {"rows": list(cursor.fetchall())}
    finally:
        connection.close()


def list_product_developers(
    *, token_ids: Iterable[int] | None = None,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Read developer choices from all visible saved ZYing products."""
    if product_access.actor():
        token_ids = None
    _scope_sql, scope_predicate, scope_params = _product_token_scope(token_ids)
    if scope_predicate == "1 = 0":
        return {"rows": []}
    access_sql, access_params = product_access.predicate(PRODUCT_TABLE)
    where = ["`source_type` = %s"] + ([access_sql] if access_sql else [])
    params: list[Any] = ["zying"] + access_params
    if scope_predicate:
        where.append(scope_predicate)
        params.extend(scope_params)
    where.append(
        "(TRIM(COALESCE(`product_developer_id`, '')) <> '' "
        "OR TRIM(COALESCE(`product_developer_name`, '')) <> '')"
    )
    connection = (connection_factory or _connect_list)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                "SELECT /*+ MAX_EXECUTION_TIME(8000) */ DISTINCT "
                "TRIM(COALESCE(`product_developer_id`, '')) AS id, "
                "TRIM(COALESCE(`product_developer_name`, '')) AS name "
                f"FROM `{PRODUCT_TABLE}` WHERE {' AND '.join(where)}",
                tuple(params),
            )
            saved_rows = cursor.fetchall()
            if not saved_rows and token_ids is None and not product_access.actor():
                # The legacy directory has no token/company ownership metadata.
                # Only users with unrestricted product visibility may use it.
                cursor.execute(
                    "SELECT /*+ MAX_EXECUTION_TIME(8000) */ 1 AS present "
                    "FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s LIMIT 1",
                    ("zying_product",),
                )
                if cursor.fetchone():
                    cursor.execute(
                        "SELECT /*+ MAX_EXECUTION_TIME(8000) */ DISTINCT "
                        "TRIM(COALESCE(`产品开发编号`, '')) AS id, "
                        "TRIM(COALESCE(`产品开发`, '')) AS name "
                        "FROM `zying_product` "
                        "WHERE TRIM(COALESCE(`产品开发编号`, '')) <> '' "
                        "OR TRIM(COALESCE(`产品开发`, '')) <> ''"
                    )
                    saved_rows = cursor.fetchall()
        developers: dict[str, str] = {}
        for row in saved_rows:
            developer_id = str(row.get("id") or "").strip()
            developer_name = str(row.get("name") or "").strip()
            developer_id = developer_id or developer_name
            if not developer_id:
                continue
            developer_name = developer_name or developer_id
            previous = developers.get(developer_id)
            # Prefer an actual name over an ID placeholder. Resolve historical
            # name variants deterministically, regardless of database order.
            if previous is None or (
                developer_name == developer_id, developer_name.casefold(), developer_name
            ) < (previous == developer_id, previous.casefold(), previous):
                developers[developer_id] = developer_name
        return {"rows": [
            {"id": developer_id, "name": developer_name}
            for developer_id, developer_name in sorted(
                developers.items(), key=lambda row: (row[1].casefold(), row[0])
            )
        ]}
    finally:
        connection.close()


def get_1688_product_options(
    *, connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Load collector choices across all source records, without pagination."""
    connection = (connection_factory or _connect_list)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            access_sql, access_params = product_access.predicate(PRODUCT_TABLE)
            cursor.execute(
                "SELECT DISTINCT TRIM(REPLACE(COALESCE(NULLIF(JSON_UNQUOTE("
                "JSON_EXTRACT(IF(JSON_VALID(`source_snapshot_json`), "
                "`source_snapshot_json`, '{}'), '$.plugin_snapshot.created_by')), "
                "'null'), ''), '浏览器插件：', '')) AS collector_salesperson "
                f"FROM `{PRODUCT_TABLE}` WHERE `source_type` = 'ai_original' "
                + (f"AND {access_sql} " if access_sql else "")
                + "HAVING collector_salesperson <> '' ORDER BY collector_salesperson",
                tuple(access_params)
            )
            rows = cursor.fetchall()
        return {"collector_salespeople": sorted({
            str(row.get("collector_salesperson") or "").strip()
            for row in rows if str(row.get("collector_salesperson") or "").strip()
        })}
    finally:
        connection.close()


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
                SELECT category.`id`, category.`name`, category.`listing_ratio_percent`,
                       category.`created_at`,
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


def _normalize_management_category_listing_ratio(value: Any = 100) -> Decimal:
    try:
        ratio = Decimal("100" if value in (None, "") else str(value))
    except Exception as exc:
        raise ValueError("分类上架比例必须是数字") from exc
    if not ratio.is_finite() or ratio <= 0 or ratio > 10000:
        raise ValueError("分类上架比例必须大于 0 且不超过 10000%")
    return ratio.quantize(Decimal("0.0001"))


def create_management_category(
    name: str,
    listing_ratio_percent: Any = 100,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    normalized_name = _normalize_management_category_name(name)
    normalized_ratio = _normalize_management_category_listing_ratio(listing_ratio_percent)
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
                f"INSERT INTO `{MANAGEMENT_CATEGORY_TABLE}` "
                "(`name`, `listing_ratio_percent`) VALUES (%s, %s)",
                (normalized_name, normalized_ratio),
            )
            category_id = int(cursor.lastrowid)
        connection.commit()
        return {
            "id": category_id,
            "name": normalized_name,
            "listing_ratio_percent": float(normalized_ratio),
        }
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_management_category(
    category_id: int,
    name: str,
    listing_ratio_percent: Any = None,
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
    normalized_ratio = (
        None if listing_ratio_percent is None
        else _normalize_management_category_listing_ratio(listing_ratio_percent)
    )
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
            if normalized_ratio is None:
                cursor.execute(
                    f"UPDATE `{MANAGEMENT_CATEGORY_TABLE}` SET `name` = %s WHERE `id` = %s",
                    (normalized_name, normalized_id),
                )
            else:
                cursor.execute(
                    f"UPDATE `{MANAGEMENT_CATEGORY_TABLE}` SET `name` = %s, "
                    "`listing_ratio_percent` = %s WHERE `id` = %s",
                    (normalized_name, normalized_ratio, normalized_id),
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
        return {
            "id": normalized_id, "name": normalized_name, "changed": changed,
            **({"listing_ratio_percent": float(normalized_ratio)} if normalized_ratio is not None else {}),
        }
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
                f"SELECT `id` FROM `{MANAGEMENT_CATEGORY_TABLE}` WHERE `id` = %s FOR UPDATE",
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
                    "ON collection_item.`source_item_id` = product.`source_item_id` AND collection_item.`organization_key` = product.`organization_key` "
                    "SET product.`management_category_id` = %s "
                    f"WHERE collection_item.`id` IN ({placeholders})",
                    tuple([normalized_category_id] + ids),
                )
            else:
                cursor.execute(
                    f"UPDATE `{COLLECTION_TABLE}` AS collection_item "
                    f"INNER JOIN `{PRODUCT_TABLE}` AS product "
                    "ON collection_item.`source_item_id` = product.`source_item_id` AND collection_item.`organization_key` = product.`organization_key` "
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
    product_item_id: int | None = None,
    search: str = "",
    source_type: str = "",
    status: str = "",
    store_name: str = "",
    site_id: str = "",
    group_name: str = "",
    salesperson: str = "",
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
    if product_item_id is not None:
        product_item_id = int(product_item_id)
        if product_item_id <= 0:
            raise ValueError("产品记录编号必须为正整数")
        base_where.append("records.`product_item_id` = %s")
        base_params.append(product_item_id)
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
    record_access, record_params = product_access.predicate("record_product")
    if record_access:
        base_where.append(f"(records.`product_item_id` IS NULL OR EXISTS (SELECT 1 FROM `{PRODUCT_TABLE}` AS record_product "
                          f"WHERE record_product.`id` = records.`product_item_id` AND {record_access}))")
        base_params.extend(record_params)
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

    salesperson = str(salesperson or "").strip()
    if len(salesperson) > 100:
        raise ValueError("业务员名称不能超过 100 个字符")
    if salesperson == "__unassigned__":
        base_where.append("COALESCE(TRIM(settings.`salesperson`), '') = ''")
    elif salesperson:
        base_where.append("TRIM(settings.`salesperson`) = %s")
        base_params.append(salesperson)

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

    source_type = str(source_type or "").strip().lower()
    if source_type:
        if source_type not in PRODUCT_SOURCE_TYPES:
            raise ValueError(f"不支持的产品来源: {source_type}")
        base_where.append(f"EXISTS (SELECT 1 FROM `{PRODUCT_TABLE}` AS source_product "
                          "WHERE source_product.`id` = records.`product_item_id` "
                          "AND source_product.`source_type` = %s)")
        base_params.append(source_type)

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
            total = counts.get(status, 0) if status else counts["all"]
            cursor.execute(
                f"SELECT records.*, (SELECT source_product.`source_type` FROM `{PRODUCT_TABLE}` AS source_product "
                "WHERE source_product.`id` = records.`product_item_id`) AS source_type, COALESCE(settings.`group_name`, '') AS `group_name` "
                f"FROM (SELECT records.`id` FROM {from_sql} {where_sql} "
                "ORDER BY COALESCE(records.`started_at`, records.`created_at`) DESC, records.`id` DESC "
                "LIMIT %s OFFSET %s) AS page_ids "
                f"INNER JOIN `{PUBLISH_RECORD_TABLE}` AS records ON records.`id` = page_ids.`id` "
                "LEFT JOIN `mercado_store_site_settings` AS settings "
                "ON settings.`token_id` = records.`token_id` AND settings.`site_id` = records.`site_id` "
                "ORDER BY COALESCE(records.`started_at`, records.`created_at`) DESC, records.`id` DESC",
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
            if product_access.actor():
                product_ids = [row['product_item_id'] for row in rows if row.get('product_item_id')]
                if product_ids:
                    check_catalog_access('products', product_ids)
        connection.commit()
        return rows
    finally:
        connection.close()


def claim_due_rate_limited_product_publish_records(
    *,
    now: datetime | None = None,
    limit: int = 50,
    connection_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """Claim current rate-limit failures whose two-hour retry interval elapsed."""
    current_time = now or datetime.now().replace(microsecond=0)
    due_before = current_time - timedelta(hours=PRODUCT_PUBLISH_RATE_LIMIT_RETRY_HOURS)
    normalized_limit = max(1, min(int(limit or 50), 500))
    rate_limit_predicates = (
        "LOWER(COALESCE(records.`failure_reason`, '')) LIKE %s",
        "LOWER(COALESCE(records.`failure_reason`, '')) LIKE %s",
        "COALESCE(records.`failure_reason`, '') LIKE %s",
        "LOWER(COALESCE(records.`result_json`, '')) LIKE %s",
        "LOWER(COALESCE(records.`result_json`, '')) LIKE %s",
        "(LOWER(COALESCE(records.`failure_reason`, '')) LIKE %s AND "
        "(LOWER(COALESCE(records.`failure_reason`, '')) LIKE '%%get /%%' OR "
        "LOWER(COALESCE(records.`failure_reason`, '')) LIKE '%%post /%%' OR "
        "LOWER(COALESCE(records.`failure_reason`, '')) LIKE '%%put /%%' OR "
        "LOWER(COALESCE(records.`failure_reason`, '')) LIKE '%%delete /%%'))",
    )
    rate_limit_patterns = (
        "%local_rate_limited%",
        "%integration.rate_limited.%",
        "%平台刊登服务限流%",
        '%"platform_rate_limited":true%',
        '%"platform_rate_limited": true%',
        "% 失败 (http 429)%",
    )
    connection = (connection_factory or _connect)()
    claimed: list[dict[str, Any]] = []
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"""
                SELECT records.* FROM `{PUBLISH_RECORD_TABLE}` AS records
                WHERE records.`status` = 'failed'
                  AND records.`product_item_id` IS NOT NULL
                  AND ({' OR '.join(rate_limit_predicates)})
                  AND COALESCE(records.`finished_at`, records.`updated_at`, records.`created_at`) <= %s
                  AND (records.`auto_retry_started_at` IS NULL
                       OR records.`auto_retry_started_at` <= %s)
                  AND NOT EXISTS (
                      SELECT 1 FROM `{PUBLISH_RECORD_TABLE}` AS newer
                      WHERE newer.`product_item_id` = records.`product_item_id`
                        AND newer.`token_id` = records.`token_id`
                        AND newer.`site_id` = records.`site_id`
                        AND newer.`id` > records.`id`
                  )
                ORDER BY COALESCE(records.`finished_at`, records.`updated_at`, records.`created_at`),
                         records.`id`
                LIMIT %s FOR UPDATE
                """,
                tuple(rate_limit_patterns) + (
                    due_before.strftime("%Y-%m-%d %H:%M:%S"),
                    due_before.strftime("%Y-%m-%d %H:%M:%S"),
                    normalized_limit,
                ),
            )
            rows = list(cursor.fetchall() or [])
            claimed_at = current_time.strftime("%Y-%m-%d %H:%M:%S")
            for row in rows:
                record_id = int(row.get("id") or 0)
                cursor.execute(
                    f"UPDATE `{PUBLISH_RECORD_TABLE}` "
                    "SET `auto_retry_started_at` = %s "
                    "WHERE `id` = %s AND `status` = 'failed'",
                    (claimed_at, record_id),
                )
                if int(cursor.rowcount or 0) == 1:
                    updated = dict(row)
                    updated["auto_retry_started_at"] = current_time
                    claimed.append(_json_safe_row(updated))
        connection.commit()
        return claimed
    except BaseException:
        connection.rollback()
        raise
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
                f"SELECT `id`, `source_type`, `organization_key`, `owner_user_id`, "
                f"`owner_name`, `visibility` FROM `{PRODUCT_TABLE}` "
                f"WHERE `id` IN ({placeholders}) FOR UPDATE",
                tuple(ids),
            )
            selected_rows = list(cursor.fetchall() or [])
            user = product_access.actor()
            if user and any(not product_access.can_operate(row, user) for row in selected_rows):
                raise PermissionError("当前账号无权修改所选产品的审核状态")
            if status == "approved" and any(
                str(row.get("source_type") or "").strip().lower() == "ai_original"
                for row in selected_rows
            ):
                raise ValueError("AI 原创产品请在 AI 原创页面逐件审核；通过前必须校验全部必填项")
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
        "source_url": ("来源链接", 1500, True),
        "currency_id": ("币种", 3, False),
    }
    for field, (label, max_length, allow_empty) in text_rules.items():
        if field not in changes:
            continue
        value = str(changes.get(field) or "").strip()
        if not allow_empty and not value:
            raise ValueError(f"{label}不能为空")
        if len(value) > max_length:
            raise ValueError(f"{label}不能超过 {max_length} 个字符")
        if field in {"main_image_url", "source_url"} and value and not value.lower().startswith(("http://", "https://")):
            raise ValueError(f"{label}必须以 http:// 或 https:// 开头")
        if field == "currency_id":
            value = value.upper()
            if not re.fullmatch(r"[A-Z]{3}", value):
                raise ValueError("币种必须是三个英文字母")
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
    profitability_stale = bool({"price", "currency_id", "weight_g", "category_id"}.intersection(normalized))
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
        net_proceeds_assignment = _preserve_purchase_assignment_sql(net_proceeds_assignment)
        updated_at_assignment = _preserve_purchase_assignment_sql(updated_at_assignment)
        source_assignment = _preserve_purchase_assignment_sql(source_assignment)
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
    raw_changes = dict(changes or {})
    source_changes: dict[str, Any] = {}
    for field in ("variations", "variation_dimensions", "attributes", "sale_terms", "pictures"):
        if field not in raw_changes:
            continue
        rows = raw_changes.pop(field)
        maximum = 2000 if field == "variations" else 200
        if not isinstance(rows, list) or len(rows) > maximum:
            raise ValueError(f"{field} 必须是数组，最多保存 {maximum} 项")
        if field == "pictures":
            pictures = []
            for raw in rows:
                item = dict(raw) if isinstance(raw, Mapping) else {"source": str(raw)}
                url = str(item.get("secure_url") or item.get("url") or item.get("source") or "").strip()
                if not url.lower().startswith(("https://", "http://")) or len(url) > 1500:
                    raise ValueError("产品图片必须是有效的 http/https 链接")
                pictures.append(item)
            source_changes[field] = pictures
        elif field in {"attributes", "sale_terms"}:
            if any(not isinstance(row, Mapping) or not (row.get("id") or row.get("name")) for row in rows):
                raise ValueError("属性必须填写编号或名称")
            source_changes[field] = _editor_attributes(rows)
        elif field == "variations":
            source_changes[field] = _editor_variations(rows)
        else:
            source_changes[field] = _editor_variation_dimensions(rows)
    if "available_quantity" in raw_changes:
        quantity = raw_changes.pop("available_quantity")
        if quantity is not None:
            try:
                number = Decimal(str(quantity))
                if not number.is_finite() or number < 0 or number != number.to_integral_value() or number > 2147483647:
                    raise ValueError()
                quantity = int(number)
            except Exception as exc:
                raise ValueError("库存必须是非负整数") from exc
        source_changes["available_quantity"] = quantity
    if "condition" in raw_changes:
        condition = str(raw_changes.pop("condition") or "").strip()
        if condition not in {"new", "used", "not_specified"}:
            raise ValueError("产品成色无效")
        source_changes["condition"] = condition
    copy_changes = {}
    for field in ("title_pt", "description_pt"):
        if field in raw_changes:
            value = str(raw_changes.pop(field) or "").strip()
            if len(value) > (255 if field == "title_pt" else 60000):
                raise ValueError("葡语标题或描述超过长度限制")
            copy_changes[field] = value
    net_changed = "net_proceeds_usd" in raw_changes
    net = None
    if net_changed:
        try:
            net = Decimal(str(raw_changes.pop("net_proceeds_usd")))
            if not net.is_finite() or abs(net) >= Decimal("10000000000000000"):
                raise ValueError()
            net = net.quantize(Decimal("0.0001"))
            if abs(net) >= Decimal("10000000000000000"):
                raise ValueError()
        except Exception as exc:
            raise ValueError("净收益必须是有效金额，最多 16 位整数") from exc
    normalized = _normalize_product_content_changes(raw_changes) if raw_changes else {}
    snapshot_changed = bool(source_changes or normalized or copy_changes) or net_changed
    if not normalized and not snapshot_changed:
        raise ValueError("没有可保存的产品内容")
    assignments, values, profitability_stale = _product_content_update_plan(
        normalized, preserve_zying_net_proceeds=True
    ) if normalized else ([], [], False)
    if net_changed:
        # Apply the explicit target after invalidating derived costs.
        assignments = [part for part in assignments if not part.startswith((
            "`net_proceeds_usd` =", "`profitability_source` =", "`profitability_updated_at` ="))]
        assignments.extend(["`net_proceeds_usd` = %s", "`profitability_source` = %s",
                            "`profitability_updated_at` = NOW()", "`profitability_error` = ''"])
        values.extend([net, MANUAL_PRODUCT_PROFITABILITY_SOURCE])

    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            if assignments:
                cursor.execute(
                    f"UPDATE `{PRODUCT_TABLE}` SET {', '.join(assignments)} WHERE `id` = %s",
                    tuple(values + [row_id]),
                )
                changed = int(cursor.rowcount or 0)
            else:
                changed = 0
            if changed == 0 or snapshot_changed:
                cursor.execute(
                    f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s FOR UPDATE",
                    (row_id,),
                )
                current = cursor.fetchone()
                if not current:
                    raise KeyError("产品记录不存在")
                user = product_access.actor()
                if user and not product_access.can_operate(current, user):
                    raise PermissionError("只能修改自己有操作权限的产品")
            else:
                current = None

            if snapshot_changed:
                snapshot = _loads(current.get("source_snapshot_json"), {})
                if not isinstance(snapshot, dict):
                    snapshot = {}
                source = dict(snapshot.get("source") or {})
                source.update(source_changes)
                for key in ("title", "price", "currency_id", "category_id"):
                    if key in normalized:
                        source[key] = normalized[key]
                if net_changed:
                    snapshot["manual_net_proceeds"] = {"net_proceeds_usd": str(net)}
                    if current.get("source_type") == "zying":
                        snapshot["zying_net_proceeds_usd"] = net
                    elif current.get("source_type") == "ai_original":
                        overrides = dict(snapshot.get("source_1688_overrides") or {})
                        overrides["net_proceeds_usd"] = net
                        snapshot["source_1688_overrides"] = overrides
                snapshot["manual_product_fields"] = sorted(set(snapshot.get("manual_product_fields") or []) | set(source_changes))
                if "description_text" in normalized:
                    snapshot["description"] = {"plain_text": normalized["description_text"]}
                if current.get("source_type") == "ai_original":
                    prepared = dict(snapshot.get("ai_original") or {})
                    edited_copy = dict(copy_changes)
                    for column, field in (("title", "title_es"), ("description_text", "description_es")):
                        if column in normalized:
                            edited_copy[field] = normalized[column]
                    prepared.update(edited_copy)
                    _sync_ai_original_cached_copy(prepared, list(edited_copy))
                    if "category_id" in normalized:
                        prepared["category_id"] = normalized["category_id"]
                    if "main_image_url" in normalized:
                        prepared["main_image_url"] = normalized["main_image_url"]
                    if "attributes" in source_changes:
                        old_ids = {item.get("id") for item in prepared.get("attributes") or [] if isinstance(item, Mapping)}
                        new_ids = {item.get("id") for item in source_changes["attributes"]}
                        prepared["manual_attribute_removed_ids"] = sorted((set(prepared.get("manual_attribute_removed_ids") or []) | (old_ids - new_ids)) - new_ids - {None, ""})
                        prepared["manual_attributes"] = source_changes["attributes"]
                    for field in ("attributes", "variations", "variation_dimensions"):
                        if field in source_changes:
                            prepared[field] = source_changes[field]
                            if field != "attributes":
                                prepared[f"source_{field}"] = source_changes[field]
                    checkpoint = dict(prepared.get("preparation") or {})
                    if "category_id" in normalized:
                        checkpoint.update(category_ready=False, variations_ready=False, required_ready=False)
                    if set(source_changes) & {"variations", "variation_dimensions"}:
                        checkpoint.update(variations_ready=False, required_ready=False)
                    if "attributes" in source_changes:
                        checkpoint["required_ready"] = False
                    prepared["preparation"] = checkpoint
                    prepared["review_status"] = "unreviewed"
                    prepared["edit_revision"] = _ai_original_edit_revision(prepared) + 1
                    snapshot["ai_original"] = prepared
                    snapshot.setdefault("marketplace_listings", {})["mercadolibre"] = dict(prepared)
                    overrides = dict(snapshot.get("source_1688_overrides") or {})
                    overrides.update({key: value for key, value in normalized.items() if key in {
                        "weight_g", "package_length_cm", "package_width_cm", "package_height_cm"}})
                    snapshot["source_1688_overrides"] = overrides
                    shared_facts_changed = bool(
                        net_changed
                        or set(normalized).intersection({
                            "main_image_url", "weight_g", "package_length_cm",
                            "package_width_cm", "package_height_cm",
                        })
                        or set(source_changes).intersection({
                            "pictures", "variations", "variation_dimensions",
                        })
                    )
                    if shared_facts_changed:
                        _mark_ai_marketplace_drafts_unreviewed(snapshot)
                elif copy_changes:
                    copy = dict(snapshot.get("ai_copy") or {})
                    copy.update(copy_changes)
                    snapshot["ai_copy"] = copy
                snapshot["source"] = source
                if "variations" in source_changes:
                    from erp.sku_matrix import sync_single_sku_cover
                    cover = sync_single_sku_cover(source)
                    sync_single_sku_cover(snapshot.get("ai_original") or {})
                    sync_single_sku_cover((snapshot.get("marketplace_listings") or {}).get("mercadolibre") or {})
                    if cover:
                        cursor.execute(f"UPDATE `{PRODUCT_TABLE}` SET `main_image_url` = %s WHERE `id` = %s", (cover, row_id))
                if current.get("source_type") == "ai_original":
                    prepared = snapshot.get("ai_original")
                    prepared = dict(prepared) if isinstance(prepared, Mapping) else {}
                    from erp.ai_original_platforms import validate_platform_listing
                    prepared["validation"] = validate_platform_listing(
                        prepared, "mercadolibre",
                    )
                    snapshot["ai_original"] = prepared
                    snapshot.setdefault("marketplace_listings", {})["mercadolibre"] = dict(prepared)
                review_assignment = ", `review_status` = 'unreviewed'" if current.get("source_type") == "ai_original" else ""
                if current.get("source_type") == "ai_original" and "weight_g" in normalized:
                    review_assignment += ", `weight_basis` = 'ai_original_manual'"
                cursor.execute(
                    f"UPDATE `{PRODUCT_TABLE}` SET `source_snapshot_json` = %s{review_assignment} WHERE `id` = %s",
                    (_dumps(snapshot), row_id),
                )
                changed = max(changed, int(cursor.rowcount or 0))
                collection_item_id = int(current.get("collection_item_id") or 0)
                if collection_item_id > 0 and source_changes:
                    cursor.execute(
                        f"SELECT `source_json` FROM `{COLLECTION_TABLE}` WHERE `id` = %s FOR UPDATE",
                        (collection_item_id,),
                    )
                    collection_row = cursor.fetchone()
                    if collection_row:
                        collection_source = _loads(collection_row.get("source_json"), {})
                        if not isinstance(collection_source, dict):
                            collection_source = {}
                        collection_source.update(source_changes)
                        cursor.execute(
                            f"UPDATE `{COLLECTION_TABLE}` SET `source_json` = %s WHERE `id` = %s",
                            (_dumps(collection_source), collection_item_id),
                        )

            # The daily profitability worker reads collection rows, so mirror
            # edited pricing/weight data back there and mark the snapshot stale.
            mirrored_fields = [
                field for field in normalized
                if field in {
                    "title", "main_image_url", "price", "currency_id", "weight_g", "category_id",
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
                collection_assignments = [
                    _preserve_purchase_assignment_sql(value, alias="c")
                    if value.startswith(("c.`net_proceeds_usd` =", "c.`profitability_source` =", "c.`profitability_updated_at` ="))
                    else value for value in collection_assignments
                ]
                cursor.execute(
                    f"UPDATE `{COLLECTION_TABLE}` AS c "
                    f"INNER JOIN `{PRODUCT_TABLE}` AS p "
                    "ON c.`source_item_id` = p.`source_item_id` AND c.`organization_key` = p.`organization_key` "
                    f"SET {', '.join(collection_assignments)} WHERE p.`id` = %s",
                    (row_id,),
                )
            if set(normalized).intersection({"title", "category_id"}) or "attributes" in source_changes:
                from erp.product_sku_groups import refresh_products
                refresh_products(cursor, [row_id])
        connection.commit()
        return {
            "product_item_id": row_id,
            "changed": changed,
            "profitability_refresh_pending": profitability_stale and not net_changed,
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
    if not isinstance(changes, Mapping):
        raise ValueError("批量修改内容必须是对象")
    metadata_fields = {"review_status", "owner_user_id", "management_category_id", "visibility"}
    content = {key: value for key, value in changes.items() if key not in metadata_fields}
    normalized = _normalize_product_content_changes(content) if content else {}
    metadata = {key: changes[key] for key in metadata_fields if key in changes}
    if not normalized and not metadata:
        raise ValueError("没有可保存的产品内容")
    if "review_status" in metadata and metadata["review_status"] not in {"approved", "unreviewed", "suspected", "infringing", "risk"}:
        raise ValueError("审核状态无效")
    if "visibility" in metadata and metadata["visibility"] not in {"private", "enterprise"}:
        raise ValueError("产品可见性必须为私有或企业共享")
    for field in ("owner_user_id", "management_category_id"):
        if field in metadata:
            value = metadata[field]
            if field == "management_category_id" and value in (None, ""):
                metadata[field] = None
            elif isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
                raise ValueError("业务员或运营分类编号无效")
            else:
                metadata[field] = int(value)
    assignments, values, profitability_stale = _product_content_update_plan(
        normalized, preserve_zying_net_proceeds=True
    )
    placeholders = ", ".join(["%s"] * len(ids))
    selected_columns = [
        "id", "source_type", "source_snapshot_json", "organization_key",
        "owner_user_id", "owner_name", "visibility", *normalized,
    ]
    selected_columns_sql = ", ".join(f"`{field}`" for field in selected_columns)
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT {selected_columns_sql} FROM `{PRODUCT_TABLE}` "
                f"WHERE `id` IN ({placeholders}) FOR UPDATE",
                tuple(ids),
            )
            selected_rows = cursor.fetchall()
            if {int(row["id"]) for row in selected_rows} != set(ids):
                raise KeyError("部分产品不存在或已移除")
            user = product_access.actor()
            if user and any(not product_access.can_operate(row, user) for row in selected_rows):
                raise PermissionError("当前账号无权访问所选产品")
            if "owner_user_id" in metadata:
                if not user or not product_access.is_admin(user):
                    raise PermissionError("仅企业管理员可批量分配业务员")
                cursor.execute("SELECT `id`, `display_name`, `username`, `organization_key`, `is_active` "
                               "FROM `workbench_users` WHERE `id` = %s", (metadata["owner_user_id"],))
                owner = cursor.fetchone()
                if not owner or not owner.get("is_active") or product_access.organization(owner) != product_access.organization(user):
                    raise ValueError("请选择本企业启用中的业务员")
                metadata["owner_name"] = str(owner.get("display_name") or owner["username"]).strip()
            if metadata.get("management_category_id") is not None and "management_category_id" in metadata:
                cursor.execute(f"SELECT `id` FROM `{MANAGEMENT_CATEGORY_TABLE}` WHERE `id` = %s",
                               (metadata["management_category_id"],))
                if not cursor.fetchone():
                    raise ValueError("运营分类不存在")
            if metadata.get("review_status") == "approved" and any(row.get("source_type") == "ai_original" for row in selected_rows):
                raise ValueError("AI 原创产品请在 AI 原创页面逐件审核；通过前必须校验全部必填项")
            assignments.extend(f"`{field}` = %s" for field in metadata)
            values.extend(metadata.values())
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` SET {', '.join(assignments)} "
                f"WHERE `id` IN ({placeholders})",
                tuple(values + ids),
            )
            changed = int(cursor.rowcount or 0)
            for row in selected_rows:
                if row.get("source_type") != "ai_original":
                    continue
                actual_changes = {}
                for field, value in normalized.items():
                    if field in {
                        "price", "weight_g", "package_length_cm",
                        "package_width_cm", "package_height_cm",
                    }:
                        different = _decimal_from_text(row.get(field)) != _decimal_from_text(value)
                    elif field == "category_id":
                        different = (
                            str(row.get(field) or "").strip().upper()
                            != str(value or "").strip().upper()
                        )
                    else:
                        different = str(row.get(field) or "").strip() != str(value or "").strip()
                    if different:
                        actual_changes[field] = value
                if not actual_changes:
                    continue
                snapshot = _loads(row.get("source_snapshot_json"), {})
                snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else {}
                prepared = snapshot.get("ai_original")
                prepared = dict(prepared) if isinstance(prepared, Mapping) else {}
                source = snapshot.get("source")
                source = dict(source) if isinstance(source, Mapping) else {}

                edited_copy = {}
                if "title" in actual_changes:
                    edited_copy["title_es"] = actual_changes["title"]
                    prepared["title"] = actual_changes["title"]
                    source["title"] = actual_changes["title"]
                if "description_text" in actual_changes:
                    edited_copy["description_es"] = actual_changes["description_text"]
                    prepared["description"] = actual_changes["description_text"]
                    snapshot["description"] = {"plain_text": actual_changes["description_text"]}
                if edited_copy:
                    prepared.update(edited_copy)
                    _sync_ai_original_cached_copy(prepared, edited_copy)
                if "main_image_url" in actual_changes:
                    prepared["main_image_url"] = actual_changes["main_image_url"]
                if "price" in actual_changes:
                    source["price"] = actual_changes["price"]
                if "category_id" in actual_changes:
                    category_id = str(actual_changes["category_id"] or "").strip().upper()
                    source["category_id"] = category_id
                    prepared.update(
                        category_id=category_id,
                        category_name="",
                        status="failed",
                        failed_step="category",
                        workflow_status="needs_category",
                        error="类目已修改，请重新执行 AI 制作以生成该类目的必填属性",
                    )
                    checkpoint = dict(prepared.get("preparation") or {})
                    checkpoint.update(
                        category_ready=False, variations_ready=False, required_ready=False,
                    )
                    prepared["preparation"] = checkpoint

                overrides = snapshot.get("source_1688_overrides")
                overrides = dict(overrides) if isinstance(overrides, Mapping) else {}
                overrides.update({
                    field: value for field, value in actual_changes.items()
                    if field in {
                        "weight_g", "package_length_cm", "package_width_cm",
                        "package_height_cm",
                    }
                })
                snapshot["source_1688_overrides"] = overrides
                from erp.ai_original_platforms import validate_platform_listing
                prepared["validation"] = validate_platform_listing(
                    prepared, "mercadolibre",
                )
                prepared["edit_revision"] = _ai_original_edit_revision(prepared) + 1
                prepared["review_status"] = "unreviewed"
                snapshot["ai_original"] = prepared
                snapshot["source"] = source
                drafts = snapshot.get("marketplace_listings")
                drafts = dict(drafts) if isinstance(drafts, Mapping) else {}
                drafts["mercadolibre"] = dict(prepared)
                snapshot["marketplace_listings"] = drafts
                if set(actual_changes).intersection({
                    "main_image_url", "weight_g", "package_length_cm",
                    "package_width_cm", "package_height_cm",
                }):
                    _mark_ai_marketplace_drafts_unreviewed(snapshot)
                weight_basis_assignment = (
                    ", `weight_basis` = 'ai_original_manual'"
                    if "weight_g" in actual_changes else ""
                )
                cursor.execute(
                    f"UPDATE `{PRODUCT_TABLE}` SET `source_snapshot_json` = %s, "
                    f"`review_status` = 'unreviewed'{weight_basis_assignment} WHERE `id` = %s "
                    "AND `source_type` = 'ai_original'",
                    (_dumps(snapshot), int(row["id"])),
                )

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
                    "ON c.`source_item_id` = p.`source_item_id` AND c.`organization_key` = p.`organization_key` "
                    f"SET {', '.join(collection_assignments)} "
                    f"WHERE p.`id` IN ({placeholders})",
                    tuple(ids),
                )
            if "review_status" in metadata:
                cursor.execute(f"UPDATE `{PRODUCT_TABLE}` SET `review_status` = %s WHERE `id` IN ({placeholders})",
                               (metadata["review_status"], *ids))
            mirrored_metadata = {key: value for key, value in metadata.items() if key != "review_status"}
            if mirrored_metadata:
                cursor.execute(
                    f"UPDATE `{COLLECTION_TABLE}` AS c INNER JOIN `{PRODUCT_TABLE}` AS p "
                    "ON c.`source_item_id` = p.`source_item_id` AND c.`organization_key` = p.`organization_key` "
                    f"SET {', '.join(f'c.`{key}` = p.`{key}`' for key in mirrored_metadata)} "
                    f"WHERE p.`id` IN ({placeholders})", tuple(ids))
            if set(normalized).intersection({"title", "category_id"}):
                from erp.product_sku_groups import refresh_products
                refresh_products(cursor, ids)
        connection.commit()
        return {
            "requested": len(ids),
            "changed": changed,
            "updated_fields": list(normalized) + list(metadata),
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
    if 'variations' in (changes or {}) or 'variation_dimensions' in (changes or {}):
        if len(ids) != 1 or set(changes) - {'variations', 'variation_dimensions'}:
            raise ValueError('SKU 明细只能单件保存，请勿批量覆盖其他产品的 SKU')
        normalized = {}
        if 'variations' in changes:
            normalized['variations'] = _editor_variations(changes['variations'])
        if 'variation_dimensions' in changes:
            normalized['variation_dimensions'] = _editor_variation_dimensions(changes['variation_dimensions'])
        connection = (connection_factory or _connect)()
        try:
            with connection.cursor() as cursor:
                ensure_collection_tables(cursor)
                cursor.execute(f'SELECT * FROM `{COLLECTION_TABLE}` WHERE `id` = %s FOR UPDATE', (ids[0],))
                row = cursor.fetchone()
                if not row:
                    raise ValueError('采集商品不存在')
                if product_access.actor() and not product_access.can_operate(row, product_access.actor()):
                    raise PermissionError('只能修改自己的采集商品')
                if row.get('added_to_products'):
                    raise ValueError('商品已加入产品列表，请在产品列表修改 SKU')
                source = _loads(row.get('source_json'), {})
                source.update(normalized)
                cursor.execute(f'UPDATE `{COLLECTION_TABLE}` SET `source_json` = %s WHERE `id` = %s', (_dumps(source), ids[0]))
            connection.commit()
            return {'changed': 1, 'updated_fields': list(normalized)}
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
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
                product_assignments = [
                    _preserve_purchase_assignment_sql(assignment, alias="p")
                    if assignment.startswith(tuple(
                        f"p.`{field}` = " for field in (
                            "net_proceeds_usd", "profitability_source", "profitability_updated_at",
                        )
                    )) else assignment
                    for assignment in product_assignments
                ]
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` AS p "
                f"INNER JOIN `{COLLECTION_TABLE}` AS c "
                "ON c.`source_item_id` = p.`source_item_id` AND c.`organization_key` = p.`organization_key` "
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


def normalize_1688_product_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Validate source edits strictly before opening a write connection."""
    if not isinstance(changes, Mapping) or not changes:
        raise ValueError("请填写需要修改的 1688 产品字段")
    unsupported = set(changes) - SOURCE_1688_NUMERIC_FIELDS - {"variations"}
    if unsupported:
        raise ValueError("不支持的 1688 产品字段：" + ", ".join(sorted(map(str, unsupported))))
    normalized = {}
    for field, value in changes.items():
        if field == "variations":
            from erp.sku_matrix import supplier_skus
            normalized[field] = supplier_skus(value, validate=True)
            continue
        if value is None or isinstance(value, str) and not value.strip():
            normalized[field] = None
            continue
        try:
            if isinstance(value, bool):
                raise ValueError()
            number = Decimal(str(value).strip())
        except Exception as exc:
            raise ValueError(f"{field} 必须是有效数字") from exc
        if not number.is_finite() or number <= 0:
            raise ValueError(f"{field} 必须是大于 0 的有效数字")
        if number > Decimal("9999999999999999.9999"):
            raise ValueError(f"{field} 超出可保存的数字范围")
        number = number.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        if number <= 0 or Decimal(str(float(number))) > Decimal("9999999999999999.9999"):
            raise ValueError(f"{field} 超出可保存的数字范围（最小 0.0001）")
        normalized[field] = number
    return normalized


def _1688_override_table_assignments(
    current: Mapping[str, Any], overrides: Mapping[str, Any],
) -> tuple[list[str], list[Any]]:
    """Keep source measurements/economics in the listing's publish inputs."""
    assignments, values = [], []
    for field, value in overrides.items():
        if field not in SOURCE_1688_NUMERIC_FIELDS:
            continue
        # Once generated, the listing uses USD; its price has separate meaning.
        if field == "price" and str(current.get("currency_id") or "CNY").upper() != "CNY":
            continue
        assignments.append(f"`{field}` = %s")
        values.append(value)
    if "weight_g" in overrides:
        assignments.append("`weight_basis` = '1688_manual'")
    if set(overrides).intersection({
        "package_length_cm", "package_width_cm", "package_height_cm",
    }):
        assignments.append(
            "`volumetric_weight_kg` = CASE WHEN `package_length_cm` > 0 "
            "AND `package_width_cm` > 0 AND `package_height_cm` > 0 THEN ROUND("
            "`package_length_cm` * `package_width_cm` * `package_height_cm` / 6000, 4) "
            "ELSE NULL END"
        )
    return assignments, values


def _mark_ai_marketplace_drafts_unreviewed(snapshot):
    """Require a fresh human review after shared source or package facts change."""
    drafts = snapshot.get("marketplace_listings")
    if not isinstance(drafts, Mapping):
        return
    updated = {}
    for platform, value in drafts.items():
        draft = dict(value) if isinstance(value, Mapping) else value
        # Mercado uses the row-level review_status as its canonical review.
        # Other targets keep an independent review inside their draft.
        if isinstance(draft, dict) and platform != "mercadolibre":
            draft["review_status"] = "unreviewed"
        updated[platform] = draft
    snapshot["marketplace_listings"] = updated


def _ai_original_review_payload(snapshot):
    """Return listing facts while excluding cost-ledger-only bookkeeping."""
    if not isinstance(snapshot, Mapping):
        return {}
    prepared = snapshot.get("ai_original")
    prepared = dict(prepared) if isinstance(prepared, Mapping) else {}
    prepared.pop("generation_budget", None)
    mercado = (snapshot.get("marketplace_listings") or {}).get("mercadolibre")
    mercado = dict(mercado) if isinstance(mercado, Mapping) else dict(prepared)
    mercado.pop("generation_budget", None)
    return {
        "ai_original": prepared,
        "mercadolibre": mercado,
        "original_1688": snapshot.get("original_1688"),
        "source": snapshot.get("source"),
        "description": snapshot.get("description"),
        "ai_copy": snapshot.get("ai_copy"),
        "source_1688_overrides": snapshot.get("source_1688_overrides"),
        "manual_net_proceeds": snapshot.get("manual_net_proceeds"),
        "purchase_net_proceeds": snapshot.get("purchase_net_proceeds"),
    }


def update_1688_product(
    product_item_id: int, changes: Mapping[str, Any],
    *, connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Atomically save source overrides while preserving collected and AI facts."""
    row_id = int(product_item_id)
    if row_id <= 0:
        raise ValueError("产品记录编号无效")
    normalized = normalize_1688_product_changes(changes)
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            row = _save_1688_product(cursor, row_id, normalized)
        connection.commit()
        return _mirror_zying_snapshot_fields(_json_safe_row(row))
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _save_1688_product(cursor, row_id, normalized):
    cursor.execute(
        f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s "
        "AND `source_type` = 'ai_original' FOR UPDATE", (row_id,),
    )
    current = cursor.fetchone()
    if not current:
        raise KeyError("1688 产品不存在")
    user = product_access.actor()
    if user and not product_access.can_operate(current, user):
        raise PermissionError("只能修改自己有操作权限的产品")
    snapshot = _loads(current.get("source_snapshot_json"), {})
    snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else {}
    overrides = snapshot.get("source_1688_overrides")
    overrides = dict(overrides) if isinstance(overrides, Mapping) else {}
    derived_sku_net = False
    if "variations" in normalized:
        from erp.sku_matrix import sync_supplier_skus
        normalized = dict(normalized)
        rows = normalized.pop("variations")
        original = snapshot.get("original_1688") or {}
        from erp.sku_matrix import sku_identity
        original_rows = original.get("variations") or []
        identities_match = {sku_identity(v) for v in rows} == {sku_identity(v) for v in original_rows}
        if not original_rows and len(rows) == 1:
            # Legacy single-SKU sources store supplier facts on the product.
            # Give that row a recorded offer identity on its first SKU edit.
            from erp.ai_original_products import extract_1688_item_id
            aliases = {str(value) for value in (
                original.get("item_id"), original.get("source_1688_item_id"),
                original.get("source_item_id"), current.get("source_item_id"),
                extract_1688_item_id(re.sub(r"^1688_?", "", str(original.get("source_item_id") or current.get("source_item_id") or ""))),
                extract_1688_item_id(original.get("source_url") or current.get("source_url")),
            ) if value}
            identities_match = sku_identity(rows[0]) in (aliases or {str(row_id)})
        if not identities_match:
            raise ValueError("SKU 编号已变化，请刷新产品后重新编辑")
        sync_supplier_skus(snapshot, rows)
        overrides["variations"] = rows
        if 'net_proceeds_usd' not in overrides and 'net_proceeds_usd' not in normalized:
            amounts = [v['net_proceeds_usd'] for v in rows if v.get('net_proceeds_usd')]
            normalized['net_proceeds_usd'] = Decimal(max(amounts)) if amounts else None
            derived_sku_net = True
    overrides.update({
        field: float(value) if value is not None else None
        for field, value in normalized.items()
        if not (derived_sku_net and field == "net_proceeds_usd")
    })
    snapshot["source_1688_overrides"] = overrides
    _mark_ai_marketplace_drafts_unreviewed(snapshot)
    # A changed purchase price also changes the source's calculated
    # net proceeds. Persist that same value for AI listing/publishing.
    if "price" in normalized and "net_proceeds_usd" not in overrides:
        normalized["net_proceeds_usd"] = _default_ai_original_net_proceeds(snapshot)
    assignments, values = _1688_override_table_assignments(current, normalized)
    assignments.append("`source_snapshot_json` = %s")
    values.append(_dumps(snapshot))
    assignments.append("`review_status` = %s")
    values.append("unreviewed")
    cursor.execute(
        f"UPDATE `{PRODUCT_TABLE}` SET {', '.join(assignments)} "
        "WHERE `id` = %s AND `source_type` = 'ai_original'", tuple(values + [row_id]),
    )
    cursor.execute(f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s", (row_id,))
    row = cursor.fetchone()
    return row


def update_1688_product_items(
    product_item_ids: Iterable[int], changes: Mapping[str, Any],
    *, connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Save source measurements for the entire selection in one transaction."""
    ids = sorted(_normalize_row_ids(product_item_ids, empty_message="请至少勾选一个 1688 产品"))
    allowed = {"weight_g", "package_length_cm", "package_width_cm", "package_height_cm"}
    if not isinstance(changes, Mapping) or set(changes).difference(allowed):
        raise ValueError("1688 产品批量修改只支持重量和包装长、宽、高")
    normalized = normalize_1688_product_changes(changes)
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            for row_id in ids:
                _save_1688_product(cursor, row_id, normalized)
        connection.commit()
        return {"requested": len(ids), "changed": len(ids), "updated_fields": list(normalized)}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _default_ai_original_net_proceeds(
    snapshot: Mapping[str, Any], *, fallback_price: Any = None,
) -> Decimal | None:
    """Resolve the source-based default without undoing explicit source edits."""
    from erp.ai_original_products import suggested_ai_original_net_proceeds

    overrides = snapshot.get("source_1688_overrides")
    overrides = overrides if isinstance(overrides, Mapping) else {}
    if "net_proceeds_usd" in overrides:
        return _decimal_from_text(overrides["net_proceeds_usd"])
    original = snapshot.get("original_1688")
    original = original if isinstance(original, Mapping) else {}
    if "price" in overrides:
        variations, price = [], overrides["price"]
    else:
        variations = original.get("variations")
        price = original.get("price")
    _, suggested = suggested_ai_original_net_proceeds(variations, fallback_price=price)
    if suggested is None and "price" not in overrides:
        _, suggested = suggested_ai_original_net_proceeds(
            variations, fallback_price=fallback_price,
        )
    return Decimal(suggested) if suggested is not None else None


def get_collected_1688_ids(offer_ids, *, connection_factory=None):
    """Read visible collected sources in one bounded, enterprise-scoped query."""
    if len(offer_ids) > 200:
        raise ValueError("最多查询 200 个商品")
    ids = list(dict.fromkeys(str(value) for value in offer_ids))
    if len(ids) > 200 or any(not re.fullmatch(r"\d{5,30}", value) for value in ids):
        raise ValueError("1688 商品编号无效或超过 200 个")
    if not ids:
        return []
    access, params = product_access.predicate(PRODUCT_TABLE)
    if not access:
        raise ValueError("请先登录")
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            placeholders = ', '.join(['%s'] * len(ids))
            cursor.execute(
                f"SELECT `source_item_id` FROM `{PRODUCT_TABLE}` "
                f"WHERE `source_type` = 'ai_original' AND `source_item_id` IN ({placeholders}) AND {access}",
                tuple(['1688' + value for value in ids] + params),
            )
            return [str(row['source_item_id'])[4:] for row in cursor.fetchall()]
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

    category_id = normalize_collection_management_category_id(raw_product.get("management_category_id"))
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
            _execute_source_scoped(cursor, f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `source_item_id` = %s FOR UPDATE", (product["source_item_id"],))
            existing = cursor.fetchone()
            if existing and product_access.actor() and not product_access.can_operate(existing, product_access.actor()):
                raise PermissionError("只能重新采集自己的产品")
            existing = existing if existing and existing.get("source_type") == "ai_original" else {}
            existing_snapshot = _loads(existing.get("source_snapshot_json"), {})
            overrides = (
                existing_snapshot.get("source_1688_overrides")
                if isinstance(existing_snapshot, Mapping) else None
            )
            overrides = dict(overrides) if isinstance(overrides, Mapping) else {}
            if overrides:
                source_snapshot["source_1688_overrides"] = overrides
            if isinstance(existing_snapshot, Mapping) and isinstance(existing_snapshot.get("purchase_net_proceeds"), Mapping):
                source_snapshot["purchase_net_proceeds"] = dict(existing_snapshot["purchase_net_proceeds"])
            if isinstance(existing_snapshot, Mapping) and isinstance(existing_snapshot.get("manual_net_proceeds"), Mapping):
                source_snapshot["manual_net_proceeds"] = dict(existing_snapshot["manual_net_proceeds"])
            if isinstance(existing_snapshot, Mapping) and isinstance(existing_snapshot.get("marketplace_listings"), Mapping):
                source_snapshot["marketplace_listings"] = dict(existing_snapshot["marketplace_listings"])
            existing_prepared = existing_snapshot.get("ai_original") if isinstance(existing_snapshot, Mapping) else None
            if isinstance(existing_prepared, Mapping):
                # Recollection changes source facts, never the already spent
                # allowance. Keep checkpoints; fingerprints invalidate only
                # steps affected by the new source on the next generation.
                source_snapshot["ai_original"] = dict(existing_prepared)
                source_snapshot["ai_original"]["edit_revision"] = _ai_original_edit_revision(existing_prepared) + 1
                if existing_prepared.get("status") != "completed":
                    source_snapshot["ai_original"].update(status="pending", error="")
                source_snapshot.setdefault("marketplace_listings", {})["mercadolibre"] = dict(source_snapshot["ai_original"])
            # Completed listings keep their saved source snapshot on recollection.
            # Calculate from that same snapshot, and preserve an entered net value.
            if isinstance(overrides.get('variations'), list):
                from erp.sku_matrix import sku_identity, sync_supplier_skus
                reviewed = {sku_identity(v): v for v in overrides['variations']}
                merged = [{**v, **reviewed.get(sku_identity(v), {})} for v in product.get('variations') or []]
                if not merged and len(reviewed) == 1:
                    identity, reviewed_single = next(iter(reviewed.items()))
                    offer_id = str(product.get('source_1688_item_id') or '')
                    if offer_id and identity in {offer_id, f'1688{offer_id}', f'1688_{offer_id}'}:
                        merged = [dict(reviewed_single)]
                sync_supplier_skus(source_snapshot, merged)
            net_snapshot = (
                existing_snapshot if isinstance(existing_prepared, Mapping)
                and existing_prepared.get("status") == "completed" else source_snapshot
            )
            default_net = _default_ai_original_net_proceeds(net_snapshot)
            previous_net = _default_ai_original_net_proceeds(
                existing_snapshot if isinstance(existing_snapshot, Mapping) else {},
            )
            source_net_changed = bool(existing) and default_net != previous_net
            if existing:
                _mark_ai_marketplace_drafts_unreviewed(source_snapshot)
            _execute_access_insert(cursor, f"""
                INSERT INTO `{PRODUCT_TABLE}` (
                    `collection_item_id`, `source_type`, `review_status`,
                    `source_item_id`, `source_url`, `main_image_url`, `title`,
                    `description_text`, `price`, `currency_id`, `weight_g`,
                    `package_length_cm`, `package_width_cm`, `package_height_cm`,
                    `weight_basis`, `source_snapshot_json`, `added_at`, `net_proceeds_usd`
                ) VALUES (0, 'ai_original', 'unreviewed', %s, %s, %s, %s, %s,
                          %s, 'CNY', %s, %s, %s, %s, %s, %s, %s, %s)
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
                    `price` = IF(`source_type` = 'ai_original' AND `currency_id` = 'CNY', VALUES(`price`), `price`),
                    `weight_basis` = IF(`source_type` = 'ai_original', VALUES(`weight_basis`), `weight_basis`),
                    `weight_g` = IF(`source_type` = 'ai_original', VALUES(`weight_g`), `weight_g`),
                    `package_length_cm` = IF(`source_type` = 'ai_original', VALUES(`package_length_cm`), `package_length_cm`),
                    `package_width_cm` = IF(`source_type` = 'ai_original', VALUES(`package_width_cm`), `package_width_cm`),
                    `package_height_cm` = IF(`source_type` = 'ai_original', VALUES(`package_height_cm`), `package_height_cm`),
                    `net_proceeds_usd` = IF(`source_type` = 'ai_original' AND `net_proceeds_usd` IS NULL,
                        VALUES(`net_proceeds_usd`), `net_proceeds_usd`),
                    `source_snapshot_json` = IF(`source_type` = 'ai_original' AND
                        JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(`source_snapshot_json`),
                        `source_snapshot_json`, '{{}}'), '$.ai_original.status')) <> 'completed',
                        VALUES(`source_snapshot_json`), `source_snapshot_json`),
                    `updated_at` = CURRENT_TIMESTAMP
                """, (
                    product["source_item_id"], product["source_url"],
                    product.get("main_image_url") or "", product["title"],
                    product.get("description_text") or "", _decimal_from_text(product.get("price")),
                    weight_g, dimensions[0], dimensions[1], dimensions[2],
                    "1688_source" if weight_g else "", _dumps(source_snapshot), now, default_net,
                ))
            if existing:
                _execute_source_scoped(
                    cursor,
                    f"UPDATE `{PRODUCT_TABLE}` SET `review_status` = %s "
                    "WHERE `source_item_id` = %s AND `source_type` = 'ai_original'",
                    ("unreviewed", product["source_item_id"]),
                )
            if isinstance(existing_prepared, Mapping) and existing_prepared.get("status") == "completed":
                from erp.ai_original_images import merge_recollected_sku_images
                supplemented = merge_recollected_sku_images(existing_snapshot, product)
                supplemented = dict(supplemented) if isinstance(supplemented, Mapping) else dict(existing_snapshot)
                prepared = supplemented.get("ai_original")
                if isinstance(prepared, Mapping):
                    prepared = dict(prepared)
                    prepared["edit_revision"] = _ai_original_edit_revision(existing_prepared) + 1
                    supplemented["ai_original"] = prepared
                    supplemented.setdefault("marketplace_listings", {})["mercadolibre"] = dict(prepared)
                # The upsert intentionally keeps a completed snapshot. Persist
                # the review reset explicitly so a recollection cannot leave an
                # old Amazon/Yandex approval attached to changed source facts.
                _mark_ai_marketplace_drafts_unreviewed(supplemented)
                _execute_source_scoped(cursor, f"UPDATE `{PRODUCT_TABLE}` SET `source_snapshot_json` = %s "
                    "WHERE `source_item_id` = %s AND `source_type` = 'ai_original'",
                    (_dumps(supplemented), product['source_item_id']))
            if category_id is not None:
                _execute_source_scoped(cursor, f"UPDATE `{PRODUCT_TABLE}` SET `management_category_id` = %s "
                    "WHERE `source_item_id` = %s AND `source_type` = 'ai_original'", (category_id, product["source_item_id"]))
            if overrides or source_net_changed:
                assignments, values = _1688_override_table_assignments(existing, overrides)
                if source_net_changed and "net_proceeds_usd" not in overrides:
                    assignments.append("`net_proceeds_usd` = %s")
                    values.append(default_net)
                if assignments:
                    _execute_source_scoped(cursor, f"UPDATE `{PRODUCT_TABLE}` SET {', '.join(assignments)} "
                        "WHERE `source_item_id` = %s AND `source_type` = 'ai_original'", tuple(values + [product["source_item_id"]]))
            _execute_source_scoped(cursor, f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `source_item_id` = %s", (product["source_item_id"],))
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
        "category_id", "category_name", "net_proceeds_usd", "review_status", "currency_id",
    }
    normalized = {key: value for key, value in dict(changes or {}).items() if key in allowed}
    content_fields = allowed - {"review_status", "source_snapshot_json"}
    if "review_status" not in normalized and set(normalized).intersection(content_fields):
        normalized["review_status"] = "unreviewed"
    platform_listing = (changes or {}).get("marketplace_listing")
    expected_revision = (changes or {}).get("expected_ai_original_revision")
    expected_budget_revision = (changes or {}).get("expected_generation_budget_revision")
    if expected_budget_revision is not None and (isinstance(expected_budget_revision, bool)
            or not isinstance(expected_budget_revision, int) or expected_budget_revision < 0):
        raise ValueError("费用记录版本必须是非负整数")
    if expected_revision is not None and (isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0):
        raise ValueError("刊登资料版本必须是非负整数")
    if platform_listing is not None:
        from erp.ai_original_platforms import normalize_listing_options

        if not isinstance(platform_listing, Mapping):
            raise ValueError("平台草稿必须是对象")
        platform, _ = normalize_listing_options(platform_listing.get("target_platform"), platform_listing.get("template_id", "standard"))
        if platform == "mercadolibre":
            raise ValueError("美客多草稿请通过原刊登编辑接口保存")
        platform_listing = dict(platform_listing)
        if platform_listing.get("review_status") == "approved":
            # Generation/internal snapshot writes can prepare a draft, but only
            # the explicit per-card review endpoint may grant approval.
            platform_listing["review_status"] = "unreviewed"
        # A platform-only update must never change the Mercado row columns.
        normalized = {"source_snapshot_json": {}}
        expected_revision = None
    if not normalized:
        raise ValueError("没有可保存的 AI 原创产品字段")
    if "review_status" in normalized and normalized["review_status"] not in PRODUCT_REVIEW_STATUSES:
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
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s "
                "AND `source_type` = 'ai_original' FOR UPDATE", (row_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise KeyError("AI 原创产品不存在")
            user = product_access.actor()
            if user and not product_access.can_operate(current, user):
                raise PermissionError("只能修改自己有操作权限的 AI 原创产品")
            if normalized.get("review_status") == "approved":
                content_changed = "source_snapshot_json" in normalized
                for field in set(normalized).intersection(content_fields):
                    if field in decimal_fields:
                        different = (
                            _decimal_from_text(normalized.get(field))
                            != _decimal_from_text(current.get(field))
                        )
                    elif field in {"category_id", "currency_id"}:
                        different = (
                            str(normalized.get(field) or "").strip().upper()
                            != str(current.get(field) or "").strip().upper()
                        )
                    else:
                        different = (
                            str(normalized.get(field) or "").strip()
                            != str(current.get(field) or "").strip()
                        )
                    content_changed = content_changed or different
                if ("weight_g" in normalized
                        and current.get("weight_basis") != "ai_original_manual"):
                    content_changed = True
                if content_changed:
                    raise ValueError("修改商品资料后需重新审核，审核通过必须单独保存")
            approval_snapshot = _loads(current.get("source_snapshot_json"), {})
            approval_snapshot = (
                approval_snapshot if isinstance(approval_snapshot, Mapping) else {}
            )
            shared_package_fields = {
                "weight_g", "package_length_cm", "package_width_cm", "package_height_cm",
                "net_proceeds_usd",
            }
            reset_platform_reviews = platform_listing is None and any(
                field in normalized and (
                    _decimal_from_text(normalized[field]) != _decimal_from_text(current.get(field))
                    or (field == "weight_g" and current.get("weight_basis") != "ai_original_manual")
                )
                for field in shared_package_fields
            )
            manual_category_change = (
                platform_listing is None
                and "category_id" in normalized
                and "source_snapshot_json" not in normalized
                and str(normalized.get("category_id") or "").strip().upper()
                    != str(current.get("category_id") or "").strip().upper()
            )
            if (reset_platform_reviews or manual_category_change) and "source_snapshot_json" not in normalized:
                normalized["source_snapshot_json"] = _loads(current.get("source_snapshot_json"), {})
                assignments.append("`source_snapshot_json` = %s")
                values.append(_dumps(normalized["source_snapshot_json"]))
            if "source_snapshot_json" in normalized or expected_revision is not None:
                # An AI worker can save a snapshot loaded before a source edit.
                # Lock and merge the current overrides so that save cannot erase it.
                existing_snapshot = approval_snapshot
                existing_prepared = existing_snapshot.get("ai_original") if isinstance(existing_snapshot, Mapping) else {}
                old_budget = (existing_prepared or {}).get("generation_budget")
                budget_revision = int(old_budget.get("revision") or 0) if isinstance(old_budget, Mapping) else 0
                if expected_budget_revision is not None and expected_budget_revision != budget_revision:
                    raise ValueError("商品费用记录已变化，已停止付费调用；请刷新后继续")
                current_revision = _ai_original_edit_revision(existing_prepared)
                if expected_revision is not None and expected_revision != current_revision:
                    raise ValueError("刊登资料已被人工修改，请重新执行未完成步骤")
                incoming_snapshot = _loads(normalized.get("source_snapshot_json"), {})
                if isinstance(existing_snapshot, Mapping) and isinstance(incoming_snapshot, Mapping):
                    if platform_listing is not None:
                        incoming_snapshot = dict(existing_snapshot)
                        drafts = dict(existing_snapshot.get("marketplace_listings") or {})
                        drafts[platform] = dict(platform_listing)
                        incoming_snapshot["marketplace_listings"] = drafts
                    else:
                        incoming_snapshot = dict(incoming_snapshot)
                        # Manual/source edits can carry an older snapshot. A
                        # saved reservation must never disappear or roll back
                        # when an unrelated field is updated concurrently.
                        old_budget = (existing_prepared or {}).get("generation_budget")
                        prepared = incoming_snapshot.get("ai_original")
                        if isinstance(old_budget, Mapping):
                            prepared = dict(prepared) if isinstance(prepared, Mapping) else dict(existing_prepared)
                            new_budget = prepared.get("generation_budget")
                            old_revision = int(old_budget.get("revision") or 0)
                            new_revision = int(new_budget.get("revision") or 0) if isinstance(new_budget, Mapping) else -1
                            if new_revision <= old_revision or expected_budget_revision is None:
                                prepared["generation_budget"] = dict(old_budget)
                            incoming_snapshot["ai_original"] = prepared
                        if expected_revision is not None and isinstance(incoming_snapshot.get("ai_original"), Mapping):
                            incoming_snapshot["ai_original"] = dict(incoming_snapshot["ai_original"])
                            incoming_snapshot["ai_original"]["edit_revision"] = current_revision
                        # Workers hold old snapshots. Preserve other platforms'
                        # newest drafts, including manual edits made meanwhile.
                        drafts = dict(incoming_snapshot.get("marketplace_listings") or {})
                        for target_platform, raw_draft in list(drafts.items()):
                            if target_platform == "mercadolibre" or not isinstance(raw_draft, Mapping):
                                continue
                            draft = dict(raw_draft)
                            if draft.get("review_status") == "approved":
                                draft["review_status"] = "unreviewed"
                            drafts[target_platform] = draft
                        drafts.update(existing_snapshot.get("marketplace_listings") or {})
                        if isinstance(incoming_snapshot.get("ai_original"), Mapping):
                            drafts["mercadolibre"] = dict(incoming_snapshot["ai_original"])
                        incoming_snapshot["marketplace_listings"] = drafts
                    overrides = existing_snapshot.get("source_1688_overrides")
                    if isinstance(overrides, Mapping):
                        incoming_snapshot["source_1688_overrides"] = dict(overrides)
                    purchase_target = existing_snapshot.get("purchase_net_proceeds")
                    if isinstance(purchase_target, Mapping):
                        incoming_snapshot["purchase_net_proceeds"] = dict(purchase_target)
                    manual_target = existing_snapshot.get("manual_net_proceeds")
                    if isinstance(manual_target, Mapping):
                        incoming_snapshot["manual_net_proceeds"] = dict(manual_target)
                    if manual_category_change:
                        prepared = incoming_snapshot.get("ai_original")
                        prepared = dict(prepared) if isinstance(prepared, Mapping) else {}
                        category_id = str(normalized.get("category_id") or "").strip().upper()
                        prepared.update(
                            category_id=category_id,
                            status="failed",
                            failed_step="category",
                            workflow_status="needs_category",
                            error="类目已修改，请重新执行 AI 制作以生成该类目的必填属性",
                        )
                        checkpoint = dict(prepared.get("preparation") or {})
                        checkpoint.update(
                            category_ready=False, variations_ready=False, required_ready=False,
                        )
                        prepared["preparation"] = checkpoint
                        incoming_snapshot["ai_original"] = prepared
                        drafts = incoming_snapshot.get("marketplace_listings")
                        drafts = dict(drafts) if isinstance(drafts, Mapping) else {}
                        drafts["mercadolibre"] = dict(prepared)
                        incoming_snapshot["marketplace_listings"] = drafts
                    if reset_platform_reviews:
                        _mark_ai_marketplace_drafts_unreviewed(incoming_snapshot)
                    if "source_snapshot_json" in normalized:
                        if (platform_listing is None and "review_status" not in normalized
                                and _ai_original_review_payload(incoming_snapshot)
                                != _ai_original_review_payload(existing_snapshot)):
                            normalized["review_status"] = "unreviewed"
                            assignments.append("`review_status` = %s")
                            values.append("unreviewed")
                        # Approval must validate the exact merged snapshot that
                        # this transaction will store, not the pre-edit row.
                        approval_snapshot = (
                            incoming_snapshot if isinstance(incoming_snapshot, Mapping) else {}
                        )
                        values[list(normalized).index("source_snapshot_json")] = _dumps(incoming_snapshot)
                        if (platform_listing is None and "net_proceeds_usd" not in normalized
                                and current.get("net_proceeds_usd") is None):
                            default_net = _default_ai_original_net_proceeds(
                                incoming_snapshot,
                                fallback_price=(current.get("price")
                                    if str(current.get("currency_id") or "").upper() == "CNY"
                                    else None),
                            )
                            if default_net is not None:
                                assignments.append(
                                    "`net_proceeds_usd` = IF(`net_proceeds_usd` IS NULL, %s, `net_proceeds_usd`)"
                                )
                                values.append(default_net)
                                normalized["net_proceeds_usd"] = default_net
            if normalized.get("review_status") == "approved":
                snapshot = approval_snapshot
                prepared = snapshot.get("ai_original")
                prepared = prepared if isinstance(prepared, Mapping) else {}
                from erp.ai_original_state import manual_approval_issues
                candidate = {**current, **normalized}
                if "weight_g" in normalized:
                    candidate["weight_basis"] = "ai_original_manual"
                issues = manual_approval_issues(candidate, prepared, snapshot.get("original_1688"))
                if issues:
                    raise ValueError("审核通过前需补齐所有必填项：" + "；".join(issues))
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
            # Report success only for the values actually stored by MySQL.
            # Decimal columns may round user input; never silently approve a
            # different net amount or an unchanged review result.
            for field in ("weight_g", "net_proceeds_usd", "review_status", "category_id"):
                if field not in normalized:
                    continue
                expected = normalized[field]
                actual = (row or {}).get(field)
                if field in decimal_fields:
                    expected = _decimal_from_text(expected)
                    actual = _decimal_from_text(actual)
                    matches = expected == actual or (expected is not None and actual is not None
                        and abs(expected - actual) <= Decimal("0.00005"))
                else:
                    matches = expected == actual
                if not matches:
                    raise ValueError(f"保存后回读的 {field} 不一致，请刷新后重试")
        connection.commit()
        return _mirror_zying_snapshot_fields(_json_safe_row(row))
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _update_ai_original_net_proceeds(ids, changes, *, connection_factory=None):
    """Persist fixed amounts or multiply each locked row's current amount."""
    fields = {"net_proceeds_usd", "net_proceeds_ratio"}
    if len(changes) != 1 or not set(changes).issubset(fields):
        raise ValueError("请选择统一金额或按比例修改净收益，不能同时设置")
    field, raw = next(iter(changes.items()))
    try:
        value = Decimal(str(raw))
    except Exception as exc:
        raise ValueError("净收益金额或比例必须是大于 0 的有效数字") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("净收益金额或比例必须是大于 0 的有效数字")
    if field == "net_proceeds_ratio" and value > 10000:
        raise ValueError("净收益比例不能超过 10000%")
    if field == "net_proceeds_usd" and value >= Decimal("10000000000000000"):
        raise ValueError("净收益金额超出可保存范围")
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT `id`, `net_proceeds_usd`, `source_snapshot_json`, `organization_key`, `owner_user_id`, "
                f"`owner_name`, `visibility` FROM `{PRODUCT_TABLE}` "
                f"WHERE `source_type` = 'ai_original' AND `id` IN ({', '.join(['%s'] * len(ids))}) FOR UPDATE",
                tuple(ids),
            )
            rows = cursor.fetchall()
            if {int(row["id"]) for row in rows} != set(ids):
                raise KeyError("部分 AI 原创产品不存在或已移除")
            user = product_access.actor()
            if user and any(not product_access.can_operate(row, user) for row in rows):
                raise PermissionError("当前账号无权访问所选产品")
            updates, skipped = [], 0
            for row in rows:
                amount = value
                if field == "net_proceeds_ratio":
                    current = _decimal_from_text(row.get("net_proceeds_usd"))
                    if current is None or current <= 0:
                        skipped += 1
                        continue
                    amount = current * value / Decimal("100")
                if amount >= Decimal("10000000000000000"):
                    raise ValueError("修改后的净收益金额超出可保存范围")
                amount = amount.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                if amount <= 0:
                    raise ValueError("修改后的净收益金额不能小于 0.0001 USD")
                updates.append((amount, int(row["id"]), row.get("source_snapshot_json")))
            # Validate the entire selection before writing any amount.
            for amount, row_id, raw_snapshot in updates:
                snapshot = _loads(raw_snapshot, {})
                snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else {}
                _mark_ai_marketplace_drafts_unreviewed(snapshot)
                cursor.execute(
                    f"UPDATE `{PRODUCT_TABLE}` SET `net_proceeds_usd` = %s, "
                    "`source_snapshot_json` = %s, `review_status` = 'unreviewed' "
                    "WHERE `id` = %s AND `source_type` = 'ai_original'",
                    (amount, _dumps(snapshot), row_id),
                )
                cursor.execute(
                    f"SELECT `net_proceeds_usd` FROM `{PRODUCT_TABLE}` WHERE `id` = %s", (row_id,),
                )
                saved = cursor.fetchone()
                if not saved or _decimal_from_text(saved.get("net_proceeds_usd")) != amount:
                    raise ValueError("净收益保存后回读不一致，请刷新后重试")
        connection.commit()
        return {"requested": len(ids), "changed": len(updates), "skipped": skipped,
                "updated_fields": ["net_proceeds_usd", "review_status"]}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_ai_original_product_items(
    product_item_ids: Iterable[int],
    changes: Mapping[str, Any],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Batch-edit net proceeds, or fill products missing all dimensions."""

    ids = _normalize_row_ids(product_item_ids, empty_message="请至少勾选一个 AI 原创产品")
    if set(changes or {}).intersection({"net_proceeds_usd", "net_proceeds_ratio"}):
        return _update_ai_original_net_proceeds(ids, changes, connection_factory=connection_factory)
    allowed = {
        "package_length_cm", "package_width_cm", "package_height_cm",
    }
    unsupported = set(changes or {}).difference(allowed)
    if unsupported:
        raise ValueError("AI 原创产品批量修改只支持包装长、宽、高")
    normalized = _normalize_product_content_changes(changes)
    assignments, values, profitability_stale = _product_content_update_plan(normalized)
    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT `id`, `package_length_cm`, `package_width_cm`, `package_height_cm`, "
                "`source_snapshot_json`, "
                "`organization_key`, `owner_user_id`, `owner_name`, `visibility` "
                f"FROM `{PRODUCT_TABLE}` "
                f"WHERE `source_type` = 'ai_original' AND `id` IN ({placeholders}) "
                "FOR UPDATE",
                tuple(ids),
            )
            selected_rows = cursor.fetchall()
            found_ids = {int(row["id"]) for row in selected_rows}
            if found_ids != set(ids):
                raise KeyError("部分 AI 原创产品不存在或已移除")
            user = product_access.actor()
            if user and any(not product_access.can_operate(row, user) for row in selected_rows):
                raise PermissionError("当前账号无权访问所选产品")

            missing_ids = [int(row["id"]) for row in selected_rows if all(
                not row.get(field) or float(row[field]) <= 0 for field in allowed
            )]
            changed = 0
            if missing_ids:
                missing_placeholders = ", ".join(["%s"] * len(missing_ids))
                cursor.execute(
                    f"UPDATE `{PRODUCT_TABLE}` SET {', '.join(assignments)}, "
                    "`review_status` = 'unreviewed' "
                    f"WHERE `source_type` = 'ai_original' AND `id` IN ({missing_placeholders})",
                    tuple(values + missing_ids),
                )
                changed = int(cursor.rowcount or 0)
                selected_by_id = {int(row["id"]): row for row in selected_rows}
                for row_id in missing_ids:
                    snapshot = _loads(selected_by_id[row_id].get("source_snapshot_json"), {})
                    snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else {}
                    _mark_ai_marketplace_drafts_unreviewed(snapshot)
                    cursor.execute(
                        f"UPDATE `{PRODUCT_TABLE}` SET `source_snapshot_json` = %s "
                        "WHERE `id` = %s AND `source_type` = 'ai_original'",
                        (_dumps(snapshot), row_id),
                    )

            dimension_fields = [field for field in normalized if field in allowed]
            collection_assignments = [
                f"c.`{field}` = p.`{field}`" for field in dimension_fields
            ]
            if dimension_fields and missing_ids:
                collection_assignments.append(
                    "c.`volumetric_weight_kg` = p.`volumetric_weight_kg`"
                )
                cursor.execute(
                    f"UPDATE `{COLLECTION_TABLE}` AS c "
                    f"INNER JOIN `{PRODUCT_TABLE}` AS p "
                    "ON c.`source_item_id` = p.`source_item_id` AND c.`organization_key` = p.`organization_key` "
                    f"SET {', '.join(collection_assignments)} "
                    f"WHERE p.`source_type` = 'ai_original' AND p.`id` IN ({missing_placeholders})",
                    tuple(missing_ids),
                )
        connection.commit()
        return {
            "requested": len(ids),
            "changed": changed,
            "updated_fields": [*normalized, "review_status"],
            "profitability_refresh_pending": profitability_stale,
        }
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ai_original_edit_revision(prepared: Any) -> int:
    value = prepared.get("edit_revision") if isinstance(prepared, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _sync_ai_original_cached_copy(prepared: dict[str, Any], fields: Iterable[str]) -> None:
    cached = prepared.get("generated_copy")
    if not isinstance(cached, Mapping):
        return
    cached = dict(cached)
    for field in fields:
        cached[field] = prepared[field]
        if field == "title_es":
            cached["title"] = prepared[field]
        elif field == "description_es":
            cached["description"] = prepared[field]
    prepared["generated_copy"] = cached


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
    if len(value) > 2000:
        raise ValueError("变体数量超过 2000，请拆分商品后再保存")
    result = []
    for raw in value:
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


def _editor_variation_dimensions(value: Any) -> list[dict[str, Any]]:
    """Persist the editable dimension names and option values for SKU generation."""
    if not isinstance(value, list):
        raise ValueError("变体规格必须是数组")
    result: list[dict[str, Any]] = []
    for raw in value[:20]:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name") or "").strip()[:128]
        if not name:
            continue
        raw_values = raw.get("values")
        if isinstance(raw_values, str):
            raw_values = re.split(r"[,，]", raw_values)
        if not isinstance(raw_values, list):
            raw_values = []
        values = list(dict.fromkeys(
            str(item or "").strip()[:255]
            for item in raw_values
            if str(item or "").strip()
        ))[:200]
        result.append({
            "id": str(raw.get("id") or "").strip()[:64],
            "name": name,
            "values": values,
        })
    return result


def _update_ai_platform_listing(product_item_id, data, platform, *, connection_factory=None):
    """Edit one platform under a row lock without altering any other draft."""
    from erp.ai_original_platforms import validate_platform_listing

    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s "
                "AND `source_type` = 'ai_original' FOR UPDATE", (product_item_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise KeyError("AI 原创产品不存在")
            user = product_access.actor()
            if user and not product_access.can_operate(current, user):
                raise PermissionError("只能修改自己有操作权限的 AI 原创产品")
            snapshot = _loads(current.get("source_snapshot_json"), {})
            drafts = dict(snapshot.get("marketplace_listings") or {})
            draft = dict(drafts.get(platform) or {})
            if not draft:
                raise ValueError("请先生成该平台的商品草稿")
            content_fields = {
                "title", "description", "item_highlights", "bullet_points", "attributes",
            }
            if data.get("review_status") == "approved" and set(data).intersection(content_fields):
                raise ValueError("编辑平台草稿后需重新审核，审核通过必须单独保存")
            for field in ("title", "description", "item_highlights"):
                if field in data:
                    if not isinstance(data[field], str):
                        raise ValueError(f"{field} 必须是文本")
                    draft[field] = data[field].strip()
            if "bullet_points" in data:
                points = data["bullet_points"]
                if not isinstance(points, list) or any(not isinstance(value, str) for value in points):
                    raise ValueError("卖点必须是文本数组")
                draft["bullet_points"] = [value.strip() for value in points if value.strip()]
            if "attributes" in data:
                if not isinstance(data["attributes"], list):
                    raise ValueError("商品属性必须是数组")
                draft["attributes"] = _editor_attributes(data["attributes"])
            draft["validation"] = validate_platform_listing(draft, platform)
            review_status = data.get("review_status")
            if "review_status" in data:
                if review_status not in PRODUCT_REVIEW_STATUSES:
                    raise ValueError("AI 原创产品审核状态无效")
                if review_status in {"approved", "unreviewed"} and (
                    draft.get("status") != "completed" or not draft.get("main_image_url")
                    or draft["validation"]["errors"]
                ):
                    raise ValueError("进入待审核或通过前，需完成 AI 制作并补齐所有必填项")
                if review_status == "approved":
                    if current.get("review_status") in {"suspected", "infringing", "risk"}:
                        raise ValueError("商品仍标记为侵权风险或待复核，请先处理 Mercado 状态")
                    from erp.ai_original_state import manual_listing_issues
                    issues = manual_listing_issues(current)
                    if issues:
                        raise ValueError("审核通过前需补齐所有必填项：" + "；".join(issues))
                draft["review_status"] = review_status
            elif set(data).difference({"target_platform"}):
                draft["review_status"] = "unreviewed"
            if draft["validation"]["errors"] and not (
                set(data) <= {"target_platform", "review_status"}
                and review_status in {"failed", "suspected", "infringing", "risk"}
            ):
                raise ValueError("；".join(draft["validation"]["errors"]))
            # Saving text cannot turn a failed generation or missing image into a completed job.
            draft["manually_edited"] = True
            drafts[platform] = draft
            snapshot["marketplace_listings"] = drafts
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` SET `source_snapshot_json` = %s "
                "WHERE `id` = %s AND `source_type` = 'ai_original'",
                (_dumps(snapshot), product_item_id),
            )
            cursor.execute(f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s", (product_item_id,))
            row = cursor.fetchone()
        connection.commit()
        return _mirror_zying_snapshot_fields(_json_safe_row(row))
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


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
    from erp.ai_original_platforms import normalize_listing_options

    platform, _ = normalize_listing_options(data.get("target_platform", "mercadolibre"))
    if platform != "mercadolibre":
        return _update_ai_platform_listing(row_id, data, platform, connection_factory=connection_factory)
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s "
                "AND `source_type` = 'ai_original' FOR UPDATE",
                (row_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise KeyError("AI 原创产品不存在")
            user = product_access.actor()
            if user and not product_access.can_operate(current, user):
                raise PermissionError("只能修改自己有操作权限的 AI 原创产品")
            snapshot = _loads(current.get("source_snapshot_json"), {})
            if not isinstance(snapshot, dict):
                snapshot = {}
            original = dict(snapshot.get("original_1688") or {})
            prepared = dict(snapshot.get("ai_original") or {})
            source = dict(snapshot.get("source") or {})

            source_title = _editor_text(data.get("source_title"), 255)
            source_description = _editor_text(data.get("source_description"), 50000)
            source_title_changed = (
                "source_title" in data and bool(source_title)
                and source_title != _editor_text(original.get("title"), 255)
            )
            source_description_changed = (
                "source_description" in data
                and source_description != _editor_text(original.get("description_text"), 50000)
            )
            category_id = _editor_text(data.get("category_id", prepared.get("category_id") or source.get("category_id") or current.get("category_id")), 64).upper()
            category_name = _editor_text(data.get("category_name", prepared.get("category_name") or source.get("category_name") or current.get("category_name")), 255)
            if "category_locked" in data and not isinstance(data["category_locked"], bool):
                raise ValueError("锁定分类必须是布尔值")
            category_locked = data.get("category_locked", prepared.get("category_locked") is True)
            if not category_id:
                category_locked = False
                category_name = ""
            elif category_locked and not re.fullmatch(r"CBT\d+", category_id):
                raise ValueError("锁定分类时必须填写有效的 CBT 分类编号")
            title_es = _editor_text(data.get("title_es"), 255)
            title_pt = _editor_text(data.get("title_pt"), 255)
            description_es = _editor_text(data.get("description_es"), 50000)
            description_pt = _editor_text(data.get("description_pt"), 50000)
            attributes = _editor_attributes(data.get("attributes"))
            variations = _editor_variations(data.get("variations"))
            variation_dimensions = _editor_variation_dimensions(data.get("variation_dimensions", []))
            category_changed = (
                category_id != str(prepared.get("category_id") or source.get("category_id") or current.get("category_id") or "").strip().upper()
                or category_locked != (prepared.get("category_locked") is True)
            )
            variations_changed = "variations" in data and variations != _editor_variations(prepared.get("variations", original.get("variations")))
            dimensions_changed = "variation_dimensions" in data and variation_dimensions != _editor_variation_dimensions(prepared.get("variation_dimensions", original.get("variation_dimensions", [])))
            attributes_changed = "attributes" in data and attributes != _editor_attributes(prepared.get("attributes"))
            old_attribute_ids = {item.get("id") for item in _editor_attributes(prepared.get("attributes")) if item.get("id")}
            new_attribute_ids = {item.get("id") for item in attributes if item.get("id")}
            removed_attribute_ids = (
                set(prepared.get("manual_attribute_removed_ids") or [])
                | (old_attribute_ids - new_attribute_ids)
            ) - new_attribute_ids
            copy_changes = [field for field, value, limit in (
                ("title_es", title_es, 255), ("title_pt", title_pt, 255),
                ("description_es", description_es, 50000), ("description_pt", description_pt, 50000),
            ) if field in data and value != _editor_text(prepared.get(field), limit)]

            if "source_title" in data and source_title:
                original["title"] = source_title
            if "source_description" in data:
                original["description_text"] = source_description
            if "category_id" in data:
                original["category_id"] = category_id
                source["category_id"] = category_id
                prepared["category_id"] = category_id
            if "category_name" in data:
                original["category_name"] = category_name
                source["category_name"] = category_name
                prepared["category_name"] = category_name
            if "category_id" in data and not category_id:
                original["category_name"] = ""
                source["category_name"] = ""
                prepared["category_name"] = ""
            prepared["category_locked"] = category_locked
            if category_locked:
                prepared["category_id"] = category_id
                prepared["category_name"] = category_name
                source["category_id"] = category_id
                source["category_name"] = category_name
            if "title_es" in data:
                prepared["title_es"] = title_es
                prepared["title"] = title_es
            if "title_pt" in data:
                prepared["title_pt"] = title_pt
            if "description_es" in data:
                prepared["description_es"] = description_es
                prepared["description"] = description_es
            if "description_pt" in data:
                prepared["description_pt"] = description_pt
            _sync_ai_original_cached_copy(prepared, copy_changes)
            if "attributes" in data:
                prepared["attributes"] = attributes
                source["attributes"] = attributes
                if attributes_changed:
                    prepared["manual_attributes"] = attributes
                    prepared["manual_attribute_removed_ids"] = sorted(removed_attribute_ids)
            if "variations" in data:
                prepared["variations"] = variations
                source["variations"] = variations
                from erp.sku_matrix import sync_single_sku_cover
                cover = sync_single_sku_cover(prepared)
                sync_single_sku_cover(source)
                if cover:
                    cursor.execute(f"UPDATE `{PRODUCT_TABLE}` SET `main_image_url` = %s WHERE `id` = %s", (cover, row_id))
                if variations_changed:
                    prepared["source_variations"] = variations
            if "variation_dimensions" in data:
                prepared["variation_dimensions"] = variation_dimensions
                source["variation_dimensions"] = variation_dimensions
                if dimensions_changed:
                    prepared["source_variation_dimensions"] = variation_dimensions
            checkpoint = dict(prepared.get("preparation") or {})
            if category_changed:
                checkpoint.update(category_ready=False, variations_ready=False, required_ready=False)
            if variations_changed or dimensions_changed:
                checkpoint.update(variations_ready=False, required_ready=False)
            if attributes_changed:
                checkpoint["required_ready"] = False
            prepared["preparation"] = checkpoint
            if any(field in data for field in ("source_title", "source_description", "category_id", "category_name")):
                snapshot["original_1688"] = original
            manual = {}
            for field in ("weight_g", "net_proceeds_usd", "package_length_cm", "package_width_cm", "package_height_cm"):
                if field not in data:
                    continue
                value = _decimal_from_text(data[field])
                if data[field] not in (None, "") and (value is None or value <= 0):
                    raise ValueError(f"{field} 必须大于 0")
                manual[field] = value
            if manual:
                manual_changed = any(
                    _decimal_from_text(current.get(field)) != value
                    or (field == "weight_g" and current.get("weight_basis") != "ai_original_manual")
                    for field, value in manual.items()
                )
                assignments = [f"`{field}` = %s" for field in manual]
                if "weight_g" in manual:
                    assignments.append("`weight_basis` = 'ai_original_manual'")
                if any(field.startswith("package_") for field in manual):
                    assignments.append("`volumetric_weight_kg` = CASE WHEN `package_length_cm` > 0 AND `package_width_cm` > 0 AND `package_height_cm` > 0 THEN ROUND(`package_length_cm` * `package_width_cm` * `package_height_cm` / 6000, 4) ELSE NULL END")
                cursor.execute(f"UPDATE `{PRODUCT_TABLE}` SET {', '.join(assignments)} WHERE `id` = %s", tuple(manual.values()) + (row_id,))
                current.update(manual)
            else:
                manual_changed = False
            _sync_original_package_attributes(prepared, current)
            _sync_original_package_attributes(source, current)
            from erp.ai_original_platforms import validate_platform_listing
            prepared["validation"] = validate_platform_listing(prepared, "mercadolibre")
            prepared["edit_revision"] = _ai_original_edit_revision(prepared) + 1
            if source_title_changed or source_description_changed or variations_changed or dimensions_changed or manual_changed:
                _mark_ai_marketplace_drafts_unreviewed(snapshot)
            snapshot["ai_original"] = prepared
            snapshot.setdefault("marketplace_listings", {})["mercadolibre"] = dict(prepared)
            snapshot["source"] = source
            description_for_row = description_es or source_description
            if description_for_row:
                snapshot["description"] = {"plain_text": description_for_row}

            row_title = title_es or title_pt or str(current.get("title") or "")
            row_description = description_for_row or str(current.get("description_text") or "")
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` SET `title` = %s, `description_text` = %s, "
                "`category_id` = %s, `category_name` = %s, `source_snapshot_json` = %s, "
                "`review_status` = %s "
                "WHERE `id` = %s AND `source_type` = 'ai_original'",
                (
                    row_title[:255], row_description[:50000], category_id,
                    category_name, _dumps(snapshot), "unreviewed", row_id,
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
            user = product_access.actor()
            if user and not product_access.can_operate(current, user):
                raise PermissionError("只能修改自己有操作权限的 AI 原创产品")
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
            # Translation performs a network call. Re-read after it completes so
            # a newly generated/edited platform draft is not lost to this old snapshot.
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `id` = %s "
                "AND `source_type` = 'ai_original' FOR UPDATE", (row_id,),
            )
            latest = cursor.fetchone()
            if not latest:
                raise KeyError("AI 原创产品不存在")
            if user and not product_access.can_operate(latest, user):
                raise PermissionError("只能修改自己有操作权限的 AI 原创产品")
            latest_snapshot = _loads(latest.get("source_snapshot_json"), {})
            if (latest_snapshot.get("ai_original") != snapshot.get("ai_original")
                    or latest_snapshot.get("original_1688") != snapshot.get("original_1688")):
                raise ValueError("翻译期间商品资料已更新，请刷新后重新翻译")
            snapshot = dict(latest_snapshot)
            prepared["title"] = prepared.get("title_es") or ""
            prepared["description"] = prepared.get("description_es") or ""
            _sync_ai_original_cached_copy(prepared, [field for field in (
                "title_es", "title_pt", "description_es", "description_pt",
            ) if field in prepared])
            prepared["edit_revision"] = _ai_original_edit_revision(prepared) + 1
            snapshot["ai_original"] = prepared
            snapshot.setdefault("marketplace_listings", {})["mercadolibre"] = dict(prepared)
            source = dict(snapshot.get("source") or {})
            source["attributes"] = attributes
            source["variations"] = variations
            snapshot["source"] = source
            cursor.execute(
                f"UPDATE `{PRODUCT_TABLE}` SET `source_snapshot_json` = %s, `review_status` = %s "
                "WHERE `id` = %s",
                (_dumps(snapshot), "unreviewed", row_id),
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


def zying_captured_metrics(snapshot: Mapping[str, Any], record: Mapping[str, Any] | None = None) -> dict:
    """Read source-owned Zying values, including API and clicked-form captures.

    netproceed is the USD target entered in Zying, independent of the price
    currency. Missing values must not become zero or estimated proceeds.
    """
    record = record or {}
    page = snapshot.get("page_snapshot")
    page = page if isinstance(page, Mapping) else {}
    detail = record.get("detail_data") or page.get("zying_detail")
    detail = detail if isinstance(detail, Mapping) else {}
    form = record.get("detail_form_fields") or page.get("detail_form_fields")
    form = form if isinstance(form, Mapping) else {}

    def number(*values):
        return next((parsed for value in values
                     if (parsed := _decimal_from_text(value)) is not None), None)

    net = number(record.get("net_income"), snapshot.get("zying_net_proceeds_usd"),
                 detail.get("sale_netproceed"), form.get("netproceed"))
    weight = number(record.get("package_gross_weight"), snapshot.get("weight_g"),
                    detail.get("sale_weight"), form.get("weight"))
    raw_size = detail.get("sale_size")
    size = _loads(raw_size, []) if isinstance(raw_size, str) else raw_size
    size = size if isinstance(size, list) else []
    record_size = re.findall(r"\d+(?:[.,]\d+)?", str(record.get("package_dimensions") or ""))[:3]
    # Never shift a partially captured dimension into a different axis.
    if len(record_size) != 3:
        record_size = []
    result = {"net_proceeds_usd": net, "weight_g": weight}
    for index, (field, form_key) in enumerate((
        ("package_length_cm", "sizeLength"), ("package_width_cm", "sizeWidth"),
        ("package_height_cm", "sizeHeight"),
    )):
        result[field] = number(
            record_size[index] if record_size else None, snapshot.get(field),
            size[index] if len(size) > index else None, form.get(form_key),
        )
    return result


def upsert_zying_products_to_products(
    records: Iterable[Mapping[str, Any]],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, int]:
    """将智赢二级详情快照写入统一产品列表，供审核和上架流程复用。"""

    values = []
    category_ids = set()
    destination_categories = []
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
        destination_category_id = normalize_collection_management_category_id(
            record.get("management_category_id")
        )
        destination_categories.append(destination_category_id)
        if destination_category_id is not None:
            category_ids.add(destination_category_id)
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
                "salesperson": record.get("salesperson") or plugin_snapshot.get("salesperson") or "",
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
        metrics = zying_captured_metrics(snapshot, record)
        snapshot.update(metrics)
        weight_g = metrics["weight_g"]
        dimensions = [
            metrics["package_length_cm"], metrics["package_width_cm"],
            metrics["package_height_cm"],
        ]
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
        net_proceeds = metrics["net_proceeds_usd"]
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

    category_column_sql = ""
    category_update_sql = ""
    if category_ids:
        values = [
            row + (category_id,)
            for row, category_id in zip(values, destination_categories)
        ]
        category_column_sql = ", `management_category_id`"
        category_update_sql = (
            "`management_category_id` = IF(`source_type` = 'zying', "
            "COALESCE(VALUES(`management_category_id`), `management_category_id`), "
            "`management_category_id`),"
        )

    mirrored_fields = (
        "source_url", "main_image_url", "title", "description_text", "price",
        "currency_id", "weight_g", "volumetric_weight_kg", "package_length_cm",
        "package_width_cm", "package_height_cm", "weight_basis", "sale_price_usd",
        "category_id", "category_name", "net_proceeds_usd",
        "profitability_updated_at", "profitability_source", "source_snapshot_json",
        "product_developer_id", "product_developer_name",
    )
    updates = ",\n                    ".join(
        f"`{field}` = IF(`source_type` = 'zying', "
        + (
            _preserve_purchase_snapshot_sql(zying=True)
            if field == "source_snapshot_json"
            else (
                f"IF({_purchase_profitability_guard_sql()}, VALUES(`{field}`), `{field}`)"
                if field in {"profitability_updated_at", "profitability_source"}
                else f"IF({_purchase_profitability_guard_sql()}, COALESCE(VALUES(`{field}`), `{field}`), `{field}`)"
                if field == "net_proceeds_usd"
                else f"COALESCE(VALUES(`{field}`), `{field}`)"
                if field in {"weight_g", "package_length_cm", "package_width_cm", "package_height_cm"}
                else f"VALUES(`{field}`)"
            )
        )
        + f", `{field}`)"
        for field in mirrored_fields
    )
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            _validate_collection_management_categories(cursor, category_ids)
            _execute_access_insert(cursor, f"""
                INSERT INTO `{PRODUCT_TABLE}` (
                    `collection_item_id`, `source_type`, `review_status`, `source_item_id`,
                    `source_url`, `main_image_url`, `title`, `description_text`, `price`,
                    `currency_id`, `weight_g`, `volumetric_weight_kg`,
                    `package_length_cm`, `package_width_cm`, `package_height_cm`,
                    `weight_basis`, `sale_price_usd`, `category_id`, `category_name`,
                    `net_proceeds_usd`, `profitability_updated_at`, `profitability_source`,
                    `source_snapshot_json`, `added_at`, `product_developer_id`,
                    `product_developer_name`{category_column_sql}
                ) VALUES ({", ".join(["%s"] * len(values[0]))})
                ON DUPLICATE KEY UPDATE
                    {category_update_sql}
                    {updates},
                    `updated_at` = CURRENT_TIMESTAMP
                """, values, many=True)
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

    # Secret token records used by the sync worker do not include site settings.
    # Read this small indexed directory explicitly instead of inferring an owner
    # from the authorization holder or the user who triggered synchronization.
    token = dict(token)
    if "site_settings" not in token:
        settings_connection = (connection_factory or _connect)()
        try:
            with settings_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT `site_id`, `salesperson` FROM `mercado_store_site_settings` WHERE `token_id` = %s",
                    (int(token["id"]),),
                )
                token["site_settings"] = cursor.fetchall()
        finally:
            settings_connection.close()

    now = _now()
    values_by_salesperson = {}
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
        record_site_id = str(record.get("site_id") or "").strip().upper()
        site_setting = next(
            (
                setting for setting in token.get("site_settings") or []
                if str(setting.get("site_id") or "").strip().upper() == record_site_id
            ),
            {},
        )
        snapshot = {
            "source": source,
            "description": {},
            "page_snapshot": {},
            "plugin_snapshot": {
                "source_type": "pulled",
                "store_name": record.get("store_name"),
                "site_id": record.get("site_id"),
                "token_id": token.get("id"),
                "salesperson": str(site_setting.get("salesperson") or "").strip(),
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
        salesperson = str(site_setting.get("salesperson") or "").strip()
        values_by_salesperson.setdefault(salesperson, []).append((
            0, "pulled", "unreviewed", record["item_id"], record["permalink"],
            record["thumbnail_url"], record["title"], record.get("price"),
            record.get("currency_id"), record.get("weight_g"),
            record.get("volumetric_weight_kg"), record.get("package_length_cm"),
            record.get("package_width_cm"), record.get("package_height_cm"),
            "official_api" if record.get("weight_g") is not None else "official_missing",
            sale_price_usd, record.get("category_id"), record.get("listing_type_id"),
            net_amount, _dumps(snapshot), now,
        ))
    if not values_by_salesperson:
        return {"count": 0, "skipped": skipped}

    pulled_fields = (
        "source_url", "main_image_url", "title", "price", "currency_id",
        "weight_g", "volumetric_weight_kg", "package_length_cm",
        "package_width_cm", "package_height_cm", "weight_basis",
        "sale_price_usd", "category_id", "listing_type_id", "net_proceeds_usd",
        "source_snapshot_json",
    )
    updates = ",\n                    ".join(
        f"`{field}` = IF(`source_type` = 'pulled' AND JSON_EXTRACT(IF(JSON_VALID(`source_snapshot_json`), `source_snapshot_json`, '{{}}'), '$.sku_family') IS NULL, "
        + (
            _preserve_purchase_snapshot_sql()
            if field == "source_snapshot_json"
            else (
                f"IF({_purchase_profitability_guard_sql()}, VALUES(`{field}`), `{field}`)"
                if field == "net_proceeds_usd" else f"VALUES(`{field}`)"
            )
        )
        + f", `{field}`)"
        for field in pulled_fields
    )
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            # Mark the transaction in DBUtils too: its max-usage recycle must
            # not reconnect halfway through a product/SKU batch.
            if callable(getattr(connection, "begin", None)):
                connection.begin()
            for salesperson, values in values_by_salesperson.items():
                # Store ownership is per site, never the user who started sync.
                inherited = dict(token, owner_name=salesperson, owner_user_id=None)
                _execute_access_insert(cursor, f"""
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
                    `owner_name` = IF(`source_type` = 'pulled', VALUES(`owner_name`), `owner_name`),
                    `owner_user_id` = IF(`source_type` = 'pulled', VALUES(`owner_user_id`), `owner_user_id`),
                    `updated_at` = CURRENT_TIMESTAMP
                """, values, many=True, inherited=inherited)
                from erp.product_sku_groups import update_rows as update_sku_groups
                item_ids = [value[3] for value in values]
                cursor.execute(
                    f"SELECT id, source_type, organization_key, title, category_id, source_snapshot_json "
                    f"FROM `{PRODUCT_TABLE}` WHERE source_item_id IN ({','.join(['%s'] * len(item_ids))})",
                    tuple(item_ids))
                synced_rows = cursor.fetchall()
                update_sku_groups(cursor, synced_rows)
                from erp.product_sku_groups import identity as sku_identity
                from erp.product_sku_materialize import materialize
                materialize(cursor, [group[0] for row in synced_rows if (group := sku_identity(row))])
        connection.commit()
        return {"count": sum(map(len, values_by_salesperson.values())), "skipped": skipped}
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
    assignments = [
        _preserve_purchase_assignment_sql(assignment, alias="products")
        if assignment.startswith(tuple(
            f"products.`{field}` = " for field in (
                "net_proceeds_usd", "profitability_source", "profitability_updated_at",
            )
        )) else assignment
        for assignment in assignments
    ]
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
                f"""
                SELECT product_item.*,
                       management_category.`name` AS `management_category_name`,
                       management_category.`listing_ratio_percent`
                           AS `management_category_listing_ratio_percent`,
                       TRIM(REPLACE(COALESCE(NULLIF(collection_task.`created_by`, ''), ''),
                           '浏览器插件：', '')) AS `salesperson`
                FROM `{PRODUCT_TABLE}` AS product_item
                LEFT JOIN `{MANAGEMENT_CATEGORY_TABLE}` AS management_category
                  ON management_category.`id` = product_item.`management_category_id`
                LEFT JOIN `{COLLECTION_TABLE}` AS collection_item
                  ON collection_item.`id` = product_item.`collection_item_id`
                LEFT JOIN `{TASK_TABLE}` AS collection_task
                  ON collection_task.`id` = collection_item.`task_id`
                WHERE product_item.`id` IN ({placeholders})
                ORDER BY product_item.`id` ASC
                """,
                tuple(ids),
            )
            rows = [
                _mirror_zying_snapshot_fields(_json_safe_row(row))
                for row in cursor.fetchall()
            ]
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
                "`infringement_reason`, `infringement_checked_at`, `infringement_check_json` "
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
                      ON collection_items.`source_item_id` = products.`source_item_id` AND collection_items.`organization_key` = products.`organization_key`
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
                _copy_list_infringement_metadata(cursor, "product_list", "collection_list", ids)
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
                _execute_source_scoped(cursor, f"UPDATE `{COLLECTION_TABLE}` SET `added_to_products` = 0, "
                    "`review_status` = %s, `last_publish_status` = %s, "
                    "`infringement_risk_level` = %s, `infringement_keywords` = %s, "
                    "`infringement_reason` = %s, `infringement_checked_at` = %s, `infringement_check_json` = %s "
                    "WHERE `source_item_id` = %s", (
                        str(row.get("review_status") or "unreviewed"),
                        row.get("last_publish_status"),
                        row.get("infringement_risk_level"),
                        row.get("infringement_keywords"),
                        row.get("infringement_reason"),
                        row.get("infringement_checked_at"),
                        row.get("infringement_check_json"),
                        str(row.get("source_item_id") or ""),
                    ))
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
                    "WHERE (`id` = %s OR `source_item_id` = %s) AND `organization_key` = %s "
                    "ORDER BY (`id` = %s) DESC, `id` DESC LIMIT 1",
                    (
                        int(row.get("collection_item_id") or 0),
                        str(row.get("source_item_id") or ""),
                        row.get("organization_key") or product_access.organization(),
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
                    _execute_access_insert(cursor, f"""
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
                        """, values, inherited=row)
                    collection_row_id = int(cursor.lastrowid or 0)
                    if not collection_row_id:
                        _execute_source_scoped(cursor, f"SELECT `id` FROM `{COLLECTION_TABLE}` "
                            "WHERE `source_item_id` = %s ORDER BY `id` DESC LIMIT 1", (str(row.get("source_item_id") or ""),))
                        collection_row_id = int((cursor.fetchone() or {}).get("id") or 0)
                    created += 1
                if collection_row_id:
                    cursor.execute(f"UPDATE `{COLLECTION_TABLE}` SET `organization_key` = %s, "
                                   "`owner_user_id` = %s, `owner_name` = %s, `visibility` = %s WHERE `id` = %s",
                                   (row.get('organization_key') or product_access.organization(), row.get('owner_user_id'),
                                    row.get('owner_name') or '', row.get('visibility') or 'enterprise', collection_row_id))
                    cursor.execute(
                        f"UPDATE `{COLLECTION_TABLE}` SET `infringement_check_json` = %s, `category_id` = %s, `category_name` = %s WHERE `id` = %s",
                        (row.get("infringement_check_json"), row.get("category_id"), row.get("category_name"), collection_row_id),
                    )
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
                    from bit.bit_mysql import copy_infringement_check_metadata
                    copy_infringement_check_metadata(cursor, "product_list", "collection_list", moved_pairs)
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
    if status not in {"pending", "publishing", "published", "failed", "pending_review"}:
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


def _purchase_profitability_guard_sql(alias: str = "") -> str:
    """Keep purchase and explicitly edited targets authoritative during refreshes."""
    prefix = f"{alias}." if alias else ""
    return (
        f"LOWER(TRIM(COALESCE({prefix}`profitability_source`, ''))) "
        f"<> '{PURCHASE_PROFITABILITY_SOURCE}' AND "
        f"LOWER(TRIM(COALESCE({prefix}`profitability_source`, ''))) "
        f"<> '{MANUAL_PRODUCT_PROFITABILITY_SOURCE}'"
    )


def _preserve_purchase_assignment_sql(assignment: str, *, alias: str = "") -> str:
    column, value = assignment.split(" = ", 1)
    return f"{column} = IF({_purchase_profitability_guard_sql(alias)}, {value}, {column})"


def _preserve_purchase_snapshot_sql(*, zying: bool = False) -> str:
    """Merge authoritative target metadata into a new source snapshot atomically."""
    old_snapshot = "IF(JSON_VALID(`source_snapshot_json`), `source_snapshot_json`, '{}')"
    purchase_fields = (
        "'$.purchase_net_proceeds', "
        f"JSON_EXTRACT({old_snapshot}, '$.purchase_net_proceeds'), "
        "'$.manual_net_proceeds', "
        f"JSON_EXTRACT({old_snapshot}, '$.manual_net_proceeds')"
    )
    if zying:
        purchase_fields += ", '$.zying_net_proceeds_usd', `net_proceeds_usd`"
    return (
        f"IF({_purchase_profitability_guard_sql()}, VALUES(`source_snapshot_json`), "
        f"JSON_SET(VALUES(`source_snapshot_json`), {purchase_fields}))"
    )


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
                    AND {_purchase_profitability_guard_sql()}
                    {"AND `source_type` NOT IN ('zying', 'ai_original')" if table == PRODUCT_TABLE else ""}
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
                for row in pending_rows:
                    normalized_row = _json_safe_row(row)
                    if table == COLLECTION_TABLE:
                        normalized_row["profitability_source"] = "fixed_commission_15_pct"
                    rows.append(normalized_row)
                remaining = batch_limit - len(pending_rows)
                if remaining <= 0:
                    continue
                legacy_collection_refresh = (
                    "NOT (LOWER(COALESCE(`profitability_source`, '')) "
                    # PyMySQL formats parameterized statements with Python's
                    # percent operator, so a literal SQL LIKE wildcard must
                    # be doubled here.
                    "LIKE 'fixed_commission_15_pct%%') OR "
                    if table == COLLECTION_TABLE else ""
                )
                cursor.execute(
                    f"""
                    SELECT * FROM `{table}`
                    WHERE {eligibility_sql}
                      AND `profitability_updated_at` IS NOT NULL
                      AND (
                          {legacy_collection_refresh}`profitability_updated_at` < %s
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
                for row in cursor.fetchall():
                    normalized_row = _json_safe_row(row)
                    if table == COLLECTION_TABLE:
                        normalized_row["profitability_source"] = "fixed_commission_15_pct"
                    rows.append(normalized_row)
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
    if str(snapshot.get("source_type") or "").strip().lower() in {
        "zying", "ai_original",
    } or str(snapshot.get("profitability_source") or "").strip().lower() in {PURCHASE_PROFITABILITY_SOURCE, MANUAL_PRODUCT_PROFITABILITY_SOURCE}:
        # Protect source-owned proceeds even when a row was already fetched by
        # an estimator worker before the queue exclusion took effect. ZYing
        # supplies the value directly; AI-original rows use the manually
        # confirmed 1688 cost and must not be reinterpreted as Mercado items.
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

    def write_guard(table: str) -> str:
        # Recheck the persisted owner inside the UPDATE. A worker may have
        # fetched its inputs before the purchase transaction saved the target,
        # and collection mirrors/fallback writes do not always carry an input
        # snapshot to compare. The row lock makes this check atomic with save.
        return (
            f" AND {_purchase_profitability_guard_sql()}"
            + (
                " AND `source_type` NOT IN ('zying', 'ai_original')"
                if table == PRODUCT_TABLE else ""
            )
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
                    f"WHERE `id` = %s AND `source_item_id` = %s"
                    f"{write_guard(PRODUCT_TABLE)}{cas_sql}",
                    tuple(values + [int(snapshot["id"]), item_id] + cas_values),
                )
                applied = int(cursor.rowcount or 0) > 0
            elif collection_item_id is not None:
                collection_cas_sql, collection_cas_values = cas(COLLECTION_TABLE)
                cursor.execute(
                    f"UPDATE `{COLLECTION_TABLE}` SET {assignments} "
                    f"WHERE `id` = %s AND `source_item_id` = %s"
                    f"{write_guard(COLLECTION_TABLE)}{collection_cas_sql}",
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
                    cursor.execute(f"""
                        UPDATE `{PRODUCT_TABLE}`
                        SET {assignments}
                        WHERE `source_item_id` = %s
                          AND `collection_item_id` = %s
                          AND `source_type` = 'collected'
                          {write_guard(PRODUCT_TABLE)}
                          {product_cas_sql}
                          AND NOT EXISTS (
                              SELECT 1 FROM `{COLLECTION_TABLE}` AS newer
                              WHERE newer.`source_item_id` = %s AND newer.`id` > %s
                                AND newer.`organization_key` = `{PRODUCT_TABLE}`.`organization_key`
                          )
                        """, tuple(
                            values
                            + [item_id, collection_item_id]
                            + product_cas_values
                            + [item_id, collection_item_id]
                        ))
            else:
                for table in (COLLECTION_TABLE, PRODUCT_TABLE):
                    cursor.execute(
                        f"UPDATE `{table}` SET {assignments} WHERE `source_item_id` = %s"
                        f"{write_guard(table)}",
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
                    "AND `source_type` NOT IN ('zying', 'ai_original')"
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
                          AND {_purchase_profitability_guard_sql()}
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
                          AND {_purchase_profitability_guard_sql()}
                          {source_filter}
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
            user = product_access.actor()
            if user and (len(selected_rows) != len(ids) or any(
                not product_access.can_operate(row, user)
                for row in selected_rows
            )):
                raise ValueError("只能认领自己采集的产品；无采集归属的产品不可认领")
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
                (
                    _preserve_purchase_assignment_sql(f"`{column}` = VALUES(`{column}`)")
                    if column in {"net_proceeds_usd", "profitability_source", "profitability_updated_at"}
                    else f"`{column}` = VALUES(`{column}`)"
                )
                for column in PROFITABILITY_COLUMNS
            )
            for row in selected_rows:
                snapshot = {
                    "source": _loads(row.get("source_json"), {}),
                    "description": _loads(row.get("description_json"), {}),
                    "page_snapshot": _loads(row.get("page_snapshot_json"), {}),
                    "plugin_snapshot": _loads(row.get("plugin_snapshot_json"), {}),
                }
                ai_copy = _loads(row.get("ai_copy_json"), {})
                if isinstance(ai_copy, Mapping) and ai_copy:
                    snapshot["ai_copy"] = dict(ai_copy)
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
                    _dumps(snapshot), _now(), row.get("ai_weight_price_json"),
                    row.get("infringement_check_json"),
                )
                _execute_access_insert(cursor, f"""
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
                        `source_snapshot_json`, `added_at`, `ai_weight_price_json`,
                        `infringement_check_json`
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
                        `ai_weight_price_json` = VALUES(`ai_weight_price_json`),
                        `infringement_check_json` = VALUES(`infringement_check_json`),
                        `source_snapshot_json` = {_preserve_purchase_snapshot_sql()},
                        `updated_at` = CURRENT_TIMESTAMP
                    """, values, inherited=row)
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
                    _copy_list_infringement_metadata(cursor, "collection_list", "product_list", complete_ids)
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


def check_catalog_access(item_type, item_ids, *, manage_visibility=False, operate=False, connection_factory=None):
    """Validate the whole selection before returning or mutating any product."""
    user = product_access.actor()
    if not user:
        return
    table = {'products': PRODUCT_TABLE, 'collection': COLLECTION_TABLE}.get(item_type)
    if not table:
        raise ValueError('产品类型无效')
    ids = _normalize_row_ids(item_ids, empty_message='请至少选择一个产品')
    connection = (connection_factory or _connect_list)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(f"SELECT `id`, `organization_key`, `owner_user_id`, `owner_name`, `visibility` "
                           f"FROM `{table}` WHERE `id` IN ({', '.join(['%s'] * len(ids))})", tuple(ids))
            rows = cursor.fetchall()
            if len(rows) != len(ids) or any(not product_access.can_view(row, user) for row in rows):
                raise PermissionError('当前账号无权访问所选产品')
            if operate and any(not product_access.can_operate(row, user) for row in rows):
                raise PermissionError('只能操作自己的产品')
            if manage_visibility and any(not (product_access.is_admin(user) or product_access.owns(row, user)) for row in rows):
                raise PermissionError('只能设置自己采集产品的可见性')
    finally:
        connection.close()


def set_catalog_visibility(item_type, item_ids, visibility, *, connection_factory=None):
    if visibility not in {'private', 'enterprise'}:
        raise ValueError('产品可见性必须为私有或企业共享')
    if not product_access.actor():
        raise PermissionError('请先登录')
    ids = _normalize_row_ids(item_ids, empty_message='请至少选择一个产品')
    table = {'products': PRODUCT_TABLE, 'collection': COLLECTION_TABLE}.get(item_type)
    if not table:
        raise ValueError('产品类型无效')
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(f"SELECT * FROM `{table}` WHERE `id` IN ({', '.join(['%s'] * len(ids))}) FOR UPDATE", tuple(ids))
            rows = cursor.fetchall()
            user = product_access.actor()
            if len(rows) != len(ids) or any(not product_access.can_view(row, user) or not (product_access.is_admin(user) or product_access.owns(row, user)) for row in rows):
                raise PermissionError('只能设置自己采集产品的可见性')
            # The collection and product copies are one logical product.
            for row in rows:
                for linked_table in (PRODUCT_TABLE, COLLECTION_TABLE):
                    cursor.execute(f"UPDATE `{linked_table}` SET `visibility` = %s WHERE `organization_key` = %s "
                                   "AND `source_item_id` = %s AND (`owner_user_id` <=> %s "
                                   "AND COALESCE(`owner_name`, '') = %s)",
                                   (visibility, row['organization_key'], row['source_item_id'], row.get('owner_user_id'), row.get('owner_name') or ''))
            # Always update the selected rows (a batch can contain several owners).
            cursor.execute(f"UPDATE `{table}` SET `visibility` = %s WHERE `id` IN ({', '.join(['%s'] * len(ids))})", (visibility, *ids))
        connection.commit()
        return {'count': len(ids), 'visibility': visibility}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _protect_catalog_function(function):
    """Enforce ID access for API, browser extension and worker store calls."""
    import inspect
    from functools import wraps
    signature = inspect.signature(function)
    keys = [key for key in signature.parameters if key in {
        'product_item_id', 'product_item_ids', 'collection_item_id', 'collection_item_ids',
    }]
    if not keys:
        return function
    @wraps(function)
    def protected(*args, **kwargs):
        if product_access.actor():
            bound = signature.bind(*args, **kwargs)
            for key in keys:
                value = bound.arguments.get(key)
                if value is None:
                    continue
                check_catalog_access('products' if key.startswith('product') else 'collection',
                                     value if key.endswith('ids') else [value],
                                     operate=not function.__name__.startswith('get_'),
                                     connection_factory=kwargs.get('connection_factory'))
        return function(*args, **kwargs)
    return protected


# Functions without explicit IDs (ingestion and maintenance) retain their own scope.
for _catalog_name, _catalog_function in list(globals().items()):
    if callable(_catalog_function) and getattr(_catalog_function, '__module__', '') == __name__ and not _catalog_name.startswith('_'):
        globals()[_catalog_name] = _protect_catalog_function(_catalog_function)


def update_catalog_attributes(kind, item_ids, schemas, operation, *, connection_factory=None):
    """Apply attribute-only changes to locked current rows, with full selection ACLs."""
    from copy import deepcopy
    from erp.catalog_attributes import KINDS, facts, apply
    if kind not in KINDS:
        raise ValueError('不支持的产品列表')
    ids = sorted(_normalize_row_ids(item_ids, empty_message='请至少选择一个产品'))
    if len(ids) > 100:
        raise ValueError('每批最多修改 100 件产品')
    if not isinstance(schemas, dict) or not isinstance(operation, dict):
        raise ValueError('属性参数无效')
    table = COLLECTION_TABLE if kind == 'collection' else PRODUCT_TABLE
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_collection_tables(cursor)
            cursor.execute(f"SELECT * FROM `{table}` WHERE `id` IN ({', '.join(['%s'] * len(ids))}) ORDER BY `id` FOR UPDATE", tuple(ids))
            rows = cursor.fetchall()
            user = product_access.actor()
            if not user or len(rows) != len(ids) or any(not product_access.can_operate(row, user) for row in rows):
                raise PermissionError('只能修改自己有操作权限的产品')
            if kind in {'supplier', 'original'} and any(row.get('source_type') != 'ai_original' for row in rows):
                raise ValueError('所选产品不属于当前列表')
            work = [(row, kind, True) for row in rows]
            # Collected and added-to-catalog rows represent the same logical product.
            # Apply the operation to each copy's own values, never copy a whole SKU matrix.
            if kind in {'collection', 'products'}:
                for row in rows:
                    if row.get('source_type') == 'ai_original':
                        continue
                    if kind == 'collection':
                        cursor.execute(f"SELECT * FROM `{PRODUCT_TABLE}` WHERE `collection_item_id` = %s AND `organization_key` = %s ORDER BY `id` FOR UPDATE",
                                       (row['id'], row.get('organization_key')))
                        linked_rows, linked_kind = cursor.fetchall(), 'products'
                    else:
                        cursor.execute(f"SELECT * FROM `{COLLECTION_TABLE}` WHERE `id` = %s AND `organization_key` = %s FOR UPDATE",
                                       (row.get('collection_item_id') or 0, row.get('organization_key')))
                        linked_rows, linked_kind = cursor.fetchall(), 'collection'
                    for linked in linked_rows:
                        if not product_access.can_operate(linked, user):
                            raise PermissionError('关联产品没有操作权限，未保存本批属性')
                        if facts(linked, linked_kind)['category_id'] == facts(row, kind)['category_id']:
                            work.append((linked, linked_kind, False))
            changed, results, processed = 0, [], set()
            for row, current_kind, requested in work:
                table = COLLECTION_TABLE if current_kind == 'collection' else PRODUCT_TABLE
                identity = (table, row['id'])
                if identity in processed:
                    continue
                processed.add(identity)
                category = facts(row, current_kind)['category_id']
                schema = schemas.get(category)
                source, count, report = apply(row, current_kind, schema, operation)
                if count:
                    if current_kind == 'collection':
                        cursor.execute(f"UPDATE `{table}` SET `source_json` = %s WHERE `id` = %s", (_dumps(source), row['id']))
                    else:
                        snapshot = _loads(row.get('source_snapshot_json'), {}) or {}
                        if current_kind == 'supplier':
                            snapshot['original_1688'] = source
                            snapshot.setdefault('source_1688_overrides', {})['attributes'] = source.get('attributes') or []
                        elif current_kind == 'original' or row.get('source_type') == 'ai_original':
                            prepared = source
                            prepared['manual_attributes'] = source.get('attributes') or []
                            prepared['manual_attribute_removed_ids'] = [key for key in prepared.get('manual_attribute_removed_ids') or []
                                                                       if key not in {a.get('id') for a in prepared['manual_attributes']}]
                            if 'variations' in prepared:
                                prepared['source_variations'] = deepcopy(prepared['variations'])
                            prepared['preparation'] = {**(prepared.get('preparation') or {}), 'required_ready': False}
                            prepared['review_status'] = 'unreviewed'
                            prepared['edit_revision'] = _ai_original_edit_revision(prepared) + 1
                            snapshot['ai_original'] = prepared
                            snapshot.setdefault('marketplace_listings', {})['mercadolibre'] = dict(prepared)
                            snapshot.setdefault('source', {}).update({key: prepared[key] for key in ('attributes', 'variations') if key in prepared})
                        else:
                            snapshot['source'] = source
                        _mark_ai_marketplace_drafts_unreviewed(snapshot)
                        cursor.execute(f"UPDATE `{table}` SET `source_snapshot_json` = %s, `review_status` = %s WHERE `id` = %s",
                                       (_dumps(snapshot), 'unreviewed', row['id']))
                    changed += int(requested)
                if requested:
                    results.append({**report, 'changed_fields': count})
        connection.commit()
        return {'requested': len(ids), 'changed': changed, 'results': results}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
