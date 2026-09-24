import csv
import base64
import io
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

STATUSES = ("pending", "waiting_merchant_reply", "success", "exception", "skipped", "blocked", "risk")
COMPLETED = ("success", "skipped", "blocked", "risk")
CHINA = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


def _mysql_disconnected(exc):
    from pymysql.err import InterfaceError, OperationalError
    return (isinstance(exc, InterfaceError) and exc.args and exc.args[0] == 0
            or isinstance(exc, OperationalError) and exc.args
            and exc.args[0] in {2002, 2003, 2006, 2013, 2055})


class _MySQLTransactionInterrupted(RuntimeError):
    """The transaction has not reached COMMIT; the whole operation may retry."""


class MySQLCommitUncertain(RuntimeError):
    """Never replay a write when the server may already have committed it."""


def _retry_mysql_transaction(method):
    # Decorate storage operations only. Browser saves and merchant messages must
    # never be replayed because a later database call failed.
    @wraps(method)
    def run(self, *args, **kwargs):
        for attempt in range(3):
            try:
                return method(self, *args, **kwargs)
            except _MySQLTransactionInterrupted as exc:
                if attempt == 2:
                    cause = exc.__cause__
                    raise RuntimeError(
                        f"MySQL连接中断，重试2次后仍失败：{type(cause).__name__}: {cause}"
                    ) from cause
                logger.warning("AI核重核价数据库事务 %s 连接中断，重新连接重试 %s/2",
                               method.__name__, attempt + 1)
                time.sleep(.2 * (attempt + 1))
    return run

MYSQL_TABLES = {
    "tasks": "erp_ai_weight_price_tasks",
    "collection_items": "erp_ai_weight_price_collection_items",
    "merchants": "erp_ai_weight_price_merchants",
    "events": "erp_ai_weight_price_events",
    "state": "erp_ai_weight_price_state",
    "runs": "erp_ai_weight_price_runs",
    "run_items": "erp_ai_weight_price_run_items",
}


class _CompatRow(dict):
    """DictCursor row with the positional access used by the SQLite store."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _CompatResult:
    def __init__(self, rows, rowcount):
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _mysql_sql(sql):
    """Translate the small SQLite SQL dialect used by Store to MySQL."""

    sql = sql.replace("BEGIN IMMEDIATE", "START TRANSACTION")
    sql = sql.replace("json_extract(payload,'$.title')", "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.title'))")
    sql = sql.replace("json_extract(t.payload,'$.title')", "JSON_UNQUOTE(JSON_EXTRACT(t.payload,'$.title'))")
    sql = sql.replace("json_extract(ri.payload,'$.title')", "JSON_UNQUOTE(JSON_EXTRACT(ri.payload,'$.title'))")
    sql = sql.replace("json_extract(ri.payload,'$.status')", "JSON_UNQUOTE(JSON_EXTRACT(ri.payload,'$.status'))")
    sql = sql.replace("json_extract(payload,'$.source_page')", "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.source_page'))")
    sql = sql.replace("json_extract(payload,'$.source_index')", "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.source_index'))")
    sql = sql.replace("json_extract(payload,'$.owner_user_id')", "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.owner_user_id'))")
    sql = sql.replace("CAST(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.source_page')) AS INTEGER)",
                      "CAST(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.source_page')) AS UNSIGNED)")
    sql = sql.replace("CAST(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.source_index')) AS INTEGER)",
                      "CAST(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.source_index')) AS UNSIGNED)")
    for source, target in MYSQL_TABLES.items():
        sql = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(source)}(?![A-Za-z0-9_])",
                     f"`{target}`", sql)
    sql = re.sub(r"(?<![`A-Za-z0-9_])key(?![`A-Za-z0-9_])", "`key`", sql)
    return sql.replace("?", "%s")


class _MySQLConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, args=()):
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            # connect() already began the transaction through the driver API,
            # which also disables DBUtils' mid-transaction statement reconnect.
            return _CompatResult([], 0)
        cursor = self.connection.cursor()
        try:
            cursor.execute(_mysql_sql(sql), args)
            rows = []
            if cursor.description:
                names = [item[0] for item in cursor.description]
                rows = [
                    _CompatRow(row) if isinstance(row, dict) else _CompatRow(zip(names, row))
                    for row in cursor.fetchall()
                ]
            return _CompatResult(rows, cursor.rowcount)
        finally:
            # Preserve the original query error when cleanup also disconnects.
            try:
                cursor.close()
            except Exception:
                logger.warning("AI核重核价数据库游标清理失败，保留原始异常", exc_info=True)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


def _create_mysql_schema(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
          erp_goods_id VARCHAR(191) PRIMARY KEY, status VARCHAR(32) NOT NULL,
          stage VARCHAR(64) NOT NULL, payload LONGTEXT NOT NULL,
          created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
          KEY tasks_status (status, updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS collection_items (
          scope VARCHAR(191) NOT NULL, erp_goods_id VARCHAR(191) NOT NULL,
          page INT NOT NULL, PRIMARY KEY(scope, erp_goods_id),
          KEY collection_items_product (erp_goods_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS merchants (
          merchant_id VARCHAR(191) PRIMARY KEY, task_id VARCHAR(191) NOT NULL,
          day VARCHAR(10) NOT NULL, reserved_at DOUBLE NOT NULL,
          sent_at DOUBLE NULL, conversation_url VARCHAR(2048), message LONGTEXT NOT NULL,
          KEY merchants_day (day), KEY merchants_task (task_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
          id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, at DOUBLE NOT NULL,
          level VARCHAR(16) NOT NULL, task_id VARCHAR(191), message LONGTEXT NOT NULL,
          owner_user_id BIGINT NULL, owner_name VARCHAR(191) NOT NULL DEFAULT '',
          KEY events_task (task_id), KEY events_at (at), KEY events_owner (owner_user_id, id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS state (
          `key` VARCHAR(191) PRIMARY KEY, value LONGTEXT NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS runs (
          run_id VARCHAR(191) PRIMARY KEY, payload LONGTEXT NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS run_items (
          run_id VARCHAR(191) NOT NULL, erp_goods_id VARCHAR(191) NOT NULL,
          sequence INT NOT NULL, payload LONGTEXT NOT NULL,
          PRIMARY KEY(run_id, erp_goods_id), KEY run_items_order(run_id, sequence)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def _ensure_mysql_schema(db):
    expected = set(MYSQL_TABLES.values())
    placeholders = ",".join("?" for _ in expected)
    rows = db.execute(
        """SELECT TABLE_NAME FROM information_schema.TABLES
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (""" + placeholders + ")",
        tuple(expected),
    ).fetchall()
    if {str(row["TABLE_NAME"]) for row in rows} < expected:
        _create_mysql_schema(db)
    event_columns = {
        str(row["COLUMN_NAME"])
        for row in db.execute(
            """SELECT COLUMN_NAME FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=?""",
            (MYSQL_TABLES["events"],),
        ).fetchall()
    }
    if "owner_user_id" not in event_columns:
        db.execute("ALTER TABLE events ADD COLUMN owner_user_id BIGINT NULL, ADD KEY events_owner (owner_user_id, id)")
    if "owner_name" not in event_columns:
        db.execute("ALTER TABLE events ADD COLUMN owner_name VARCHAR(191) NOT NULL DEFAULT ''")


def business_day(now):
    return datetime.fromtimestamp(now, CHINA).strftime("%Y-%m-%d")


class Store:
    def __init__(self, root, backend=None, connection_factory=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.backend = str(backend or os.environ.get("AI_WEIGHT_PRICE_STORAGE") or "sqlite").strip().lower()
        if self.backend not in {"sqlite", "mysql"}:
            raise ValueError("AI核重核价存储后端必须是 sqlite 或 mysql")
        self.path = self.root / "tasks.sqlite3" if self.backend == "sqlite" else None
        self.connection_factory = connection_factory
        self._actor_context = ContextVar(f"ai_weight_price_actor_{id(self)}", default=None)
        self.dirty = True
        self.read_only = False
        if self.backend == "sqlite":
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
                  level TEXT NOT NULL, task_id TEXT, message TEXT NOT NULL,
                  owner_user_id INTEGER, owner_name TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_items (run_id TEXT NOT NULL, erp_goods_id TEXT NOT NULL,
                  sequence INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id, erp_goods_id));
            """)
                event_columns = {row[1] for row in db.execute("PRAGMA table_info(events)").fetchall()}
                if "owner_user_id" not in event_columns:
                    db.execute("ALTER TABLE events ADD COLUMN owner_user_id INTEGER")
                if "owner_name" not in event_columns:
                    db.execute("ALTER TABLE events ADD COLUMN owner_name TEXT NOT NULL DEFAULT ''")
                db.execute("CREATE INDEX IF NOT EXISTS events_owner ON events(owner_user_id,id)")
        else:
            with self.connect() as db:
                _ensure_mysql_schema(db)
                flags = db.execute(
                    "SELECT @@GLOBAL.read_only AS read_only, @@GLOBAL.super_read_only AS super_read_only"
                ).fetchone()
                self.read_only = bool(flags and (
                    str(flags.get("read_only", "")).upper() in {"1", "ON", "TRUE"}
                    or str(flags.get("super_read_only", "")).upper() in {"1", "ON", "TRUE"}
                ))

    @contextmanager
    def connect(self):
        if self.backend == "sqlite":
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()
            return

        db = None
        committing = False
        try:
            if self.connection_factory is None:
                from bit.bit_mysql import config, pymysql
                connection = pymysql.connect(**config)
            else:
                connection = self.connection_factory()
            db = _MySQLConnection(connection)
            # Explicit begin is essential with DBUtils: otherwise a lost
            # connection can silently reconnect and replay only the last SQL,
            # discarding earlier writes from the same transaction.
            connection.begin()
            yield db
            committing = True
            db.commit()
        except Exception as exc:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    logger.warning("AI核重核价数据库回滚失败，保留原始异常", exc_info=True)
            if _mysql_disconnected(exc):
                if committing:
                    raise MySQLCommitUncertain(
                        f"MySQL提交时连接中断，提交结果待核对，未自动重放：{type(exc).__name__}: {exc}"
                    ) from exc
                raise _MySQLTransactionInterrupted("MySQL事务提交前连接中断") from exc
            raise
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    logger.warning("AI核重核价数据库连接清理失败", exc_info=True)

    @property
    def storage_description(self):
        if self.backend == "mysql":
            return "MySQL（服务器中心库；只读）" if self.read_only else "MySQL（服务器中心库）"
        return str(self.path)

    @staticmethod
    def _normalize_actor(actor, view_all=None):
        if not actor:
            return None
        user_id = actor.get("id") or actor.get("owner_user_id")
        if user_id in (None, ""):
            return None
        return {
            "id": int(user_id),
            "username": str(actor.get("username") or actor.get("owner_username") or "").strip(),
            "display_name": str(
                actor.get("display_name") or actor.get("owner_display_name")
                or actor.get("username") or actor.get("owner_username") or user_id
            ).strip(),
            "view_all": bool(actor.get("view_all")) if view_all is None else bool(view_all),
        }

    def set_actor(self, actor, *, view_all=None):
        normalized = self._normalize_actor(actor, view_all)
        self._actor_context.set(normalized)
        return normalized

    def actor(self):
        # Fault-injection and recovery paths can construct a minimal Store
        # around an already-open MySQL connection without running __init__.
        # Treat that legacy/no-context shape as an anonymous actor instead of
        # masking the database error that the caller is trying to handle.
        context = getattr(self, "_actor_context", None)
        return context.get() if context is not None else None

    @contextmanager
    def actor_scope(self, actor, *, view_all=None):
        token = self._actor_context.set(self._normalize_actor(actor, view_all))
        try:
            yield
        finally:
            self._actor_context.reset(token)

    def _owner_fields(self):
        actor = self.actor()
        if not actor:
            return {}
        return {
            "owner_user_id": actor["id"],
            "owner_username": actor["username"],
            "owner_display_name": actor["display_name"],
        }

    def _state_key(self, key):
        actor = self.actor()
        return f"user:{actor['id']}:{key}" if actor else key

    def _scope_key(self, scope):
        actor = self.actor()
        return f"user:{actor['id']}:{scope}" if actor else scope

    def _may_view(self, payload):
        actor = self.actor()
        if not actor or actor.get("view_all"):
            return True
        try:
            return int(payload.get("owner_user_id") or 0) == actor["id"]
        except (TypeError, ValueError):
            return False

    def _owner_filter(self, payload_column="payload"):
        actor = self.actor()
        if not actor or actor.get("view_all"):
            return None, []
        return f"json_extract({payload_column},'$.owner_user_id')=?", [actor["id"]]

    @_retry_mysql_transaction
    def log(self, message, task_id=None, level="INFO"):
        actor = self.actor() or {}
        with self.connect() as db:
            db.execute("INSERT INTO events(at,level,task_id,message,owner_user_id,owner_name) VALUES(?,?,?,?,?,?)",
                       (time.time(), level, task_id, str(message), actor.get("id"), actor.get("display_name", "")))

    @_retry_mysql_transaction
    def logs(self, after=0, limit=200):
        actor = self.actor()
        owner_sql = " AND owner_user_id=?" if actor and not actor.get("view_all") else ""
        owner_args = [actor["id"]] if owner_sql else []
        with self.connect() as db:
            if after:
                rows = db.execute("SELECT * FROM events WHERE id>?" + owner_sql + " ORDER BY id LIMIT ?",
                                  [after, *owner_args, limit]).fetchall()
            else:
                where = (" WHERE owner_user_id=?" if owner_sql else "")
                rows = reversed(db.execute("SELECT * FROM events" + where + " ORDER BY id DESC LIMIT ?",
                                           [*owner_args, limit]).fetchall())
            return [dict(row) for row in rows]

    @_retry_mysql_transaction
    def state(self, key, default=None):
        key = self._state_key(key)
        with self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        value = json.loads(row[0])
        # A cleared checkpoint is persisted as JSON null. Callers that pass a
        # concrete default expect that shape for both missing and cleared state.
        return default if value is None and default is not None else value

    @_retry_mysql_transaction
    def set_state(self, key, value):
        key = self._state_key(key)
        with self.connect() as db:
            payload = json.dumps(value, ensure_ascii=False)
            if self.backend == "mysql":
                db.execute("INSERT INTO state VALUES(?,?) ON DUPLICATE KEY UPDATE value=VALUES(value)", (key, payload))
            else:
                db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, payload))

    @_retry_mysql_transaction
    def save_run(self, run):
        run = {**run, **self._owner_fields()}
        with self.connect() as db:
            payload = json.dumps(run, ensure_ascii=False)
            if self.backend == "mysql":
                db.execute("INSERT INTO runs VALUES(?,?) ON DUPLICATE KEY UPDATE payload=VALUES(payload)",
                           (run["run_id"], payload))
            else:
                db.execute("INSERT OR REPLACE INTO runs VALUES(?,?)", (run["run_id"], payload))
        self.set_state("latest_run_id", run["run_id"])

    @_retry_mysql_transaction
    def clear_run(self, run_id):
        """Remove one execution batch without erasing reusable product history.

        Product rows, merchant de-duplication records and the Edge login binding
        deliberately survive.  They can belong to older batches or represent
        external actions that must not be forgotten merely because the current
        batch is dismissed from the UI.
        """
        if not run_id:
            return {"run_items": 0}
        with self.connect() as db:
            removed = db.execute("DELETE FROM run_items WHERE run_id=?", (run_id,)).rowcount
            db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        self.dirty = True
        return {"run_items": removed}

    @_retry_mysql_transaction
    def record_run_item(self, run_id, key, **details):
        if not run_id:
            return
        payload = {**self.get(key), **details}
        with self.connect() as db:
            if self.backend == "mysql":
                db.execute("INSERT INTO run_items VALUES(?,?,(SELECT COUNT(*)+1 FROM run_items ri WHERE ri.run_id=?),?) "
                           "ON DUPLICATE KEY UPDATE payload=VALUES(payload)",
                           (run_id, key, run_id, json.dumps(payload, ensure_ascii=False)))
            else:
                db.execute("INSERT INTO run_items VALUES(?,?,(SELECT COUNT(*)+1 FROM run_items WHERE run_id=?),?) "
                           "ON CONFLICT(run_id,erp_goods_id) DO UPDATE SET payload=excluded.payload",
                           (run_id, key, run_id, json.dumps(payload, ensure_ascii=False)))

    @_retry_mysql_transaction
    def has_run_item(self, run_id, key):
        if not run_id:
            return False
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM run_items WHERE run_id=? AND erp_goods_id=?",
                (run_id, key),
            ).fetchone() is not None

    @_retry_mysql_transaction
    def run_report(self, run_id):
        with self.connect() as db:
            run = db.execute("SELECT payload FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                raise ValueError("执行批次不存在")
            rows = db.execute("SELECT payload FROM run_items WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
        decoded_run = json.loads(run[0])
        if not self._may_view(decoded_run):
            raise ValueError("执行批次不存在")
        return decoded_run, [json.loads(row[0]) for row in rows]

    @_retry_mysql_transaction
    def run_items(self, run_id, status="", search="", page=1, page_size=50):
        """Return the latest task state for items belonging to one execution run."""
        if status and status not in STATUSES:
            raise ValueError("状态无效")
        clauses, args = ["ri.run_id=?"], [run_id]
        if status:
            clauses.append("json_extract(ri.payload,'$.status')=?")
            args.append(status)
        if search:
            clauses.append("(ri.erp_goods_id LIKE ? OR json_extract(ri.payload,'$.title') LIKE ?)")
            args.extend(["%" + search + "%"] * 2)
        where = " WHERE " + " AND ".join(clauses)
        with self.connect() as db:
            run = db.execute("SELECT payload FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                raise ValueError("执行批次不存在")
            if not self._may_view(json.loads(run[0])):
                raise ValueError("执行批次不存在")
            total = db.execute("SELECT COUNT(*) FROM run_items ri" + where, args).fetchone()[0]
            rows = db.execute("SELECT ri.sequence,ri.payload run_payload FROM run_items ri" + where
                              + " ORDER BY ri.sequence LIMIT ? OFFSET ?",
                              args + [page_size, (page - 1) * page_size]).fetchall()
        result = []
        run_payload = json.loads(run[0])
        for row in rows:
            snapshot = json.loads(row["run_payload"])
            result.append({**snapshot,
                           "execution_result": snapshot.get("execution_result", ""),
                           "execution_reason": snapshot.get("execution_reason", ""),
                           "run_sequence": row["sequence"]})
        return {"run": run_payload, "total": total, "rows": result}

    @staticmethod
    def decode(row):
        return {**json.loads(row["payload"]), **{k: row[k] for k in ("erp_goods_id", "status", "stage", "created_at", "updated_at")}}

    @_retry_mysql_transaction
    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE erp_goods_id=?", (key,)).fetchone()
        if not row:
            raise KeyError("任务不存在")
        result = self.decode(row)
        if not self._may_view(result):
            raise KeyError("任务不存在")
        return result

    @_retry_mysql_transaction
    def add(self, record):
        record = {**record, **self._owner_fields()}
        key = str(record.get("erp_goods_id") or "").strip()
        if not key or not str(record.get("title") or "").strip():
            raise ValueError("ERP商品ID和标题不能为空")
        now = time.time()
        with self.connect() as db:
            sql = "INSERT IGNORE INTO tasks VALUES(?,?,?,?,?,?)" if self.backend == "mysql" else "INSERT OR IGNORE INTO tasks VALUES(?,?,?,?,?,?)"
            changed = db.execute(sql, (key, "pending", "collected", json.dumps(record, ensure_ascii=False), now, now)).rowcount
            if not changed and self._owner_fields():
                existing = db.execute("SELECT * FROM tasks WHERE erp_goods_id=?", (key,)).fetchone()
                if existing:
                    payload = {**json.loads(existing["payload"]), **self._owner_fields()}
                    db.execute("UPDATE tasks SET payload=?,updated_at=? WHERE erp_goods_id=?",
                               (json.dumps(payload, ensure_ascii=False), now, key))
        self.dirty = self.dirty or bool(changed)
        return changed

    @_retry_mysql_transaction
    def update(self, key, **changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE erp_goods_id=?", (key,)).fetchone()
            if row is None:
                raise KeyError("任务不存在")
            data = self.decode(row)
            if not self._may_view(data):
                raise KeyError("任务不存在")
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

    def skip(self, key, reason):
        self.update(key, status="skipped", stage="no_exact_match", skip_reason=str(reason),
                    skipped_at=time.time(), exception_reason="", exception_detail="")
        self.log("未完全匹配，已跳过：" + str(reason) + "；继续下一件", key, "WARNING")

    @_retry_mysql_transaction
    def list(self, status="", search="", page=1, page_size=50, scope=None):
        if status and status not in STATUSES:
            raise ValueError("状态无效")
        clauses, args = [], []
        owner_clause, owner_args = self._owner_filter()
        if owner_clause:
            clauses.append(owner_clause)
            args.extend(owner_args)
        if scope is not None:
            clauses.append("erp_goods_id IN (SELECT erp_goods_id FROM collection_items WHERE scope=?)")
            args.append(self._scope_key(scope))
        if status:
            clauses.append("status=?")
            args.append(status)
        if search:
            clauses.append("(erp_goods_id LIKE ? OR json_extract(payload,'$.title') LIKE ?)")
            args.extend(["%" + search + "%"] * 2)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM tasks" + where, args).fetchone()[0]
            # A task's database creation time is not the user's visible page
            # order: old collections and retries can interleave records.  When
            # available, follow the source page and one-based card position so
            # processing starts at the first product on the selected page.
            rows = db.execute("SELECT * FROM tasks" + where +
                              " ORDER BY COALESCE(CAST(json_extract(payload,'$.source_page') AS INTEGER),2147483647),"
                              " COALESCE(CAST(json_extract(payload,'$.source_index') AS INTEGER),2147483647),"
                              " created_at,erp_goods_id LIMIT ? OFFSET ?",
                              args + [page_size, (page - 1) * page_size]).fetchall()
        return {"total": total, "rows": [self.decode(row) for row in rows]}

    @_retry_mysql_transaction
    def counts(self, scope=None):
        owner_clause, owner_args = self._owner_filter()
        with self.connect() as db:
            clauses, args = [], []
            if owner_clause:
                clauses.append(owner_clause)
                args.extend(owner_args)
            if scope is not None:
                clauses.append("erp_goods_id IN (SELECT erp_goods_id FROM collection_items WHERE scope=?)")
                args.append(self._scope_key(scope))
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = db.execute("SELECT status,COUNT(*) n FROM tasks" + where + " GROUP BY status", args).fetchall()
        return {**dict.fromkeys(STATUSES, 0), **{r["status"]: r["n"] for r in rows}}

    @_retry_mysql_transaction
    def run_counts(self, run_id):
        if not run_id:
            return dict.fromkeys(STATUSES, 0)
        with self.connect() as db:
            has_items = db.execute("SELECT 1 FROM run_items WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
            if not has_items:
                # A probe or a restored legacy run may not have batch rows yet;
                # let the UI fall back to the historical totals until the
                # first item is recorded.
                return None
            rows = db.execute("SELECT t.status,COUNT(*) n FROM tasks t JOIN run_items ri "
                              "ON ri.erp_goods_id=t.erp_goods_id WHERE ri.run_id=? GROUP BY t.status",
                              (run_id,)).fetchall()
        return {**dict.fromkeys(STATUSES, 0), **{row["status"]: row["n"] for row in rows}}

    @_retry_mysql_transaction
    def reset_scope(self, scope):
        scope = self._scope_key(scope)
        with self.connect() as db:
            db.execute("DELETE FROM collection_items WHERE scope=?", (scope,))

    @_retry_mysql_transaction
    def include_in_scope(self, scope, key, page):
        scope = self._scope_key(scope)
        with self.connect() as db:
            if self.backend == "mysql":
                db.execute("INSERT INTO collection_items VALUES(?,?,?) ON DUPLICATE KEY UPDATE page=VALUES(page)",
                           (scope, key, page))
            else:
                db.execute("INSERT OR REPLACE INTO collection_items VALUES(?,?,?)", (scope, key, page))

    @_retry_mysql_transaction
    def quota(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM merchants WHERE day=?", (business_day(now),)).fetchone()[0]
            last = db.execute("SELECT MAX(COALESCE(sent_at,reserved_at)) FROM merchants").fetchone()[0]
        return {"today": count, "last": last}

    @_retry_mysql_transaction
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

    @_retry_mysql_transaction
    def sent(self, task_id, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("UPDATE merchants SET sent_at=? WHERE task_id=?", (now, task_id))
        # Keep the pre-click lower bound: a merchant may answer before the post-click check ends.
        self.update(task_id, stage="waiting")

    @_retry_mysql_transaction
    def recover(self):
        with self.connect() as db:
            rows = db.execute("SELECT erp_goods_id,stage FROM tasks WHERE status!='exception' AND stage IN ('send_reserved','writing')").fetchall()
        for row in rows:
            self.exception(row["erp_goods_id"], "上次操作中断，发送或保存结果不确定，请人工核对", row["stage"])

    def csv(self, status=""):
        fields = ["erp_goods_id", "title", "main_image_url", "description", "erp_sku", "cost_price", "net_income_usd", "pricing", "weight_g", "verification_mode", "manual_verification",
                  "erp_before", "write_intent", "erp_after", "write_verified", "write_history",
                  "reference_weight_g", "measured_weight_g", "status", "decision_status", "decision_reason", "skip_reason", "skipped_at", "exception_reason", "exception_detail",
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
        for status, name in [("", "all"), ("success", "success"), ("exception", "exceptions"), ("skipped", "skipped"), ("blocked", "blocked"), ("risk", "risk")]:
            target = folder / (name + ".csv")
            temp = target.with_suffix(".tmp")
            temp.write_bytes(self.csv(status))
            try:
                temp.replace(target)
            except PermissionError:
                self.log(f"报表 {name}.csv 被 Excel 占用；关闭文件后重新导出", level="WARNING")
        self.dirty = False


REMOTE_METHODS = frozenset({
    "log", "logs", "state", "set_state", "save_run", "clear_run", "record_run_item",
    "has_run_item", "run_report", "run_items", "get", "add", "update", "exception", "skip",
    "list", "counts", "run_counts", "reset_scope", "include_in_scope", "quota", "reserve",
    "sent", "recover", "csv", "export",
})


class RemoteStore:
    """Client-side proxy for the server-owned MySQL Store.

    The client still needs ``root`` for its local Edge profile and visual
    frames, but no task database is created there. Every business-data
    operation goes through the authenticated internal database API.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = None
        self.backend = "api"
        self.dirty = True
        self._actor_context = ContextVar(f"ai_weight_price_remote_actor_{id(self)}", default=None)

    def set_actor(self, actor, *, view_all=None):
        normalized = Store._normalize_actor(actor, view_all)
        self._actor_context.set(normalized)
        return normalized

    def actor(self):
        return self._actor_context.get()

    @contextmanager
    def actor_scope(self, actor, *, view_all=None):
        token = self._actor_context.set(Store._normalize_actor(actor, view_all))
        try:
            yield
        finally:
            self._actor_context.reset(token)

    @property
    def storage_description(self):
        return "服务器 MySQL（API）"

    def _call(self, method, *args, **kwargs):
        from bit.bit_db_api import _request

        try:
            value = _request(
                "POST",
                "/api/db/ai-weight-price/store",
                json={"method": method, "args": list(args), "kwargs": kwargs,
                      "actor": self.actor()},
                timeout=120,
            )
        except RuntimeError as exc:
            message = str(exc)
            if "任务不存在" in message or "执行批次不存在" in message:
                raise KeyError(message) from exc
            raise ValueError(message) from exc
        if isinstance(value, dict) and value.get("__bytes__"):
            return base64.b64decode(value["__bytes__"])
        return value

    def __getattr__(self, name):
        if name not in REMOTE_METHODS:
            raise AttributeError(name)
        return lambda *args, **kwargs: self._call(name, *args, **kwargs)


__all__ = [
    "CHINA", "COMPLETED", "REMOTE_METHODS", "RemoteStore", "STATUSES", "Store",
    "business_day",
]
