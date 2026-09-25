"""MySQL persistence for listings synchronized from authorized stores."""

from __future__ import annotations

import json
import re
import threading
import time
from copy import deepcopy
from erp.query_cache import QueryCache
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping


STORE_LINK_TABLE = "erp_mercadolibre_store_links"
STORE_LINK_SYNC_STATE_TABLE = "erp_mercadolibre_store_link_sync_state"
PRODUCT_TABLE = "erp_mercadolibre_products"
MANAGEMENT_CATEGORY_TABLE = "erp_mercadolibre_management_categories"
SYNCED_ORDER_TABLE = "mercado_synced_orders"
STORE_LINK_SALES_PAGE_INDEX = "idx_erp_meli_store_link_sales_page"
STORE_LINK_SITE_PAGE_INDEX = "idx_erp_meli_store_link_site_page"
STORE_LINK_CATEGORY_PAGE_INDEX = "idx_erp_meli_store_link_category_page"
STORE_LINK_SEARCH_INDEX = "idx_erp_meli_store_link_search"
STORE_LINK_DEFAULT_PAGE_SIZE = 200
STORE_LINK_MAX_PAGE_SIZE = 1000
STORE_LINK_METADATA_CACHE_SECONDS = 60
STORE_LINK_RECENT_SALES_CACHE_SECONDS = 30
STORE_LINK_RECENT_SALES_CACHE_MAX_ENTRIES = 20000

# MySQL's default InnoDB FULLTEXT stopword list. These words are not indexed,
# but adding a Boolean prefix wildcard (for example ``+com*``) makes MySQL
# keep the stopword in the query as a required term. Store-link titles in
# Portuguese, English, and other markets commonly contain these words.
INNODB_DEFAULT_STOPWORDS = frozenset(
    """
    a about an are as at be by com de en for from how i in is it la of on or
    that the this to was what when where who will with und www
    """.split()
)

_schema_lock = threading.RLock()
_store_link_schema_ready = False
_sync_state_schema_ready = False
_filtered_count_cache = QueryCache(ttl=15, max_entries=512)
_metadata_cache_lock = threading.RLock()
_metadata_cache: dict[str, Any] = {"expires_at": 0.0, "data": None}
_scoped_metadata_cache: dict[tuple[int, ...], dict[str, Any]] = {}
_recent_sales_cache_lock = threading.RLock()
_recent_sales_cache: dict[tuple[int, str], tuple[float, int]] = {}


def _connect() -> Any:
    from bit.bit_mysql import config, pymysql

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
            KEY `idx_erp_meli_store_link_site_page` (`is_current`, `site_id`),
            KEY `idx_erp_meli_store_link_category_page`
                (`is_current`, `category_id`, `sold_quantity`, `last_synced_at`, `id`),
            FULLTEXT KEY `idx_erp_meli_store_link_search`
                (`title`, `item_id`, `seller_sku`, `store_name`)
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
            STORE_LINK_SALES_PAGE_INDEX: (
                "INDEX",
                "(`is_current`, `sold_quantity`, `last_synced_at`, `id`)",
            ),
            STORE_LINK_SITE_PAGE_INDEX: ("INDEX", "(`is_current`, `site_id`)"),
            STORE_LINK_CATEGORY_PAGE_INDEX: (
                "INDEX",
                "(`is_current`, `category_id`, `sold_quantity`, `last_synced_at`, `id`)",
            ),
            STORE_LINK_SEARCH_INDEX: (
                "FULLTEXT INDEX",
                "(`title`, `item_id`, `seller_sku`, `store_name`)",
            ),
        }
        for index_name, (index_type, columns_sql) in required_indexes.items():
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
                lock_name = f"mercado:{STORE_LINK_TABLE}:{index_name}"[:64]
                cursor.execute("SELECT GET_LOCK(%s, 300) AS `acquired`", (lock_name,))
                lock_row = cursor.fetchone() or {}
                acquired = (
                    lock_row.get("acquired")
                    if isinstance(lock_row, Mapping)
                    else lock_row[0] if lock_row else None
                )
                if acquired is not None and int(acquired) != 1:
                    raise RuntimeError(f"等待店铺链接索引 {index_name} 初始化超时")
                try:
                    # Another service process may have built the index while
                    # this connection waited for the cross-process lock.
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
                                f"ALTER TABLE `{STORE_LINK_TABLE}` ADD {index_type} "
                                f"`{index_name}` {columns_sql}"
                            )
                        except Exception as exc:
                            # Be compatible with an older process that started
                            # the same migration before advisory locking existed.
                            if not exc.args or exc.args[0] != 1061:
                                raise
                finally:
                    if acquired is not None:
                        cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
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
                WHERE tokens.`enabled` = 1
                  AND (
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
        elif re.search(r"(^|\W)oz($|\W)", unit):
            result *= Decimal("28.349523125")
    elif "mm" in unit:
        result /= 10
    elif re.search(r"(^|\W)m($|\W)", unit) and "cm" not in unit:
        result *= 100
    elif re.search(r"(^|\W)(?:in|inch|inches)($|\W)", unit):
        result *= Decimal("2.54")
    elif re.search(r"(^|\W)(?:ft|foot|feet)($|\W)", unit):
        result *= Decimal("30.48")
    return result


def _decorate_store_link_markers(rows: list[dict[str, Any]]) -> None:
    """Attach workflow markers without changing the large listing table schema."""
    if not rows:
        return
    identities = [
        (
            int(row.get("token_id") or 0),
            str(row.get("site_id") or "").strip().upper(),
            str(row.get("item_id") or "").strip().upper(),
        )
        for row in rows
        if int(row.get("token_id") or 0) > 0
        and str(row.get("site_id") or "").strip()
        and str(row.get("item_id") or "").strip()
    ]
    if not identities:
        return
    try:
        from erp.mercadolibre_store_link_marker_store import markers_for_rows

        action_markers = markers_for_rows(rows)
    except Exception:
        # A marker database must never make the primary listing table fail.
        action_markers = {}
    try:
        from erp.mercadolibre_promotion_store import PromotionStore

        promotion_markers = PromotionStore().applied_item_keys(identities)
    except Exception:
        # Activity synchronization is an optional companion store.
        promotion_markers = set()
    # The ad-analysis module already keeps the last successful remote snapshot.
    # Use it as a read-only fallback so ads created before the marker store was
    # introduced are still visible when that snapshot is available.
    snapshot_ad_markers: set[tuple[int, str, str]] = set()
    try:
        from bit.bit_ad_analysis import _load_snapshot

        snapshot = _load_snapshot()
        for ad_row in snapshot.get("links") or []:
            ad_identity = (
                int(ad_row.get("token_id") or 0),
                str(ad_row.get("site_id") or "").strip().upper(),
                str(ad_row.get("item_id") or "").strip().upper(),
            )
            campaign_status = str(ad_row.get("campaign_status") or "").strip().lower()
            group_status = str(ad_row.get("ad_group_status") or ad_row.get("status") or "").strip().lower()
            if ad_identity[0] > 0 and ad_identity[1] and ad_identity[2] and group_status == "active" and (
                not campaign_status or campaign_status == "active"
            ):
                snapshot_ad_markers.add(ad_identity)
    except Exception:
        snapshot_ad_markers = set()
    for row in rows:
        identity = (
            int(row.get("token_id") or 0),
            str(row.get("site_id") or "").strip().upper(),
            str(row.get("item_id") or "").strip().upper(),
        )
        action = action_markers.get(identity) or {}
        promotion_applied = identity in promotion_markers
        advertising_enabled = bool(action.get("advertising_enabled")) or identity in snapshot_ad_markers
        video_uploaded = bool(action.get("video_uploaded"))
        # Keep descriptive names and short aliases so API consumers can use the
        # response without knowing the UI's terminology.
        row["promotion_applied"] = promotion_applied
        row["activity_applied"] = promotion_applied
        row["advertising_enabled"] = advertising_enabled
        row["ad_enabled"] = advertising_enabled
        row["video_uploaded"] = video_uploaded
        row["has_promotion"] = promotion_applied
        row["has_activity"] = promotion_applied
        row["has_ad"] = advertising_enabled
        row["has_video"] = video_uploaded
        row["ad_campaign_id"] = action.get("ad_campaign_id") or ""
        row["video_clip_uuid"] = action.get("video_clip_uuid") or ""


def _active_ad_marker_keys(
    *,
    token_ids: Iterable[int] | None = None,
    site_id: str = "",
) -> set[tuple[int, str, str]]:
    """Load durable and snapshot-backed active Product Ads identities."""

    try:
        from erp.mercadolibre_store_link_marker_store import marked_item_keys

        keys = marked_item_keys(
            "advertising_enabled", token_ids=token_ids, site_id=site_id
        )
    except Exception:
        keys = set()
    allowed_ids = {
        int(value) for value in token_ids or () if int(value or 0) > 0
    }
    normalized_site = str(site_id or "").strip().upper()
    try:
        from bit.bit_ad_analysis import _load_snapshot

        snapshot = _load_snapshot()
        for ad_row in snapshot.get("links") or []:
            identity = (
                int(ad_row.get("token_id") or 0),
                str(ad_row.get("site_id") or "").strip().upper(),
                str(ad_row.get("item_id") or "").strip().upper(),
            )
            campaign_status = str(ad_row.get("campaign_status") or "").strip().lower()
            group_status = str(
                ad_row.get("ad_group_status") or ad_row.get("status") or ""
            ).strip().lower()
            if (
                identity[0] > 0
                and identity[1]
                and identity[2]
                and group_status == "active"
                and (not campaign_status or campaign_status == "active")
                and (not allowed_ids or identity[0] in allowed_ids)
                and (not normalized_site or identity[1] == normalized_site)
            ):
                keys.add(identity)
    except Exception:
        pass
    return keys


def _marker_filter_keys(
    marker: str,
    *,
    token_ids: Iterable[int] | None = None,
    site_id: str = "",
) -> set[tuple[int, str, str]]:
    if marker == "promotion_applied":
        try:
            from erp.mercadolibre_promotion_store import PromotionStore

            return PromotionStore().all_applied_item_keys(
                token_ids=token_ids, site_id=site_id
            )
        except Exception:
            return set()
    if marker == "advertising_enabled":
        return _active_ad_marker_keys(token_ids=token_ids, site_id=site_id)
    if marker == "video_uploaded":
        try:
            from erp.mercadolibre_store_link_marker_store import marked_item_keys

            return marked_item_keys("video_uploaded", token_ids=token_ids, site_id=site_id)
        except Exception:
            return set()
    raise ValueError(f"未知店铺链接标志：{marker}")


def _append_marker_filter(
    conditions: list[str],
    values: list[Any],
    *,
    marker: str,
    enabled: bool,
    token_ids: Iterable[int] | None = None,
    site_id: str = "",
) -> None:
    """Add a local-marker identity predicate to the MySQL listing query."""

    identities = sorted(_marker_filter_keys(marker, token_ids=token_ids, site_id=site_id))
    if not identities:
        conditions.append("1 = 0" if enabled else "1 = 1")
        return
    chunks = [identities[index:index + 400] for index in range(0, len(identities), 400)]
    groups = []
    for chunk in chunks:
        groups.append(" OR ".join(
            "(links.`token_id` = %s AND links.`site_id` = %s AND links.`item_id` = %s)"
            for _identity in chunk
        ))
        for identity in chunk:
            values.extend(identity)
    predicate = "(" + ") OR (".join(groups) + ")"
    conditions.append(predicate if enabled else f"NOT ({predicate})")


def _normalize_marker_filter(value: Any, label: str) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        raw = ""
    else:
        raw = str(value).strip().lower()
    if raw in {"", "all", "any"}:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{label}筛选值无效")


def _attribute_number(item: Mapping[str, Any], ids: set[str], *, weight: bool = False) -> Decimal | None:
    for attribute in item.get("attributes") or []:
        if not isinstance(attribute, Mapping) or str(attribute.get("id") or "").upper() not in ids:
            continue
        value = _value_struct_number(attribute.get("value_struct"), weight=weight)
        if value is None:
            value = _value_struct_number(attribute.get("value_name"), weight=weight)
        if value is None:
            for entry in attribute.get("values") or []:
                if not isinstance(entry, Mapping):
                    continue
                value = _value_struct_number(entry.get("struct"), weight=weight)
                if value is None:
                    value = _value_struct_number(entry.get("name"), weight=weight)
                if value is not None:
                    break
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


def _direct_package_measurements(item: Mapping[str, Any]) -> dict[str, Decimal | None]:
    """Read package values stored directly on one item or variation."""

    shipping = _shipping_dimensions(item)
    values = {
        "weight": _attribute_number(
            item, {"PACKAGE_WEIGHT", "WEIGHT", "NET_WEIGHT"}, weight=True
        ),
        "length": _attribute_number(item, {"PACKAGE_LENGTH", "LENGTH"}),
        "width": _attribute_number(item, {"PACKAGE_WIDTH", "WIDTH"}),
        "height": _attribute_number(item, {"PACKAGE_HEIGHT", "HEIGHT"}),
    }
    for field in values:
        if values[field] is None:
            values[field] = shipping[field]
    return values


def _package_measurement_score(values: Mapping[str, Decimal | None]) -> tuple[int, int, Decimal]:
    """Prefer complete packages, then the variant with the highest actual weight."""

    populated = sum(values.get(field) is not None for field in ("weight", "length", "width", "height"))
    actual_g = values.get("weight") or Decimal("0")
    return (int(populated == 4), populated, actual_g)


def _package_measurements(item: Mapping[str, Any]) -> dict[str, Decimal | None]:
    """Resolve package values from root attributes or nested variation attributes.

    Mercado's local marketplace response commonly leaves package data out of the
    root item and returns it under ``variations[].attributes`` instead.  A product
    list row represents the whole listing, so when variants differ we retain the
    complete package with the highest actual weight because this workflow does
    not use volumetric weight to select a shipping band.
    """

    root = _direct_package_measurements(item)
    if all(root.get(field) is not None for field in ("weight", "length", "width", "height")):
        return root

    candidates = [
        _direct_package_measurements(variation)
        for variation in item.get("variations") or []
        if isinstance(variation, Mapping)
    ]
    if not candidates:
        return root
    best = max(candidates, key=_package_measurement_score)
    if all(best.get(field) is not None for field in ("weight", "length", "width", "height")):
        return best
    return {
        field: root.get(field) if root.get(field) is not None else best.get(field)
        for field in ("weight", "length", "width", "height")
    }


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

    package = _package_measurements(item)
    weight = package["weight"]
    length = package["length"]
    width = package["width"]
    height = package["height"]
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


def invalidate_store_link_metadata_cache() -> None:
    """Discard low-frequency filters and totals after synchronized data changes."""

    _filtered_count_cache.clear()
    with _metadata_cache_lock:
        _metadata_cache.update({
            "expires_at": 0.0, "data": None,
            "expires_at_without_categories": 0.0,
            "data_without_categories": None,
        })
        _scoped_metadata_cache.clear()


def _recent_sales_for_pairs(
    cursor: Any,
    target_pairs: Iterable[tuple[int, str]],
    *,
    use_cache: bool,
) -> dict[tuple[int, str], int]:
    """Read 14-day sales for the visible links without re-parsing old pages.

    Orders store their item lines in ``raw_json``.  The old query expanded every
    line from every recent order for each page request, even when only a few
    visible links were needed.  Keep a short-lived per-link cache and filter the
    JSON document before expanding it so refreshes do not repeat that work.
    """

    normalized_pairs = sorted({
        (int(token_id), str(item_id))
        for token_id, item_id in target_pairs
        if int(token_id or 0) > 0 and str(item_id or "")
    })
    if not normalized_pairs:
        return {}

    now = time.monotonic()
    result: dict[tuple[int, str], int] = {}
    missing_pairs = normalized_pairs
    if use_cache:
        with _recent_sales_cache_lock:
            missing_pairs = []
            for pair in normalized_pairs:
                cached = _recent_sales_cache.get(pair)
                if cached and cached[0] > now:
                    result[pair] = int(cached[1])
                else:
                    missing_pairs.append(pair)

    if missing_pairs:
        token_ids = sorted({pair[0] for pair in missing_pairs})
        item_ids = sorted({pair[1] for pair in missing_pairs})
        token_sql = ", ".join(["%s"] * len(token_ids))
        pair_sql = ", ".join(["(%s, %s)"] * len(missing_pairs))
        item_ids_json = _dumps(item_ids)
        pair_values = [value for pair in missing_pairs for value in pair]
        cursor.execute(
            f"""
            SELECT recent_orders.`token_id`, order_items.`item_id`,
                   SUM(COALESCE(order_items.`quantity`, 0)) AS `sales_14d`
            FROM `{SYNCED_ORDER_TABLE}` AS recent_orders
            CROSS JOIN JSON_TABLE(
                IF(
                    JSON_VALID(recent_orders.`raw_json`),
                    recent_orders.`raw_json`,
                    JSON_OBJECT('order_items', JSON_ARRAY())
                ), '$.order_items[*]'
                COLUMNS (
                    `item_id` VARCHAR(64) PATH '$.item.id',
                    `quantity` INT PATH '$.quantity'
                )
            ) AS order_items
            WHERE recent_orders.`date_created` >= UTC_TIMESTAMP() - INTERVAL 14 DAY
              AND COALESCE(recent_orders.`status`, '') NOT IN ('cancelled', 'invalid')
              AND recent_orders.`token_id` IN ({token_sql})
              AND JSON_OVERLAPS(
                    JSON_EXTRACT(
                        IF(
                            JSON_VALID(recent_orders.`raw_json`),
                            recent_orders.`raw_json`,
                            JSON_OBJECT('order_items', JSON_ARRAY())
                        ),
                        '$.order_items[*].item.id'
                    ),
                    CAST(%s AS JSON)
                  )
              AND (recent_orders.`token_id`, order_items.`item_id`) IN ({pair_sql})
            GROUP BY recent_orders.`token_id`, order_items.`item_id`
            """,
            tuple(token_ids + [item_ids_json] + pair_values),
        )
        result.update({
            (int(row.get("token_id") or 0), str(row.get("item_id") or "")):
            int(row.get("sales_14d") or 0)
            for row in cursor.fetchall()
        })
        for pair in missing_pairs:
            result.setdefault(pair, 0)
        if use_cache:
            expires_at = now + STORE_LINK_RECENT_SALES_CACHE_SECONDS
            with _recent_sales_cache_lock:
                for pair in missing_pairs:
                    _recent_sales_cache[pair] = (expires_at, result[pair])
                if len(_recent_sales_cache) > STORE_LINK_RECENT_SALES_CACHE_MAX_ENTRIES:
                    expired = [
                        pair for pair, cached in _recent_sales_cache.items()
                        if cached[0] <= now
                    ]
                    for pair in expired:
                        _recent_sales_cache.pop(pair, None)
                    overflow = len(_recent_sales_cache) - STORE_LINK_RECENT_SALES_CACHE_MAX_ENTRIES
                    for pair in list(_recent_sales_cache)[:max(0, overflow)]:
                        _recent_sales_cache.pop(pair, None)
    return result


def _query_store_link_metadata(
    cursor: Any, *, token_ids: Iterable[int] | None = None,
    include_categories: bool = True,
) -> dict[str, Any]:
    scoped_token_ids = None
    if token_ids is not None:
        scoped_token_ids = sorted({
            int(value) for value in token_ids or () if int(value or 0) > 0
        })
    token_where = ""
    token_values: tuple[Any, ...] = ()
    if scoped_token_ids is not None:
        if scoped_token_ids:
            token_where = " WHERE `token_id` IN (" + ", ".join(
                ["%s"] * len(scoped_token_ids)
            ) + ")"
            token_values = tuple(scoped_token_ids)
        else:
            token_where = " WHERE 1 = 0"

    cursor.execute(
        f"""
        SELECT `token_id`, `site_id`, COALESCE(`group_name`, '') AS `group_name`
        FROM `mercado_store_site_settings`
        {token_where}
        """,
        token_values,
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

    token_store_where = token_where.replace("`token_id`", "tokens.`id`")
    cursor.execute(
        f"""
        SELECT tokens.`id` AS `token_id`,
               tokens.`display_name` AS `store_name`,
               NULL AS `link_count`, NULL AS `last_synced_at`
        FROM `mercado_store_tokens` AS tokens
        {token_store_where}
        ORDER BY tokens.`display_name`, tokens.`id`
        """,
        token_values,
    )
    stores = [_json_safe_row(row) for row in cursor.fetchall()]
    token_group_names: dict[int, set[str]] = {}
    for setting in site_settings:
        token_value = int(setting.get("token_id") or 0)
        group_value = str(setting.get("group_name") or "").strip() or "__ungrouped__"
        token_group_names.setdefault(token_value, set()).add(group_value)
    store_groups = {
        str(store.get("token_id") or ""): sorted(
            token_group_names.get(int(store.get("token_id") or 0), {"__ungrouped__"}),
            key=str.casefold,
        )
        for store in stores
    }
    link_token_clause = token_where.replace(" WHERE", " AND").replace(
        "`token_id`", "links.`token_id`"
    )
    cursor.execute(
        f"""
        SELECT links.`site_id`, COUNT(*) AS `link_count`
        FROM `{STORE_LINK_TABLE}` AS links
        WHERE links.`is_current` = 1
          AND links.`site_id` IS NOT NULL AND links.`site_id` <> ''
          {link_token_clause}
        GROUP BY links.`site_id` ORDER BY links.`site_id`
        """,
        token_values,
    )
    sites = [_json_safe_row(row) for row in cursor.fetchall()]
    if include_categories:
        cursor.execute(
            f"""
        SELECT category_counts.`category_id`,
               COALESCE(NULLIF(product.`category_name`, ''), '') AS `category_name`,
               category_counts.`link_count`
        FROM (
            SELECT links.`category_id`, MIN(links.`id`) AS `sample_link_id`,
                   COUNT(*) AS `link_count`
            FROM `{STORE_LINK_TABLE}` AS links
            FORCE INDEX (`{STORE_LINK_CATEGORY_PAGE_INDEX}`)
            WHERE links.`is_current` = 1
              AND links.`category_id` IS NOT NULL AND links.`category_id` <> ''
              {link_token_clause}
            GROUP BY links.`category_id`
        ) AS category_counts
        LEFT JOIN `{STORE_LINK_TABLE}` AS sample_link
          ON sample_link.`id` = category_counts.`sample_link_id`
        LEFT JOIN `{PRODUCT_TABLE}` AS product
          ON product.`source_item_id` = sample_link.`item_id`
        ORDER BY `category_name`, category_counts.`category_id`
        """,
            token_values,
        )
        mercado_categories = [_json_safe_row(row) for row in cursor.fetchall()]
    else:
        mercado_categories = []
    summary_where = token_where.replace("`token_id`", f"`{STORE_LINK_TABLE}`.`token_id`")
    cursor.execute(
        f"SELECT COUNT(*) AS `all_count` FROM `{STORE_LINK_TABLE}`{summary_where}",
        token_values,
    )
    summary = dict(cursor.fetchone() or {})
    current_token_clause = token_where.replace(" WHERE", " AND")
    cursor.execute(
        f"SELECT COUNT(*) AS `current_count` FROM `{STORE_LINK_TABLE}` "
        f"WHERE `is_current` = 1{current_token_clause}",
        token_values,
    )
    summary.update(cursor.fetchone() or {})
    cursor.execute(
        f"SELECT COUNT(DISTINCT `token_id`) AS `store_count` "
        f"FROM `{STORE_LINK_TABLE}` WHERE `is_current` = 1{current_token_clause}",
        token_values,
    )
    summary.update(cursor.fetchone() or {})
    cursor.execute(
        f"SELECT MAX(`last_synced_at`) AS `last_synced_at` "
        f"FROM `{STORE_LINK_TABLE}` WHERE `is_current` = 1{current_token_clause}",
        token_values,
    )
    summary.update(cursor.fetchone() or {})
    return {
        "group_map": group_map,
        "groups": groups,
        "stores": stores,
        "store_groups": store_groups,
        "sites": sites,
        "mercado_categories": mercado_categories,
        "summary": _json_safe_row(summary),
    }


def _store_link_metadata(
    cursor: Any,
    *,
    use_cache: bool,
    token_ids: Iterable[int] | None = None,
    include_categories: bool = True,
) -> dict[str, Any]:
    if token_ids is not None:
        scoped_key = (include_categories, *tuple(sorted({
            int(value) for value in token_ids or () if int(value or 0) > 0
        })))
        if not use_cache:
            return _query_store_link_metadata(cursor, token_ids=token_ids, include_categories=include_categories)
        now = time.monotonic()
        with _metadata_cache_lock:
            cached_entry = _scoped_metadata_cache.get(scoped_key)
            if (
                cached_entry
                and float(cached_entry.get("expires_at") or 0) > now
                and cached_entry.get("data") is not None
            ):
                return deepcopy(cached_entry["data"])
            metadata = _query_store_link_metadata(cursor, token_ids=token_ids, include_categories=include_categories)
            _scoped_metadata_cache[scoped_key] = {
                "expires_at": time.monotonic() + STORE_LINK_METADATA_CACHE_SECONDS,
                "data": deepcopy(metadata),
            }
            if len(_scoped_metadata_cache) > 256:
                oldest_key = min(
                    _scoped_metadata_cache,
                    key=lambda key: float(
                        _scoped_metadata_cache[key].get("expires_at") or 0
                    ),
                )
                _scoped_metadata_cache.pop(oldest_key, None)
            return metadata
    if not use_cache:
        return (_query_store_link_metadata(cursor) if include_categories
                else _query_store_link_metadata(cursor, include_categories=False))
    now = time.monotonic()
    with _metadata_cache_lock:
        cache_suffix = "" if include_categories else "_without_categories"
        cached = _metadata_cache.get("data" + cache_suffix)
        if cached is not None and float(_metadata_cache.get("expires_at" + cache_suffix) or 0) > now:
            return deepcopy(cached)
        metadata = (_query_store_link_metadata(cursor) if include_categories
                    else _query_store_link_metadata(cursor, include_categories=False))
        _metadata_cache.update({
            "expires_at" + cache_suffix: time.monotonic() + STORE_LINK_METADATA_CACHE_SECONDS,
            "data" + cache_suffix: deepcopy(metadata),
        })
        return metadata


def list_store_links(
    *,
    search: str = "",
    token_id: int | None = None,
    token_ids: Iterable[int] | None = None,
    filter_token_ids: Iterable[int] | None = None,
    site_id: str = "",
    group_name: str = "",
    status: str = "",
    promotion_applied: Any = None,
    advertising_enabled: Any = None,
    video_uploaded: Any = None,
    management_category_id: Any = None,
    mercado_category: str = "",
    include_categories: bool = True,
    sales_sort: str = "desc",
    sort_by: str = "sold_quantity",
    sort_order: str = "",
    current_only: bool = True,
    page: int = 1,
    page_size: int = STORE_LINK_DEFAULT_PAGE_SIZE,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = max(
        1,
        min(int(page_size or STORE_LINK_DEFAULT_PAGE_SIZE), STORE_LINK_MAX_PAGE_SIZE),
    )
    marker_filters = {
        "promotion_applied": _normalize_marker_filter(promotion_applied, "活动标志"),
        "advertising_enabled": _normalize_marker_filter(advertising_enabled, "广告标志"),
        "video_uploaded": _normalize_marker_filter(video_uploaded, "视频标志"),
    }
    conditions: list[str] = []
    values: list[Any] = []
    scoped_token_ids = None
    if token_ids is not None:
        scoped_token_ids = sorted({
            int(value) for value in token_ids or () if int(value or 0) > 0
        })
    if current_only:
        conditions.append("links.`is_current` = 1")
    selected_token_ids = sorted({
        int(value) for value in (filter_token_ids or ()) if int(value or 0) > 0
    })
    if selected_token_ids:
        placeholders = ", ".join(["%s"] * len(selected_token_ids))
        conditions.append(f"links.`token_id` IN ({placeholders})")
        values.extend(selected_token_ids)
    elif token_id not in (None, ""):
        conditions.append("links.`token_id` = %s")
        values.append(int(token_id))
    elif scoped_token_ids is not None:
        if not scoped_token_ids:
            conditions.append("1 = 0")
        else:
            placeholders = ", ".join(["%s"] * len(scoped_token_ids))
            conditions.append(f"links.`token_id` IN ({placeholders})")
            values.extend(scoped_token_ids)
    site_id = str(site_id or "").strip().upper()[:16]
    if site_id:
        conditions.append("links.`site_id` = %s")
        values.append(site_id)
    group_name = str(group_name or "").strip()[:100]
    status = str(status or "").strip()
    if status:
        conditions.append("links.`status` = %s")
        values.append(status)
    category_filter = str(management_category_id or "").strip().lower()
    if category_filter in {"uncategorized", "unclassified", "none"}:
        conditions.append(
            f"NOT EXISTS (SELECT 1 FROM `{PRODUCT_TABLE}` AS categorized_product "
            "WHERE categorized_product.`source_item_id` = links.`item_id` "
            "AND categorized_product.`management_category_id` IS NOT NULL)"
        )
    elif category_filter:
        try:
            normalized_category_id = int(category_filter)
        except (TypeError, ValueError) as exc:
            raise ValueError("产品分类编号无效") from exc
        if normalized_category_id <= 0:
            raise ValueError("产品分类编号无效")
        conditions.append(
            f"EXISTS (SELECT 1 FROM `{PRODUCT_TABLE}` AS categorized_product "
            "WHERE categorized_product.`source_item_id` = links.`item_id` "
            "AND categorized_product.`management_category_id` = %s)"
        )
        values.append(normalized_category_id)
    mercado_category = str(mercado_category or "").strip()[:255]
    if mercado_category:
        conditions.append("links.`category_id` = %s")
        values.append(mercado_category)
    search = str(search or "").strip()
    if search:
        # The listing table has more than one million rows. A leading-wildcard
        # LIKE on four text columns forces two full scans (COUNT + page query).
        # Boolean full-text prefix terms retain product-name/item/SKU/store
        # lookup while allowing MySQL to resolve matches from the FTS index.
        search_terms = re.findall(r"[^\W_]+", search, flags=re.UNICODE)
        boolean_query = " ".join(
            f"+{term[:84]}*"
            for term in dict.fromkeys(search_terms)
            if len(term) >= 3 and term.casefold() not in INNODB_DEFAULT_STOPWORDS
        )
        if not boolean_query:
            raise ValueError("搜索内容至少需要一个可索引的 3 字符关键词")
        conditions.append(
            "MATCH(links.`title`, links.`item_id`, links.`seller_sku`, "
            "links.`store_name`) AGAINST (%s IN BOOLEAN MODE)"
        )
        values.append(boolean_query)
    links_from_sql = f" FROM `{STORE_LINK_TABLE}` AS links"
    sort_aliases = {
        "price": "price",
        "weight_g": "weight_g",
        "weight": "weight_g",
        "sold_quantity": "sold_quantity",
        "total_sales": "sold_quantity",
        "sales": "sold_quantity",
        "sales_14d": "sales_14d",
        "recent_sales": "sales_14d",
        "available_quantity": "available_quantity",
        "inventory": "available_quantity",
        "stock": "available_quantity",
        "last_synced_at": "last_synced_at",
        "sync_time": "last_synced_at",
        "net_proceeds_usd": "net_proceeds_usd",
        "net_proceeds": "net_proceeds_usd",
        "net_income": "net_proceeds_usd",
    }
    normalized_sort_by = sort_aliases.get(
        str(sort_by or "sold_quantity").strip().lower(),
        "sold_quantity",
    )
    direction_source = sort_order if str(sort_order or "").strip() else sales_sort
    sort_direction = (
        "ASC" if str(direction_source or "").strip().lower() == "asc" else "DESC"
    )
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_store_link_table(cursor)
            metadata = _store_link_metadata(
                cursor,
                use_cache=connection_factory is None,
                token_ids=scoped_token_ids,
                include_categories=include_categories,
            )
            group_map = metadata["group_map"]
            groups = metadata["groups"]
            stores = metadata["stores"]
            store_groups = metadata["store_groups"]
            sites = metadata["sites"]
            mercado_categories = metadata["mercado_categories"]
            summary = metadata["summary"]

            cursor.execute(
                """
                SELECT 1 AS `table_exists`
                FROM `information_schema`.`TABLES`
                WHERE `TABLE_SCHEMA` = DATABASE() AND `TABLE_NAME` = %s
                LIMIT 1
                """,
                (SYNCED_ORDER_TABLE,),
            )
            table_row = cursor.fetchone() or {}
            synced_orders_available = bool(
                table_row.get("table_exists")
                if isinstance(table_row, Mapping)
                else table_row[0] if table_row else False
            )

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
            marker_scope_token_ids = (
                selected_token_ids
                if selected_token_ids
                else [int(token_id)]
                if token_id not in (None, "")
                else scoped_token_ids
            )
            for marker, enabled in marker_filters.items():
                if enabled is not None:
                    _append_marker_filter(
                        filtered_conditions,
                        filtered_values,
                        marker=marker,
                        enabled=enabled,
                        token_ids=marker_scope_token_ids,
                        site_id=site_id,
                    )
            where_sql = (
                " WHERE " + " AND ".join(filtered_conditions)
                if filtered_conditions else ""
            )
            scoped_only_filter = (
                scoped_token_ids is not None
                and not selected_token_ids
                and token_id in (None, "")
                and not site_id
                and not group_name
                and not status
                and not category_filter
                and not mercado_category
                and not search
                and all(value is None for value in marker_filters.values())
            )
            if scoped_only_filter:
                total = int(summary.get(
                    "current_count" if current_only else "all_count"
                ) or 0)
            elif not filtered_conditions:
                total = int(summary.get("all_count") or 0)
            elif filtered_conditions == ["links.`is_current` = 1"]:
                total = int(summary.get("current_count") or 0)
            else:
                count_sql = f"SELECT COUNT(*) AS `total`{links_from_sql}{where_sql}"
                def load_count():
                    cursor.execute(count_sql, tuple(filtered_values))
                    return int((cursor.fetchone() or {}).get("total") or 0)
                if connection_factory is not None:
                    total = load_count()
                else:
                    from bit.bit_mysql import config
                    database_key = tuple(str(config.get(k, "")) for k in
                                         ("host", "port", "database", "db", "user"))
                    # Include the actual SQL predicates AND permission scope.
                    # Cached totals never bypass the live row authorization query.
                    count_key = (database_key, count_sql, tuple(filtered_values),
                                 None if scoped_token_ids is None else tuple(scoped_token_ids))
                    total = _filtered_count_cache.get_or_load(count_key, load_count)
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            recent_sales_join_sql = ""
            page_recent_sales_sql = "0 AS `sales_14d`"
            if normalized_sort_by == "sales_14d" and synced_orders_available:
                recent_sales_join_sql = f"""
                LEFT JOIN (
                    SELECT recent_orders.`token_id`, order_items.`item_id`,
                           SUM(COALESCE(order_items.`quantity`, 0)) AS `sales_14d`
                    FROM `{SYNCED_ORDER_TABLE}` AS recent_orders
                    CROSS JOIN JSON_TABLE(
                        IF(
                            JSON_VALID(recent_orders.`raw_json`),
                            recent_orders.`raw_json`,
                            JSON_OBJECT('order_items', JSON_ARRAY())
                        ), '$.order_items[*]'
                        COLUMNS (
                            `item_id` VARCHAR(64) PATH '$.item.id',
                            `quantity` INT PATH '$.quantity'
                        )
                    ) AS order_items
                    WHERE recent_orders.`date_created` >= UTC_TIMESTAMP() - INTERVAL 14 DAY
                      AND COALESCE(recent_orders.`status`, '') NOT IN ('cancelled', 'invalid')
                      AND COALESCE(order_items.`item_id`, '') <> ''
                    GROUP BY recent_orders.`token_id`, order_items.`item_id`
                ) AS recent_sales
                  ON recent_sales.`token_id` = links.`token_id`
                 AND recent_sales.`item_id` = links.`item_id`
                """
                page_recent_sales_sql = "COALESCE(recent_sales.`sales_14d`, 0) AS `sales_14d`"

            if normalized_sort_by == "sales_14d":
                page_primary_order = f"`sales_14d` {sort_direction}"
                outer_primary_order = f"page_ids.`sales_14d` {sort_direction}"
            else:
                page_primary_order = f"links.`{normalized_sort_by}` {sort_direction}"
                outer_primary_order = f"full_links.`{normalized_sort_by}` {sort_direction}"
            page_order_parts = [page_primary_order]
            outer_order_parts = [outer_primary_order]
            if normalized_sort_by != "sold_quantity":
                page_order_parts.append("links.`sold_quantity` DESC")
                outer_order_parts.append("full_links.`sold_quantity` DESC")
            page_order_parts.extend(["links.`last_synced_at` DESC", "links.`id` DESC"])
            outer_order_parts.extend([
                "full_links.`last_synced_at` DESC",
                "full_links.`id` DESC",
            ])
            page_order_sql = ", ".join(page_order_parts)
            outer_order_sql = ", ".join(outer_order_parts)
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
                       full_links.`updated_at`, page_ids.`sales_14d`,
                       product.`category_name`, product.`management_category_id`,
                       management_category.`name` AS `management_category_name`
                FROM `{STORE_LINK_TABLE}` AS full_links
                LEFT JOIN `{PRODUCT_TABLE}` AS product
                  ON product.`source_item_id` = full_links.`item_id`
                LEFT JOIN `{MANAGEMENT_CATEGORY_TABLE}` AS management_category
                  ON management_category.`id` = product.`management_category_id`
                INNER JOIN (
                    SELECT links.`id`, {page_recent_sales_sql}
                    {links_from_sql}{recent_sales_join_sql}{where_sql}
                    ORDER BY {page_order_sql} LIMIT %s OFFSET %s
                ) AS page_ids ON page_ids.`id` = full_links.`id`
                ORDER BY {outer_order_sql}
                """,
                tuple(filtered_values + [page_size, (page - 1) * page_size]),
            )
            rows = [_json_safe_row(row) for row in cursor.fetchall()]
            if normalized_sort_by != "sales_14d" and synced_orders_available and rows:
                target_pairs = sorted({
                    (int(row.get("token_id") or 0), str(row.get("item_id") or ""))
                    for row in rows
                    if int(row.get("token_id") or 0) > 0 and str(row.get("item_id") or "")
                })
                recent_sales_by_link = _recent_sales_for_pairs(
                    cursor,
                    target_pairs,
                    use_cache=connection_factory is None,
                )
                for row in rows:
                    row["sales_14d"] = recent_sales_by_link.get(
                        (int(row.get("token_id") or 0), str(row.get("item_id") or "")),
                        0,
                    )
            for row in rows:
                row["sales_14d"] = int(row.get("sales_14d") or 0)
                row["group_name"] = group_map.get(
                    (int(row.get("token_id") or 0), str(row.get("site_id") or "").upper()),
                    "",
                )
            _decorate_store_link_markers(rows)
        return {
            "rows": rows,
            "stores": stores,
            "store_groups": store_groups,
            "sites": sites,
            "groups": groups,
            "mercado_categories": mercado_categories,
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
                SELECT `id`, `token_id`, `store_name`, `item_id`, `site_id`, `status`, `is_current`,
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
    numeric_allowed = (
        "price", "weight_g", "package_length_cm", "package_width_cm",
        "package_height_cm", "net_proceeds_usd",
    )
    clean_changes = {
        field: _decimal_change(field, changes[field])
        for field in numeric_allowed
        if field in changes and changes[field] not in (None, "")
    }
    if "status" in changes and changes["status"] not in (None, ""):
        status = str(changes["status"] or "").strip().lower()
        if status not in {"active", "paused"}:
            raise ValueError("店铺链接状态只能是 active 或 paused")
        clean_changes["status"] = status
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


def delete_store_links(
    link_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, int]:
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
        raise ValueError("每次最多删除 1000 条店铺链接")

    placeholders = ", ".join(["%s"] * len(ids))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_store_link_table(cursor)
            cursor.execute(
                f"DELETE FROM `{STORE_LINK_TABLE}` WHERE `id` IN ({placeholders})",
                tuple(ids),
            )
            deleted = int(cursor.rowcount or 0)
        connection.commit()
        if connection_factory is None:
            invalidate_store_link_metadata_cache()
        return {"requested": len(ids), "deleted": deleted}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "STORE_LINK_TABLE",
    "STORE_LINK_SYNC_STATE_TABLE",
    "bulk_update_store_links",
    "delete_store_links",
    "ensure_store_link_table",
    "ensure_store_link_sync_state_table",
    "finalize_store_snapshot",
    "get_store_links_by_ids",
    "invalidate_store_link_metadata_cache",
    "list_store_links",
    "list_due_store_link_token_ids",
    "listing_record",
    "mark_store_link_sync_finished",
    "mark_store_link_sync_started",
    "order_store_link_token_ids_for_full_sync",
    "request_store_link_sync",
    "replace_store_snapshot",
]
