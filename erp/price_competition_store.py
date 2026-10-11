"""Durable pricing observations, explicit cost assumptions and manual approvals."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


class PriceCompetitionStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("BIT_PRICE_COMPETITION_DB_PATH") or
                         Path(__file__).resolve().parents[1] / ".data" / "price-competition.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    token_id INTEGER NOT NULL, item_id TEXT NOT NULL,
                    snapshot TEXT, costs TEXT, checked_at REAL, error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(token_id,item_id));
                CREATE TABLE IF NOT EXISTS previews (
                    id TEXT PRIMARY KEY, token_id INTEGER NOT NULL, item_id TEXT NOT NULL,
                    actor TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL,
                    confirmed_at REAL);
                CREATE INDEX IF NOT EXISTS preview_item ON previews(token_id,item_id,confirmed_at);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def get(self, token_id, item_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM observations WHERE token_id=? AND item_id=?",
                             (token_id, item_id)).fetchone()
        if not row:
            return {}
        result = dict(row)
        for field in ("snapshot", "costs"):
            result[field] = json.loads(result[field]) if result[field] else None
        return result

    def observe(self, token_id, item_id, snapshot=None, error=""):
        with self.connection() as db:
            db.execute("""INSERT INTO observations(token_id,item_id,snapshot,checked_at,error)
                VALUES(?,?,?,?,?) ON CONFLICT(token_id,item_id) DO UPDATE SET
                snapshot=excluded.snapshot,checked_at=excluded.checked_at,error=excluded.error""",
                (token_id, item_id, json.dumps(snapshot, ensure_ascii=False) if snapshot else None,
                 time.time(), error))

    def save_costs(self, token_id, item_id, costs):
        with self.connection() as db:
            db.execute("""INSERT INTO observations(token_id,item_id,costs) VALUES(?,?,?)
                ON CONFLICT(token_id,item_id) DO UPDATE SET costs=excluded.costs""",
                (token_id, item_id, json.dumps(costs, ensure_ascii=False)))

    def preview(self, token_id, item_id, actor, payload):
        preview_id, expires = uuid.uuid4().hex, time.time() + 300
        with self.connection() as db:
            db.execute("INSERT INTO previews VALUES(?,?,?,?,?,?,NULL)",
                       (preview_id, token_id, item_id, actor, json.dumps(payload, ensure_ascii=False), expires))
        return {"preview_id": preview_id, "expires_at": expires, **payload}

    def get_preview(self, preview_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM previews WHERE id=?", (preview_id,)).fetchone()
        if not row:
            raise KeyError("价格建议预览不存在")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def confirm(self, preview_id, *, token_id, item_id, snapshot, costs):
        # Serialize the final comparison with cost edits and observation refreshes.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            observed = db.execute("SELECT snapshot,costs,error FROM observations WHERE token_id=? AND item_id=?",
                                  (token_id, item_id)).fetchone()
            if (not observed or observed["error"] or json.loads(observed["snapshot"] or "null") != snapshot
                    or json.loads(observed["costs"] or "null") != costs):
                raise ValueError("价格或成本资料已变化，请重新预览")
            changed = db.execute("UPDATE previews SET confirmed_at=? WHERE id=? AND confirmed_at IS NULL AND expires>?",
                                 (time.time(), preview_id, time.time())).rowcount
            if not changed:
                raise ValueError("预览已过期或已确认，请重新预览")

    def latest_approval(self, token_id, item_id):
        with self.connection() as db:
            row = db.execute("SELECT actor,payload,confirmed_at FROM previews WHERE token_id=? AND item_id=? "
                             "AND confirmed_at IS NOT NULL ORDER BY confirmed_at DESC LIMIT 1",
                             (token_id, item_id)).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        return {"actor": row["actor"], "confirmed_at": row["confirmed_at"],
                "suggestion": payload["suggestion"], "snapshot": payload["snapshot"], "costs": payload["costs"]}
