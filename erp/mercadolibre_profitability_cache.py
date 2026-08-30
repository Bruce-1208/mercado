"""Persistent daily cache for Mercado Libre exchange rates, fees and shipping."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Mapping


EXCHANGE_RATE_TABLE = "erp_mercadolibre_exchange_rates"
DAILY_EXCHANGE_RATE_TABLE = "erp_mercadolibre_exchange_rate_daily"
COMMISSION_TABLE = "erp_mercadolibre_category_commissions"
SHIPPING_RATE_TABLE = "erp_mercadolibre_shipping_rates"
DEFAULT_CACHE_HOURS = 24

_schema_lock = threading.Lock()
_schema_ready = False


def _connect() -> Any:
    import pymysql
    from bit.bit_mysql import config

    return pymysql.connect(**config)


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _text_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}


def _exchange_rate_date(value: Mapping[str, Any], refreshed_at: str) -> str:
    """Use the official snapshot date, falling back to the local refresh date."""
    source_created_at = str(value.get("creation_date") or "").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", source_created_at)
    if match:
        return match.group(1)
    return str(refreshed_at)[:10]


def _cache_key(parts: Mapping[str, Any]) -> str:
    normalized = {
        str(key): (
            round(float(value), 4)
            if isinstance(value, float)
            else str(value or "").strip().upper()
        )
        for key, value in sorted(parts.items())
    }
    return hashlib.sha256(_dumps(normalized).encode("utf-8")).hexdigest()


def ensure_profitability_cache_tables(cursor: Any) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{EXCHANGE_RATE_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `from_currency_id` VARCHAR(16) NOT NULL,
            `to_currency_id` VARCHAR(16) NOT NULL,
            `rate` DECIMAL(24,12) NOT NULL,
            `source_created_at` VARCHAR(64) NULL,
            `source_valid_until` VARCHAR(64) NULL,
            `payload_json` LONGTEXT NULL,
            `refreshed_at` DATETIME NOT NULL,
            `expires_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_exchange_pair`
                (`from_currency_id`, `to_currency_id`),
            KEY `idx_erp_meli_exchange_expiry` (`expires_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{DAILY_EXCHANGE_RATE_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `from_currency_id` VARCHAR(16) NOT NULL,
            `to_currency_id` VARCHAR(16) NOT NULL,
            `rate_date` DATE NOT NULL,
            `rate` DECIMAL(24,12) NOT NULL,
            `source_created_at` VARCHAR(64) NULL,
            `source_valid_until` VARCHAR(64) NULL,
            `payload_json` LONGTEXT NULL,
            `refreshed_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_exchange_daily`
                (`from_currency_id`, `to_currency_id`, `rate_date`),
            KEY `idx_erp_meli_exchange_daily_lookup`
                (`from_currency_id`, `to_currency_id`, `rate_date`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    # Preserve the current snapshot as the first daily record when upgrading an
    # existing installation. Future refreshes are written to both tables.
    cursor.execute(
        f"""
        INSERT IGNORE INTO `{DAILY_EXCHANGE_RATE_TABLE}` (
            `from_currency_id`, `to_currency_id`, `rate_date`, `rate`,
            `source_created_at`, `source_valid_until`, `payload_json`, `refreshed_at`
        )
        SELECT `from_currency_id`, `to_currency_id`,
               COALESCE(
                   STR_TO_DATE(LEFT(NULLIF(`source_created_at`, ''), 10), '%Y-%m-%d'),
                   DATE(`refreshed_at`)
               ), `rate`,
               `source_created_at`, `source_valid_until`, `payload_json`, `refreshed_at`
        FROM `{EXCHANGE_RATE_TABLE}`
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{COMMISSION_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `cache_key` CHAR(64) NOT NULL,
            `site_id` VARCHAR(16) NOT NULL,
            `category_id` VARCHAR(64) NOT NULL,
            `listing_type_id` VARCHAR(64) NOT NULL,
            `listing_type_name` VARCHAR(128) NULL,
            `price` DECIMAL(20,4) NOT NULL,
            `currency_id` VARCHAR(16) NULL,
            `logistic_type` VARCHAR(64) NULL,
            `shipping_mode` VARCHAR(32) NULL,
            `billable_weight_g` DECIMAL(20,4) NULL,
            `commission_amount` DECIMAL(20,4) NOT NULL,
            `percentage_fee` DECIMAL(12,6) NULL,
            `fixed_fee` DECIMAL(20,4) NULL,
            `financing_add_on_fee` DECIMAL(20,4) NULL,
            `payload_json` LONGTEXT NULL,
            `refreshed_at` DATETIME NOT NULL,
            `expires_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_commission_quote` (`cache_key`),
            KEY `idx_erp_meli_commission_site_category`
                (`site_id`, `category_id`, `listing_type_id`),
            KEY `idx_erp_meli_commission_expiry` (`expires_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{SHIPPING_RATE_TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `cache_key` CHAR(64) NOT NULL,
            `site_id` VARCHAR(16) NOT NULL,
            `marketplace_user_id` VARCHAR(64) NOT NULL,
            `category_id` VARCHAR(64) NOT NULL,
            `listing_type_id` VARCHAR(64) NOT NULL,
            `price` DECIMAL(20,4) NOT NULL,
            `dimensions` VARCHAR(128) NOT NULL,
            `logistic_type` VARCHAR(64) NULL,
            `shipping_mode` VARCHAR(32) NULL,
            `currency_id` VARCHAR(16) NULL,
            `shipping_amount` DECIMAL(20,4) NOT NULL,
            `api_billable_weight_g` DECIMAL(20,4) NULL,
            `payload_json` LONGTEXT NULL,
            `refreshed_at` DATETIME NOT NULL,
            `expires_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_shipping_quote` (`cache_key`),
            KEY `idx_erp_meli_shipping_site` (`site_id`, `marketplace_user_id`),
            KEY `idx_erp_meli_shipping_expiry` (`expires_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def _ensure_schema(connection: Any) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with connection.cursor() as cursor:
            required = {
                (EXCHANGE_RATE_TABLE, "rate"),
                (DAILY_EXCHANGE_RATE_TABLE, "snapshot_date"),
                (COMMISSION_TABLE, "listing_type_id"),
                (SHIPPING_RATE_TABLE, "dimensions"),
            }
            cursor.execute(
                """
                SELECT `TABLE_NAME`, `COLUMN_NAME`
                FROM `information_schema`.`COLUMNS`
                WHERE `TABLE_SCHEMA` = DATABASE()
                  AND `TABLE_NAME` IN (%s, %s, %s, %s)
                """,
                (
                    EXCHANGE_RATE_TABLE,
                    DAILY_EXCHANGE_RATE_TABLE,
                    COMMISSION_TABLE,
                    SHIPPING_RATE_TABLE,
                ),
            )
            existing = {
                (str(row.get("TABLE_NAME") or row.get("Table_name") or ""),
                 str(row.get("COLUMN_NAME") or row.get("Column_name") or ""))
                for row in cursor.fetchall() or []
                if isinstance(row, Mapping)
            }
            if not required.issubset(existing):
                ensure_profitability_cache_tables(cursor)
        connection.commit()
        _schema_ready = True


def _freshness(ttl_hours: int) -> tuple[str, str]:
    refreshed_at = _now()
    expires_at = refreshed_at + timedelta(hours=max(1, int(ttl_hours)))
    return _text_time(refreshed_at), _text_time(expires_at)


class DatabaseProfitabilityCache:
    """Read-through cache whose official API values remain valid for one day."""

    def __init__(self, *, connection_factory=None, ttl_hours: int = DEFAULT_CACHE_HOURS):
        self.connection_factory = connection_factory or _connect
        self.ttl_hours = max(1, int(ttl_hours))

    def _connection(self) -> Any:
        connection = self.connection_factory()
        try:
            _ensure_schema(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def get_exchange_rate(self, from_currency_id: str, to_currency_id: str) -> dict[str, Any] | None:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT * FROM `{EXCHANGE_RATE_TABLE}`
                    WHERE `from_currency_id` = %s AND `to_currency_id` = %s
                      AND `expires_at` > %s
                    LIMIT 1
                    """,
                    (
                        str(from_currency_id or "").upper(),
                        str(to_currency_id or "").upper(),
                        _text_time(_now()),
                    ),
                )
                row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def put_exchange_rate(
        self,
        from_currency_id: str,
        to_currency_id: str,
        value: Mapping[str, Any],
    ) -> None:
        refreshed_at, expires_at = _freshness(self.ttl_hours)
        rate_date = _exchange_rate_date(value, refreshed_at)
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO `{EXCHANGE_RATE_TABLE}` (
                        `from_currency_id`, `to_currency_id`, `rate`,
                        `source_created_at`, `source_valid_until`, `payload_json`,
                        `refreshed_at`, `expires_at`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `rate` = VALUES(`rate`),
                        `source_created_at` = VALUES(`source_created_at`),
                        `source_valid_until` = VALUES(`source_valid_until`),
                        `payload_json` = VALUES(`payload_json`),
                        `refreshed_at` = VALUES(`refreshed_at`),
                        `expires_at` = VALUES(`expires_at`)
                    """,
                    (
                        str(from_currency_id or "").upper(),
                        str(to_currency_id or "").upper(),
                        value.get("ratio"),
                        str(value.get("creation_date") or "")[:64],
                        str(value.get("valid_until") or "")[:64],
                        _dumps(value),
                        refreshed_at,
                        expires_at,
                    ),
                )
                cursor.execute(
                    f"""
                    INSERT INTO `{DAILY_EXCHANGE_RATE_TABLE}` (
                        `from_currency_id`, `to_currency_id`, `rate_date`, `rate`,
                        `source_created_at`, `source_valid_until`, `payload_json`,
                        `refreshed_at`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `rate` = VALUES(`rate`),
                        `source_created_at` = VALUES(`source_created_at`),
                        `source_valid_until` = VALUES(`source_valid_until`),
                        `payload_json` = VALUES(`payload_json`),
                        `refreshed_at` = VALUES(`refreshed_at`)
                    """,
                    (
                        str(from_currency_id or "").upper(),
                        str(to_currency_id or "").upper(),
                        rate_date,
                        value.get("ratio"),
                        str(value.get("creation_date") or "")[:64],
                        str(value.get("valid_until") or "")[:64],
                        _dumps(value),
                        refreshed_at,
                    ),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def put_exchange_rate_history(
        self,
        from_currency_id: str,
        to_currency_id: str,
        values: list[Mapping[str, Any]],
    ) -> int:
        """Persist a dated series efficiently and expose its latest row as fallback."""
        refreshed_at, expires_at = _freshness(self.ttl_hours)
        from_currency_id = str(from_currency_id or "").upper()
        to_currency_id = str(to_currency_id or "").upper()
        snapshots = []
        for value in values or []:
            try:
                rate = float(value.get("ratio"))
            except (TypeError, ValueError):
                continue
            if rate <= 0:
                continue
            snapshots.append((
                _exchange_rate_date(value, refreshed_at),
                rate,
                str(value.get("creation_date") or "")[:64],
                str(value.get("valid_until") or "")[:64],
                _dumps(value),
            ))
        if not snapshots:
            return 0
        snapshots.sort(key=lambda row: row[0])
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.executemany(
                    f"""
                    INSERT INTO `{DAILY_EXCHANGE_RATE_TABLE}` (
                        `from_currency_id`, `to_currency_id`, `rate_date`, `rate`,
                        `source_created_at`, `source_valid_until`, `payload_json`,
                        `refreshed_at`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `rate` = VALUES(`rate`),
                        `source_created_at` = VALUES(`source_created_at`),
                        `source_valid_until` = VALUES(`source_valid_until`),
                        `payload_json` = VALUES(`payload_json`),
                        `refreshed_at` = VALUES(`refreshed_at`)
                    """,
                    [
                        (
                            from_currency_id, to_currency_id, rate_date, rate,
                            source_created_at, source_valid_until, payload_json,
                            refreshed_at,
                        )
                        for rate_date, rate, source_created_at, source_valid_until, payload_json
                        in snapshots
                    ],
                )
                latest = snapshots[-1]
                cursor.execute(
                    f"""
                    INSERT INTO `{EXCHANGE_RATE_TABLE}` (
                        `from_currency_id`, `to_currency_id`, `rate`,
                        `source_created_at`, `source_valid_until`, `payload_json`,
                        `refreshed_at`, `expires_at`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `rate` = VALUES(`rate`),
                        `source_created_at` = VALUES(`source_created_at`),
                        `source_valid_until` = VALUES(`source_valid_until`),
                        `payload_json` = VALUES(`payload_json`),
                        `refreshed_at` = VALUES(`refreshed_at`),
                        `expires_at` = VALUES(`expires_at`)
                    """,
                    (
                        from_currency_id, to_currency_id, latest[1], latest[2],
                        latest[3], latest[4], refreshed_at, expires_at,
                    ),
                )
            connection.commit()
            return len(snapshots)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def commission_key(
        *,
        site_id: str,
        category_id: str,
        listing_type_id: str,
        price: float,
        currency_id: str,
        logistic_type: str,
        shipping_mode: str,
        billable_weight_g: float | None,
    ) -> str:
        return _cache_key(locals())

    def get_commission(self, **quote: Any) -> dict[str, Any] | None:
        key = self.commission_key(**quote)
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT * FROM `{COMMISSION_TABLE}` "
                    "WHERE `cache_key` = %s AND `expires_at` > %s LIMIT 1",
                    (key, _text_time(_now())),
                )
                row = cursor.fetchone()
            if not row:
                return None
            return {
                "amount": row.get("commission_amount"),
                "currency_id": row.get("currency_id"),
                "rate": row.get("percentage_fee"),
                "fixed_fee": row.get("fixed_fee"),
                "financing_add_on_fee": row.get("financing_add_on_fee"),
                "listing_type_name": row.get("listing_type_name"),
                "refreshed_at": row.get("refreshed_at"),
                "cache_source": "database_daily_cache",
            }
        finally:
            connection.close()

    def put_commission(self, quote: Mapping[str, Any], value: Mapping[str, Any]) -> None:
        key = self.commission_key(**dict(quote))
        refreshed_at, expires_at = _freshness(self.ttl_hours)
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO `{COMMISSION_TABLE}` (
                        `cache_key`, `site_id`, `category_id`, `listing_type_id`,
                        `listing_type_name`, `price`, `currency_id`, `logistic_type`,
                        `shipping_mode`, `billable_weight_g`, `commission_amount`,
                        `percentage_fee`, `fixed_fee`, `financing_add_on_fee`,
                        `payload_json`, `refreshed_at`, `expires_at`
                    ) VALUES ({", ".join(["%s"] * 17)})
                    ON DUPLICATE KEY UPDATE
                        `listing_type_name` = VALUES(`listing_type_name`),
                        `currency_id` = VALUES(`currency_id`),
                        `commission_amount` = VALUES(`commission_amount`),
                        `percentage_fee` = VALUES(`percentage_fee`),
                        `fixed_fee` = VALUES(`fixed_fee`),
                        `financing_add_on_fee` = VALUES(`financing_add_on_fee`),
                        `payload_json` = VALUES(`payload_json`),
                        `refreshed_at` = VALUES(`refreshed_at`),
                        `expires_at` = VALUES(`expires_at`)
                    """,
                    (
                        key,
                        quote["site_id"], quote["category_id"], quote["listing_type_id"],
                        value.get("listing_type_name"), quote["price"], value.get("currency_id"),
                        quote["logistic_type"], quote["shipping_mode"],
                        quote.get("billable_weight_g"), value.get("amount"), value.get("rate"),
                        value.get("fixed_fee"), value.get("financing_add_on_fee"),
                        _dumps(value.get("payload") or value), refreshed_at, expires_at,
                    ),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def shipping_key(
        *,
        site_id: str,
        marketplace_user_id: str,
        category_id: str,
        listing_type_id: str,
        price: float,
        dimensions: str,
        logistic_type: str,
        shipping_mode: str,
        free_shipping: bool,
    ) -> str:
        return _cache_key(locals())

    def get_shipping(self, **quote: Any) -> dict[str, Any] | None:
        key = self.shipping_key(**quote)
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT * FROM `{SHIPPING_RATE_TABLE}` "
                    "WHERE `cache_key` = %s AND `expires_at` > %s LIMIT 1",
                    (key, _text_time(_now())),
                )
                row = cursor.fetchone()
            if not row:
                return None
            return {
                "amount": row.get("shipping_amount"),
                "currency_id": row.get("currency_id"),
                "api_billable_weight_g": row.get("api_billable_weight_g"),
                "refreshed_at": row.get("refreshed_at"),
                "cache_source": "database_daily_cache",
            }
        finally:
            connection.close()

    def put_shipping(self, quote: Mapping[str, Any], value: Mapping[str, Any]) -> None:
        key = self.shipping_key(**dict(quote))
        refreshed_at, expires_at = _freshness(self.ttl_hours)
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO `{SHIPPING_RATE_TABLE}` (
                        `cache_key`, `site_id`, `marketplace_user_id`, `category_id`,
                        `listing_type_id`, `price`, `dimensions`, `logistic_type`,
                        `shipping_mode`, `currency_id`, `shipping_amount`,
                        `api_billable_weight_g`, `payload_json`, `refreshed_at`, `expires_at`
                    ) VALUES ({", ".join(["%s"] * 15)})
                    ON DUPLICATE KEY UPDATE
                        `currency_id` = VALUES(`currency_id`),
                        `shipping_amount` = VALUES(`shipping_amount`),
                        `api_billable_weight_g` = VALUES(`api_billable_weight_g`),
                        `payload_json` = VALUES(`payload_json`),
                        `refreshed_at` = VALUES(`refreshed_at`),
                        `expires_at` = VALUES(`expires_at`)
                    """,
                    (
                        key,
                        quote["site_id"], quote["marketplace_user_id"], quote["category_id"],
                        quote["listing_type_id"], quote["price"], quote["dimensions"],
                        quote["logistic_type"], quote["shipping_mode"], value.get("currency_id"),
                        value.get("amount"), value.get("api_billable_weight_g"),
                        _dumps(value.get("payload") or value), refreshed_at, expires_at,
                    ),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


__all__ = [
    "COMMISSION_TABLE",
    "DAILY_EXCHANGE_RATE_TABLE",
    "DEFAULT_CACHE_HOURS",
    "DatabaseProfitabilityCache",
    "EXCHANGE_RATE_TABLE",
    "SHIPPING_RATE_TABLE",
    "ensure_profitability_cache_tables",
]
