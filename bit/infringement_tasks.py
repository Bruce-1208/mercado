"""Durable, account-owned screening jobs. Credentials never enter this store."""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value):
    return json.dumps(_clean(value), ensure_ascii=False, default=str)


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()
                if str(key).lower() not in {"api_key", "token", "password", "authorization", "secret"}}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


class InfringementTaskStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    params TEXT NOT NULL, status TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_owner ON tasks(user_id, created_at);
                CREATE TABLE IF NOT EXISTS task_items (
                    task_id TEXT NOT NULL, source_type TEXT NOT NULL,
                    row_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    result TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, source_type, row_id)
                );
                CREATE INDEX IF NOT EXISTS task_item_lookup ON task_items(source_type, row_id, updated_at);
                CREATE TABLE IF NOT EXISTS active_items (
                    source_type TEXT NOT NULL, row_id INTEGER NOT NULL, task_id TEXT NOT NULL,
                    PRIMARY KEY(source_type, row_id)
                );
                CREATE TABLE IF NOT EXISTS task_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    message TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_logs_lookup ON task_logs(task_id,id);
            """)

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def recover(self):
        """Called once on service startup, never during polling."""
        with self.lock, self.connect() as db:
            db.execute("UPDATE tasks SET status='interrupted', message=?, updated_at=? WHERE status IN ('queued','running')",
                       ("服务重启中断，可重试未完成商品", _now()))
            db.execute("UPDATE task_items SET status='interrupted' WHERE status IN ('queued','processing')")
            db.execute("DELETE FROM active_items")

    def create(self, user_id, params, items):
        if int(user_id or 0) <= 0 or not items:
            raise ValueError("当前账号或检测商品无效")
        if _clean(params) != params:
            raise ValueError("检测任务不能保存凭证")
        task_id, now = uuid.uuid4().hex, _now()
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in items:
                existing = db.execute("SELECT task_id FROM active_items WHERE source_type=? AND row_id=?",
                                      (item["source_type"], item["row_id"])).fetchone()
                if existing:
                    raise ValueError("部分商品正在检测，请等待完成或取消原任务")
            db.execute("INSERT INTO tasks VALUES (?,?,?,'queued',0,?,?,?)",
                       (task_id, int(user_id), _json(params), "等待检测", now, now))
            for item in items:
                key = (item["source_type"], int(item["row_id"]))
                db.execute("INSERT INTO task_items(task_id,source_type,row_id,updated_at) VALUES (?,?,?,?)",
                           (task_id, *key, now))
                db.execute("INSERT INTO active_items VALUES (?,?,?)", (*key, task_id))
        return self.get(task_id, user_id)

    def get(self, task_id, user_id, *, include_results=True):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, int(user_id))).fetchone()
            if not row:
                raise KeyError("检测任务不存在")
            task = dict(row)
            task["params"] = json.loads(task["params"])
            counts = {x["status"]: x["n"] for x in db.execute(
                "SELECT status, COUNT(*) n FROM task_items WHERE task_id=? GROUP BY status", (task_id,))}
            task.update(task_id=task["id"], total=sum(counts.values()),
                        completed=counts.get("completed", 0), failed=counts.get("failed", 0),
                        running=task["status"] not in TERMINAL, cancel_requested=bool(task["cancel_requested"]),
                        detection_scope=task["params"].get("detection_scope", "title"))
            task["logs"] = [x["message"] for x in reversed(list(db.execute(
                "SELECT message FROM task_logs WHERE task_id=? ORDER BY id DESC LIMIT 100", (task_id,))))]
            if include_results:
                task["results"] = [{**json.loads(x["result"]), "source_type": x["source_type"],
                                    "row_id": x["row_id"], "status": x["status"]}
                                   for x in db.execute("SELECT * FROM task_items WHERE task_id=? ORDER BY updated_at DESC LIMIT 100", (task_id,))]
        return task

    def list(self, user_id, limit=20):
        with self.connect() as db:
            ids = [x["id"] for x in db.execute("SELECT id FROM tasks WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                                              (int(user_id), max(1, min(int(limit), 50))))]
        return [self.get(task_id, user_id, include_results=False) for task_id in ids]

    def items(self, task_id, *, unfinished_only=False):
        with self.connect() as db:
            clause = " AND status <> 'completed'" if unfinished_only else ""
            return [dict(x) for x in db.execute("SELECT source_type,row_id FROM task_items WHERE task_id=?" + clause, (task_id,))]

    def set_status(self, task_id, status, message=""):
        with self.lock, self.connect() as db:
            db.execute("UPDATE tasks SET status=?,message=?,updated_at=? WHERE id=?", (status, str(message)[:1000], _now(), task_id))
            if status in TERMINAL:
                db.execute("DELETE FROM active_items WHERE task_id=?", (task_id,))
                pending_state = "cancelled" if status == "cancelled" else "failed" if status == "failed" else "interrupted"
                db.execute("UPDATE task_items SET status=?,updated_at=? WHERE task_id=? AND status IN ('queued','processing')",
                           (pending_state, _now(), task_id))

    def update_item(self, task_id, event):
        source, row_id = event.get("source_type"), event.get("row_id", event.get("source_row_id"))
        if not source or not row_id:
            return
        with self.lock, self.connect() as db:
            db.execute("UPDATE task_items SET status=?,result=?,updated_at=? WHERE task_id=? AND source_type=? AND row_id=?",
                       (event.get("status", "processing"), _json(event), _now(), task_id, source, int(row_id)))
            db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (_now(), task_id))

    def log(self, task_id, message):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO task_logs(task_id,message,created_at) VALUES (?,?,?)", (task_id, str(message)[:2000], _now()))
            db.execute("DELETE FROM task_logs WHERE task_id=? AND id NOT IN (SELECT id FROM task_logs WHERE task_id=? ORDER BY id DESC LIMIT 500)", (task_id, task_id))

    def cancel(self, task_id, user_id):
        self.get(task_id, user_id, include_results=False)
        with self.lock, self.connect() as db:
            db.execute("UPDATE tasks SET cancel_requested=1,updated_at=? WHERE id=? AND status IN ('queued','running')", (_now(), task_id))
        return self.get(task_id, user_id)

    def cancelled(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT cancel_requested FROM tasks WHERE id=?", (task_id,)).fetchone()
        return not row or bool(row["cancel_requested"])

    def item_history(self, source_type, row_id, user_id, limit=20):
        with self.connect() as db:
            return [{**json.loads(x["result"]), "task_id": x["task_id"], "status": x["status"], "updated_at": x["updated_at"]}
                    for x in db.execute("SELECT i.* FROM task_items i JOIN tasks t ON t.id=i.task_id WHERE i.source_type=? AND i.row_id=? AND t.user_id=? ORDER BY i.updated_at DESC LIMIT ?",
                                        (source_type, int(row_id), int(user_id), limit))]

    def item_statuses(self, items, user_id):
        # One scan for the account, not a query per row.
        keys = {(x["source_type"], int(x["row_id"])) for x in items}
        with self.connect() as db:
            result = {}
            for source in {key[0] for key in keys}:
                ids = [key[1] for key in keys if key[0] == source]
                for start in range(0, len(ids), 400):
                    chunk = ids[start:start + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    rows = db.execute(f"SELECT i.* FROM task_items i JOIN tasks t ON t.id=i.task_id WHERE t.user_id=? AND i.source_type=? AND i.row_id IN ({placeholders}) ORDER BY i.updated_at DESC, i.rowid DESC", (int(user_id), source, *chunk))
                    for row in rows:
                        key = (row["source_type"], row["row_id"])
                        if key not in result:
                            result[key] = {**json.loads(row["result"]), "status": row["status"], "task_id": row["task_id"], "updated_at": row["updated_at"]}
        return result

    def active(self, items=None):
        with self.connect() as db:
            if items is None:
                return db.execute("SELECT 1 FROM active_items LIMIT 1").fetchone() is not None
            keys = {(x["source_type"], int(x["row_id"])) for x in items}
            return any((row["source_type"], row["row_id"]) in keys
                       for row in db.execute("SELECT source_type,row_id FROM active_items"))
