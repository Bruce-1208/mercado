"""Persistent control-plane state for outbound local automation agents."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path


TERMINAL_JOB_STATUSES = frozenset(("success", "error", "stopped"))
ACTIVE_JOB_STATUSES = frozenset(("queued", "running", "stopping"))
DEFAULT_JOB_LEASE_SECONDS = 15 * 60
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,95}$")


def default_local_agent_hub_path():
    """Return a deploy-directory-independent queue database path.

    Keeping the control-plane database below the source checkout makes a
    rolling/copy deployment silently create a second queue.  The Agent can
    then be online on one checkout while the browser enqueues work on another.
    """
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"])
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(
            os.environ.get("XDG_STATE_HOME")
            or os.environ.get("XDG_DATA_HOME")
            or (Path.home() / ".local" / "share")
        )
    return root / "Zeshun" / "MercadoWorkbench" / "local-agent-hub.sqlite3"


def migrate_local_agent_hub(source, destination):
    """Copy a legacy SQLite queue to its stable path using SQLite backup.

    The backup API includes committed WAL contents.  Existing destinations
    always win so a restart can never overwrite a newer live queue.
    """
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if source == destination or destination.exists() or not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.with_name(destination.name + ".migration.lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Another starting worker owns the migration. It will either publish
        # the destination atomically or leave the legacy database untouched.
        return False
    temporary = destination.with_name(
        destination.name + f".migration-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    )
    try:
        os.close(lock_fd)
        old_connection = sqlite3.connect(str(source), timeout=30)
        new_connection = sqlite3.connect(str(temporary), timeout=30)
        try:
            old_connection.backup(new_connection)
        finally:
            new_connection.close()
            old_connection.close()
        if destination.exists():
            temporary.unlink(missing_ok=True)
            return False
        os.replace(temporary, destination)
        return True
    finally:
        temporary.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def normalize_agent_id(value):
    value = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError("Agent 编号格式无效")
    return value


def normalize_job_id(value):
    value = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError("任务编号格式无效")
    return value


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class LocalAgentStore:
    """Small SQLite-backed queue shared by Flask workers and agent polls."""

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._queue_id = ""
        self._heartbeat_cache = {}
        self._heartbeat_cache_lock = threading.Lock()

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=15, factory=_ClosingConnection)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 15000")
            self._ensure_schema(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _ensure_schema(self, connection):
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            # WAL is persistent. Set it once under the initialization lock,
            # not on every heartbeat connection. SQLite may return BUSY here
            # immediately during a second process's cold start.
            deadline = time.monotonic() + 15
            while True:
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_agents (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    agent_version TEXT NOT NULL,
                    business_version TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    last_seen REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS local_agent_jobs (
                    job_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    required_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_by_id INTEGER,
                    created_by_name TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    claimed_session_id TEXT NOT NULL DEFAULT '',
                    lease_expires_at REAL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    claimed_at REAL,
                    started_at REAL,
                    finished_at REAL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(agent_id) REFERENCES local_agents(agent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_local_agent_jobs_claim
                    ON local_agent_jobs(agent_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_local_agent_jobs_history
                    ON local_agent_jobs(job_type, status, finished_at);

                CREATE TABLE IF NOT EXISTS local_agent_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES local_agent_jobs(job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_local_agent_events_job
                    ON local_agent_events(job_id, event_id);

                CREATE TABLE IF NOT EXISTS local_agent_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            agent_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(local_agents)")
            }
            if "session_id" not in agent_columns:
                connection.execute(
                    "ALTER TABLE local_agents ADD COLUMN session_id TEXT NOT NULL DEFAULT ''"
                )
            job_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(local_agent_jobs)")
            }
            if "claimed_session_id" not in job_columns:
                connection.execute(
                    "ALTER TABLE local_agent_jobs ADD COLUMN claimed_session_id "
                    "TEXT NOT NULL DEFAULT ''"
                )
            if "lease_expires_at" not in job_columns:
                connection.execute(
                    "ALTER TABLE local_agent_jobs ADD COLUMN lease_expires_at REAL"
                )
            connection.execute(
                "INSERT OR IGNORE INTO local_agent_metadata (key, value) VALUES ('queue_id', ?)",
                (uuid.uuid4().hex,),
            )
            queue_row = connection.execute(
                "SELECT value FROM local_agent_metadata WHERE key = 'queue_id'"
            ).fetchone()
            self._queue_id = str(queue_row[0] or "") if queue_row else ""
            connection.commit()
            self._schema_ready = True

    @property
    def queue_id(self):
        if not self._schema_ready:
            connection = self._connect()
            connection.close()
        return self._queue_id

    @staticmethod
    def _agent_row(row, now=None, online_seconds=45):
        if row is None:
            return None
        now = time.time() if now is None else float(now)
        data = dict(row)
        try:
            data["capabilities"] = json.loads(data.pop("capabilities_json"))
        except (TypeError, ValueError):
            data["capabilities"] = []
        data["online"] = now - float(data.get("last_seen") or 0) <= online_seconds
        return data

    @staticmethod
    def _job_row(row):
        if row is None:
            return None
        data = dict(row)
        for source, target, fallback in (
            ("payload_json", "payload", {}),
            ("result_json", "result", {}),
        ):
            try:
                data[target] = json.loads(data.pop(source))
            except (TypeError, ValueError):
                data[target] = fallback
        data["cancel_requested"] = bool(data.get("cancel_requested"))
        return data

    def heartbeat(
        self,
        agent_id,
        *,
        name,
        hostname="",
        platform="",
        agent_version="",
        business_version="",
        capabilities=(),
        session_id="",
        current_job_id="",
        lease_seconds=DEFAULT_JOB_LEASE_SECONDS,
        now=None,
    ):
        agent_id = normalize_agent_id(agent_id)
        now = time.time() if now is None else float(now)
        capabilities = sorted({str(item).strip() for item in capabilities if str(item).strip()})
        name = str(name or hostname or agent_id).strip()[:120] or agent_id
        session_id = str(session_id or "").strip()[:96]
        current_job_id = str(current_job_id or "").strip()
        lease_seconds = max(60.0, float(lease_seconds or DEFAULT_JOB_LEASE_SECONDS))
        heartbeat_key = (name, str(hostname), str(platform), str(agent_version),
                         str(business_version), tuple(capabilities), session_id,
                         current_job_id, lease_seconds)
        # Also protects the server from older agents that poll every second.
        # Cancellation is read separately on every request. A new session,
        # job, or version always writes immediately; leases are >=60 seconds.
        with self._heartbeat_cache_lock:
            cached = self._heartbeat_cache.get(agent_id)
            if cached and cached[1] == heartbeat_key and 0 <= now - cached[0] < 5:
                return self._agent_row(cached[2], now=now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT session_id FROM local_agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            previous_session = str(previous["session_id"] or "") if previous else ""
            if session_id and previous_session and session_id != previous_session:
                connection.execute(
                    """
                    UPDATE local_agent_jobs
                    SET status = CASE WHEN cancel_requested = 1 THEN 'stopped' ELSE 'error' END,
                        message = CASE
                            WHEN cancel_requested = 1 THEN 'Agent 已重启，原任务已停止'
                            ELSE 'Agent 已重启，原任务执行进程已丢失'
                        END,
                        finished_at = ?, updated_at = ?, lease_expires_at = NULL
                    WHERE agent_id = ? AND status IN ('running', 'stopping')
                      AND claimed_session_id <> ?
                    """,
                    (now, now, agent_id, session_id),
                )
            connection.execute(
                """
                INSERT INTO local_agents (
                    agent_id, name, hostname, platform, agent_version,
                    business_version, capabilities_json, session_id, last_seen,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    name = excluded.name,
                    hostname = excluded.hostname,
                    platform = excluded.platform,
                    agent_version = excluded.agent_version,
                    business_version = excluded.business_version,
                    capabilities_json = excluded.capabilities_json,
                    session_id = CASE
                        WHEN excluded.session_id <> '' THEN excluded.session_id
                        ELSE local_agents.session_id
                    END,
                    last_seen = excluded.last_seen,
                    updated_at = excluded.updated_at
                """,
                (
                    agent_id,
                    name,
                    str(hostname or "")[:255],
                    str(platform or "")[:255],
                    str(agent_version or "")[:64],
                    str(business_version or "")[:128],
                    json.dumps(capabilities, ensure_ascii=False),
                    session_id,
                    now,
                    now,
                    now,
                ),
            )
            if current_job_id and session_id:
                connection.execute(
                    """
                    UPDATE local_agent_jobs
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE job_id = ? AND agent_id = ?
                      AND claimed_session_id = ?
                      AND status IN ('running', 'stopping')
                    """,
                    (
                        now + lease_seconds,
                        now,
                        normalize_job_id(current_job_id),
                        agent_id,
                        session_id,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM local_agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        with self._heartbeat_cache_lock:
            self._heartbeat_cache[agent_id] = (now, heartbeat_key, dict(row))
            if len(self._heartbeat_cache) > 4096:
                del self._heartbeat_cache[next(iter(self._heartbeat_cache))]
        return self._agent_row(row, now=now)

    def list_agents(self, *, online_seconds=45, capability="", now=None):
        now = time.time() if now is None else float(now)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*,
                       SUM(CASE WHEN j.status IN ('queued', 'running', 'stopping')
                                THEN 1 ELSE 0 END) AS active_jobs
                FROM local_agents a
                LEFT JOIN local_agent_jobs j ON j.agent_id = a.agent_id
                GROUP BY a.agent_id
                ORDER BY a.name COLLATE NOCASE, a.agent_id
                """
            ).fetchall()
        agents = [self._agent_row(row, now=now, online_seconds=online_seconds) for row in rows]
        if capability:
            agents = [row for row in agents if capability in row.get("capabilities", ())]
        return agents

    def get_agent(self, agent_id, *, online_seconds=45, now=None):
        agent_id = normalize_agent_id(agent_id)
        now = time.time() if now is None else float(now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM local_agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return self._agent_row(row, now=now, online_seconds=online_seconds)

    def enqueue_job(
        self,
        job_id,
        agent_id,
        job_type,
        payload,
        *,
        required_version="",
        created_by_id=None,
        created_by_name="",
        now=None,
    ):
        job_id = normalize_job_id(job_id)
        agent_id = normalize_agent_id(agent_id)
        now = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO local_agent_jobs (
                    job_id, agent_id, job_type, payload_json, required_version,
                    status, message, created_by_id, created_by_name,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', '等待本机 Agent 接收', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    agent_id,
                    str(job_type or "").strip()[:64],
                    json.dumps(payload or {}, ensure_ascii=False),
                    str(required_version or "")[:128],
                    created_by_id,
                    str(created_by_name or "")[:120],
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO local_agent_events (job_id, event_type, content, created_at) VALUES (?, 'status', ?, ?)",
                (job_id, "任务已进入本机 Agent 队列\n", now),
            )
            row = connection.execute(
                "SELECT * FROM local_agent_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._job_row(row)

    def claim_job(
        self,
        agent_id,
        *,
        session_id="",
        lease_seconds=DEFAULT_JOB_LEASE_SECONDS,
        now=None,
    ):
        agent_id = normalize_agent_id(agent_id)
        now = time.time() if now is None else float(now)
        session_id = str(session_id or "").strip()[:96]
        lease_seconds = max(60.0, float(lease_seconds or DEFAULT_JOB_LEASE_SECONDS))
        connection = self._connect()
        try:
            # The normal running/idle poll needs no write lock. Recheck all
            # conditions inside the transaction below before claiming a job.
            # Returning on a racing enqueue is safe: the next poll sees it.
            active = connection.execute(
                "SELECT lease_expires_at, updated_at FROM local_agent_jobs "
                "WHERE agent_id = ? AND status IN ('running', 'stopping')",
                (agent_id,),
            ).fetchall()
            if any(
                (row["lease_expires_at"] is not None and row["lease_expires_at"] >= now)
                or (row["lease_expires_at"] is None and row["updated_at"] >= now - lease_seconds)
                for row in active
            ):
                return None
            if not active and not connection.execute(
                "SELECT 1 FROM local_agent_jobs WHERE agent_id = ? "
                "AND status = 'queued' AND cancel_requested = 0 LIMIT 1", (agent_id,),
            ).fetchone():
                return None
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE local_agent_jobs
                SET status = CASE WHEN cancel_requested = 1 THEN 'stopped' ELSE 'error' END,
                    message = CASE
                        WHEN cancel_requested = 1 THEN 'Agent 任务租约已过期，任务已停止'
                        ELSE 'Agent 任务租约已过期，执行状态已丢失'
                    END,
                    finished_at = ?, updated_at = ?, lease_expires_at = NULL
                WHERE agent_id = ? AND status IN ('running', 'stopping')
                  AND (
                    (lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                    OR (lease_expires_at IS NULL AND updated_at < ?)
                  )
                """,
                (now, now, agent_id, now, now - lease_seconds),
            )
            if connection.execute(
                "SELECT 1 FROM local_agent_jobs WHERE agent_id = ? "
                "AND status IN ('running', 'stopping') LIMIT 1", (agent_id,),
            ).fetchone():
                connection.commit()
                return None
            row = connection.execute(
                """
                SELECT * FROM local_agent_jobs
                WHERE agent_id = ? AND status = 'queued' AND cancel_requested = 0
                ORDER BY created_at, job_id
                LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            job_id = row["job_id"]
            connection.execute(
                """
                UPDATE local_agent_jobs
                SET status = 'running', message = '本机 Agent 已接收任务',
                    claimed_at = ?, started_at = ?, updated_at = ?,
                    claimed_session_id = ?, lease_expires_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (now, now, now, session_id, now + lease_seconds, job_id),
            )
            connection.execute(
                "INSERT INTO local_agent_events (job_id, event_type, content, created_at) VALUES (?, 'status', ?, ?)",
                (job_id, "本机 Agent 已接收任务，正在启动业务代码\n", now),
            )
            claimed = connection.execute(
                "SELECT * FROM local_agent_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            job = self._job_row(claimed)
            stored_payload = dict(job.get("payload") or {})
            sensitive_payload_keys = (
                "deepseek_api_key",
                "dashscope_api_key",
                "runtime_api_key",
            )
            scrubbed = False
            for key in sensitive_payload_keys:
                if stored_payload.pop(key, None) is not None:
                    scrubbed = True
            if scrubbed:
                connection.execute(
                    "UPDATE local_agent_jobs SET payload_json = ? WHERE job_id = ?",
                    (json.dumps(stored_payload, ensure_ascii=False), job_id),
                )
            connection.commit()
            return job
        finally:
            connection.close()

    def append_event(
        self,
        job_id,
        agent_id,
        *,
        content="",
        event_type="log",
        status="",
        message="",
        result=None,
        now=None,
    ):
        job_id = normalize_job_id(job_id)
        agent_id = normalize_agent_id(agent_id)
        now = time.time() if now is None else float(now)
        status = str(status or "").strip().lower()
        if status and status not in TERMINAL_JOB_STATUSES | frozenset(("running", "stopping")):
            raise ValueError("任务状态无效")
        content = str(content or "")
        if len(content.encode("utf-8")) > 512 * 1024:
            raise ValueError("单次日志内容过大")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM local_agent_jobs WHERE job_id = ? AND agent_id = ?",
                (job_id, agent_id),
            ).fetchone()
            if row is None:
                raise KeyError("任务不存在或不属于当前 Agent")
            if content:
                connection.execute(
                    "INSERT INTO local_agent_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                    (job_id, str(event_type or "log")[:32], content, now),
                )
            if status:
                if row["status"] in TERMINAL_JOB_STATUSES:
                    status = row["status"]
                elif row["cancel_requested"]:
                    status = "stopped" if status in TERMINAL_JOB_STATUSES else "stopping"
                finished_at = now if status in TERMINAL_JOB_STATUSES else None
                connection.execute(
                    """
                    UPDATE local_agent_jobs
                    SET status = ?, message = ?, result_json = ?,
                        finished_at = COALESCE(?, finished_at), updated_at = ?,
                        lease_expires_at = CASE WHEN ? IS NOT NULL THEN NULL ELSE lease_expires_at END
                    WHERE job_id = ? AND agent_id = ?
                    """,
                    (
                        status,
                        str(message or status)[:500],
                        json.dumps(result or {}, ensure_ascii=False),
                        finished_at,
                        now,
                        finished_at,
                        job_id,
                        agent_id,
                    ),
                )
            updated = connection.execute(
                "SELECT * FROM local_agent_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._job_row(updated)

    def request_cancel(self, job_id, *, now=None):
        job_id = normalize_job_id(job_id)
        now = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM local_agent_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None or row["status"] in TERMINAL_JOB_STATUSES:
                return False
            connection.execute(
                """
                UPDATE local_agent_jobs
                SET cancel_requested = 1,
                    status = CASE WHEN status = 'queued' THEN 'stopped' ELSE 'stopping' END,
                    message = '已请求停止本机 Agent 任务',
                    finished_at = CASE WHEN status = 'queued' THEN ? ELSE finished_at END,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (now, now, job_id),
            )
            connection.execute(
                "INSERT INTO local_agent_events (job_id, event_type, content, created_at) VALUES (?, 'status', ?, ?)",
                (job_id, "已提交停止请求\n", now),
            )
        return True

    def cancellation_job_ids(self, agent_id):
        # Legacy Agents can keep a worker alive after its lease was reaped.
        # Keep returning that exact job id until the worker observes the stop;
        # an error status alone only revokes DB access and causes endless 403s.
        agent_id = normalize_agent_id(agent_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id FROM local_agent_jobs
                WHERE agent_id = ? AND (
                    (cancel_requested = 1 AND status IN ('running', 'stopping'))
                    OR (claimed_at IS NOT NULL AND status IN ('error', 'stopped', 'success'))
                )
                """,
                (agent_id,),
            ).fetchall()
        return [row["job_id"] for row in rows]

    def reap_expired_jobs(
        self,
        *,
        agent_id="",
        lease_seconds=DEFAULT_JOB_LEASE_SECONDS,
        now=None,
    ):
        """Finish tasks whose owning Agent has stopped renewing its lease."""
        now = time.time() if now is None else float(now)
        lease_seconds = max(60.0, float(lease_seconds or DEFAULT_JOB_LEASE_SECONDS))
        params = [now, now]
        agent_clause = ""
        if agent_id:
            agent_clause = " AND j.agent_id = ?"
            params.append(normalize_agent_id(agent_id))
        params.extend((now, now - lease_seconds, now - lease_seconds))
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE local_agent_jobs AS j
                SET status = CASE WHEN cancel_requested = 1 THEN 'stopped' ELSE 'error' END,
                    message = CASE
                        WHEN cancel_requested = 1 THEN 'Agent 任务租约已过期，任务已停止'
                        ELSE 'Agent 任务租约已过期，执行状态已丢失'
                    END,
                    finished_at = ?, updated_at = ?, lease_expires_at = NULL
                WHERE status IN ('running', 'stopping'){agent_clause}
                  AND (
                    (lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                    OR (
                        lease_expires_at IS NULL AND updated_at < ?
                        AND NOT EXISTS (
                            SELECT 1 FROM local_agents AS a
                            WHERE a.agent_id = j.agent_id AND a.last_seen >= ?
                        )
                    )
                  )
                """,
                params,
            )
        return int(cursor.rowcount or 0)

    def get_job(self, job_id):
        job_id = normalize_job_id(job_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM local_agent_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._job_row(row)

    def list_jobs(self, *, agent_id="", job_type="", limit=100):
        clauses = []
        params = []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(normalize_agent_id(agent_id))
        if job_type:
            clauses.append("job_type = ?")
            params.append(str(job_type))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM local_agent_jobs{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._job_row(row) for row in rows]

    def prune_job_history(self, *, retention_seconds, job_type="", now=None):
        """Remove terminal jobs and their server-side logs after the retention window."""
        now = time.time() if now is None else float(now)
        retention_seconds = max(0, float(retention_seconds))
        clauses = ["status IN ('success', 'error', 'stopped')"]
        params = []
        if job_type:
            clauses.append("job_type = ?")
            params.append(str(job_type))
        clauses.append("COALESCE(finished_at, updated_at) < ?")
        params.append(now - retention_seconds)
        where = " AND ".join(clauses)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT job_id FROM local_agent_jobs WHERE {where}",
                params,
            ).fetchall()
            job_ids = [row["job_id"] for row in rows]
            if not job_ids:
                return 0
            placeholders = ",".join("?" for _ in job_ids)
            connection.execute(
                f"DELETE FROM local_agent_events WHERE job_id IN ({placeholders})",
                job_ids,
            )
            connection.execute(
                f"DELETE FROM local_agent_jobs WHERE job_id IN ({placeholders})",
                job_ids,
            )
        return len(job_ids)

    def events_after(self, job_id, after_id=0, *, limit=500):
        job_id = normalize_job_id(job_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_type, content, created_at
                FROM local_agent_events
                WHERE job_id = ? AND event_id > ?
                ORDER BY event_id
                LIMIT ?
                """,
                (job_id, max(0, int(after_id)), max(1, min(int(limit), 2000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def event_page(self, job_id, after_id=0, *, limit=200, max_bytes=512 * 1024):
        """Read a bounded log page without materializing a large event backlog."""
        job_id = normalize_job_id(job_id)
        limit = max(1, min(int(limit), 200))
        events, byte_count = [], 0
        connection = self._connect()
        try:
            cursor = connection.execute(
                "SELECT event_id, event_type, content, created_at FROM local_agent_events "
                "WHERE job_id = ? AND event_id > ? ORDER BY event_id LIMIT ?",
                (job_id, max(0, int(after_id)), limit + 1),
            )
            for row in cursor:
                size = len(str(row["content"] or "").encode("utf-8"))
                # Always deliver one event, even if a legacy event exceeds the budget.
                if events and (len(events) >= limit or byte_count + size > max_bytes):
                    return events, True
                events.append(dict(row))
                byte_count += size
            return events, False
        finally:
            connection.close()

    @staticmethod
    def render_event_content(event):
        event = dict(event or {})
        content = str(event.get("content") or "")
        if not content:
            return ""
        try:
            created_at = float(event.get("created_at"))
            timestamp = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(created_at)
            )
        except (TypeError, ValueError, OverflowError, OSError):
            return content
        return "".join(
            f"[{timestamp}] {line}" if line.rstrip("\r\n") else line
            for line in content.splitlines(keepends=True)
        )

    def recent_log(self, job_id, *, max_chars=512 * 1024):
        """Read the newest log chunks, including logs after the first 500 events."""
        job_id = normalize_job_id(job_id)
        chunks = []
        size = 0
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT content, created_at
                FROM local_agent_events
                WHERE job_id = ?
                ORDER BY event_id DESC
                """,
                (job_id,),
            )
            for row in rows:
                rendered = self.render_event_content(row)
                chunks.append(rendered)
                size += len(rendered)
                if size >= max_chars:
                    break
        return "".join(reversed(chunks))[-max_chars:]
