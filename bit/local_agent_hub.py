"""Persistent control-plane state for outbound local automation agents."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path


TERMINAL_JOB_STATUSES = frozenset(("success", "error", "stopped"))
ACTIVE_JOB_STATUSES = frozenset(("queued", "running", "stopping"))
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,95}$")


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


class LocalAgentStore:
    """Small SQLite-backed queue shared by Flask workers and agent polls."""

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema(connection)
        return connection

    def _ensure_schema(self, connection):
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
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
                """
            )
            connection.commit()
            self._schema_ready = True

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
        now=None,
    ):
        agent_id = normalize_agent_id(agent_id)
        now = time.time() if now is None else float(now)
        capabilities = sorted({str(item).strip() for item in capabilities if str(item).strip()})
        name = str(name or hostname or agent_id).strip()[:120] or agent_id
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO local_agents (
                    agent_id, name, hostname, platform, agent_version,
                    business_version, capabilities_json, last_seen,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    name = excluded.name,
                    hostname = excluded.hostname,
                    platform = excluded.platform,
                    agent_version = excluded.agent_version,
                    business_version = excluded.business_version,
                    capabilities_json = excluded.capabilities_json,
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
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM local_agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
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

    def claim_job(self, agent_id, *, now=None):
        agent_id = normalize_agent_id(agent_id)
        now = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
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
                    claimed_at = ?, started_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (now, now, now, job_id),
            )
            connection.execute(
                "INSERT INTO local_agent_events (job_id, event_type, content, created_at) VALUES (?, 'status', ?, ?)",
                (job_id, "本机 Agent 已接收任务，正在启动业务代码\n", now),
            )
            claimed = connection.execute(
                "SELECT * FROM local_agent_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            connection.commit()
            return self._job_row(claimed)
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
                finished_at = now if status in TERMINAL_JOB_STATUSES else None
                connection.execute(
                    """
                    UPDATE local_agent_jobs
                    SET status = ?, message = ?, result_json = ?,
                        finished_at = COALESCE(?, finished_at), updated_at = ?
                    WHERE job_id = ? AND agent_id = ?
                    """,
                    (
                        status,
                        str(message or status)[:500],
                        json.dumps(result or {}, ensure_ascii=False),
                        finished_at,
                        now,
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
        agent_id = normalize_agent_id(agent_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id FROM local_agent_jobs
                WHERE agent_id = ? AND cancel_requested = 1
                  AND status IN ('running', 'stopping')
                """,
                (agent_id,),
            ).fetchall()
        return [row["job_id"] for row in rows]

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

    def recent_log(self, job_id, *, max_chars=512 * 1024):
        """Read the newest log chunks, including logs after the first 500 events."""
        job_id = normalize_job_id(job_id)
        chunks = []
        size = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT content FROM local_agent_events WHERE job_id = ? ORDER BY event_id DESC",
                (job_id,),
            )
            for row in rows:
                chunks.append(row["content"])
                size += len(row["content"])
                if size >= max_chars:
                    break
        return "".join(reversed(chunks))[-max_chars:]

