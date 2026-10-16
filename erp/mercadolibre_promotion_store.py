"""Small durable SQLite store for the promotion workbench.

The production application already uses MySQL for core ERP data. Promotion
sync and execution records are kept in an isolated SQLite database so a partial
API page or a failed rollout cannot overwrite existing store-link records.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


APPLIED_STATUSES = ("pending_approval", "pending", "programmed", "scheduled", "started", "active", "approved")
OFFER_IDENTITY_TYPES = frozenset({"SMART", "PRICE_MATCHING"})
CURRENT_CAMPAIGN_SQL = """LOWER(p.status_raw) NOT IN ('finished','ended','cancelled','canceled','deleted')
    AND (p.finish_date='' OR julianday(p.finish_date) IS NULL OR julianday(p.finish_date)>julianday('now'))"""


def discount_percent(price, original_price):
    try:
        price, original = float(price), float(original_price)
        if math.isfinite(price) and math.isfinite(original) and 0 < price <= original:
            return round((1 - price / original) * 100, 2)
    except (ValueError, TypeError):
        pass
    return None


DEFAULT_PATH = Path(__file__).resolve().parents[1] / ".data" / "promotions.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _precise_now() -> str:
    """Timestamp item mutations precisely enough to order concurrent reads."""
    return datetime.now(timezone.utc).isoformat()


def snapshot_started_after(snapshot_started_at: str, completed_at: str) -> bool:
    """Compare an official snapshot against a persisted completion safely.

    Historical job timestamps only have second precision. Treat that recorded
    second as incomplete until its upper bound has passed, otherwise a snapshot
    that actually began earlier in the same second could clear a submit-once
    guard.
    """
    if not snapshot_started_at or not completed_at:
        return False
    try:
        snapshot = datetime.fromisoformat(str(snapshot_started_at).replace("Z", "+00:00"))
        completed = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
        if snapshot.tzinfo is None:
            snapshot = snapshot.replace(tzinfo=timezone.utc)
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        completed_text = str(completed_at).split("T", 1)[-1]
        if "." not in completed_text:
            completed += timedelta(seconds=1)
        return snapshot > completed
    except (TypeError, ValueError):
        return False


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

                CREATE TABLE IF NOT EXISTS promotion_sync_scopes (
                    token_id INTEGER NOT NULL,
                    site_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    PRIMARY KEY(token_id, site_id)
                );

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
                    blocked_count INTEGER NOT NULL DEFAULT 0,
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
            job_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(promotion_jobs)").fetchall()
            }
            if "blocked_count" not in job_columns:
                db.execute("ALTER TABLE promotion_jobs ADD COLUMN blocked_count INTEGER NOT NULL DEFAULT 0")

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

    def replace_items(
        self, promotion_fk: int, rows: Iterable[dict[str, Any]], *,
        item_id: str | None = None, snapshot_started_at: str = "",
    ) -> int:
        """Replace an official item snapshot.

        ``snapshot_started_at`` is deliberately retained instead of the later
        database-write time.  A candidate row can only clear a submit-once
        enrollment guard when the authoritative read itself began after the
        guarded job finished.
        """
        prepared_by_key = {}
        # Rows written outside an explicit remote snapshot still need precise
        # ordering: a new snapshot may start later in the same second.
        now = str(snapshot_started_at or _precise_now())
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
        snapshot_count = len(prepared)
        with self._connect() as db:
            # The remote read can take minutes.  Serialize only this short
            # local merge, then retain rows changed after that read began so a
            # successful enrollment/withdrawal cannot be reverted by an older
            # candidate/active snapshot.  BEGIN IMMEDIATE also closes the gap
            # between selecting protected rows and deleting stale rows.
            db.execute("BEGIN IMMEDIATE")
            protected_ids: set[int] = set()
            protected_keys: set[tuple[str, str, str]] = set()
            current_rows = []
            if snapshot_started_at:
                scope_sql = "promotion_fk=?" + (" AND item_id=?" if item_id is not None else "")
                scope_params = (int(promotion_fk),) + ((str(item_id),) if item_id is not None else ())
                current_rows = db.execute(
                    "SELECT id, item_id, offer_id, candidate_id, last_synced_at "
                    "FROM promotion_items WHERE " + scope_sql,
                    scope_params,
                ).fetchall()
                for current in current_rows:
                    written_at = str(current["last_synced_at"] or "")
                    if written_at and not snapshot_started_after(snapshot_started_at, written_at):
                        protected_ids.add(int(current["id"]))
                        protected_keys.add((
                            str(current["item_id"] or ""),
                            str(current["offer_id"] or ""),
                            str(current["candidate_id"] or ""),
                        ))
            if protected_ids:
                # Delete the stale side of the merge in bounded batches.  A
                # campaign can contain tens of thousands of rows, so placing
                # every protected ID in one NOT IN clause would exceed older
                # SQLite builds' bind-parameter limit.
                deletable_ids = [
                    int(current["id"])
                    for current in current_rows
                    if int(current["id"]) not in protected_ids
                ]
                for offset in range(0, len(deletable_ids), 500):
                    batch = deletable_ids[offset:offset + 500]
                    db.execute(
                        f"DELETE FROM promotion_items WHERE id IN ({','.join('?' for _ in batch)})",
                        batch,
                    )
            else:
                delete_sql = "DELETE FROM promotion_items WHERE promotion_fk=?"
                delete_params: list[Any] = [int(promotion_fk)]
                if item_id is not None:
                    delete_sql += " AND item_id=?"
                    delete_params.append(str(item_id))
                db.execute(delete_sql, delete_params)
            if item_id is not None and any(row[1] != item_id for row in prepared):
                raise ValueError("单商品活动同步不能写入其他商品")
            prepared = [
                row for row in prepared
                if (row[1], row[2], row[3]) not in protected_keys
            ]
            db.executemany(
                """INSERT INTO promotion_items (
                    promotion_fk, item_id, offer_id, candidate_id, status_raw,
                    title, thumbnail, price, original_price, currency_id, min_price,
                    max_price, net_proceeds_json, raw_json, last_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                prepared,
            )
        return snapshot_count

    def ensure_listing_promotion(self, **fields):
        """Discover a campaign without treating item status as campaign status."""
        row = dict(fields["row"])
        with self._connect() as db:
            existing = db.execute(
                "SELECT id FROM promotions WHERE application_id=? AND seller_id=? AND site_id=? AND promotion_type=? AND promotion_id=?",
                (fields["application_id"], fields["seller_id"], fields["site_id"], row["type"], row["id"]),
            ).fetchone()
        if existing:
            return int(existing["id"])
        row["status"] = "unknown"
        return self.upsert_promotion(**dict(fields, row=row))

    def refresh_item_activity(
        self, promotion_fk, item_id, activity, *, snapshot_started_at: str = "",
    ):
        """Item-level discovery omits offer prices; retain the fuller snapshot."""
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM promotion_items WHERE promotion_fk=? AND item_id=?",
                               (int(promotion_fk), item_id)).fetchone()[0]
        if count:
            self.set_item_status(
                promotion_fk, item_id, str(activity.get("status") or "unknown"),
                snapshot_started_at=snapshot_started_at,
            )
        else:
            self.replace_items(
                promotion_fk, [dict(activity, id=item_id)], item_id=item_id,
                snapshot_started_at=snapshot_started_at,
            )

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
        group_name: str = "", site_id: str = "", participated: str = "",
        scope_pairs: Iterable[tuple[int, str]] | None = None,
        current_participation: bool = False,
        include_operation_summary: bool = True,
    ) -> list[dict[str, Any]]:
        participated = str(participated or "").strip()
        if participated not in {"", "1", "0"}:
            raise ValueError("活动参与状态无效")
        # Keep successful participation visible after withdrawal or campaign expiry.
        participation_statuses = ",".join(f"'{value}'" for value in (*APPLIED_STATUSES, "finished", "withdrawn"))
        current_participation_sql = f"""EXISTS (
            SELECT 1 FROM promotion_items pi WHERE pi.promotion_fk=p.id
                AND LOWER(pi.status_raw) IN ({','.join(repr(value) for value in APPLIED_STATUSES)})
                AND {CURRENT_CAMPAIGN_SQL})"""
        participation_sql = current_participation_sql if current_participation else f"""(
            EXISTS (SELECT 1 FROM promotion_items pi WHERE pi.promotion_fk=p.id
                AND LOWER(pi.status_raw) IN ({participation_statuses}))
            OR EXISTS (SELECT 1 FROM promotion_previews pv
                JOIN promotion_jobs pj ON pj.preview_id=pv.preview_id
                JOIN json_each(pj.results_json) entry
                WHERE pv.promotion_fk=p.id AND pj.action IN ('enroll','withdraw') AND entry.type='object'
                AND json_extract(entry.value, '$.status')='succeeded')
        )"""
        clauses, params = [], []
        if participated:
            clauses.append(("NOT " if participated == "0" else "") + participation_sql)
        ids = [int(value) for value in token_ids or () if int(value or 0) > 0]
        if ids:
            clauses.append(f"token_id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        if search:
            clauses.append("(name LIKE ? OR promotion_id LIKE ? OR store_name LIKE ?)")
            value = f"%{search}%"
            params.extend((value, value, value))
        if status:
            clauses.append("p.status_raw=?")
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
                f"""SELECT p.*, {participation_sql} AS participated,
                    COUNT(i.id) AS item_count,
                    COUNT(DISTINCT CASE WHEN LOWER(i.status_raw)='candidate' AND ({CURRENT_CAMPAIGN_SQL}) THEN i.item_id END) AS candidate_count,
                    COUNT(DISTINCT CASE WHEN LOWER(i.status_raw) IN ({','.join(repr(value) for value in APPLIED_STATUSES)}) AND ({CURRENT_CAMPAIGN_SQL}) THEN i.item_id END) AS active_item_count,
                    MAX(i.last_synced_at) AS item_counts_synced_at
                    FROM promotions p LEFT JOIN promotion_items i ON i.promotion_fk=p.id
                    {where} GROUP BY p.id
                    ORDER BY CASE p.status_raw WHEN 'started' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                             p.deadline_date ASC, p.id DESC""",
                params,
            ).fetchall()
        result = [self._row(row) for row in rows]
        if not include_operation_summary:
            return result
        outcomes = self._enrollment_results_by_promotion(row["id"] for row in result)
        stop_summaries = {}
        if result:
            with self._connect() as db:
                latest = db.execute("""SELECT * FROM (
                    SELECT v.promotion_fk, j.status, j.success_count, j.failed_count, j.unknown_count, j.blocked_count,
                        ROW_NUMBER() OVER (PARTITION BY v.promotion_fk ORDER BY j.created_at DESC, j.rowid DESC) AS rank
                    FROM promotion_jobs j JOIN promotion_previews v ON v.preview_id=j.preview_id
                    WHERE j.action='withdraw') WHERE rank=1""").fetchall()
                stop_summaries = {row['promotion_fk']: dict(row) for row in latest}
        for row in result:
            row["enrollment_summary"] = self._enrollment_summary(outcomes.get(row["id"], []))
            row["stop_summary"] = stop_summaries.get(row["id"])
        return result

    def get_promotion(self, promotion_fk: int, *, include_summary: bool = True) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM promotions WHERE id=?", (int(promotion_fk),)).fetchone()
        if not row:
            return None
        result = self._row(row)
        if include_summary:
            result["enrollment_summary"] = self.enrollment_summary(promotion_fk)
        return result

    def list_items(self, promotion_fk: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM promotion_items WHERE promotion_fk=? ORDER BY status_raw, item_id",
                (int(promotion_fk),),
            ).fetchall()
        outcomes = {
            (row["item_id"], str(row.get("offer_id") or "")): row
            for row in self.enrollment_results(promotion_fk)
        }
        result = [self._row(row) for row in rows]
        for row in result:
            row["enrollment_result"] = outcomes.get(
                (row["item_id"], str(row.get("offer_id") or ""))
            )
        return result

    def list_items_by_local_ids(self, promotion_fk: int, item_local_ids: Iterable[int]) -> list[dict[str, Any]]:
        """Read only explicitly selected local rows without scanning job history."""
        ids = []
        seen = set()
        for value in item_local_ids:
            try:
                item_local_id = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if item_local_id > 0 and item_local_id not in seen:
                seen.add(item_local_id)
                ids.append(item_local_id)
        if not ids:
            return []
        rows = []
        with self._connect() as db:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                rows.extend(db.execute(
                    "SELECT * FROM promotion_items WHERE promotion_fk=? AND id IN ("
                    + ",".join("?" for _ in batch) + ")",
                    (int(promotion_fk), *batch),
                ).fetchall())
        return [self._row(row) for row in rows]

    @staticmethod
    def _page_number(value: Any, default: int, maximum: int | None = None) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            number = default
        number = max(1, number)
        return min(number, maximum) if maximum is not None else number

    def list_items_page(
        self, promotion_fk: int, *, page: int = 1, page_size: int = 50,
        target_item_id: str = "", enrolled: str = "", product: str = "", item_id: str = "",
        selection_only: bool = False, candidate_only: bool = False,
    ) -> dict[str, Any]:
        """Fetch only the requested product page before attaching enrollment results."""
        page_size = self._page_number(page_size, 50, 500 if selection_only else 100)
        page = self._page_number(page, 1)
        promotion_fk = int(promotion_fk)
        enrolled = str(enrolled or "").strip()
        if enrolled not in {"", "1", "0"}:
            raise ValueError("报名筛选无效")
        status_sql = ",".join(f"'{value}'" for value in APPLIED_STATUSES)
        current_sql = (
            f"LOWER(status_raw) IN ({status_sql}) AND EXISTS ("
            "SELECT 1 FROM promotions p WHERE p.id=promotion_fk AND "
            + CURRENT_CAMPAIGN_SQL + ")"
        )
        condition = "promotion_fk=?"
        if candidate_only:
            condition += " AND LOWER(status_raw)='candidate'"
        if enrolled:
            condition += f" AND {'NOT ' if enrolled == '0' else ''}({current_sql})"
        params = [promotion_fk]
        for column, value in (("title", product), ("item_id", item_id)):
            value = str(value or "").strip()
            if value:
                condition += f" AND INSTR(LOWER({column}), LOWER(?)) > 0"
                params.append(value)
        with self._connect() as db:
            total = int(db.execute(
                "SELECT COUNT(*) FROM promotion_items WHERE " + condition,
                params,
            ).fetchone()[0])
            pages = max(1, (total + page_size - 1) // page_size)
            target = str(target_item_id or "").strip()
            if target:
                # Prefer an enrollable candidate, then the first ordinary row.
                # The local ID breaks ties for repeated offer/candidate identities.
                selected = db.execute(
                    f"""SELECT status_raw, item_id, id FROM promotion_items
                    WHERE {condition} AND item_id=? COLLATE NOCASE
                    ORDER BY CASE WHEN LOWER(status_raw)='candidate' THEN 0 ELSE 1 END,
                        status_raw, item_id, id LIMIT 1""",
                    (*params, target),
                ).fetchone()
                if selected:
                    preceding = int(db.execute(
                        f"""SELECT COUNT(*) FROM promotion_items WHERE {condition}
                        AND (status_raw, item_id, id) < (?, ?, ?)""",
                        (*params, selected["status_raw"], selected["item_id"], selected["id"]),
                    ).fetchone()[0])
                    page = preceding // page_size + 1
            page = min(page, pages)
            columns = "id, promotion_fk, item_id, offer_id, status_raw, price, original_price, min_price, max_price, currency_id" if selection_only else "*"
            rows = db.execute(
                f"""SELECT {columns}, ({current_sql}) AS enrolled FROM promotion_items WHERE {condition}
                ORDER BY status_raw, item_id, id LIMIT ? OFFSET ?""",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        result = [self._row(row) for row in rows]
        outcomes = {
            (row["item_id"], str(row.get("offer_id") or "")): row
            for row in ([] if selection_only else self.enrollment_results(promotion_fk, item_ids=[row["item_id"] for row in result]))
        }
        for row in result:
            row["enrollment_result"] = outcomes.get(
                (row["item_id"], str(row.get("offer_id") or ""))
            )
            row["enrolled"] = bool(row["enrolled"])
            row["current_discount_percent"] = discount_percent(row.get("price"), row.get("original_price")) if row["enrolled"] else None
        return {"rows": result, "page": page, "page_size": page_size, "total": total, "pages": pages}

    @staticmethod
    def _enrollment_summary(results: Iterable[dict[str, Any]]) -> dict[str, int]:
        summary = {"succeeded": 0, "failed": 0, "unknown": 0, "total": 0}
        for row in results:
            status = row.get("status")
            summary[status if status in {"succeeded", "failed", "unknown"} else "unknown"] += 1
            summary["total"] += 1
        return summary

    def _enrollment_results_by_promotion(self, promotion_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
        """Latest enrollment per platform item/offer, retained across snapshots."""
        ids = list(dict.fromkeys(int(value) for value in promotion_ids))
        outcomes: dict[int, dict[tuple[str, str], dict[str, Any]]] = {}
        with self._connect() as db:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                jobs = db.execute(
                    """SELECT j.*, v.promotion_fk FROM promotion_jobs j
                    JOIN promotion_previews v ON v.preview_id=j.preview_id
                    WHERE j.action='enroll' AND v.promotion_fk IN ("""
                    + ",".join("?" for _ in batch)
                    + ") ORDER BY j.created_at DESC, j.rowid DESC",
                    batch,
                ).fetchall()
                for stored in jobs:
                    job = self._row(stored)
                    bucket = outcomes.setdefault(job["promotion_fk"], {})
                    for raw in reversed(job.get("results") or []):
                        if not isinstance(raw, dict):
                            continue
                        item_id = str(raw.get("item_id") or "")
                        identity = (item_id, str(raw.get("offer_id") or ""))
                        if not item_id or identity in bucket:
                            continue
                        bucket[identity] = dict(
                            raw, item_id=item_id, offer_id=identity[1], job_id=job["job_id"],
                            created_at=job["created_at"], finished_at=job["finished_at"],
                        )
        return {key: list(value.values()) for key, value in outcomes.items()}

    @staticmethod
    def _latest_enrollment_sql(promotion_fk: int, item_ids=None, *, statuses=None):
        clauses = ["j.action='enroll'", "v.promotion_fk=?", "entry.type='object'"]
        params: list[Any] = [int(promotion_fk)]
        item_expression = "CAST(json_extract(entry.value, '$.item_id') AS TEXT)"
        offer_expression = "CAST(COALESCE(json_extract(entry.value, '$.offer_id'), '') AS TEXT)"
        clauses.append(f"COALESCE({item_expression}, '') != ''")
        if item_ids is not None:
            ids = list(dict.fromkeys(str(value) for value in item_ids if str(value or "")))
            if not ids:
                clauses.append("0")
            else:
                clauses.append(f"{item_expression} IN ({','.join('?' for _ in ids)})")
                params.extend(ids)
        if statuses is not None:
            selected_statuses = list(dict.fromkeys(str(value) for value in statuses if str(value or "")))
            if not selected_statuses:
                clauses.append("0")
            else:
                clauses.append(
                    "CAST(json_extract(entry.value, '$.status') AS TEXT) IN ("
                    + ",".join("?" for _ in selected_statuses) + ")"
                )
                params.extend(selected_statuses)
        sql = f"""WITH ranked AS (
            SELECT entry.value AS result_json,
                CAST(json_extract(entry.value, '$.item_id') AS TEXT) AS item_id,
                CAST(COALESCE(json_extract(entry.value, '$.offer_id'), '') AS TEXT) AS offer_id,
                j.job_id, j.created_at, j.finished_at, j.rowid AS job_order,
                CAST(entry.key AS INTEGER) AS result_order,
                ROW_NUMBER() OVER (
                    PARTITION BY {item_expression}, {offer_expression}
                    ORDER BY j.created_at DESC, j.rowid DESC, CAST(entry.key AS INTEGER) DESC
                ) AS result_rank
            FROM promotion_jobs j
            JOIN promotion_previews v ON v.preview_id=j.preview_id
            JOIN json_each(CASE WHEN json_valid(j.results_json)
                THEN j.results_json ELSE '[]' END) entry
            WHERE """ + " AND ".join(clauses) + "\n) "
        return sql, params

    def enrollment_summary(self, promotion_fk: int) -> dict[str, int]:
        """Aggregate retained results in SQLite without loading every result payload."""
        sql, params = self._latest_enrollment_sql(promotion_fk)
        with self._connect() as db:
            rows = db.execute(sql + """SELECT
                CASE WHEN json_extract(result_json, '$.status') IN ('succeeded','failed','unknown')
                    THEN json_extract(result_json, '$.status') ELSE 'unknown' END AS status,
                COUNT(*) AS result_count FROM ranked WHERE result_rank=1 GROUP BY status""", params).fetchall()
        summary = {"succeeded": 0, "failed": 0, "unknown": 0, "total": 0}
        for row in rows:
            summary[row["status"]] = int(row["result_count"])
            summary["total"] += int(row["result_count"])
        return summary

    def enrollment_results(
        self, promotion_fk: int, *, item_ids: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sql, params = self._latest_enrollment_sql(promotion_fk, item_ids)
        sql += "SELECT * FROM ranked WHERE result_rank=1 ORDER BY created_at DESC, job_order DESC, result_order DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(self._page_number(limit, 50, 100))
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(
            json.loads(row["result_json"]), item_id=row["item_id"], offer_id=row["offer_id"], job_id=row["job_id"],
            created_at=row["created_at"], finished_at=row["finished_at"],
        ) for row in rows]

    def enrollment_submission_guards(
        self, promotion_fk: int, *, item_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the latest ambiguous or successful submit per item/offer.

        Definite failures and local preflight exclusions carry no evidence that
        an older remote submit did not apply, so they intentionally do not
        supersede these guards.
        """
        ids = None if item_ids is None else list(dict.fromkeys(
            str(value) for value in item_ids if str(value or "")
        ))
        if ids == []:
            return []
        batches = [None] if ids is None else [
            ids[offset:offset + 500] for offset in range(0, len(ids), 500)
        ]
        result = []
        selected_ids = None if ids is None else set(ids)
        with self._connect() as db:
            for batch in batches:
                sql, params = self._latest_enrollment_sql(
                    promotion_fk, batch, statuses=("unknown", "succeeded"),
                )
                sql += """SELECT * FROM ranked WHERE result_rank=1
                    ORDER BY created_at DESC, job_order DESC, result_order DESC"""
                result.extend(db.execute(sql, params).fetchall())
            incomplete = db.execute(
                """SELECT j.*, j.rowid AS job_order, p.payload_json
                FROM promotion_jobs j
                JOIN promotion_previews p ON p.preview_id=j.preview_id
                WHERE p.promotion_fk=? AND j.action='enroll'
                  AND j.status IN ('succeeded','partial','failed','skipped')
                  AND (
                    j.total_count > (j.success_count+j.failed_count+j.unknown_count+j.blocked_count)
                    OR j.total_count > json_array_length(
                        CASE WHEN json_valid(j.results_json) THEN j.results_json ELSE '[]' END
                    )
                  )
                ORDER BY j.created_at DESC, j.rowid DESC""",
                (int(promotion_fk),),
            ).fetchall()

        candidates = []
        for row in result:
            candidates.append((
                (str(row["created_at"]), int(row["job_order"]), int(row["result_order"])),
                dict(
                    json.loads(row["result_json"]), item_id=row["item_id"],
                    offer_id=row["offer_id"], job_id=row["job_id"],
                    created_at=row["created_at"], finished_at=row["finished_at"],
                ),
            ))

        # Older versions could leave terminal jobs with fewer result rows than
        # their durable preview. Treat every missing ready row as ambiguous so
        # an upgrade cannot blindly POST it again.
        for job in incomplete:
            try:
                preview = json.loads(job["payload_json"] or "{}")
                stored_results = json.loads(job["results_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue

            def local_identity(row):
                return (
                    row.get("item_local_id"), str(row.get("item_id") or ""),
                    str(row.get("offer_id") or ""),
                )

            completed = {}
            for stored in stored_results if isinstance(stored_results, list) else ():
                if not isinstance(stored, dict):
                    continue
                key = local_identity(stored)
                completed[key] = completed.get(key, 0) + 1
            for order, ready in enumerate(
                preview.get("rows") if isinstance(preview, dict) else ()
            ):
                if not isinstance(ready, dict) or ready.get("status") != "ready":
                    continue
                item_id = str(ready.get("item_id") or "")
                if not item_id or (selected_ids is not None and item_id not in selected_ids):
                    continue
                key = local_identity(ready)
                if completed.get(key, 0):
                    completed[key] -= 1
                    continue
                candidates.append((
                    (str(job["created_at"]), int(job["job_order"]), order),
                    dict(
                        ready, item_id=item_id,
                        offer_id=str(ready.get("offer_id") or ""), status="unknown",
                        message="旧任务缺少执行结果，按结果未知保护，未重复提交",
                        job_id=job["job_id"], created_at=job["created_at"],
                        finished_at=job["finished_at"],
                    ),
                ))

        latest = {}
        for order, guard in sorted(candidates, key=lambda value: value[0], reverse=True):
            identity = (guard["item_id"], str(guard.get("offer_id") or ""))
            latest.setdefault(identity, guard)
        return list(latest.values())

    def unresolved_enrollment_results(
        self, promotion_fk: int, *, item_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return unknown submissions not superseded by a confirmed success.

        Local preflight exclusions and definite failures never prove that an
        earlier ambiguous remote request did not take effect, so they must not
        remove the submit-once guard for that item.
        """
        ids = None if item_ids is None else list(dict.fromkeys(
            str(value) for value in item_ids if str(value or "")
        ))
        if ids == []:
            return []
        batches = [None] if ids is None else [ids[offset:offset + 500] for offset in range(0, len(ids), 500)]
        result = []
        with self._connect() as db:
            for batch in batches:
                sql, params = self._latest_enrollment_sql(
                    promotion_fk, batch, statuses=("unknown", "succeeded"),
                )
                sql += """SELECT * FROM ranked WHERE result_rank=1
                    AND json_extract(result_json, '$.status')='unknown'
                    ORDER BY created_at DESC, job_order DESC, result_order DESC"""
                result.extend(db.execute(sql, params).fetchall())
        return [dict(
            json.loads(row["result_json"]), item_id=row["item_id"], offer_id=row["offer_id"], job_id=row["job_id"],
            created_at=row["created_at"], finished_at=row["finished_at"],
        ) for row in result]

    def reconcile_completed_jobs(self, promotion_fk: int, official_items: list[dict[str, Any]], *, snapshot_started_at: str = "") -> int:
        """Resolve latest ambiguous results only from a complete fresh API snapshot."""
        resolved = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            promotion = db.execute(
                "SELECT promotion_type FROM promotions WHERE id=?", (int(promotion_fk),)
            ).fetchone()
            offer_scoped = bool(
                promotion
                and str(promotion["promotion_type"] or "").upper() in OFFER_IDENTITY_TYPES
            )

            def identity(row):
                return (
                    str(row.get("id") or row.get("item_id") or ""),
                    str(row.get("offer_id") or "") if offer_scoped else "",
                )

            by_identity = {}
            for raw in official_items:
                by_identity.setdefault(identity(raw), []).append(raw)
            jobs = db.execute("""SELECT j.* FROM promotion_jobs j
                JOIN promotion_previews p ON p.preview_id=j.preview_id
                WHERE p.promotion_fk=? ORDER BY j.created_at DESC, j.rowid DESC""", (int(promotion_fk),)).fetchall()
            seen = set()
            for job in jobs:
                results = json.loads(job["results_json"] or "[]")
                changed = False
                for result in reversed(results):
                    item_id = str(result.get("item_id") or "")
                    # A row without an item identity describes a campaign-level
                    # verification failure, not a remotely confirmed item.
                    # Item snapshots cannot prove that the whole withdrawal
                    # succeeded, even when no row happens to match ('', '').
                    if not item_id:
                        continue
                    result_identity = (
                        item_id,
                        str(result.get("offer_id") or "") if offer_scoped else "",
                    )
                    if result_identity in seen:
                        continue
                    result_status = result.get("status")
                    # Only a confirmed success supersedes an older ambiguous
                    # submission. A local exclusion/definite failure carries no
                    # information about whether that older remote call applied.
                    if result_status not in {"unknown", "succeeded"}:
                        continue
                    seen.add(result_identity)
                    if (
                        job["status"] == "running" or result_status != "unknown"
                        or snapshot_started_at and not snapshot_started_after(
                            snapshot_started_at, str(job["finished_at"]),
                        )
                    ):
                        continue
                    items = by_identity.get(result_identity, [])
                    def status(item):
                        value = item.get("status")
                        return str(value.get("id") if isinstance(value, dict) else value or "").lower()
                    confirmed = False
                    if job["action"] == "withdraw":
                        confirmed = not items or all(
                            status(item) in {"candidate", "finished", "withdrawn", "cancelled", "canceled"}
                            for item in items
                        )
                    else:
                        for item in items:
                            if status(item) not in APPLIED_STATUSES:
                                continue
                            price = result.get("deal_price")
                            if price is not None:
                                try:
                                    if abs(float(item.get("price")) - float(price)) >= .005:
                                        continue
                                except (TypeError, ValueError):
                                    continue
                            if item.get("currency_id") and result.get("currency_id") and item["currency_id"] != result["currency_id"]:
                                continue
                            confirmed = True
                            break
                    if confirmed:
                        result.update(status="succeeded", message="已依据最新官方同步核实成功")
                        resolved += 1
                        changed = True
                if changed:
                    success = sum(row.get("status") == "succeeded" for row in results)
                    unknown = sum(row.get("status") == "unknown" for row in results)
                    blocked = sum(row.get("status") == "blocked" for row in results)
                    failed = len(results) - success - unknown - blocked
                    status_value = ("succeeded" if success else "skipped") if not failed and not unknown else ("failed" if failed and not success and not unknown and not blocked else "partial")
                    db.execute("UPDATE promotion_jobs SET results_json=?, success_count=?, failed_count=?, unknown_count=?, blocked_count=?, status=? WHERE job_id=?",
                               (_json(results), success, failed, unknown, blocked, status_value, job["job_id"]))
        return resolved

    def latest_job(self, promotion_fk: int, *, result_limit: int | None = None) -> dict[str, Any] | None:
        rows = self.list_jobs(limit=1, promotion_fk=promotion_fk, result_limit=result_limit)
        return rows[0] if rows else None

    def applied_item_details(
        self, keys: Iterable[tuple[int, str, str]]
    ) -> dict[tuple[int, str, str], list[dict[str, Any]]]:
        """Return listing identities currently enrolled in a synced activity."""
        return self._item_activity_details(keys, (
            "pending_approval", "pending", "programmed", "scheduled",
            "started", "active", "approved",
        ))

    def available_item_details(self, keys):
        """Platform candidates only; enrollment is never inferred from the parent."""
        return self._item_activity_details(keys, ("candidate",))

    def _item_activity_details(self, keys, applied_statuses):
        normalized = []
        for token_id, site_id, item_id in keys or ():
            try:
                identity = (
                    int(token_id),
                    str(site_id or "").strip().upper(),
                    str(item_id or "").strip().upper(),
                )
            except (TypeError, ValueError):
                continue
            if identity[0] > 0 and identity[1] and identity[2]:
                normalized.append(identity)
        normalized = list(dict.fromkeys(normalized))
        if not normalized:
            return {}
        # Candidate means that the platform is offering the product the chance
        # to enroll; it is deliberately excluded from the applied marker.
        status_sql = ",".join("?" for _ in applied_statuses)
        result = {}
        with self._connect() as db:
            # Bound SQLite expression depth and parameter count for large pages.
            for offset in range(0, len(normalized), 200):
                batch = normalized[offset:offset + 200]
                clauses = " OR ".join(
                    "(p.token_id=? AND p.site_id=? AND i.item_id=?)" for _ in batch
                )
                params = [value for identity in batch for value in identity]
                rows = db.execute(
                    f"""SELECT p.token_id, p.site_id, i.item_id,
                        p.id AS promotion_fk, p.promotion_id, p.name, p.promotion_type,
                        i.id AS item_local_id, i.offer_id, i.status_raw, i.price, i.original_price, i.currency_id, i.last_synced_at
                    FROM promotions p JOIN promotion_items i ON i.promotion_fk=p.id
                    WHERE ({clauses}) AND LOWER(i.status_raw) IN ({status_sql})
                        AND LOWER(p.status_raw) NOT IN ('finished','ended','cancelled','canceled','deleted')
                        AND (p.finish_date='' OR julianday(p.finish_date) IS NULL
                             OR julianday(p.finish_date) > julianday('now'))
                    ORDER BY p.id, i.id""",
                    params + list(applied_statuses),
                ).fetchall()
                for row in rows:
                    detail = dict(row)
                    detail["current_discount_percent"] = discount_percent(detail.get("price"), detail.get("original_price"))
                    identity = (detail.pop("token_id"), detail.pop("site_id"), detail.pop("item_id"))
                    result.setdefault(identity, []).append(detail)
        return result

    def record_scope_sync(self, token_id, site_id, status, promotion_ids=()):
        """Only a complete successful snapshot can prove non-enrollment."""
        with self._connect() as db:
            if status == 'succeeded':
                # Expire activities absent from the new complete platform snapshot.
                ids = list(dict.fromkeys(str(value) for value in promotion_ids))
                condition = f" AND promotion_id NOT IN ({','.join('?' for _ in ids)})" if ids else ''
                db.execute("UPDATE promotions SET status_raw='finished' WHERE token_id=? AND site_id=?" + condition,
                           [int(token_id), str(site_id).upper(), *ids])
            db.execute('INSERT OR REPLACE INTO promotion_sync_scopes VALUES (?,?,?,?)',
                       (int(token_id), str(site_id).upper(), status, _now()))

    def clearance_item_states(self, keys, *, details=None):
        keys = list(dict.fromkeys(keys))
        if details is None:
            details = self.applied_item_details(keys)
        with self._connect() as db:
            scopes = {(r['token_id'], r['site_id']): dict(r)
                      for r in db.execute('SELECT * FROM promotion_sync_scopes')}
        states = {}
        for key in keys:
            scope = scopes.get(key[:2], {})
            activities = [r for r in details.get(key, [])
                          if str(r.get('promotion_type') or '').upper() == 'CLEARANCE']
            checked_at = scope.get('checked_at', '')
            fresh = False
            if checked_at:
                try:
                    fresh = 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(checked_at)).total_seconds() < 86400
                except (TypeError, ValueError):
                    pass
            status = ('enrolled' if activities else 'not_enrolled') if scope.get('status') == 'succeeded' and fresh else 'unknown'
            states[key] = {'status': status, 'checked_at': checked_at, 'activities': activities}
        return states

    def applied_item_keys(
        self, keys: Iterable[tuple[int, str, str]]
    ) -> set[tuple[int, str, str]]:
        return set(self.applied_item_details(keys))

    def all_applied_item_keys(
        self,
        *,
        token_ids: Iterable[int] | None = None,
        site_id: str = "",
    ) -> set[tuple[int, str, str]]:
        """Return all listing identities currently enrolled in a synced activity."""

        applied_statuses = (
            "pending_approval", "pending", "programmed", "scheduled",
            "started", "active", "approved",
        )
        clauses = [f"LOWER(i.status_raw) IN ({','.join('?' for _ in applied_statuses)})", CURRENT_CAMPAIGN_SQL]
        params: list[Any] = list(applied_statuses)
        ids = sorted({int(value) for value in token_ids or () if int(value or 0) > 0})
        if ids:
            clauses.append(f"p.token_id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        normalized_site = str(site_id or "").strip().upper()
        if normalized_site:
            clauses.append("p.site_id = ?")
            params.append(normalized_site)
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT DISTINCT p.token_id, p.site_id, i.item_id
                FROM promotions AS p
                INNER JOIN promotion_items AS i ON i.promotion_fk = p.id
                WHERE """ + " AND ".join(clauses),
                params,
            ).fetchall()
        return {
            (int(row["token_id"]), str(row["site_id"] or "").upper(), str(row["item_id"] or "").upper())
            for row in rows
        }

    def item_rows(self, promotion_fk: int, item_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM promotion_items WHERE promotion_fk=? AND item_id=?",
                              (int(promotion_fk), str(item_id))).fetchall()
        return [self._row(row) for row in rows]

    def set_item_status(
        self, promotion_fk: int, item_id: str, status: str, *,
        offer_id: str | None = None, item_local_id: int | None = None,
        snapshot_started_at: str = "",
    ) -> int:
        """Keep the local marker responsive immediately after a mutation."""
        if item_local_id is not None:
            where = "promotion_fk=? AND id=?"
            where_params = (int(promotion_fk), int(item_local_id))
        else:
            where = "promotion_fk=? AND item_id=?" + (" AND offer_id=?" if offer_id is not None else "")
            where_params = (int(promotion_fk), str(item_id or "")) + ((offer_id,) if offer_id is not None else ())
        written_at = str(snapshot_started_at or _precise_now())
        with self._connect() as db:
            if not snapshot_started_at:
                result = db.execute(
                    "UPDATE promotion_items SET status_raw=?, last_synced_at=? WHERE " + where,
                    (str(status or "unknown"), written_at, *where_params),
                )
                return int(result.rowcount or 0)

            # Item-level official reads have the same stale-write risk as a
            # whole-campaign snapshot.  Lock only the local compare-and-write;
            # never hold a SQLite transaction across the remote request.
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT id, last_synced_at FROM promotion_items WHERE " + where,
                where_params,
            ).fetchall()
            eligible_ids = [
                int(row["id"])
                for row in current
                if not row["last_synced_at"]
                or snapshot_started_after(snapshot_started_at, str(row["last_synced_at"]))
            ]
            updated = 0
            for offset in range(0, len(eligible_ids), 500):
                batch = eligible_ids[offset:offset + 500]
                result = db.execute(
                    "UPDATE promotion_items SET status_raw=?, last_synced_at=? "
                    f"WHERE id IN ({','.join('?' for _ in batch)})",
                    (str(status or "unknown"), written_at, *batch),
                )
                updated += int(result.rowcount or 0)
            return updated

    def record_item_submission(
        self, promotion_fk: int, item_id: str, response: dict[str, Any], *,
        item_local_id: int | None = None, offer_id: str | None = None,
    ) -> None:
        """Retain returned promotional prices/net amounts until the next official sync."""
        with self._connect() as db:
            if item_local_id is not None:
                rows = db.execute(
                    "SELECT * FROM promotion_items WHERE promotion_fk=? AND id=?",
                    (int(promotion_fk), int(item_local_id)),
                ).fetchall()
            else:
                offer_clause = " AND offer_id=?" if offer_id is not None else ""
                rows = db.execute(
                    "SELECT * FROM promotion_items WHERE promotion_fk=? AND item_id=?" + offer_clause,
                    (int(promotion_fk), str(item_id)) + ((offer_id,) if offer_id is not None else ()),
                ).fetchall()
            for stored in rows:
                item = self._row(stored)
                previous_price, previous_original = item["price"], item["original_price"]
                previous_currency = item["currency_id"]
                raw = dict(item.get("raw") or {})
                response_currency = str(response.get("currency_id") or previous_currency).strip().upper()
                currency_changed = response_currency != str(previous_currency or "").strip().upper()
                if currency_changed:
                    # Values quoted in the old currency cannot be carried into
                    # a response using a new listing-price currency.
                    item["price"] = item["original_price"] = None
                    item["min_price"] = item["max_price"] = None
                    for key in ("price", "original_price", "min_price", "max_price",
                                "min_discounted_price", "max_discounted_price", "suggested_discounted_price"):
                        raw.pop(key, None)
                for key in ("price", "original_price"):
                    value = response.get(key)
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(number) or number <= 0:
                        continue
                    item[key] = number
                    raw[key] = number
                if response.get("currency_id"):
                    item["currency_id"] = response_currency
                    raw["currency_id"] = item["currency_id"]
                if isinstance(response.get("net_proceeds"), (dict, list)):
                    item["net_proceeds"] = response["net_proceeds"]
                    raw["net_proceeds"] = item["net_proceeds"]
                elif currency_changed:
                    item["net_proceeds"] = raw["net_proceeds"] = {}
                elif item["price"] != previous_price or item["original_price"] != previous_original:
                    # Changing the quote cannot re-label an old amount as the
                    # proceeds for the newly submitted price. Candidate price
                    # references remain valid at their independently quoted prices.
                    excluded = set()
                    if item["price"] != previous_price:
                        excluded.update(("amount", "selling_fee", "shipping_fee"))
                    if item["original_price"] != previous_original:
                        excluded.add("original_amount")
                    values = item.get("net_proceeds") or {}
                    if isinstance(values, dict):
                        values = {key: value for key, value in values.items() if key not in excluded}
                    elif isinstance(values, list):
                        values = [{key: value for key, value in entry.items() if key not in excluded}
                                  for entry in values if isinstance(entry, dict)]
                    else:
                        values = {}
                    item["net_proceeds"] = raw["net_proceeds"] = values
                db.execute(
                    """UPDATE promotion_items SET price=?, original_price=?, currency_id=?, min_price=?, max_price=?,
                        net_proceeds_json=?, raw_json=? WHERE id=?""",
                    (item["price"], item["original_price"], item["currency_id"], item["min_price"], item["max_price"],
                     _json(item.get("net_proceeds") or {}), _json(raw), item["id"]),
                )

    def create_preview(self, *, actor: str, action: str, promotion_fk: int, payload: dict[str, Any], expires_at: str) -> str:
        preview_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                """DELETE FROM promotion_previews
                   WHERE julianday(expires_at)<julianday('now')
                   AND NOT EXISTS (
                       SELECT 1 FROM promotion_jobs WHERE promotion_jobs.preview_id=promotion_previews.preview_id
                   )"""
            )
            db.execute(
                "INSERT INTO promotion_previews VALUES (?, ?, ?, ?, ?, ?, ?)",
                (preview_id, actor, action, int(promotion_fk), _json(payload), expires_at, _now()),
            )
        return preview_id

    def get_preview(self, preview_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM promotion_previews WHERE preview_id=?", (str(preview_id),)).fetchone()
        return self._row(row) if row else None

    def compact_preview(self, preview_id: str) -> None:
        """Release executable row payloads after a job reaches a terminal state."""
        preview = self.get_preview(preview_id)
        if not preview:
            return
        payload = preview.get("payload") or {}
        compact = {
            "promotion": payload.get("promotion") or {},
            "excluded_count": int(payload.get("excluded_count") or 0),
            "excluded_reasons": payload.get("excluded_reasons") or [],
            "compacted": True,
        }
        with self._connect() as db:
            db.execute(
                "UPDATE promotion_previews SET payload_json=? WHERE preview_id=?",
                (_json(compact), str(preview_id)),
            )

    def create_job(self, *, preview_id: str, actor: str, action: str, total: int) -> str:
        job_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT job_id FROM promotion_jobs WHERE preview_id=? LIMIT 1", (preview_id,)
            ).fetchone()
            if existing:
                raise ValueError("该预览已提交，请查看执行记录，勿重复报名")
            db.execute(
                """INSERT INTO promotion_jobs
                   (job_id, preview_id, actor, action, status, total_count, created_at)
                   VALUES (?, ?, ?, ?, 'running', ?, ?)""",
                (job_id, preview_id, actor, action, int(total), _now()),
            )
        return job_id

    def set_job_total(self, job_id: str, total: int) -> None:
        with self._connect() as db:
            db.execute("UPDATE promotion_jobs SET total_count=? WHERE job_id=?", (int(total), job_id))

    def save_job_progress(self, job_id: str, *, results: list[dict[str, Any]]) -> None:
        success = sum(row.get("status") == "succeeded" for row in results)
        unknown = sum(row.get("status") == "unknown" for row in results)
        blocked = sum(row.get("status") == "blocked" for row in results)
        with self._connect() as db:
            db.execute("""UPDATE promotion_jobs SET success_count=?, failed_count=?,
                   unknown_count=?, blocked_count=?, results_json=? WHERE job_id=?""",
                (success, len(results) - success - unknown - blocked, unknown, blocked, _json(results), job_id))

    def finish_job(
        self, job_id: str, *, results: list[dict[str, Any]],
        preserve_finished_at: bool = False,
    ) -> None:
        success = sum(1 for row in results if row.get("status") == "succeeded")
        unknown = sum(1 for row in results if row.get("status") == "unknown")
        blocked = sum(1 for row in results if row.get("status") == "blocked")
        failed = len(results) - success - unknown - blocked
        status = "succeeded" if not results else (("succeeded" if success else "skipped") if not failed and not unknown else ("failed" if failed and not success and not unknown and not blocked else "partial"))
        with self._connect() as db:
            db.execute(
                """UPDATE promotion_jobs SET status=?, total_count=?, success_count=?, failed_count=?,
                   unknown_count=?, blocked_count=?, results_json=?,
                   finished_at=CASE WHEN ? THEN finished_at ELSE ? END WHERE job_id=?""",
                (status, len(results), success, failed, unknown, blocked, _json(results),
                 bool(preserve_finished_at), _now(), str(job_id)),
            )

    def list_jobs(
        self, limit: int = 100, *, token_ids: Iterable[int] | None = None,
        promotion_fk: int | None = None, result_limit: int | None = None,
        job_ids: Iterable[str] | None = None, summary_only: bool = False,
        action: str = "", status: str = "", repairable_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if token_ids is not None:
            ids = list(dict.fromkeys(int(value) for value in token_ids))
            if not ids:
                return []
            clauses.append("p.token_id IN (" + ",".join("?" for _ in ids) + ")")
            params.extend(ids)
        if promotion_fk is not None:
            clauses.append("v.promotion_fk=?")
            params.append(int(promotion_fk))
        if job_ids is not None:
            selected_ids = list(dict.fromkeys(str(value) for value in job_ids))
            if not selected_ids:
                return []
            clauses.append("j.job_id IN (" + ",".join("?" for _ in selected_ids) + ")")
            params.extend(selected_ids)
        if action:
            clauses.append("j.action=?")
            params.append(action)
        if status:
            clauses.append("j.status=?")
            params.append(status)
        if repairable_only:
            # Historical workers could reach a terminal state before writing
            # one result per planned row. Select only those incomplete logs;
            # complete terminal jobs and live workers must remain untouched.
            result_count = "json_array_length(CASE WHEN json_valid(j.results_json) THEN j.results_json ELSE '[]' END)"
            clauses.append("""j.status IN ('succeeded','partial','failed','skipped') AND (
                j.total_count > j.success_count + j.failed_count + j.unknown_count + j.blocked_count
                OR j.total_count > """ + result_count + ")")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        job_columns = "j.*"
        if result_limit is not None:
            result_limit = self._page_number(result_limit, 50, 100)
            job_columns = """j.job_id, j.preview_id, j.actor, j.action, j.status,
                j.total_count, j.success_count, j.failed_count, j.unknown_count, j.blocked_count,
                j.created_at, j.finished_at,
                json_array_length(CASE WHEN json_valid(j.results_json)
                    THEN j.results_json ELSE '[]' END) AS results_total,
                (SELECT json_group_array(json(value)) FROM (
                    SELECT value FROM json_each(CASE WHEN json_valid(j.results_json)
                        THEN j.results_json ELSE '[]' END) ORDER BY CAST(key AS INTEGER) LIMIT ?
                )) AS results_json"""
            params.insert(0, result_limit)
        if summary_only:
            if result_limit is not None:
                params.pop(0)
            job_columns = """j.job_id, j.action, j.status, j.total_count,
                j.success_count, j.failed_count, j.unknown_count, j.blocked_count, j.created_at, j.finished_at"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT " + job_columns + """, v.promotion_fk, p.name AS promotion_name,
                    p.promotion_id, p.store_name, p.site_id, p.token_id
                FROM promotion_jobs j
                JOIN promotion_previews v ON v.preview_id=j.preview_id
                JOIN promotions p ON p.id=v.promotion_fk"""
                + where + " ORDER BY j.created_at DESC, j.rowid DESC LIMIT ?",
                params,
            ).fetchall()
        result = [self._row(row) for row in rows]
        if result_limit is not None and not summary_only:
            for row in result:
                row["results_truncated"] = row["results_total"] > len(row.get("results") or [])
        return result
