from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from yandex.app.config import settings
from yandex.app.schemas import ProductRecord


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.resolved_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS search_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL,
                    requested_count INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    found_count INTEGER NOT NULL DEFAULT 0,
                    scanned_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER REFERENCES search_runs(id) ON DELETE SET NULL,
                    source_url TEXT NOT NULL UNIQUE,
                    market_sku INTEGER,
                    offer_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    vendor TEXT NOT NULL DEFAULT '',
                    vendor_code TEXT NOT NULL DEFAULT '',
                    category_name TEXT NOT NULL DEFAULT '',
                    market_category_id INTEGER,
                    price REAL,
                    old_price REAL,
                    currency TEXT NOT NULL DEFAULT 'RUR',
                    pictures_json TEXT NOT NULL DEFAULT '[]',
                    specifications_json TEXT NOT NULL DEFAULT '{}',
                    seller_name TEXT NOT NULL DEFAULT '',
                    rating REAL,
                    reviews_count INTEGER,
                    is_foreign INTEGER NOT NULL DEFAULT 0,
                    foreign_evidence TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    publish_status TEXT NOT NULL DEFAULT 'not_published',
                    publish_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_products_run_id ON products(run_id);
                CREATE INDEX IF NOT EXISTS idx_products_market_sku ON products(market_sku);

                CREATE TABLE IF NOT EXISTS stores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alias TEXT NOT NULL,
                    encrypted_token BLOB NOT NULL,
                    token_fingerprint TEXT NOT NULL UNIQUE,
                    business_id INTEGER NOT NULL,
                    business_name TEXT NOT NULL DEFAULT '',
                    campaign_id INTEGER NOT NULL UNIQUE,
                    store_name TEXT NOT NULL DEFAULT '',
                    placement_type TEXT NOT NULL DEFAULT '',
                    api_availability TEXT NOT NULL DEFAULT '',
                    auth_scopes_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS zeshun_store_authorizations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alias TEXT NOT NULL,
                    tg_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    authorization_url TEXT NOT NULL DEFAULT '',
                    encrypted_authorized_url BLOB,
                    store_id INTEGER REFERENCES stores(id) ON DELETE SET NULL,
                    token_updated_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_zeshun_authorizations_store_id
                    ON zeshun_store_authorizations(store_id);

                CREATE TABLE IF NOT EXISTS publish_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    store_id INTEGER,
                    business_id INTEGER,
                    campaign_id INTEGER,
                    price_percent REAL NOT NULL DEFAULT 200,
                    exchange_rate REAL NOT NULL DEFAULT 1,
                    exchange_rate_date TEXT NOT NULL DEFAULT '',
                    target_currency TEXT NOT NULL DEFAULT 'RUR',
                    package_json TEXT NOT NULL DEFAULT '{}',
                    initial_stock INTEGER NOT NULL DEFAULT 0,
                    warehouse_id INTEGER,
                    warehouse_name TEXT NOT NULL DEFAULT '',
                    stock_method TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'running',
                    total INTEGER NOT NULL,
                    succeeded INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    response_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS publish_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES publish_jobs(id) ON DELETE CASCADE,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    response_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS yandex_orders (
                    store_id INTEGER NOT NULL,
                    order_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT '',
                    creation_date TEXT NOT NULL DEFAULT '',
                    update_date TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    status_synced_at TEXT NOT NULL,
                    cached_at TEXT NOT NULL,
                    PRIMARY KEY (store_id, order_id)
                );

                CREATE INDEX IF NOT EXISTS idx_yandex_orders_store_creation
                    ON yandex_orders(store_id, creation_date DESC, order_id DESC);
                CREATE INDEX IF NOT EXISTS idx_yandex_orders_store_status
                    ON yandex_orders(store_id, status, creation_date DESC);

                CREATE TABLE IF NOT EXISTS yandex_order_sync_state (
                    store_id INTEGER PRIMARY KEY,
                    new_orders_synced_at TEXT NOT NULL DEFAULT '',
                    old_orders_synced_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                """
            )
            publish_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(publish_jobs)").fetchall()
            }
            if "store_id" not in publish_columns:
                db.execute("ALTER TABLE publish_jobs ADD COLUMN store_id INTEGER")
            if "price_percent" not in publish_columns:
                db.execute(
                    "ALTER TABLE publish_jobs ADD COLUMN price_percent REAL NOT NULL DEFAULT 200"
                )
            if "exchange_rate" not in publish_columns:
                db.execute(
                    "ALTER TABLE publish_jobs ADD COLUMN exchange_rate REAL NOT NULL DEFAULT 1"
                )
            if "exchange_rate_date" not in publish_columns:
                db.execute(
                    "ALTER TABLE publish_jobs ADD COLUMN exchange_rate_date TEXT NOT NULL DEFAULT ''"
                )
            if "target_currency" not in publish_columns:
                db.execute(
                    "ALTER TABLE publish_jobs ADD COLUMN target_currency TEXT NOT NULL DEFAULT 'RUR'"
                )
            if "package_json" not in publish_columns:
                db.execute(
                    "ALTER TABLE publish_jobs ADD COLUMN package_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "initial_stock" not in publish_columns:
                db.execute(
                    "ALTER TABLE publish_jobs ADD COLUMN initial_stock INTEGER NOT NULL DEFAULT 0"
                )
            if "warehouse_id" not in publish_columns:
                db.execute("ALTER TABLE publish_jobs ADD COLUMN warehouse_id INTEGER")
            if "warehouse_name" not in publish_columns:
                db.execute(
                    "ALTER TABLE publish_jobs ADD COLUMN warehouse_name TEXT NOT NULL DEFAULT ''"
                )
            if "stock_method" not in publish_columns:
                db.execute(
                    "ALTER TABLE publish_jobs ADD COLUMN stock_method TEXT NOT NULL DEFAULT ''"
                )

    def cache_orders(
        self,
        store_id: int,
        orders: Sequence[dict[str, Any]],
        *,
        mode: str,
    ) -> dict[str, int]:
        """Persist one store's orders without mixing the two refresh cadences.

        ``insert`` only discovers new order IDs, ``update`` only refreshes rows
        already in the cache, and ``full`` performs both operations during the
        first/bootstrap pass.
        """
        if mode not in {"insert", "update", "full"}:
            raise ValueError("订单缓存模式无效")
        now = utc_now()
        inserted = 0
        updated = 0
        with self.connect() as db:
            for order in orders:
                if not isinstance(order, dict):
                    continue
                order_id = str(order.get("orderId") or order.get("id") or "").strip()
                if not order_id:
                    continue
                values = (
                    str(order.get("status") or "").upper(),
                    str(order.get("creationDate") or ""),
                    str(order.get("updateDate") or order.get("creationDate") or ""),
                    json.dumps(order, ensure_ascii=False, separators=(",", ":")),
                )
                if mode == "insert":
                    cursor = db.execute(
                        """
                        INSERT OR IGNORE INTO yandex_orders (
                            store_id, order_id, status, creation_date, update_date,
                            raw_json, first_seen_at, status_synced_at, cached_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (int(store_id), order_id, *values, now, now, now),
                    )
                    inserted += max(0, cursor.rowcount)
                elif mode == "update":
                    cursor = db.execute(
                        """
                        UPDATE yandex_orders SET
                            status = ?, creation_date = ?, update_date = ?,
                            raw_json = ?, status_synced_at = ?, cached_at = ?
                        WHERE store_id = ? AND order_id = ?
                        """,
                        (*values, now, now, int(store_id), order_id),
                    )
                    updated += max(0, cursor.rowcount)
                else:
                    existed = db.execute(
                        "SELECT 1 FROM yandex_orders WHERE store_id = ? AND order_id = ?",
                        (int(store_id), order_id),
                    ).fetchone()
                    db.execute(
                        """
                        INSERT INTO yandex_orders (
                            store_id, order_id, status, creation_date, update_date,
                            raw_json, first_seen_at, status_synced_at, cached_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(store_id, order_id) DO UPDATE SET
                            status = excluded.status,
                            creation_date = excluded.creation_date,
                            update_date = excluded.update_date,
                            raw_json = excluded.raw_json,
                            status_synced_at = excluded.status_synced_at,
                            cached_at = excluded.cached_at
                        """,
                        (int(store_id), order_id, *values, now, now, now),
                    )
                    if existed:
                        updated += 1
                    else:
                        inserted += 1
        return {"inserted": inserted, "updated": updated}

    def count_cached_orders(self, store_id: int) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS total FROM yandex_orders WHERE store_id = ?",
                (int(store_id),),
            ).fetchone()
        return int(row["total"] if row else 0)

    def cached_order_ids(self, store_id: int) -> set[str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT order_id FROM yandex_orders WHERE store_id = ?",
                (int(store_id),),
            ).fetchall()
        return {str(row["order_id"]) for row in rows}

    def list_cached_order_todos(self, store_id: int, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT raw_json FROM yandex_orders
                WHERE store_id = ? AND status IN ('PENDING', 'PROCESSING')
                ORDER BY creation_date ASC, order_id ASC LIMIT ?
                """,
                (int(store_id), max(1, min(int(limit), 500))),
            ).fetchall()
        orders: list[dict[str, Any]] = []
        for row in rows:
            try:
                order = json.loads(row["raw_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(order, dict):
                orders.append(order)
        return orders

    def list_cached_orders(
        self,
        store_id: int,
        *,
        statuses: Sequence[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], bool]:
        clauses = ["store_id = ?"]
        values: list[Any] = [int(store_id)]
        normalized_statuses = list(
            dict.fromkeys(str(value).strip().upper() for value in statuses or [] if str(value).strip())
        )
        if normalized_statuses:
            clauses.append(f"status IN ({','.join('?' for _ in normalized_statuses)})")
            values.extend(normalized_statuses)
        if date_from:
            clauses.append("substr(creation_date, 1, 10) >= ?")
            values.append(str(date_from))
        if date_to:
            clauses.append("substr(creation_date, 1, 10) <= ?")
            values.append(str(date_to))
        page_size = max(1, min(int(limit), 50))
        page_offset = max(0, int(offset))
        values.extend((page_size + 1, page_offset))
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT raw_json FROM yandex_orders
                WHERE {' AND '.join(clauses)}
                ORDER BY creation_date DESC, order_id DESC
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows[:page_size]:
            try:
                order = json.loads(row["raw_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(order, dict):
                result.append(order)
        return result, len(rows) > page_size

    def get_order_sync_state(self, store_id: int) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM yandex_order_sync_state WHERE store_id = ?",
                (int(store_id),),
            ).fetchone()
        return dict(row) if row else {
            "store_id": int(store_id),
            "new_orders_synced_at": "",
            "old_orders_synced_at": "",
            "last_error": "",
            "updated_at": "",
        }

    def update_order_sync_state(self, store_id: int, **fields: Any) -> dict[str, Any]:
        allowed = {"new_orders_synced_at", "old_orders_synced_at", "last_error"}
        updates = {key: str(value or "") for key, value in fields.items() if key in allowed}
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO yandex_order_sync_state (store_id, updated_at)
                VALUES (?, ?)
                """,
                (int(store_id), now),
            )
            if updates:
                updates["updated_at"] = now
                assignment = ", ".join(f"{key} = ?" for key in updates)
                db.execute(
                    f"UPDATE yandex_order_sync_state SET {assignment} WHERE store_id = ?",
                    (*updates.values(), int(store_id)),
                )
        return self.get_order_sync_state(store_id)

    def patch_cached_order_status(
        self,
        store_id: int,
        order_id: int,
        *,
        status: str,
        substatus: str,
    ) -> bool:
        now = utc_now()
        with self.connect() as db:
            row = db.execute(
                "SELECT raw_json FROM yandex_orders WHERE store_id = ? AND order_id = ?",
                (int(store_id), str(order_id)),
            ).fetchone()
            if not row:
                return False
            try:
                order = json.loads(row["raw_json"] or "{}")
            except (TypeError, ValueError):
                order = {}
            order.update({"status": status, "substatus": substatus, "updateDate": now})
            db.execute(
                """
                UPDATE yandex_orders SET status = ?, update_date = ?, raw_json = ?,
                    status_synced_at = ?, cached_at = ?
                WHERE store_id = ? AND order_id = ?
                """,
                (
                    status,
                    now,
                    json.dumps(order, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                    int(store_id),
                    str(order_id),
                ),
            )
        return True

    def delete_order_cache(self, store_id: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM yandex_orders WHERE store_id = ?", (int(store_id),))
            db.execute(
                "DELETE FROM yandex_order_sync_state WHERE store_id = ?", (int(store_id),)
            )

    @staticmethod
    def _decode_store(row: sqlite3.Row, *, include_secret: bool = False) -> dict[str, Any]:
        result = dict(row)
        result["auth_scopes"] = json.loads(result.pop("auth_scopes_json") or "[]")
        result.pop("token_fingerprint", None)
        if not include_secret:
            result.pop("encrypted_token", None)
        return result

    def save_store(
        self,
        *,
        alias: str,
        encrypted_token: bytes,
        token_fingerprint: str,
        store: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        business_id = int(store["business_id"])
        campaign_id = int(store["campaign_id"])
        with self.connect() as db:
            existing = db.execute(
                """
                SELECT id FROM stores
                WHERE token_fingerprint = ? OR campaign_id = ?
                ORDER BY CASE WHEN token_fingerprint = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (token_fingerprint, campaign_id, token_fingerprint),
            ).fetchone()
            if existing:
                store_id = int(existing["id"])
                db.execute(
                    """
                    UPDATE stores SET
                        alias = ?, encrypted_token = ?, token_fingerprint = ?,
                        business_id = ?, business_name = ?, campaign_id = ?,
                        store_name = ?, placement_type = ?, api_availability = ?,
                        auth_scopes_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        alias,
                        encrypted_token,
                        token_fingerprint,
                        business_id,
                        store.get("business_name", ""),
                        campaign_id,
                        store.get("store_name", ""),
                        store.get("placement_type", ""),
                        store.get("api_availability", ""),
                        json.dumps(store.get("auth_scopes") or []),
                        now,
                        store_id,
                    ),
                )
                created = False
            else:
                cursor = db.execute(
                    """
                    INSERT INTO stores (
                        alias, encrypted_token, token_fingerprint, business_id,
                        business_name, campaign_id, store_name, placement_type,
                        api_availability, auth_scopes_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alias,
                        encrypted_token,
                        token_fingerprint,
                        business_id,
                        store.get("business_name", ""),
                        campaign_id,
                        store.get("store_name", ""),
                        store.get("placement_type", ""),
                        store.get("api_availability", ""),
                        json.dumps(store.get("auth_scopes") or []),
                        now,
                        now,
                    ),
                )
                store_id = int(cursor.lastrowid)
                created = True
            row = db.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
        return self._decode_store(row), created

    def list_stores(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM stores ORDER BY alias COLLATE NOCASE, id"
            ).fetchall()
        return [self._decode_store(row) for row in rows]

    def list_legacy_stores_with_secrets(self) -> list[dict[str, Any]]:
        """Read pre-0.5 local authorizations for one-time central migration only."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM stores ORDER BY alias COLLATE NOCASE, id"
            ).fetchall()
        return [self._decode_store(row, include_secret=True) for row in rows]

    def clear_legacy_authorizations(self) -> None:
        """Remove local credentials after every row has reached central MySQL."""
        with self.connect() as db:
            db.execute("DELETE FROM zeshun_store_authorizations")
            db.execute("DELETE FROM stores")

    def get_store(self, store_id: int, *, include_secret: bool = False) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
        return self._decode_store(row, include_secret=include_secret) if row else None

    def update_store_alias(self, store_id: int, alias: str) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute(
                "UPDATE stores SET alias = ?, updated_at = ? WHERE id = ?",
                (alias, utc_now(), store_id),
            )
            row = db.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
        return self._decode_store(row) if row else None

    def update_store_connection(self, store_id: int, store: dict[str, Any]) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE stores SET
                    business_id = ?, business_name = ?, campaign_id = ?,
                    store_name = ?, placement_type = ?, api_availability = ?,
                    auth_scopes_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    int(store["business_id"]),
                    store.get("business_name", ""),
                    int(store["campaign_id"]),
                    store.get("store_name", ""),
                    store.get("placement_type", ""),
                    store.get("api_availability", ""),
                    json.dumps(store.get("auth_scopes") or []),
                    utc_now(),
                    store_id,
                ),
            )
            row = db.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
        return self._decode_store(row) if row else None

    def delete_store(self, store_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM stores WHERE id = ?", (store_id,))
            return cursor.rowcount > 0

    @staticmethod
    def _decode_zeshun_authorization(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result.pop("encrypted_authorized_url", None)
        result["authorized"] = bool(result.get("store_id") and result.get("token_updated_at"))
        return result

    def create_zeshun_authorization(
        self,
        *,
        alias: str,
        tg_code: str,
        authorization_url: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            existing = db.execute(
                "SELECT id FROM zeshun_store_authorizations WHERE tg_code = ? COLLATE NOCASE",
                (tg_code,),
            ).fetchone()
            if existing:
                raise ValueError("该 TG 码已经存在")
            cursor = db.execute(
                """
                INSERT INTO zeshun_store_authorizations (
                    alias, tg_code, authorization_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (alias, tg_code, authorization_url, now, now),
            )
            row = db.execute(
                "SELECT * FROM zeshun_store_authorizations WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
        return self._decode_zeshun_authorization(row)

    def list_zeshun_authorizations(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT authorization.*, stores.store_name, stores.business_name,
                       stores.campaign_id, stores.api_availability
                FROM zeshun_store_authorizations AS authorization
                LEFT JOIN stores ON stores.id = authorization.store_id
                ORDER BY authorization.alias COLLATE NOCASE, authorization.id
                """
            ).fetchall()
        return [self._decode_zeshun_authorization(row) for row in rows]

    def get_zeshun_authorization(self, authorization_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM zeshun_store_authorizations WHERE id = ?",
                (authorization_id,),
            ).fetchone()
        return self._decode_zeshun_authorization(row) if row else None

    def update_zeshun_authorization(
        self,
        authorization_id: int,
        *,
        alias: str,
        authorization_url: str,
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            existing = db.execute(
                "SELECT store_id FROM zeshun_store_authorizations WHERE id = ?",
                (authorization_id,),
            ).fetchone()
            if not existing:
                return None
            db.execute(
                """
                UPDATE zeshun_store_authorizations
                SET alias = ?, authorization_url = ?, updated_at = ?
                WHERE id = ?
                """,
                (alias, authorization_url, utc_now(), authorization_id),
            )
            if existing["store_id"]:
                db.execute(
                    "UPDATE stores SET alias = ?, updated_at = ? WHERE id = ?",
                    (alias, utc_now(), int(existing["store_id"])),
                )
            row = db.execute(
                "SELECT * FROM zeshun_store_authorizations WHERE id = ?",
                (authorization_id,),
            ).fetchone()
        return self._decode_zeshun_authorization(row)

    def complete_zeshun_authorization(
        self,
        authorization_id: int,
        *,
        store_id: int,
        encrypted_authorized_url: bytes | None = None,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE zeshun_store_authorizations
                SET store_id = ?, encrypted_authorized_url = ?,
                    token_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (store_id, encrypted_authorized_url, now, now, authorization_id),
            )
            if not cursor.rowcount:
                return None
            row = db.execute(
                "SELECT * FROM zeshun_store_authorizations WHERE id = ?",
                (authorization_id,),
            ).fetchone()
        return self._decode_zeshun_authorization(row)

    def delete_zeshun_authorization(self, authorization_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM zeshun_store_authorizations WHERE id = ?",
                (authorization_id,),
            )
            return cursor.rowcount > 0

    def create_search_run(self, keyword: str, requested_count: int) -> int:
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO search_runs
                    (keyword, requested_count, status, created_at, updated_at)
                VALUES (?, ?, 'queued', ?, ?)
                """,
                (keyword, requested_count, now, now),
            )
            return int(cursor.lastrowid)

    def update_search_run(self, run_id: int, **fields: Any) -> None:
        allowed = {"status", "found_count", "scanned_count", "message"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = utc_now()
        assignment = ", ".join(f"{key} = ?" for key in updates)
        with self.connect() as db:
            db.execute(
                f"UPDATE search_runs SET {assignment} WHERE id = ?",
                (*updates.values(), run_id),
            )

    def get_search_run(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM search_runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def upsert_product(self, product: ProductRecord) -> int:
        now = utc_now()
        values = (
            product.run_id,
            product.source_url,
            product.market_sku,
            product.offer_id,
            product.name,
            product.description,
            product.vendor,
            product.vendor_code,
            product.category_name,
            product.market_category_id,
            product.price,
            product.old_price,
            product.currency,
            json.dumps(product.pictures, ensure_ascii=False),
            json.dumps(product.specifications, ensure_ascii=False),
            product.seller_name,
            product.rating,
            product.reviews_count,
            int(product.is_foreign),
            product.foreign_evidence,
            json.dumps(product.raw_data, ensure_ascii=False),
            now,
            now,
        )
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO products (
                    run_id, source_url, market_sku, offer_id, name, description,
                    vendor, vendor_code, category_name, market_category_id,
                    price, old_price, currency, pictures_json,
                    specifications_json, seller_name, rating, reviews_count,
                    is_foreign, foreign_evidence, raw_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_url) DO UPDATE SET
                    run_id = excluded.run_id,
                    market_sku = COALESCE(excluded.market_sku, products.market_sku),
                    name = excluded.name,
                    description = CASE WHEN excluded.description != '' THEN excluded.description ELSE products.description END,
                    vendor = CASE WHEN excluded.vendor != '' THEN excluded.vendor ELSE products.vendor END,
                    vendor_code = CASE WHEN excluded.vendor_code != '' THEN excluded.vendor_code ELSE products.vendor_code END,
                    category_name = CASE WHEN excluded.category_name != '' THEN excluded.category_name ELSE products.category_name END,
                    market_category_id = COALESCE(excluded.market_category_id, products.market_category_id),
                    price = COALESCE(excluded.price, products.price),
                    old_price = COALESCE(excluded.old_price, products.old_price),
                    currency = excluded.currency,
                    pictures_json = CASE WHEN excluded.pictures_json != '[]' THEN excluded.pictures_json ELSE products.pictures_json END,
                    specifications_json = CASE WHEN excluded.specifications_json != '{}' THEN excluded.specifications_json ELSE products.specifications_json END,
                    seller_name = CASE WHEN excluded.seller_name != '' THEN excluded.seller_name ELSE products.seller_name END,
                    rating = COALESCE(excluded.rating, products.rating),
                    reviews_count = COALESCE(excluded.reviews_count, products.reviews_count),
                    is_foreign = excluded.is_foreign,
                    foreign_evidence = excluded.foreign_evidence,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                values,
            )
            row = db.execute(
                "SELECT id FROM products WHERE source_url = ?", (product.source_url,)
            ).fetchone()
            return int(row["id"])

    @staticmethod
    def _decode_product(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["pictures"] = json.loads(result.pop("pictures_json") or "[]")
        result["specifications"] = json.loads(result.pop("specifications_json") or "{}")
        result["raw_data"] = json.loads(result.pop("raw_json") or "{}")
        result["is_foreign"] = bool(result["is_foreign"])
        record = ProductRecord.model_validate(result)
        result["missing_publish_fields"] = record.missing_publish_fields
        result["ready_to_publish"] = not result["missing_publish_fields"]
        return result

    def list_products_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM products WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [self._decode_product(row) for row in rows]

    def get_products(self, product_ids: Sequence[int]) -> list[dict[str, Any]]:
        if not product_ids:
            return []
        placeholders = ",".join("?" for _ in product_ids)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM products WHERE id IN ({placeholders}) ORDER BY id",
                tuple(product_ids),
            ).fetchall()
        return [self._decode_product(row) for row in rows]

    def mark_product_publish(self, product_id: int, status: str, message: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE products
                SET publish_status = ?, publish_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, message, utc_now(), product_id),
            )

    def create_publish_job(
        self,
        total: int,
        business_id: int,
        campaign_id: int | None,
        store_id: int | None = None,
        price_percent: float = 200,
        exchange_rate: float = 1,
        exchange_rate_date: str = "",
        target_currency: str = "RUR",
        package: dict[str, float] | None = None,
        initial_stock: int = 0,
        stock_target: Any = None,
    ) -> int:
        now = utc_now()
        stock_target_data = stock_target.public_dict() if stock_target else {}
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO publish_jobs
                    (store_id, business_id, campaign_id, price_percent,
                     exchange_rate, exchange_rate_date, target_currency,
                     package_json, initial_stock, warehouse_id, warehouse_name,
                     stock_method, status, total, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    store_id,
                    business_id,
                    campaign_id,
                    price_percent,
                    exchange_rate,
                    exchange_rate_date,
                    target_currency,
                    json.dumps(package or {}, ensure_ascii=False),
                    initial_stock,
                    stock_target_data.get("warehouse_id"),
                    stock_target_data.get("warehouse_name", ""),
                    stock_target_data.get("method", ""),
                    total,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def finish_publish_job(
        self,
        job_id: int,
        *,
        succeeded: int,
        failed: int,
        response: dict[str, Any],
    ) -> None:
        status = "completed" if failed == 0 else "completed_with_errors"
        with self.connect() as db:
            db.execute(
                """
                UPDATE publish_jobs
                SET status = ?, succeeded = ?, failed = ?, response_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, succeeded, failed, json.dumps(response, ensure_ascii=False), utc_now(), job_id),
            )

    def get_publish_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM publish_jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return None
            results = db.execute(
                """
                SELECT pr.*, p.name, p.offer_id
                FROM publish_results pr
                JOIN products p ON p.id = pr.product_id
                WHERE pr.job_id = ?
                ORDER BY pr.id
                """,
                (job_id,),
            ).fetchall()
        job = dict(row)
        job["response"] = json.loads(job.pop("response_json") or "{}")
        job["package"] = json.loads(job.pop("package_json") or "{}")
        job["results"] = []
        for result_row in results:
            result = dict(result_row)
            result["product_name"] = result.pop("name", "")
            result["success"] = result.get("status") == "published"
            result["pending"] = result.get("status") == "stock_pending"
            result["response"] = json.loads(result.pop("response_json") or "{}")
            job["results"].append(result)
        if job["status"] == "running":
            job["succeeded"] = sum(
                1 for item in job["results"] if item["status"] == "published"
            )
            job["failed"] = sum(
                1 for item in job["results"] if item["status"] == "failed"
            )
        job["processed"] = len(job["results"])
        return job

    def add_publish_result(
        self,
        job_id: int,
        product_id: int,
        status: str,
        message: str,
        response: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO publish_results
                    (job_id, product_id, status, message, response_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    product_id,
                    status,
                    message,
                    json.dumps(response or {}, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def update_publish_result(
        self,
        job_id: int,
        product_id: int,
        status: str,
        message: str,
        response: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE publish_results
                SET status = ?, message = ?, response_json = ?
                WHERE job_id = ? AND product_id = ?
                """,
                (
                    status,
                    message,
                    json.dumps(response or {}, ensure_ascii=False),
                    job_id,
                    product_id,
                ),
            )


database = Database()
