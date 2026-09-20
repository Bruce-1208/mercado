"""Small durable SQLite store for the promotion workbench.

The production application already uses MySQL for core ERP data. Promotion
sync and execution records are kept in an isolated SQLite database so a partial
API page or a failed rollout cannot overwrite existing store-link records.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_PATH = Path(__file__).resolve().parents[1] / ".data" / "promotions.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class PromotionStore:
    def __init__(self, path: str | Path | None = None) -> None:
        configured = path or os.environ.get("BIT_PROMOTION_DB_PATH") or DEFAULT_PATH
        self.path = Path(configured).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS promotions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER NOT NULL,
                    store_name TEXT NOT NULL,
                    salesperson TEXT NOT NULL DEFAULT '',
                    group_name TEXT NOT NULL DEFAULT '',
                    application_id TEXT NOT NULL DEFAULT '',
                    seller_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    promotion_id TEXT NOT NULL,
                    promotion_type TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    status_raw TEXT NOT NULL DEFAULT '',
                    start_date TEXT NOT NULL DEFAULT '',
                    finish_date TEXT NOT NULL DEFAULT '',
                    deadline_date TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL,
                    last_synced_at TEXT NOT NULL,
                    UNIQUE(application_id, seller_id, site_id, promotion_type, promotion_id)
                );
                CREATE INDEX IF NOT EXISTS idx_promotions_scope
                    ON promotions(token_id, site_id, status_raw, deadline_date);

                CREATE TABLE IF NOT EXISTS promotion_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    promotion_fk INTEGER NOT NULL REFERENCES promotions(id) ON DELETE CASCADE,
                    item_id TEXT NOT NULL,
                    offer_id TEXT NOT NULL DEFAULT '',
                    candidate_id TEXT NOT NULL DEFAULT '',
                    status_raw TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    thumbnail TEXT NOT NULL DEFAULT '',
                    price REAL,
                    original_price REAL,
                    currency_id TEXT NOT NULL DEFAULT '',
                    min_price REAL,
                    max_price REAL,
                    net_proceeds_json TEXT NOT NULL DEFAULT '{}',
                    raw_json TEXT NOT NULL,
                    last_synced_at TEXT NOT NULL,
                    UNIQUE(promotion_fk, item_id, offer_id, candidate_id)
                );
                CREATE INDEX IF NOT EXISTS idx_promotion_items_lookup
                    ON promotion_items(promotion_fk, status_raw, item_id);

                CREATE TABLE IF NOT EXISTS promotion_previews (
                    preview_id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    promotion_fk INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS promotion_jobs (
                    job_id TEXT PRIMARY KEY,
                    preview_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    unknown_count INTEGER NOT NULL DEFAULT 0,
                    results_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT ''
                );
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(promotions)").fetchall()
            }
            if "salesperson" not in columns:
                db.execute("ALTER TABLE promotions ADD COLUMN salesperson TEXT NOT NULL DEFAULT ''")
            if "group_name" not in columns:
                db.execute("ALTER TABLE promotions ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")

    def upsert_promotion(
        self,
        *,
        token_id: int,
        store_name: str,
        application_id: str,
        seller_id: str,
        site_id: str,
        row: dict[str, Any],
        salesperson: str = "",
        group_name: str = "",
    ) -> int:
        promotion_id = str(row.get("id") or "").strip()
        promotion_type = str(row.get("type") or "UNKNOWN").strip().upper()
        if not promotion_id:
            # The users endpoint should provide an ID. Keep an explicit local
            # identity for malformed rows instead of silently merging them.
            promotion_id = f"LOCAL-{uuid.uuid4().hex}"
        now = _now()
        values = (
            int(token_id), str(store_name), str(salesperson or ""), str(group_name or ""),
            str(application_id or ""), str(seller_id),
            str(site_id).upper(), promotion_id, promotion_type,
            str(row.get("name") or promotion_id), str(row.get("status") or "unknown"),
            str(row.get("start_date") or ""), str(row.get("finish_date") or ""),
            str(row.get("deadline_date") or ""), _json(row), now,
        )
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO promotions (
                    token_id, store_name, salesperson, group_name,
                    application_id, seller_id, site_id,
                    promotion_id, promotion_type, name, status_raw, start_date,
                    finish_date, deadline_date, raw_json, last_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(application_id, seller_id, site_id, promotion_type, promotion_id)
                DO UPDATE SET token_id=excluded.token_id, store_name=excluded.store_name,
                    salesperson=excluded.salesperson, group_name=excluded.group_name,
                    name=excluded.name, status_raw=excluded.status_raw,
                    start_date=excluded.start_date, finish_date=excluded.finish_date,
                    deadline_date=excluded.deadline_date, raw_json=excluded.raw_json,
                    last_synced_at=excluded.last_synced_at
                """,
                values,
            )
            result = db.execute(
                """SELECT id FROM promotions WHERE application_id=? AND seller_id=?
                   AND site_id=? AND promotion_type=? AND promotion_id=?""",
                (str(application_id or ""), str(seller_id), str(site_id).upper(), promotion_type, promotion_id),
            ).fetchone()
            return int(result["id"])

    def replace_items(self, promotion_fk: int, rows: Iterable[dict[str, Any]]) -> int:
        prepared_by_key = {}
        now = _now()
        for raw in rows:
            row = dict(raw or {})
            status = row.get("status") or {}
            status_raw = status.get("id") if isinstance(status, dict) else status
            net = row.get("net_proceeds") or {}
            prepared = (
                int(promotion_fk), str(row.get("id") or row.get("item_id") or ""),
                str(row.get("offer_id") or ""), str(row.get("candidate_id") or ""),
                str(status_raw or "unknown"), str(row.get("title") or ""),
                str(row.get("thumbnail") or row.get("thumbnail_url") or ""),
                row.get("price"), row.get("original_price"), str(row.get("currency_id") or ""),
                row.get("min_discounted_price") or row.get("min_price"),
                row.get("max_discounted_price") or row.get("max_price"),
                _json(net), _json(row), now,
            )
            prepared_by_key[(prepared[1], prepared[2], prepared[3])] = prepared
        prepared = list(prepared_by_key.values())
        with self._connect() as db:
            db.execute("DELETE FROM promotion_items WHERE promotion_fk=?", (int(promotion_fk),))
            db.executemany(
                """INSERT INTO promotion_items (
                    promotion_fk, item_id, offer_id, candidate_id, status_raw,
                    title, thumbnail, price, original_price, currency_id, min_price,
                    max_price, net_proceeds_json, raw_json, last_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                prepared,
            )
        return len(prepared)

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("raw_json", "net_proceeds_json", "payload_json", "results_json"):
            if key in result:
                try:
                    result[key[:-5] if key.endswith("_json") else key] = json.loads(result.pop(key) or "null")
                except json.JSONDecodeError:
                    result[key[:-5]] = None
        return result

    def list_promotions(
        self, *, token_ids: Iterable[int] | None = None, search: str = "",
        status: str = "", promotion_type: str = "", salesperson: str = "",
        group_name: str = "", site_id: str = "",
        scope_pairs: Iterable[tuple[int, str]] | None = None,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        ids = [int(value) for value in token_ids or () if int(value or 0) > 0]
        if ids:
            clauses.append(f"token_id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        if search:
            clauses.append("(name LIKE ? OR promotion_id LIKE ? OR store_name LIKE ?)")
            value = f"%{search}%"
            params.extend((value, value, value))
        if status:
            clauses.append("status_raw=?")
            params.append(status)
        if promotion_type:
            clauses.append("promotion_type=?")
            params.append(promotion_type.upper())
        if scope_pairs is not None:
            # The salesperson/group assignment can change after an activity was
            # synced.  The caller can provide the current authorized
            # token/site pairs so historical activity snapshots remain
            # searchable without requiring another full sync.
            normalized_pairs = []
            seen_pairs = set()
            for token_id, current_site_id in scope_pairs:
                try:
                    pair = (int(token_id), str(current_site_id or "").strip().upper())
                except (TypeError, ValueError):
                    continue
                if pair[0] > 0 and pair[1] and pair not in seen_pairs:
                    seen_pairs.add(pair)
                    normalized_pairs.append(pair)
            if not normalized_pairs:
                clauses.append("1=0")
            else:
                clauses.append(
                    "(" + " OR ".join(
                        "(token_id=? AND site_id=?)" for _ in normalized_pairs
                    ) + ")"
                )
                for token_id, current_site_id in normalized_pairs:
                    params.extend((token_id, current_site_id))
        else:
            if salesperson:
                clauses.append("salesperson=?")
                params.append(salesperson)
            if group_name:
                clauses.append("group_name=?")
                params.append(group_name)
            if site_id:
                clauses.append("site_id=?")
                params.append(site_id.upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT p.*,
                    COUNT(i.id) AS item_count,
                    SUM(CASE WHEN i.status_raw='candidate' THEN 1 ELSE 0 END) AS candidate_count,
                    SUM(CASE WHEN i.status_raw IN ('started','active','pending','programmed') THEN 1 ELSE 0 END) AS active_item_count
                    FROM promotions p LEFT JOIN promotion_items i ON i.promotion_fk=p.id
                    {where} GROUP BY p.id
                    ORDER BY CASE p.status_raw WHEN 'started' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                             p.deadline_date ASC, p.id DESC""",
                params,
            ).fetchall()
        return [self._row(row) for row in rows]

    def get_promotion(self, promotion_fk: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM promotions WHERE id=?", (int(promotion_fk),)).fetchone()
        return self._row(row) if row else None

    def list_items(self, promotion_fk: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM promotion_items WHERE promotion_fk=? ORDER BY status_raw, item_id",
                (int(promotion_fk),),
            ).fetchall()
        return [self._row(row) for row in rows]

    def create_preview(self, *, actor: str, action: str, promotion_fk: int, payload: dict[str, Any], expires_at: str) -> str:
        preview_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO promotion_previews VALUES (?, ?, ?, ?, ?, ?, ?)",
                (preview_id, actor, action, int(promotion_fk), _json(payload), expires_at, _now()),
            )
        return preview_id

    def get_preview(self, preview_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM promotion_previews WHERE preview_id=?", (str(preview_id),)).fetchone()
        return self._row(row) if row else None

    def create_job(self, *, preview_id: str, actor: str, action: str, total: int) -> str:
        job_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                """INSERT INTO promotion_jobs
                   (job_id, preview_id, actor, action, status, total_count, created_at)
                   VALUES (?, ?, ?, ?, 'running', ?, ?)""",
                (job_id, preview_id, actor, action, int(total), _now()),
            )
        return job_id

    def finish_job(self, job_id: str, *, results: list[dict[str, Any]]) -> None:
        success = sum(1 for row in results if row.get("status") == "succeeded")
        unknown = sum(1 for row in results if row.get("status") == "unknown")
        failed = len(results) - success - unknown
        status = "succeeded" if success == len(results) else ("failed" if not success and not unknown else "partial")
        with self._connect() as db:
            db.execute(
                """UPDATE promotion_jobs SET status=?, success_count=?, failed_count=?,
                   unknown_count=?, results_json=?, finished_at=? WHERE job_id=?""",
                (status, success, failed, unknown, _json(results), _now(), str(job_id)),
            )

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM promotion_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._row(row) for row in rows]
