"""Persistent switches for the 12-hour Mercado overview refresh jobs."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable


SETTINGS_TABLE = "erp_mercadolibre_overview_sync_settings"
OFFICIAL_INFRACTIONS_SCOPE = "official_infractions"
PROHIBITED_LISTINGS_SCOPE = "prohibited_listings"
SUPPORTED_SCOPES = (OFFICIAL_INFRACTIONS_SCOPE, PROHIBITED_LISTINGS_SCOPE)
DEFAULT_ENABLED = True
AUTO_SYNC_HOURS = 12

_schema_lock = threading.RLock()
_schema_ready = False


def _connect() -> Any:
    import pymysql
    from bit.bit_mysql import config

    return pymysql.connect(**config)


def _now() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_scope(scope: str) -> str:
    value = str(scope or "").strip().lower()
    if value not in SUPPORTED_SCOPES:
        raise ValueError("不支持的全量更新类型")
    return value


def ensure_overview_sync_settings_table(cursor: Any) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{SETTINGS_TABLE}` (
                `scope` VARCHAR(64) NOT NULL,
                `enabled` TINYINT(1) NOT NULL DEFAULT 1,
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (`scope`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        _schema_ready = True


def get_overview_sync_settings(
    scope: str,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_scope(scope)
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_overview_sync_settings_table(cursor)
            cursor.execute(
                f"SELECT `scope`, `enabled`, `updated_at` FROM `{SETTINGS_TABLE}` "
                "WHERE `scope` = %s",
                (normalized,),
            )
            row = cursor.fetchone() or {}
    finally:
        connection.close()
    updated_at = row.get("updated_at")
    if isinstance(updated_at, datetime):
        updated_at = updated_at.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "scope": normalized,
        "enabled": bool(row.get("enabled", DEFAULT_ENABLED)),
        "interval_hours": AUTO_SYNC_HOURS,
        "settings_updated_at": str(updated_at or ""),
    }


def set_overview_sync_enabled(
    scope: str,
    enabled: bool,
    *,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_scope(scope)
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须是布尔值")
    connection = (connection_factory or _connect)()
    try:
        with connection.cursor() as cursor:
            ensure_overview_sync_settings_table(cursor)
            cursor.execute(
                f"""
                INSERT INTO `{SETTINGS_TABLE}` (`scope`, `enabled`, `updated_at`)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    `enabled` = VALUES(`enabled`),
                    `updated_at` = VALUES(`updated_at`)
                """,
                (normalized, int(enabled), _now()),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return get_overview_sync_settings(
        normalized,
        connection_factory=connection_factory,
    )


__all__ = [
    "AUTO_SYNC_HOURS",
    "OFFICIAL_INFRACTIONS_SCOPE",
    "PROHIBITED_LISTINGS_SCOPE",
    "ensure_overview_sync_settings_table",
    "get_overview_sync_settings",
    "set_overview_sync_enabled",
]
