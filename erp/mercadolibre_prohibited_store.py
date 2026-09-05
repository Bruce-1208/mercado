"""Persistence for current Mercado Libre listings prohibited by policy."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping


PROHIBITED_TABLE = "erp_mercadolibre_prohibited_listings"
PROHIBITED_SYNC_STATE_TABLE = "erp_mercadolibre_prohibited_sync_state"

_schema_lock = threading.RLock()
_schema_ready = False


def _connect() -> Any:
    import pymysql
    from bit.bit_mysql import config

    return pymysql.connect(**config)


def _now() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _json_safe_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, value in tuple(result.items()):
        if isinstance(value, datetime):
            result[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(value, Decimal):
            result[key] = float(value)
        elif isinstance(value, bytes):
            result[key] = value.decode("utf-8", errors="replace")
    if "is_current" in result:
        result["is_current"] = bool(result.get("is_current"))
    return result


def ensure_prohibited_tables(cursor: Any) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{PROHIBITED_TABLE}` (
                `id` BIGINT NOT NULL AUTO_INCREMENT,
                `token_id` BIGINT NOT NULL,
                `store_name` VARCHAR(128) NOT NULL,
                `salesperson` VARCHAR(100) NULL,
                `group_name` VARCHAR(100) NULL,
                `seller_id` VARCHAR(64) NOT NULL,
                `site_id` VARCHAR(16) NOT NULL,
                `item_id` VARCHAR(64) NOT NULL,
                `global_item_id` VARCHAR(64) NULL,
                `family_id` VARCHAR(64) NULL,
                `title` VARCHAR(512) NULL,
                `permalink` VARCHAR(1500) NULL,
                `thumbnail_url` VARCHAR(1500) NULL,
                `status` VARCHAR(64) NULL,
                `sub_status` VARCHAR(255) NULL,
                `infraction_id` VARCHAR(64) NULL,
                `infraction_reason` VARCHAR(512) NOT NULL,
                `remedy` TEXT NULL,
                `infraction_date` DATETIME NULL,
                `sync_marker` VARCHAR(64) NOT NULL,
                `raw_json` LONGTEXT NULL,
                `is_current` TINYINT(1) NOT NULL DEFAULT 1,
                `last_checked_at` DATETIME NOT NULL,
                `first_seen_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (`id`),
                UNIQUE KEY `uniq_erp_meli_prohibited` (`token_id`, `item_id`),
                KEY `idx_erp_meli_prohibited_current` (`is_current`, `site_id`, `infraction_date`),
                KEY `idx_erp_meli_prohibited_store` (`token_id`, `is_current`, `site_id`),
                KEY `idx_erp_meli_prohibited_salesperson` (`salesperson`, `is_current`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{PROHIBITED_SYNC_STATE_TABLE}` (
                `token_id` BIGINT NOT NULL,
                `requested_at` DATETIME NULL,
                `last_started_at` DATETIME NULL,
                `last_completed_at` DATETIME NULL,
                `last_status` VARCHAR(32) NOT NULL DEFAULT 'pending',
                `last_error` TEXT NULL,
                `scanned_count` INT NOT NULL DEFAULT 0,
                `prohibited_count` INT NOT NULL DEFAULT 0,
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (`token_id`),
                KEY `idx_erp_meli_prohibited_sync_due` (`requested_at`, `last_completed_at`),
                KEY `idx_erp_meli_prohibited_sync_retry` (`last_started_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        _schema_ready = True


def _clean_token_ids(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    for value in values or ():
        try:
            token_id = int(value)
        except (TypeError, ValueError):
            continue
        if token_id > 0 and token_id not in result:
            result.append(token_id)
    return result


def request_prohibited_sync(
    token_ids: Iterable[int],
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> int:
    ids = _clean_token_ids(token_ids)
    if not ids:
        return 0
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_prohibited_tables(cursor)
            cursor.executemany(
                f"""
                INSERT INTO `{PROHIBITED_SYNC_STATE_TABLE}`
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


def mark_prohibited_sync_started(
    token_id: int,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> None:
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_prohibited_tables(cursor)
            cursor.execute(
                f"""
                INSERT INTO `{PROHIBITED_SYNC_STATE_TABLE}`
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


def mark_prohibited_sync_finished(
    token_id: int,
    status: str,
    *,
    scanned_count: int = 0,
    prohibited_count: int = 0,
    error: str = "",
    connection_factory: Callable[[], Any] | None = None,
) -> None:
    status = str(status or "error").strip().lower()[:32]
    successful = status in {"success", "partial", "completed"}
    finished_at = _now()
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_prohibited_tables(cursor)
            if successful:
                cursor.execute(
                    f"""
                    INSERT INTO `{PROHIBITED_SYNC_STATE_TABLE}`
                        (`token_id`, `last_started_at`, `last_completed_at`, `last_status`,
                         `last_error`, `scanned_count`, `prohibited_count`)
                    VALUES (%s, %s, %s, %s, NULL, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `last_completed_at` = VALUES(`last_completed_at`),
                        `last_status` = VALUES(`last_status`), `last_error` = NULL,
                        `scanned_count` = VALUES(`scanned_count`),
                        `prohibited_count` = VALUES(`prohibited_count`),
                        `requested_at` = CASE
                            WHEN `requested_at` IS NULL OR `last_started_at` IS NULL
                              OR `requested_at` <= `last_started_at`
                            THEN NULL ELSE `requested_at` END
                    """,
                    (
                        int(token_id), finished_at, finished_at, status,
                        int(scanned_count or 0), int(prohibited_count or 0),
                    ),
                )
            else:
                cursor.execute(
                    f"""
                    INSERT INTO `{PROHIBITED_SYNC_STATE_TABLE}`
                        (`token_id`, `last_started_at`, `last_status`, `last_error`)
                    VALUES (%s, %s, 'error', %s)
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


def list_due_prohibited_token_ids(
    *,
    interval_hours: int = 24,
    retry_minutes: int = 60,
    limit: int = 1000,
    connection_factory: Callable[[], Any] | None = None,
) -> list[int]:
    due_before = (
        datetime.now().replace(microsecond=0) - timedelta(hours=max(1, int(interval_hours)))
    ).strftime("%Y-%m-%d %H:%M:%S")
    retry_before = (
        datetime.now().replace(microsecond=0) - timedelta(minutes=max(1, int(retry_minutes)))
    ).strftime("%Y-%m-%d %H:%M:%S")
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_prohibited_tables(cursor)
            cursor.execute(
                f"""
                SELECT tokens.`id`
                FROM `mercado_store_tokens` AS tokens
                LEFT JOIN `{PROHIBITED_SYNC_STATE_TABLE}` AS state
                  ON state.`token_id` = tokens.`id`
                WHERE tokens.`enabled` = 1
                  AND (
                    state.`requested_at` IS NOT NULL
                    OR state.`last_completed_at` IS NULL
                    OR state.`last_completed_at` <= %s
                ) AND (
                    state.`last_started_at` IS NULL OR state.`last_started_at` <= %s
                )
                ORDER BY CASE WHEN state.`requested_at` IS NOT NULL THEN 0 ELSE 1 END,
                         COALESCE(state.`requested_at`, state.`last_completed_at`) ASC,
                         tokens.`id` ASC
                LIMIT %s
                """,
                (due_before, retry_before, max(1, min(int(limit or 1000), 1000))),
            )
            return [int(row["id"]) for row in cursor.fetchall()]
    finally:
        connection.close()


def replace_prohibited_snapshot(
    token: Mapping[str, Any],
    listings: Iterable[Mapping[str, Any]],
    *,
    sync_marker: str,
    checked_at: str | None = None,
    finalize: bool = True,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, int]:
    token_id = int(token.get("id") or 0)
    if token_id <= 0 or not sync_marker:
        raise ValueError("保存禁限售快照时缺少有效店铺或同步标记")
    checked_at = checked_at or _now()
    store_name = str(token.get("display_name") or token.get("nickname") or token_id)[:128]
    rows = []
    seen: set[str] = set()
    for source in listings or ():
        item_id = str(source.get("item_id") or source.get("id") or "").strip().upper()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        rows.append((
            token_id, store_name, str(source.get("salesperson") or "")[:100],
            str(source.get("group_name") or "")[:100], str(source.get("seller_id") or "")[:64],
            str(source.get("site_id") or item_id[:3]).upper()[:16], item_id,
            str(source.get("global_item_id") or source.get("user_product_id") or "")[:64],
            str(source.get("family_id") or "")[:64], str(source.get("title") or "")[:512],
            str(source.get("permalink") or "")[:1500], str(source.get("thumbnail_url") or "")[:1500],
            str(source.get("status") or "")[:64], str(source.get("sub_status") or "")[:255],
            str(source.get("infraction_id") or "")[:64],
            str(source.get("infraction_reason") or "The product is prohibited.")[:512],
            str(source.get("remedy") or ""), source.get("infraction_date") or None,
            sync_marker, json.dumps(source.get("raw_json") or source, ensure_ascii=False, default=str),
            checked_at,
        ))
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_prohibited_tables(cursor)
            if rows:
                cursor.executemany(
                    f"""
                    INSERT INTO `{PROHIBITED_TABLE}` (
                        `token_id`, `store_name`, `salesperson`, `group_name`, `seller_id`,
                        `site_id`, `item_id`, `global_item_id`, `family_id`, `title`,
                        `permalink`, `thumbnail_url`, `status`, `sub_status`, `infraction_id`,
                        `infraction_reason`, `remedy`, `infraction_date`, `sync_marker`,
                        `raw_json`, `last_checked_at`
                    ) VALUES ({', '.join(['%s'] * 21)})
                    ON DUPLICATE KEY UPDATE
                        `store_name` = VALUES(`store_name`), `salesperson` = VALUES(`salesperson`),
                        `group_name` = VALUES(`group_name`), `seller_id` = VALUES(`seller_id`),
                        `site_id` = VALUES(`site_id`), `global_item_id` = VALUES(`global_item_id`),
                        `family_id` = VALUES(`family_id`), `title` = VALUES(`title`),
                        `permalink` = VALUES(`permalink`), `thumbnail_url` = VALUES(`thumbnail_url`),
                        `status` = VALUES(`status`), `sub_status` = VALUES(`sub_status`),
                        `infraction_id` = VALUES(`infraction_id`),
                        `infraction_reason` = VALUES(`infraction_reason`), `remedy` = VALUES(`remedy`),
                        `infraction_date` = VALUES(`infraction_date`),
                        `sync_marker` = VALUES(`sync_marker`), `raw_json` = VALUES(`raw_json`),
                        `is_current` = 1, `last_checked_at` = VALUES(`last_checked_at`)
                    """,
                    rows,
                )
            if finalize:
                cursor.execute(
                    f"""
                    UPDATE `{PROHIBITED_TABLE}` SET `is_current` = 0, `last_checked_at` = %s
                    WHERE `token_id` = %s AND (`sync_marker` IS NULL OR `sync_marker` <> %s)
                    """,
                    (checked_at, token_id, sync_marker),
                )
        connection.commit()
        return {"total": len(rows), "current": len(rows)}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_prohibited_sync_context(
    token_id: int,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Return the last successful clock and current rows for an incremental refresh."""

    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_prohibited_tables(cursor)
            cursor.execute(
                f"SELECT `last_completed_at` FROM `{PROHIBITED_SYNC_STATE_TABLE}` "
                "WHERE `token_id` = %s LIMIT 1",
                (int(token_id),),
            )
            state = dict(cursor.fetchone() or {})
            cursor.execute(
                f"""
                SELECT `item_id`, `seller_id`, `site_id`, `infraction_id`,
                       `infraction_reason`, `remedy`, `infraction_date`,
                       `title`, `thumbnail_url`, `permalink`, `status`, `sub_status`,
                       `global_item_id`, `family_id`, `last_checked_at`
                FROM `{PROHIBITED_TABLE}`
                WHERE `token_id` = %s AND `is_current` = 1
                ORDER BY `id`
                """,
                (int(token_id),),
            )
            rows = [_json_safe_row(row) for row in cursor.fetchall()]
        return {"last_completed_at": _json_safe_row(state).get("last_completed_at"), "rows": rows}
    finally:
        connection.close()


def list_prohibited_listings(
    *,
    search: str = "",
    token_id: int | None = None,
    site_id: str = "",
    salesperson: str = "",
    risk_type: str = "",
    page: int = 1,
    page_size: int = 100,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = max(20, min(int(page_size or 100), 500))
    risk_type = str(risk_type or "").strip().lower()
    if risk_type not in {"", "prohibited", "rights_holder_reply"}:
        risk_type = ""
    conditions = ["1 = 1"]
    values: list[Any] = []
    if token_id not in (None, ""):
        conditions.append("items.`token_id` = %s")
        values.append(int(token_id))
    site_id = str(site_id or "").strip().upper()[:16]
    if site_id:
        conditions.append("items.`site_id` = %s")
        values.append(site_id)
    salesperson = str(salesperson or "").strip()[:100]
    if salesperson:
        conditions.append("COALESCE(settings.`salesperson`, items.`salesperson`, '') = %s")
        values.append(salesperson)
    if risk_type:
        conditions.append("items.`risk_type` = %s")
        values.append(risk_type)
    search = str(search or "").strip()
    if search:
        pattern = f"%{search}%"
        conditions.append(
            "(items.`item_id` LIKE %s OR items.`global_item_id` LIKE %s "
            "OR items.`title` LIKE %s OR items.`store_name` LIKE %s "
            "OR items.`rights_holder` LIKE %s)"
        )
        values.extend([pattern] * 5)
    where_sql = " WHERE " + " AND ".join(conditions)
    risk_union_sql = f"""
        SELECT CONCAT('P-', prohibited.`id`) AS `record_id`,
               'prohibited' AS `risk_type`, prohibited.`token_id`,
               prohibited.`store_name`, prohibited.`salesperson`, prohibited.`group_name`,
               prohibited.`seller_id`, prohibited.`site_id`, prohibited.`item_id`,
               prohibited.`global_item_id`, prohibited.`family_id`, prohibited.`title`,
               prohibited.`permalink`, prohibited.`thumbnail_url`, prohibited.`status`,
               prohibited.`sub_status`, prohibited.`infraction_id`,
               prohibited.`infraction_reason`, prohibited.`remedy`,
               '' AS `rights_holder`, NULL AS `due_at`,
               prohibited.`infraction_date`, prohibited.`last_checked_at`
        FROM `{PROHIBITED_TABLE}` AS prohibited
        WHERE prohibited.`is_current` = 1
        UNION ALL
        SELECT CONCAT('R-', reports.`id`) AS `record_id`,
               'rights_holder_reply' AS `risk_type`, reports.`token_id`,
               reports.`store_name`, reports.`salesperson`, reports.`group_name`,
               reports.`seller_id`, reports.`site_id`, reports.`item_id`,
               '' AS `global_item_id`, '' AS `family_id`, reports.`title`,
               COALESCE(NULLIF(reports.`permalink`, ''), NULLIF(links.`permalink`, ''))
                   AS `permalink`,
               COALESCE(NULLIF(reports.`thumbnail_url`, ''), NULLIF(links.`thumbnail_url`, ''))
                   AS `thumbnail_url`,
               reports.`status`, 'waiting_for_rights_holder_reply' AS `sub_status`,
               reports.`source_id` AS `infraction_id`, reports.`reason` AS `infraction_reason`,
               '请在截止时间前回复权利人' AS `remedy`, reports.`rights_holder`,
               reports.`due_at`, reports.`occurred_at` AS `infraction_date`,
               reports.`last_checked_at`
        FROM `erp_mercadolibre_infractions` AS reports
        LEFT JOIN `erp_mercadolibre_store_links` AS links
          ON links.`token_id` = reports.`token_id`
         AND links.`item_id` = reports.`item_id`
        WHERE reports.`source_type` = 'rights_holder'
          AND reports.`status` = 'WAITING_DOCUMENTATION'
    """
    settings_join = """
        LEFT JOIN `mercado_store_site_settings` AS settings
          ON settings.`token_id` = items.`token_id` AND settings.`site_id` = items.`site_id`
    """
    risks_from_sql = f" FROM ({risk_union_sql}) AS items {settings_join}"
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_prohibited_tables(cursor)
            from erp.mercadolibre_infraction_store import ensure_infraction_tables

            ensure_infraction_tables(cursor)
            cursor.execute(
                f"SELECT COUNT(*) AS `total`{risks_from_sql}{where_sql}",
                tuple(values),
            )
            total = int((cursor.fetchone() or {}).get("total") or 0)
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            cursor.execute(
                f"""
                SELECT items.`record_id` AS `id`, items.`risk_type`, items.`token_id`,
                       items.`store_name`,
                       COALESCE(settings.`salesperson`, items.`salesperson`, '') AS `salesperson`,
                       COALESCE(settings.`group_name`, items.`group_name`, '') AS `group_name`,
                       items.`seller_id`, items.`site_id`, items.`item_id`,
                       items.`global_item_id`, items.`family_id`, items.`title`,
                       items.`permalink`, items.`thumbnail_url`, items.`status`,
                       items.`sub_status`, items.`infraction_id`, items.`infraction_reason`,
                       items.`remedy`, items.`rights_holder`, items.`due_at`,
                       items.`infraction_date`, items.`last_checked_at`,
                       counts.`risk_count`, counts.`prohibited_count`,
                       counts.`rights_holder_reply_count`,
                       state.`last_completed_at` AS `store_last_synced_at`
                {risks_from_sql}
                INNER JOIN (
                    SELECT `token_id`, `site_id`, COUNT(*) AS `risk_count`,
                           SUM(CASE WHEN `risk_type` = 'prohibited' THEN 1 ELSE 0 END)
                               AS `prohibited_count`,
                           SUM(CASE WHEN `risk_type` = 'rights_holder_reply' THEN 1 ELSE 0 END)
                               AS `rights_holder_reply_count`
                    FROM ({risk_union_sql}) AS all_risks
                    GROUP BY `token_id`, `site_id`
                ) AS counts ON counts.`token_id` = items.`token_id`
                           AND counts.`site_id` = items.`site_id`
                LEFT JOIN `{PROHIBITED_SYNC_STATE_TABLE}` AS state
                  ON state.`token_id` = items.`token_id`
                {where_sql}
                ORDER BY CASE WHEN items.`risk_type` = 'rights_holder_reply' THEN 0 ELSE 1 END,
                         COALESCE(items.`due_at`, '9999-12-31 23:59:59') ASC,
                         counts.`risk_count` DESC,
                         items.`infraction_date` DESC, items.`last_checked_at` DESC,
                         items.`record_id` DESC
                LIMIT %s OFFSET %s
                """,
                tuple(values + [page_size, (page - 1) * page_size]),
            )
            rows = [_json_safe_row(row) for row in cursor.fetchall()]
            cursor.execute(
                f"""
                SELECT items.`token_id`, items.`store_name`, items.`site_id`,
                       COALESCE(settings.`salesperson`, items.`salesperson`, '') AS `salesperson`,
                       COALESCE(settings.`group_name`, items.`group_name`, '') AS `group_name`,
                       COUNT(*) AS `risk_count`,
                       SUM(CASE WHEN items.`risk_type` = 'prohibited' THEN 1 ELSE 0 END)
                           AS `prohibited_count`,
                       SUM(CASE WHEN items.`risk_type` = 'rights_holder_reply' THEN 1 ELSE 0 END)
                           AS `rights_holder_reply_count`,
                       MAX(items.`infraction_date`) AS `latest_infraction_at`,
                       MAX(items.`last_checked_at`) AS `last_synced_at`
                {risks_from_sql}
                LEFT JOIN `{PROHIBITED_SYNC_STATE_TABLE}` AS state
                  ON state.`token_id` = items.`token_id`
                {where_sql}
                GROUP BY items.`token_id`, items.`store_name`, items.`site_id`,
                         COALESCE(settings.`salesperson`, items.`salesperson`, ''),
                         COALESCE(settings.`group_name`, items.`group_name`, '')
                ORDER BY `risk_count` DESC, items.`store_name`, items.`site_id`
                """,
                tuple(values),
            )
            groups = [_json_safe_row(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT tokens.`id` AS `token_id`, tokens.`display_name` AS `store_name`,
                       state.`last_completed_at` AS `last_synced_at`, state.`last_status`,
                       state.`last_error`
                FROM `mercado_store_tokens` AS tokens
                LEFT JOIN `erp_mercadolibre_prohibited_sync_state` AS state
                  ON state.`token_id` = tokens.`id`
                ORDER BY tokens.`display_name`, tokens.`id`
                """
            )
            stores = [_json_safe_row(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT DISTINCT COALESCE(`salesperson`, '') AS `salesperson`
                FROM `mercado_store_site_settings`
                WHERE COALESCE(`salesperson`, '') <> '' ORDER BY `salesperson`
                """
            )
            salespersons = [_json_safe_row(row) for row in cursor.fetchall()]
            cursor.execute(
                f"""
                SELECT COUNT(*) AS `current_count`,
                       SUM(CASE WHEN items.`risk_type` = 'prohibited' THEN 1 ELSE 0 END)
                           AS `prohibited_count`,
                       SUM(CASE WHEN items.`risk_type` = 'rights_holder_reply' THEN 1 ELSE 0 END)
                           AS `rights_holder_reply_count`,
                       COUNT(DISTINCT items.`token_id`) AS `store_count`,
                       COUNT(DISTINCT items.`site_id`) AS `site_count`,
                       MAX(items.`last_checked_at`) AS `last_checked_at`,
                       MIN(CASE WHEN items.`risk_type` = 'rights_holder_reply'
                                THEN items.`due_at` ELSE NULL END) AS `next_due_at`
                FROM ({risk_union_sql}) AS items
                """
            )
            summary = _json_safe_row(cursor.fetchone() or {})
        return {
            "rows": rows, "groups": groups, "stores": stores,
            "salespersons": salespersons, "summary": summary,
            "total": total, "page": page, "page_size": page_size, "pages": pages,
        }
    finally:
        connection.close()


__all__ = [
    "ensure_prohibited_tables", "get_prohibited_sync_context", "list_due_prohibited_token_ids",
    "list_prohibited_listings", "mark_prohibited_sync_finished",
    "mark_prohibited_sync_started", "replace_prohibited_snapshot",
    "request_prohibited_sync",
]
