"""Central product observations and resumable diagnosis runs; no credentials."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class DiagnosisStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("BIT_PRODUCT_DIAGNOSIS_DB_PATH") or
                         Path(__file__).resolve().parents[1] / ".data" / "product-diagnosis.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS diagnosis_products (
                    link_id INTEGER PRIMARY KEY, token_id INTEGER NOT NULL,
                    site_id TEXT NOT NULL, item_id TEXT NOT NULL,
                    risk TEXT NOT NULL, searchable TEXT NOT NULL,
                    checked_at TEXT NOT NULL, valid_until TEXT NOT NULL,
                    current INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS diagnosis_scope ON diagnosis_products(token_id,site_id,current,risk);
                CREATE TABLE IF NOT EXISTS diagnosis_runs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    actor TEXT NOT NULL, scopes TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
                    scope_errors TEXT NOT NULL DEFAULT '[]',
                    cursor_token INTEGER NOT NULL DEFAULT 0, cursor_link INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS diagnosis_active_run ON diagnosis_runs((1))
                    WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS diagnosis_run_products (
                    run_id TEXT NOT NULL, link_id INTEGER NOT NULL,
                    PRIMARY KEY(run_id,link_id)
                );
            """)
            if 'scope_errors' not in {row['name'] for row in db.execute('PRAGMA table_info(diagnosis_runs)')}:
                db.execute("ALTER TABLE diagnosis_runs ADD COLUMN scope_errors TEXT NOT NULL DEFAULT '[]'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, result, run_id=None):
        with self.connect() as db:
            db.execute("""INSERT INTO diagnosis_products
                (link_id,token_id,site_id,item_id,risk,searchable,checked_at,valid_until,current,payload)
                VALUES (?,?,?,?,?,?,?,?,1,?) ON CONFLICT(link_id) DO UPDATE SET
                token_id=excluded.token_id,site_id=excluded.site_id,item_id=excluded.item_id,
                risk=excluded.risk,searchable=excluded.searchable,checked_at=excluded.checked_at,
                valid_until=excluded.valid_until,current=1,payload=excluded.payload""",
                (result["link_id"], result["token_id"], result["site_id"], result["item_id"], result["risk"],
                 " ".join(str(result.get(key) or "") for key in ("title", "item_id", "seller_sku", "store_name")),
                 result["checked_at"], result["valid_until"], encode(result)))
            if run_id:
                inserted = db.execute("INSERT OR IGNORE INTO diagnosis_run_products VALUES (?,?)", (run_id, result["link_id"])).rowcount
                # Persist the cursor with the result, so restart cannot skip a product.
                db.execute("UPDATE diagnosis_runs SET processed=processed+?,failed=failed+?,cursor_token=?,cursor_link=?,updated_at=? WHERE id=?",
                           (inserted, inserted if result["data_status"] != "complete" else 0,
                            result["token_id"], result["link_id"], stamp(), run_id))

    def get(self, link_id, token_ids):
        with self.connect() as db:
            row = db.execute("SELECT * FROM diagnosis_products WHERE link_id=? AND current=1", (link_id,)).fetchone()
        if not row or row["token_id"] not in token_ids:
            raise KeyError("商品不存在或不在授权范围内")
        return self.decorate(json.loads(row["payload"]))

    @staticmethod
    def decorate(row):
        row["stale"] = row["valid_until"] < stamp()
        row["observed_risk"] = row["risk"]
        if row["stale"] and row["risk"] == "healthy":
            row["risk"] = "pending"
        return row

    def list_page(self, token_ids, *, site_id="", search="", risk="", category="", page=1, page_size=50):
        if not token_ids:
            return {"rows": [], "total": 0, "summary": {}, "page": page, "page_size": page_size}
        args = list(token_ids)
        conditions = [f"token_id IN ({','.join('?' for _ in args)})", "current=1"]
        if site_id:
            conditions.append("site_id=?")
            args.append(site_id)
        if search:
            conditions.append("searchable LIKE ? ESCAPE '\\'")
            args.append("%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        # Expired healthy observations require a new read before claiming normality.
        effective_risk = "CASE WHEN risk='healthy' AND valid_until<? THEN 'pending' ELSE risk END"
        sql = "SELECT *, " + effective_risk + " AS effective_risk FROM diagnosis_products WHERE " + " AND ".join(conditions)
        base_args = [stamp(), *args]
        if category:
            sql += " AND EXISTS (SELECT 1 FROM json_each(payload,'$.issues') WHERE json_extract(value,'$.category')=?)"
            base_args.append(category)
        with self.connect() as db:
            summary = {row["effective_risk"]: row["n"] for row in db.execute("SELECT effective_risk,COUNT(*) n FROM (" + sql + ") GROUP BY effective_risk", base_args)}
            summary["total"] = sum(summary.values())
            filtered = "SELECT * FROM (" + sql + ")"
            filtered_args = list(base_args)
            if risk:
                filtered += " WHERE effective_risk=?"
                filtered_args.append(risk)
            total = db.execute("SELECT COUNT(*) FROM (" + filtered + ")", filtered_args).fetchone()[0]
            rows = db.execute(filtered + " ORDER BY CASE effective_risk WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 WHEN 'pending' THEN 3 ELSE 4 END,checked_at DESC,link_id LIMIT ? OFFSET ?",
                              [*filtered_args, page_size, (page - 1) * page_size]).fetchall()
        return {"rows": [self.decorate(json.loads(row["payload"])) for row in rows], "total": total,
                "summary": summary, "page": page, "page_size": page_size}

    def create_run(self, scopes, actor):
        run_id, now = uuid.uuid4().hex, stamp()
        with self.connect() as db:
            try:
                db.execute("INSERT INTO diagnosis_runs(id,status,actor,scopes,created_at,updated_at) VALUES (?,'queued',?,?,?,?)",
                           (run_id, actor, encode(scopes), now, now))
            except sqlite3.IntegrityError:
                raise ValueError("已有商品诊断正在执行，请稍后重试") from None
        return run_id

    def next_run(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM diagnosis_runs WHERE status IN ('queued','running') ORDER BY created_at LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE diagnosis_runs SET status='running',updated_at=? WHERE id=?", (stamp(), row["id"]))
        return self.decode_run(row) if row else None

    @staticmethod
    def decode_run(row):
        result = dict(row)
        result["scopes"] = json.loads(result["scopes"])
        result["scope_errors"] = json.loads(result["scope_errors"])
        return result

    def runs(self, token_ids):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM diagnosis_runs ORDER BY created_at DESC,rowid DESC LIMIT 100").fetchall()
        return [self.decode_run(row) for row in rows if {int(v["token_id"]) for v in json.loads(row["scopes"])} <= set(token_ids)][:10]

    def update_run(self, run_id, **values):
        if not values.keys() <= {"status", "total", "message", "cursor_token", "cursor_link", "scope_errors"}:
            raise ValueError("诊断进度字段无效")
        if "scope_errors" in values:
            values["scope_errors"] = encode(values["scope_errors"])
        with self.connect() as db:
            db.execute("UPDATE diagnosis_runs SET " + ",".join(key + "=?" for key in values) + ",updated_at=? WHERE id=?",
                       [*values.values(), stamp(), run_id])

    def finish_scope(self, run_id, token_id, site_id=""):
        """Only a complete inventory traversal may retire missing products."""
        with self.connect() as db:
            db.execute("UPDATE diagnosis_products SET current=0 WHERE token_id=?" + (" AND site_id=?" if site_id else "") +
                       " AND link_id NOT IN (SELECT link_id FROM diagnosis_run_products WHERE run_id=?)",
                       [token_id, *([site_id] if site_id else []), run_id])
