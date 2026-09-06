import csv
import io
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATUSES = ("pending", "waiting_merchant_reply", "success", "exception")
CHINA = timezone(timedelta(hours=8))


def business_day(now):
    return datetime.fromtimestamp(now, CHINA).strftime("%Y-%m-%d")


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tasks.sqlite3"
        self.dirty = True
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                  erp_goods_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                  stage TEXT NOT NULL, payload TEXT NOT NULL,
                  created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, updated_at);
                CREATE TABLE IF NOT EXISTS collection_items (
                  scope TEXT NOT NULL, erp_goods_id TEXT NOT NULL, page INTEGER NOT NULL,
                  PRIMARY KEY(scope, erp_goods_id));
                CREATE TABLE IF NOT EXISTS merchants (
                  merchant_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, day TEXT NOT NULL,
                  reserved_at REAL NOT NULL, sent_at REAL, conversation_url TEXT, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                  level TEXT NOT NULL, task_id TEXT, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def log(self, message, task_id=None, level="INFO"):
        with self.connect() as db:
            db.execute("INSERT INTO events(at,level,task_id,message) VALUES(?,?,?,?)",
                       (time.time(), level, task_id, str(message)))

    def logs(self, after=0, limit=200):
        with self.connect() as db:
            if after:
                rows = db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit)).fetchall()
            else:
                rows = reversed(db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall())
            return [dict(row) for row in rows]

    def state(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    @staticmethod
    def decode(row):
        return {**json.loads(row["payload"]), **{k: row[k] for k in ("erp_goods_id", "status", "stage", "created_at", "updated_at")}}

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE erp_goods_id=?", (key,)).fetchone()
        if not row:
            raise KeyError("任务不存在")
        return self.decode(row)

    def add(self, record):
        key = str(record.get("erp_goods_id") or "").strip()
        if not key or not str(record.get("title") or "").strip():
            raise ValueError("ERP商品ID和标题不能为空")
        now = time.time()
        with self.connect() as db:
            changed = db.execute("INSERT OR IGNORE INTO tasks VALUES(?,?,?,?,?,?)",
                                 (key, "pending", "collected", json.dumps(record, ensure_ascii=False), now, now)).rowcount
        self.dirty = self.dirty or bool(changed)
        return changed

    def update(self, key, **changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE erp_goods_id=?", (key,)).fetchone()
            if row is None:
                raise KeyError("任务不存在")
            data = self.decode(row)
            data.update(changes)
            if data["status"] not in STATUSES:
                raise ValueError("状态无效")
            data["updated_at"] = time.time()
            db.execute("UPDATE tasks SET status=?,stage=?,payload=?,updated_at=? WHERE erp_goods_id=?",
                       (data["status"], data["stage"], json.dumps(data, ensure_ascii=False), data["updated_at"], key))
        self.dirty = True
        return data

    def exception(self, key, reason, detail=""):
        self.update(key, status="exception", exception_reason=reason, exception_detail=str(detail))
        self.log(reason + ("：" + str(detail) if detail else ""), key, "ERROR")

    def list(self, status="", search="", page=1, page_size=50, scope=None):
        if status and status not in STATUSES:
            raise ValueError("状态无效")
        clauses, args = [], []
        if scope is not None:
            clauses.append("erp_goods_id IN (SELECT erp_goods_id FROM collection_items WHERE scope=?)")
            args.append(scope)
        if status:
            clauses.append("status=?")
            args.append(status)
        if search:
            clauses.append("(erp_goods_id LIKE ? OR json_extract(payload,'$.title') LIKE ?)")
            args.extend(["%" + search + "%"] * 2)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM tasks" + where, args).fetchone()[0]
            rows = db.execute("SELECT * FROM tasks" + where + " ORDER BY created_at,erp_goods_id LIMIT ? OFFSET ?",
                              args + [page_size, (page - 1) * page_size]).fetchall()
        return {"total": total, "rows": [self.decode(row) for row in rows]}

    def counts(self, scope=None):
        with self.connect() as db:
            where = " WHERE erp_goods_id IN (SELECT erp_goods_id FROM collection_items WHERE scope=?)" if scope is not None else ""
            rows = db.execute("SELECT status,COUNT(*) n FROM tasks" + where + " GROUP BY status", (scope,) if scope is not None else ()).fetchall()
        return {**dict.fromkeys(STATUSES, 0), **{r["status"]: r["n"] for r in rows}}

    def reset_scope(self, scope):
        with self.connect() as db:
            db.execute("DELETE FROM collection_items WHERE scope=?", (scope,))

    def include_in_scope(self, scope, key, page):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO collection_items VALUES(?,?,?)", (scope, key, page))

    def quota(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM merchants WHERE day=?", (business_day(now),)).fetchone()[0]
            last = db.execute("SELECT MAX(COALESCE(sent_at,reserved_at)) FROM merchants").fetchone()[0]
        return {"today": count, "last": last}

    def reserve(self, task, config, message, conversation_url, baseline, now=None):
        """Reserve before clicking Send. Never refund an ambiguous delivery."""
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM state WHERE key='circuit' AND value!='null'").fetchone():
                return "circuit"
            if db.execute("SELECT 1 FROM merchants WHERE merchant_id=?", (task["merchant_id"],)).fetchone():
                return "duplicate"
            if db.execute("SELECT COUNT(*) FROM merchants WHERE day=?", (business_day(now),)).fetchone()[0] >= config["daily_limit"]:
                return "daily"
            last = db.execute("SELECT MAX(COALESCE(sent_at,reserved_at)) FROM merchants").fetchone()[0]
            if last is not None and now - last < max(60, config["consult_interval_seconds"]):
                return "interval"
            waiting = db.execute("SELECT COUNT(*) FROM tasks WHERE status='waiting_merchant_reply'").fetchone()[0]
            if waiting >= min(2, config["max_waiting"]):
                return "waiting"
            row = db.execute("SELECT * FROM tasks WHERE erp_goods_id=?", (task["erp_goods_id"],)).fetchone()
            if not row or row["status"] != "pending":
                return "status"
            db.execute("INSERT INTO merchants VALUES(?,?,?,?,NULL,?,?)",
                       (task["merchant_id"], task["erp_goods_id"], business_day(now), now, conversation_url, message))
            data = self.decode(row)
            data.update(conversation_url=conversation_url, conversation_id=conversation_url,
                        sent_message=message, reply_baseline=baseline, sent_at=now, next_poll_at=now + config["poll_minutes"] * 60,
                        deadline=now + config["timeout_minutes"] * 60)
            db.execute("UPDATE tasks SET status='waiting_merchant_reply',stage='send_reserved',payload=?,updated_at=? WHERE erp_goods_id=?",
                       (json.dumps(data, ensure_ascii=False), now, task["erp_goods_id"]))
        self.dirty = True
        return "ok"

    def sent(self, task_id, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("UPDATE merchants SET sent_at=? WHERE task_id=?", (now, task_id))
        # Keep the pre-click lower bound: a merchant may answer before the post-click check ends.
        self.update(task_id, stage="waiting")

    def recover(self):
        with self.connect() as db:
            rows = db.execute("SELECT erp_goods_id,stage FROM tasks WHERE status!='exception' AND stage IN ('send_reserved','writing')").fetchall()
        for row in rows:
            self.exception(row["erp_goods_id"], "上次操作中断，发送或保存结果不确定，请人工核对", row["stage"])

    def csv(self, status=""):
        fields = ["erp_goods_id", "title", "main_image_url", "description", "erp_sku", "cost_price", "weight_g",
                  "reference_weight_g", "measured_weight_g", "status", "exception_reason", "exception_detail",
                  "supplier_url", "supplier_sku_id", "supplier_sku", "merchant_id", "match_confidence",
                  "conversation_id", "merchant_reply", "validation", "created_at", "updated_at", "raw_json"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        page = 1
        while True:
            rows = self.list(status=status, page=page, page_size=500)["rows"]
            if not rows:
                break
            for row in rows:
                values = {k: row.get(k, "") for k in fields}
                values["raw_json"] = json.dumps(row, ensure_ascii=False)
                for key, value in values.items():
                    if isinstance(value, (dict, list)):
                        value = json.dumps(value, ensure_ascii=False)
                    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                        value = "'" + value
                    values[key] = value
                writer.writerow(values)
            page += 1
        return ("\ufeff" + output.getvalue()).encode("utf-8")

    def export(self):
        if not self.dirty:
            return
        folder = self.root / "reports"
        folder.mkdir(exist_ok=True)
        for status, name in [("", "all"), ("success", "success"), ("exception", "exceptions")]:
            target = folder / (name + ".csv")
            temp = target.with_suffix(".tmp")
            temp.write_bytes(self.csv(status))
            try:
                temp.replace(target)
            except PermissionError:
                self.log(f"报表 {name}.csv 被 Excel 占用；关闭文件后重新导出", level="WARNING")
        self.dirty = False
