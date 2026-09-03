"""Persistence and grouped reporting for Mercado Libre infringement data."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping


INFRACTION_TABLE = "erp_mercadolibre_infractions"
INFRACTION_SYNC_STATE_TABLE = "erp_mercadolibre_infraction_sync_state"

SITE_NAMES = {
    "MLM": "墨西哥",
    "MLB": "巴西",
    "MLC": "智利",
    "MCO": "哥伦比亚",
    "MLA": "阿根廷",
    "MLU": "乌拉圭",
}

_schema_lock = threading.RLock()
_schema_ready = False


def _connect() -> Any:
    import pymysql
    from bit.bit_mysql import config

    return pymysql.connect(**config)


def _now() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _json_safe_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row or {})
    for key, value in tuple(result.items()):
        if isinstance(value, datetime):
            result[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(value, Decimal):
            result[key] = float(value)
        elif isinstance(value, bytes):
            result[key] = value.decode("utf-8", errors="replace")
    return result


def _ensure_infraction_column(cursor: Any, column: str, definition: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM `{INFRACTION_TABLE}` LIKE %s", (column,))
    if cursor.fetchone():
        return False
    cursor.execute(
        f"ALTER TABLE `{INFRACTION_TABLE}` ADD COLUMN `{column}` {definition}"
    )
    return True


def ensure_infraction_tables(cursor: Any) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{INFRACTION_TABLE}` (
                `id` BIGINT NOT NULL AUTO_INCREMENT,
                `token_id` BIGINT NOT NULL,
                `store_name` VARCHAR(128) NOT NULL,
                `seller_id` VARCHAR(64) NULL,
                `site_id` VARCHAR(16) NOT NULL,
                `source_type` VARCHAR(32) NOT NULL,
                `source_id` VARCHAR(128) NOT NULL,
                `item_id` VARCHAR(64) NOT NULL,
                `title` VARCHAR(512) NULL,
                `thumbnail_url` VARCHAR(1500) NULL,
                `permalink` VARCHAR(1500) NULL,
                `occurred_at` DATETIME NULL,
                `due_at` DATETIME NULL,
                `status` VARCHAR(64) NULL,
                `reason_code` VARCHAR(64) NULL,
                `reason` TEXT NULL,
                `remedy` TEXT NULL,
                `rights_holder` VARCHAR(255) NULL,
                `salesperson` VARCHAR(100) NULL,
                `group_name` VARCHAR(100) NULL,
                `raw_json` LONGTEXT NULL,
                `is_current` TINYINT(1) NOT NULL DEFAULT 1,
                `resolution_status` VARCHAR(32) NOT NULL DEFAULT 'current',
                `resolved_at` DATETIME NULL,
                `last_seen_at` DATETIME NULL,
                `seen_count` INT NOT NULL DEFAULT 1,
                `first_seen_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `last_checked_at` DATETIME NOT NULL,
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (`id`),
                UNIQUE KEY `uniq_erp_meli_infraction_source`
                    (`token_id`, `source_type`, `source_id`),
                KEY `idx_erp_meli_infraction_date` (`occurred_at`, `source_type`),
                KEY `idx_erp_meli_infraction_store` (`token_id`, `site_id`, `occurred_at`),
                KEY `idx_erp_meli_infraction_item` (`item_id`, `source_type`),
                KEY `idx_erp_meli_infraction_owner` (`group_name`, `salesperson`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        _ensure_infraction_column(
            cursor,
            "thumbnail_url",
            "VARCHAR(1500) NULL AFTER `title`",
        )
        _ensure_infraction_column(
            cursor,
            "permalink",
            "VARCHAR(1500) NULL AFTER `thumbnail_url`",
        )
        _ensure_infraction_column(
            cursor,
            "is_current",
            "TINYINT(1) NOT NULL DEFAULT 1 AFTER `raw_json`",
        )
        _ensure_infraction_column(
            cursor,
            "resolution_status",
            "VARCHAR(32) NOT NULL DEFAULT 'current' AFTER `is_current`",
        )
        _ensure_infraction_column(
            cursor,
            "resolved_at",
            "DATETIME NULL AFTER `resolution_status`",
        )
        _ensure_infraction_column(
            cursor,
            "last_seen_at",
            "DATETIME NULL AFTER `resolved_at`",
        )
        _ensure_infraction_column(
            cursor,
            "seen_count",
            "INT NOT NULL DEFAULT 1 AFTER `last_seen_at`",
        )
        cursor.execute(
            f"""
            UPDATE `{INFRACTION_TABLE}`
            SET `last_seen_at` = COALESCE(`last_seen_at`, `last_checked_at`),
                `seen_count` = GREATEST(COALESCE(`seen_count`, 1), 1)
            WHERE `last_seen_at` IS NULL OR `seen_count` IS NULL OR `seen_count` < 1
            """
        )
        cursor.execute(
            f"""
            UPDATE `{INFRACTION_TABLE}`
            SET `is_current` = 0,
                `resolution_status` = 'appeal_success',
                `resolved_at` = COALESCE(`resolved_at`, `last_checked_at`)
            WHERE `source_type` = 'rights_holder'
              AND `status` IN ('DOCUMENTATION_APPROVED', 'MEMBER_NOT_RESPOND', 'ROLLBACK')
            """
        )
        cursor.execute(
            f"""
            UPDATE `{INFRACTION_TABLE}`
            SET `is_current` = 0,
                `resolution_status` = 'appeal_failed',
                `resolved_at` = COALESCE(`resolved_at`, `last_checked_at`)
            WHERE `source_type` = 'rights_holder'
              AND `status` IN ('DOCUMENTATION_NOT_APPROVED', 'DOCUMENTATION_NOT_PRESENTED')
            """
        )
        cursor.execute(
            f"""
            UPDATE `{INFRACTION_TABLE}`
            SET `is_current` = 0,
                `resolution_status` = 'historical',
                `resolved_at` = COALESCE(`resolved_at`, `last_checked_at`)
            WHERE `reason_code` = 'LEGACY_PAGE'
            """
        )
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{INFRACTION_SYNC_STATE_TABLE}` (
                `token_id` BIGINT NOT NULL,
                `requested_at` DATETIME NULL,
                `last_started_at` DATETIME NULL,
                `last_completed_at` DATETIME NULL,
                `last_status` VARCHAR(32) NOT NULL DEFAULT 'pending',
                `last_error` TEXT NULL,
                `detection_scanned_count` INT NOT NULL DEFAULT 0,
                `detection_matched_count` INT NOT NULL DEFAULT 0,
                `rights_holder_count` INT NOT NULL DEFAULT 0,
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (`token_id`),
                KEY `idx_erp_meli_infraction_due` (`requested_at`, `last_completed_at`),
                KEY `idx_erp_meli_infraction_retry` (`last_status`, `last_started_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        _schema_ready = True


def _clean_token_ids(values: Iterable[Any]) -> list[int]:
    result: list[int] = []
    for value in values or ():
        try:
            token_id = int(value)
        except (TypeError, ValueError):
            continue
        if token_id > 0 and token_id not in result:
            result.append(token_id)
    return result


def request_infraction_sync(
    token_ids: Iterable[Any],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    ids = _clean_token_ids(token_ids)
    if not ids:
        return 0
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_infraction_tables(cursor)
            cursor.executemany(
                f"""
                INSERT INTO `{INFRACTION_SYNC_STATE_TABLE}`
                    (`token_id`, `requested_at`, `last_status`, `last_error`)
                VALUES (%s, %s, 'queued', NULL)
                ON DUPLICATE KEY UPDATE
                    `requested_at` = VALUES(`requested_at`),
                    `last_status` = 'queued', `last_error` = NULL
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


def mark_infraction_sync_started(
    token_id: int,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> None:
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_infraction_tables(cursor)
            cursor.execute(
                f"""
                INSERT INTO `{INFRACTION_SYNC_STATE_TABLE}`
                    (`token_id`, `last_started_at`, `last_status`, `last_error`)
                VALUES (%s, %s, 'running', NULL)
                ON DUPLICATE KEY UPDATE
                    `last_started_at` = VALUES(`last_started_at`),
                    `last_status` = 'running', `last_error` = NULL
                """,
                (int(token_id), _now()),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_infraction_sync_finished(
    token_id: int,
    status: str,
    *,
    detection_scanned_count: int = 0,
    detection_matched_count: int = 0,
    rights_holder_count: int = 0,
    error: str = "",
    connection_factory: Callable[[], Any] | None = None,
) -> None:
    status = str(status or "error").strip().lower()[:32]
    completed = status in {"success", "completed"}
    finished_at = _now()
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_infraction_tables(cursor)
            if completed:
                cursor.execute(
                    f"""
                    INSERT INTO `{INFRACTION_SYNC_STATE_TABLE}` (
                        `token_id`, `last_started_at`, `last_completed_at`, `last_status`,
                        `last_error`, `detection_scanned_count`,
                        `detection_matched_count`, `rights_holder_count`
                    ) VALUES (%s, %s, %s, %s, NULL, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `last_completed_at` = VALUES(`last_completed_at`),
                        `last_status` = VALUES(`last_status`), `last_error` = NULL,
                        `detection_scanned_count` = VALUES(`detection_scanned_count`),
                        `detection_matched_count` = VALUES(`detection_matched_count`),
                        `rights_holder_count` = VALUES(`rights_holder_count`),
                        `requested_at` = CASE
                            WHEN `requested_at` IS NULL OR `last_started_at` IS NULL
                              OR `requested_at` <= `last_started_at`
                            THEN NULL ELSE `requested_at` END
                    """,
                    (
                        int(token_id), finished_at, finished_at, status,
                        int(detection_scanned_count or 0),
                        int(detection_matched_count or 0),
                        int(rights_holder_count or 0),
                    ),
                )
            else:
                cursor.execute(
                    f"""
                    INSERT INTO `{INFRACTION_SYNC_STATE_TABLE}` (
                        `token_id`, `last_started_at`, `last_status`, `last_error`,
                        `detection_scanned_count`, `detection_matched_count`,
                        `rights_holder_count`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `last_status` = VALUES(`last_status`),
                        `last_error` = VALUES(`last_error`),
                        `detection_scanned_count` = VALUES(`detection_scanned_count`),
                        `detection_matched_count` = VALUES(`detection_matched_count`),
                        `rights_holder_count` = VALUES(`rights_holder_count`)
                    """,
                    (
                        int(token_id), finished_at, status or "error",
                        str(error or "")[:4000], int(detection_scanned_count or 0),
                        int(detection_matched_count or 0), int(rights_holder_count or 0),
                    ),
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_due_infraction_token_ids(
    *,
    interval_hours: int = 12,
    retry_minutes: int = 60,
    limit: int = 1000,
    connection_factory: Callable[[], Any] | None = None,
) -> list[int]:
    due_before = (
        datetime.now().replace(microsecond=0)
        - timedelta(hours=max(1, int(interval_hours or 12)))
    ).strftime("%Y-%m-%d %H:%M:%S")
    retry_before = (
        datetime.now().replace(microsecond=0)
        - timedelta(minutes=max(1, int(retry_minutes or 60)))
    ).strftime("%Y-%m-%d %H:%M:%S")
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_infraction_tables(cursor)
            cursor.execute(
                f"""
                SELECT tokens.`id`
                FROM `mercado_store_tokens` AS tokens
                LEFT JOIN `{INFRACTION_SYNC_STATE_TABLE}` AS state
                  ON state.`token_id` = tokens.`id`
                WHERE tokens.`enabled` = 1
                  AND (
                    state.`requested_at` IS NOT NULL
                    OR state.`last_completed_at` IS NULL
                    OR state.`last_completed_at` <= %s
                    OR state.`last_status` IN ('error', 'partial')
                  )
                  AND (state.`last_started_at` IS NULL OR state.`last_started_at` <= %s)
                ORDER BY CASE WHEN state.`requested_at` IS NOT NULL THEN 0 ELSE 1 END,
                         COALESCE(state.`last_completed_at`, state.`last_started_at`) ASC,
                         tokens.`id` ASC
                LIMIT %s
                """,
                (due_before, retry_before, max(1, min(int(limit or 1000), 1000))),
            )
            return [int(row["id"]) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_infraction_sync_context(
    token_id: int,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_infraction_tables(cursor)
            cursor.execute(
                f"""
                SELECT `last_started_at`, `last_completed_at`, `last_status`, `last_error`
                FROM `{INFRACTION_SYNC_STATE_TABLE}`
                WHERE `token_id` = %s LIMIT 1
                """,
                (int(token_id),),
            )
            return _json_safe_row(cursor.fetchone() or {})
    finally:
        connection.close()


def count_infraction_records(
    *, connection_factory: Callable[[], Any] | None = None
) -> int:
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_infraction_tables(cursor)
            cursor.execute(f"SELECT COUNT(*) AS `total` FROM `{INFRACTION_TABLE}`")
            return int((cursor.fetchone() or {}).get("total") or 0)
    finally:
        connection.close()


def _stable_source_id(source: Mapping[str, Any]) -> str:
    explicit = str(source.get("source_id") or "").strip()
    if explicit:
        return explicit[:128]
    identity = "|".join(
        str(source.get(key) or "")
        for key in ("source_type", "item_id", "occurred_at", "reason")
    )
    return hashlib.sha1(identity.encode("utf-8", errors="replace")).hexdigest()


def upsert_infraction_records(
    token: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
    *,
    checked_at: str | None = None,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    token_id = int(token.get("id") or 0)
    if token_id <= 0:
        raise ValueError("保存侵权记录时缺少有效店铺授权")
    store_name = str(token.get("display_name") or token.get("nickname") or token_id)[:128]
    checked_at = checked_at or _now()
    rows = []
    official_items: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    for source in records or ():
        source_type = str(source.get("source_type") or "detection").strip()[:32]
        source_id = _stable_source_id({**dict(source), "source_type": source_type})
        key = (source_type, source_id)
        if key in seen:
            continue
        seen.add(key)
        item_id = str(source.get("item_id") or "").strip().upper()[:64]
        if not item_id:
            continue
        reason_code = str(source.get("reason_code") or "").strip()[:64]
        if reason_code != "LEGACY_PAGE":
            official_items.add((source_type, item_id))
        rows.append(
            (
                token_id,
                store_name,
                str(source.get("seller_id") or "")[:64],
                str(source.get("site_id") or item_id[:3]).strip().upper()[:16],
                source_type,
                source_id,
                item_id,
                str(source.get("title") or "")[:512],
                str(source.get("thumbnail_url") or "")[:1500],
                str(source.get("permalink") or "")[:1500],
                source.get("occurred_at") or None,
                source.get("due_at") or None,
                str(source.get("status") or "")[:64],
                reason_code,
                str(source.get("reason") or ""),
                str(source.get("remedy") or ""),
                str(source.get("rights_holder") or "")[:255],
                str(source.get("salesperson") or "")[:100],
                str(source.get("group_name") or "")[:100],
                json.dumps(source.get("raw_json") or source, ensure_ascii=False, default=str),
                checked_at,
            )
        )
    if not rows:
        return 0

    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_infraction_tables(cursor)
            if official_items:
                cursor.executemany(
                    f"""
                    DELETE FROM `{INFRACTION_TABLE}`
                    WHERE `token_id` = %s AND `source_type` = %s AND `item_id` = %s
                      AND `reason_code` = 'LEGACY_PAGE'
                    """,
                    [
                        (token_id, source_type, item_id)
                        for source_type, item_id in sorted(official_items)
                    ],
                )
            cursor.executemany(
                f"""
                INSERT INTO `{INFRACTION_TABLE}` (
                    `token_id`, `store_name`, `seller_id`, `site_id`, `source_type`,
                    `source_id`, `item_id`, `title`, `thumbnail_url`, `permalink`,
                    `occurred_at`, `due_at`, `status`, `reason_code`, `reason`, `remedy`,
                    `rights_holder`, `salesperson`, `group_name`, `raw_json`, `last_checked_at`
                ) VALUES ({', '.join(['%s'] * 21)})
                ON DUPLICATE KEY UPDATE
                    `store_name` = VALUES(`store_name`), `seller_id` = VALUES(`seller_id`),
                    `site_id` = VALUES(`site_id`), `item_id` = VALUES(`item_id`),
                    `title` = COALESCE(NULLIF(VALUES(`title`), ''), `title`),
                    `thumbnail_url` = COALESCE(
                        NULLIF(VALUES(`thumbnail_url`), ''), `thumbnail_url`
                    ),
                    `permalink` = COALESCE(NULLIF(VALUES(`permalink`), ''), `permalink`),
                    `occurred_at` = VALUES(`occurred_at`),
                    `due_at` = VALUES(`due_at`), `status` = VALUES(`status`),
                    `reason_code` = VALUES(`reason_code`), `reason` = VALUES(`reason`),
                    `remedy` = VALUES(`remedy`), `rights_holder` = VALUES(`rights_holder`),
                    `salesperson` = VALUES(`salesperson`), `group_name` = VALUES(`group_name`),
                    `raw_json` = VALUES(`raw_json`),
                    `last_checked_at` = VALUES(`last_checked_at`)
                """,
                rows,
            )
        connection.commit()
        return len(rows)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _display_group(value: Any) -> str:
    return str(value or "").strip() or "未分组"


def _display_salesperson(value: Any) -> str:
    return str(value or "").strip() or "未分配"


def _build_group_tree(account_rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for raw in account_rows or ():
        row = _json_safe_row(raw)
        group_name = _display_group(row.get("group_name"))
        salesperson = _display_salesperson(row.get("salesperson"))
        detection_count = int(row.get("detection_count") or 0)
        rights_holder_count = int(row.get("rights_holder_count") or 0)
        total = detection_count + rights_holder_count
        group = groups.setdefault(
            group_name,
            {
                "group_name": group_name,
                "total": 0,
                "detection_count": 0,
                "rights_holder_count": 0,
                "store_count": 0,
                "site_count": 0,
                "salespeople": OrderedDict(),
            },
        )
        person = group["salespeople"].setdefault(
            salesperson,
            {
                "salesperson": salesperson,
                "total": 0,
                "detection_count": 0,
                "rights_holder_count": 0,
                "store_count": 0,
                "site_count": 0,
                "stores": OrderedDict(),
            },
        )

        token_id = row.get("token_id")
        store_key = str(token_id or "").strip() or (
            f"{row.get('store_name') or ''}\x1f{row.get('seller_id') or ''}"
        )
        store = person["stores"].get(store_key)
        if store is None:
            store = {
                **row,
                "group_name": group_name,
                "salesperson": salesperson,
                "detection_count": 0,
                "rights_holder_count": 0,
                "total": 0,
                "site_count": 0,
                "site_names": [],
                "sites": [],
                "latest_infraction_at": None,
                "last_synced_at": None,
            }
            person["stores"][store_key] = store
            group["store_count"] += 1
            person["store_count"] += 1

        site_id = str(row.get("site_id") or "").upper()
        site_name = SITE_NAMES.get(site_id, site_id)
        store["detection_count"] += detection_count
        store["rights_holder_count"] += rights_holder_count
        store["total"] += total
        store["site_count"] += 1
        store["site_names"].append(site_name)
        store["sites"].append(
            {
                "site_id": site_id,
                "site_name": site_name,
                "detection_count": detection_count,
                "rights_holder_count": rights_holder_count,
                "total": total,
                "latest_infraction_at": row.get("latest_infraction_at"),
            }
        )
        for timestamp_key in ("latest_infraction_at", "last_synced_at"):
            candidate = row.get(timestamp_key)
            if candidate and (
                not store.get(timestamp_key)
                or str(candidate) > str(store.get(timestamp_key))
            ):
                store[timestamp_key] = candidate
        if row.get("last_status") in {"error", "partial"}:
            store["last_status"] = row.get("last_status")
            store["last_error"] = row.get("last_error")

        for target in (group, person):
            target["total"] += total
            target["detection_count"] += detection_count
            target["rights_holder_count"] += rights_holder_count
            target["site_count"] += 1

    result = []
    for group in groups.values():
        people = []
        for person in group["salespeople"].values():
            stores = list(person["stores"].values())
            for store in stores:
                store["sites"].sort(key=lambda item: str(item.get("site_id") or ""))
            stores.sort(key=lambda item: str(item.get("store_name") or ""))
            stores.sort(
                key=lambda item: (
                    int(item.get("total") or 0),
                    str(item.get("latest_infraction_at") or ""),
                ),
                reverse=True,
            )
            person["stores"] = stores
            people.append(person)
        people.sort(key=lambda item: (-item["total"], item["salesperson"]))
        group["salespeople"] = people
        result.append(group)
    result.sort(key=lambda item: (-item["total"], item["group_name"]))
    return result


def list_infraction_dashboard(
    *,
    days: int = 30,
    group_name: str = "",
    salesperson: str = "",
    source_type: str = "",
    search: str = "",
    detail_token_id: int | str = 0,
    page: int = 1,
    page_size: int = 100,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    days = max(1, min(int(days or 30), 3650))
    page = max(1, int(page or 1))
    page_size = max(20, min(int(page_size or 100), 500))
    cutoff = (
        datetime.now().replace(microsecond=0) - timedelta(days=days)
    ).strftime("%Y-%m-%d %H:%M:%S")
    group_name = str(group_name or "").strip()
    salesperson = str(salesperson or "").strip()
    source_type = str(source_type or "").strip().lower()
    if source_type not in {"", "detection", "rights_holder"}:
        source_type = ""
    search = str(search or "").strip()
    detail_token_id = int(detail_token_id or 0)
    if detail_token_id < 0:
        detail_token_id = 0

    ownership_group = "COALESCE(NULLIF(settings.`group_name`, ''), NULLIF(items.`group_name`, ''), '未分组')"
    ownership_person = "COALESCE(NULLIF(settings.`salesperson`, ''), NULLIF(items.`salesperson`, ''), '未分配')"
    conditions = ["items.`occurred_at` >= %s"]
    values: list[Any] = [cutoff]
    if group_name:
        conditions.append(f"{ownership_group} = %s")
        values.append(group_name)
    if salesperson:
        conditions.append(f"{ownership_person} = %s")
        values.append(salesperson)
    if source_type:
        conditions.append("items.`source_type` = %s")
        values.append(source_type)
    if search:
        pattern = f"%{search}%"
        conditions.append(
            "(items.`item_id` LIKE %s OR items.`title` LIKE %s OR "
            "items.`reason` LIKE %s OR items.`store_name` LIKE %s OR "
            "items.`rights_holder` LIKE %s)"
        )
        values.extend([pattern] * 5)
    if detail_token_id:
        conditions.append("items.`token_id` = %s")
        values.append(detail_token_id)
    where_sql = " WHERE " + " AND ".join(conditions)
    settings_join = """
        LEFT JOIN `mercado_store_site_settings` AS settings
          ON settings.`token_id` = items.`token_id`
         AND settings.`site_id` = items.`site_id`
        LEFT JOIN `erp_mercadolibre_store_links` AS links
          ON links.`token_id` = items.`token_id`
         AND links.`item_id` = items.`item_id`
    """

    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_infraction_tables(cursor)
            cursor.execute(
                f"SELECT COUNT(*) AS `total` FROM `{INFRACTION_TABLE}` AS items "
                f"{settings_join}{where_sql}",
                tuple(values),
            )
            total = int((cursor.fetchone() or {}).get("total") or 0)
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            cursor.execute(
                f"""
                SELECT items.`id`, items.`token_id`, items.`store_name`, items.`seller_id`,
                       items.`site_id`, items.`source_type`, items.`source_id`, items.`item_id`,
                       items.`title`,
                       COALESCE(
                           NULLIF(items.`thumbnail_url`, ''),
                           NULLIF(links.`thumbnail_url`, ''),
                           NULLIF(JSON_UNQUOTE(JSON_EXTRACT(
                               items.`raw_json`, '$.detail.item_info.pictures[0].secure_url'
                           )), ''),
                           NULLIF(JSON_UNQUOTE(JSON_EXTRACT(
                               items.`raw_json`, '$.detail.item_info.pictures[0].url'
                           )), '')
                       ) AS `thumbnail_url`,
                       COALESCE(
                           NULLIF(items.`permalink`, ''),
                           NULLIF(links.`permalink`, '')
                       ) AS `permalink`,
                       items.`occurred_at`, items.`due_at`, items.`status`,
                       items.`reason_code`, items.`reason`, items.`remedy`,
                       items.`rights_holder`, {ownership_group} AS `group_name`,
                       {ownership_person} AS `salesperson`, items.`last_checked_at`
                FROM `{INFRACTION_TABLE}` AS items
                {settings_join}
                {where_sql}
                ORDER BY items.`occurred_at` DESC, items.`id` DESC
                LIMIT %s OFFSET %s
                """,
                tuple(values + [page_size, (page - 1) * page_size]),
            )
            rows = [_json_safe_row(row) for row in cursor.fetchall()]

            account_conditions = [
                "tokens.`enabled` = 1",
                "(settings.`appeal_enabled` = 1 "
                "OR settings.`visit_stats_enabled` = 1 "
                "OR counts.`token_id` IS NOT NULL)",
            ]
            account_values: list[Any] = []
            if group_name:
                account_conditions.append(
                    "COALESCE(NULLIF(settings.`group_name`, ''), '未分组') = %s"
                )
                account_values.append(group_name)
            if salesperson:
                account_conditions.append(
                    "COALESCE(NULLIF(settings.`salesperson`, ''), '未分配') = %s"
                )
                account_values.append(salesperson)
            account_where = " WHERE " + " AND ".join(account_conditions)
            source_clause = ""
            aggregate_values: list[Any] = [cutoff]
            if source_type:
                source_clause = " AND `source_type` = %s"
                aggregate_values.append(source_type)
            cursor.execute(
                f"""
                SELECT tokens.`id` AS `token_id`, tokens.`display_name` AS `store_name`,
                       settings.`site_id`,
                       COALESCE(NULLIF(settings.`group_name`, ''), '未分组') AS `group_name`,
                       COALESCE(NULLIF(settings.`salesperson`, ''), '未分配') AS `salesperson`,
                       COALESCE(counts.`detection_count`, 0) AS `detection_count`,
                       COALESCE(counts.`rights_holder_count`, 0) AS `rights_holder_count`,
                       counts.`latest_infraction_at`, state.`last_completed_at` AS `last_synced_at`,
                       state.`last_status`, state.`last_error`
                FROM `mercado_store_tokens` AS tokens
                INNER JOIN `mercado_store_site_settings` AS settings
                  ON settings.`token_id` = tokens.`id`
                LEFT JOIN (
                    SELECT `token_id`, `site_id`,
                           SUM(CASE WHEN `source_type` = 'detection' THEN 1 ELSE 0 END)
                               AS `detection_count`,
                           SUM(CASE WHEN `source_type` = 'rights_holder' THEN 1 ELSE 0 END)
                               AS `rights_holder_count`,
                           MAX(`occurred_at`) AS `latest_infraction_at`
                    FROM `{INFRACTION_TABLE}`
                    WHERE `occurred_at` >= %s {source_clause}
                    GROUP BY `token_id`, `site_id`
                ) AS counts ON counts.`token_id` = tokens.`id`
                           AND counts.`site_id` = settings.`site_id`
                LEFT JOIN `{INFRACTION_SYNC_STATE_TABLE}` AS state
                  ON state.`token_id` = tokens.`id`
                {account_where}
                ORDER BY `group_name`, `salesperson`, tokens.`display_name`, settings.`site_id`
                """,
                tuple(aggregate_values + account_values),
            )
            account_rows = [_json_safe_row(row) for row in cursor.fetchall()]

            cursor.execute(
                """
                SELECT DISTINCT `group_name` AS `value`
                FROM `mercado_store_site_settings`
                WHERE COALESCE(`group_name`, '') <> '' ORDER BY `value`
                """
            )
            groups = [str(row.get("value") or "") for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT DISTINCT `salesperson` AS `value`
                FROM `mercado_store_site_settings`
                WHERE COALESCE(`salesperson`, '') <> '' ORDER BY `value`
                """
            )
            salespeople = [str(row.get("value") or "") for row in cursor.fetchall()]
            cursor.execute(
                f"""
                SELECT MAX(`last_completed_at`) AS `last_synced_at`,
                       SUM(CASE WHEN `last_status` = 'running' THEN 1 ELSE 0 END) AS `running_stores`,
                       SUM(CASE WHEN `last_status` IN ('error', 'partial') THEN 1 ELSE 0 END)
                           AS `problem_stores`
                FROM `{INFRACTION_SYNC_STATE_TABLE}`
                """
            )
            sync_summary = _json_safe_row(cursor.fetchone() or {})

        tree = _build_group_tree(account_rows)
        if any(group.get("group_name") == "未分组" for group in tree):
            groups = ["未分组", *[value for value in groups if value != "未分组"]]
        if any(
            person.get("salesperson") == "未分配"
            for group in tree
            for person in group.get("salespeople") or []
        ):
            salespeople = [
                "未分配",
                *[value for value in salespeople if value != "未分配"],
            ]
        summary = {
            "total": sum(int(group.get("total") or 0) for group in tree),
            "detection_count": sum(int(group.get("detection_count") or 0) for group in tree),
            "rights_holder_count": sum(int(group.get("rights_holder_count") or 0) for group in tree),
            "store_count": sum(int(group.get("store_count") or 0) for group in tree),
            "site_count": sum(int(group.get("site_count") or 0) for group in tree),
            "group_count": len(tree),
            "salesperson_count": len(
                {
                    person["salesperson"]
                    for group in tree
                    for person in group.get("salespeople") or []
                }
            ),
            **sync_summary,
        }
        return {
            "summary": summary,
            "account_groups": tree,
            "rows": rows,
            "filters": {
                "days": days,
                "group_name": group_name,
                "salesperson": salesperson,
                "source_type": source_type,
                "search": search,
                "detail_token_id": detail_token_id,
                "groups": groups,
                "salespeople": salespeople,
            },
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }
    finally:
        connection.close()


def list_missing_infraction_media(
    *,
    days: int = 365,
    limit: int = 500,
    connection_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    days = max(1, min(int(days or 365), 3650))
    limit = max(1, min(int(limit or 500), 5000))
    cutoff = (
        datetime.now().replace(microsecond=0) - timedelta(days=days)
    ).strftime("%Y-%m-%d %H:%M:%S")
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_infraction_tables(cursor)
            cursor.execute(
                f"""
                SELECT items.`token_id`, items.`item_id`, MAX(items.`occurred_at`) AS `latest_at`
                FROM `{INFRACTION_TABLE}` AS items
                LEFT JOIN `erp_mercadolibre_store_links` AS links
                  ON links.`token_id` = items.`token_id`
                 AND links.`item_id` = items.`item_id`
                WHERE items.`occurred_at` >= %s
                  AND COALESCE(
                      NULLIF(items.`thumbnail_url`, ''),
                      NULLIF(links.`thumbnail_url`, ''),
                      NULLIF(JSON_UNQUOTE(JSON_EXTRACT(
                          items.`raw_json`, '$.detail.item_info.pictures[0].secure_url'
                      )), ''),
                      NULLIF(JSON_UNQUOTE(JSON_EXTRACT(
                          items.`raw_json`, '$.detail.item_info.pictures[0].url'
                      )), '')
                  ) IS NULL
                GROUP BY items.`token_id`, items.`item_id`
                ORDER BY `latest_at` DESC
                LIMIT %s
                """,
                (cutoff, limit),
            )
            return [_json_safe_row(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def update_infraction_media(
    records: Iterable[Mapping[str, Any]],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    rows = []
    for record in records or ():
        token_id = int(record.get("token_id") or 0)
        item_id = str(record.get("item_id") or "").strip().upper()[:64]
        thumbnail_url = str(record.get("thumbnail_url") or "").strip()[:1500]
        permalink = str(record.get("permalink") or "").strip()[:1500]
        title = str(record.get("title") or "").strip()[:512]
        if token_id <= 0 or not item_id or not thumbnail_url:
            continue
        rows.append((thumbnail_url, permalink, title, token_id, item_id))
    if not rows:
        return 0
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_infraction_tables(cursor)
            cursor.executemany(
                f"""
                UPDATE `{INFRACTION_TABLE}`
                SET `thumbnail_url` = %s,
                    `permalink` = COALESCE(NULLIF(%s, ''), `permalink`),
                    `title` = COALESCE(NULLIF(`title`, ''), NULLIF(%s, ''))
                WHERE `token_id` = %s AND `item_id` = %s
                """,
                rows,
            )
        connection.commit()
        return len(rows)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "INFRACTION_SYNC_STATE_TABLE",
    "INFRACTION_TABLE",
    "SITE_NAMES",
    "count_infraction_records",
    "ensure_infraction_tables",
    "get_infraction_sync_context",
    "list_due_infraction_token_ids",
    "list_infraction_dashboard",
    "list_missing_infraction_media",
    "mark_infraction_sync_finished",
    "mark_infraction_sync_started",
    "request_infraction_sync",
    "upsert_infraction_records",
    "update_infraction_media",
]
