"""Run persistent tasks outside the HTTP service's process lifetime.

Only public task state is stored in SQLite. A worker receives its private
parameters through stdin, owns its own cross-process lease, and never restarts
an interrupted task automatically (an appeal may already have been sent).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from bit import bit_runtime_lock
from bit.bit_runtime_lock import InterProcessLock


START_GRACE_SECONDS = 30.0
_CHILDREN = {}
_CHILDREN_GUARD = threading.Lock()
_PRIVATE_KEYS = frozenset(("api_key", "deepseek_api_key", "dashscope_api_key",
                           "runtime_api_key", "auth_token", "password", "secret"))


class _Connection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _task_id(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z_.-]{1,96}", value):
        raise ValueError("缺少有效的后台任务编号")
    return value


def _public(value):
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()
                if str(key).lower() not in _PRIVATE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


def _reap_children():
    with _CHILDREN_GUARD:
        for task_id, child in list(_CHILDREN.items()):
            if child.poll() is not None:
                _CHILDREN.pop(task_id, None)


def restore_worker_stdio():
    """Restore redirected OS handles in a frozen executable without a console."""
    for name, descriptor, mode in (("stdin", 0, "r"), ("stdout", 1, "w"), ("stderr", 2, "w")):
        if getattr(sys, name, None) is not None:
            continue
        try:
            stream = open(descriptor, mode, encoding="utf-8", errors="replace", buffering=1, closefd=False)
        except OSError:
            continue
        setattr(sys, name, stream)


class FileStopEvent:
    """An Event whose cancellation survives a service restart."""

    def __init__(self, path):
        self.path = Path(path)

    def is_set(self):
        return self.path.exists()

    def set(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, float(timeout))
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            time.sleep(0.1 if remaining is None else min(0.1, remaining))
        return True


class DurableTaskStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / "tasks.sqlite3"
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )""")
        self._private_files()

    def _private_files(self):
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                path.chmod(0o600)
            except FileNotFoundError:
                pass

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, factory=_Connection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        self._private_files()
        return connection

    def _log_path(self, task_id, path=None):
        target = Path(path or self.root / f"{task_id}.log").resolve()
        if not target.is_relative_to(self.root) or target == self.root:
            raise ValueError("后台任务日志必须保存在任务目录中")
        if target == self.path or target.suffix != ".log":
            raise ValueError("后台任务日志必须使用独立的 .log 文件")
        return target

    def stop_event(self, task_id):
        return FileStopEvent(self.root / f"{_task_id(task_id)}.stop")

    def _lease(self, task_id):
        task_id = _task_id(task_id)
        lease = InterProcessLock(f"durable_task_{task_id}", owner="durable_task_worker",
                                 metadata={"task_id": task_id, "store_root": str(self.root)})
        lock_root = self.root / "locks"
        lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Generic execution ownership follows the persistent task store, even
        # when a deployment runs the new Web service from another checkout.
        lease.lock_path = lock_root / f"{lease.key}.lockdir"
        return lease

    def _worker_owner(self, task_id):
        lease = self._lease(task_id)
        if not lease.lock_path.exists() or lease._remove_stale():
            return {}
        owner = lease.read_owner() or {"owner": "initializing"}
        metadata = owner.get("metadata") or {}
        # Lock creation and writing owner.json are separate atomic operations.
        if owner.get("owner") == "initializing":
            return owner
        return owner if metadata.get("store_root") == str(self.root) else {}

    def _read(self, connection, task_id):
        row = connection.execute("SELECT state_json FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return json.loads(row["state_json"]) if row else None

    def _write(self, connection, task_id, state):
        connection.execute("UPDATE tasks SET state_json = ?, updated_at = ? WHERE task_id = ?",
                           (json.dumps(_public(state), ensure_ascii=False), time.time(), task_id))

    def get(self, task_id, *, reconcile=True):
        task_id = _task_id(task_id)
        _reap_children()
        with self._connect() as connection:
            state = self._read(connection, task_id)
            if state and state.get("running") and reconcile:
                if self._worker_owner(task_id):
                    return state
                if time.time() - float(state.get("queued_at") or 0) < START_GRACE_SECONDS:
                    return state
                # Re-read after acquiring the write transaction: a worker may
                # have finished or acquired its lease since the first read.
                connection.execute("BEGIN IMMEDIATE")
                state = self._read(connection, task_id)
                if (state and state.get("running") and not self._worker_owner(task_id)
                        and time.time() - float(state.get("queued_at") or 0) >= START_GRACE_SECONDS):
                    state.update(running=False, status="interrupted", can_stop=False,
                                 finished_at=_timestamp(),
                                 message="后台任务进程已退出；已保留日志和进度，请检查后再启动")
                    self._write(connection, task_id, state)
            return state

    def list(self, job_type=None):
        with self._connect() as connection:
            if job_type is None:
                rows = connection.execute("SELECT task_id FROM tasks ORDER BY created_at, task_id").fetchall()
            else:
                rows = connection.execute("SELECT task_id FROM tasks WHERE job_type = ? ORDER BY created_at, task_id",
                                          (str(job_type),)).fetchall()
        return [state for row in rows if (state := self.get(row["task_id"]))]

    def update(self, task_id, **updates):
        task_id = _task_id(task_id)
        if "log_path" in updates:
            updates["log_path"] = str(self._log_path(task_id, updates["log_path"]))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._read(connection, task_id)
            if state is None:
                return None
            updates = {key: value for key, value in _public(updates).items()
                       if key not in {"task_id", "job_type", "queued_at"}}
            state.update(updates)
            if state.get("running") and self.stop_event(task_id).is_set():
                state["stop_requested"] = True
                if state.get("status") in {"starting", "running"}:
                    state["status"] = "stopping"
            self._write(connection, task_id, state)
            return state

    def request_stop(self, task_id):
        task_id = _task_id(task_id)
        state = self.get(task_id)
        if not state or not state.get("running"):
            return False
        # Persist cancellation before returning; every later worker can see it.
        self.stop_event(task_id).set()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._read(connection, task_id)
            if not state or not state.get("running"):
                return False
            state.update(status="stopping", stop_requested=True, message="已请求停止后台任务")
            self._write(connection, task_id, state)
        return True

    def prune(self, retention_seconds=3 * 24 * 60 * 60):
        """Delete expired terminal records and their own logs, never active work."""
        cutoff = time.time() - max(0.0, float(retention_seconds))
        removed = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT task_id, state_json FROM tasks WHERE updated_at < ?", (cutoff,)).fetchall()
            for row in rows:
                state = json.loads(row["state_json"])
                if state.get("running") or self._worker_owner(row["task_id"]):
                    continue
                try:
                    self._log_path(row["task_id"], state.get("log_path")).unlink(missing_ok=True)
                    self.stop_event(row["task_id"]).path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    continue
                connection.execute("DELETE FROM tasks WHERE task_id = ?", (row["task_id"],))
                removed += 1
        return removed

    def start(self, job_type, payload, public_state):
        task_id = _task_id((public_state or {}).get("task_id"))
        job_type = str(job_type or "").strip()
        if not job_type:
            raise ValueError("缺少后台任务类型")
        private_input = json.dumps(dict(payload or {}), ensure_ascii=False).encode("utf-8")
        log_path = self._log_path(task_id, (public_state or {}).get("log_path"))
        queued_at = time.time()
        state = _public(dict(public_state or {}))
        state.update(task_id=task_id, job_type=job_type, log_path=str(log_path),
                     queued_at=queued_at, running=True, status="starting", pid=0,
                     started_at=state.get("started_at") or _timestamp(), finished_at="",
                     stop_requested=False, can_stop=True)
        state.setdefault("message", "后台任务正在启动")
        encoded_state = json.dumps(state, ensure_ascii=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._read(connection, task_id) is not None:
                raise ValueError("该后台任务编号已存在")
            connection.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
                               (task_id, job_type, encoded_state, queued_at, queued_at))
        process = None
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            environment = os.environ.copy()
            environment.update(BIT_BACKGROUND_SERVICES_DISABLED="1", PYTHONUNBUFFERED="1",
                               PYTHONIOENCODING="utf-8",
                               BIT_RUNTIME_LOCK_DIR=str(bit_runtime_lock.RUNTIME_LOCK_DIR))
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            command = ([sys.executable, "--durable-task-worker", str(self.root), task_id]
                       if getattr(sys, "frozen", False)
                       else [sys.executable, "-m", "bit.durable_tasks", "--run", str(self.root), task_id])
            with os.fdopen(descriptor, "ab", buffering=0) as output:
                process = subprocess.Popen(
                    command,
                    cwd=str(Path(__file__).resolve().parents[1]), env=environment,
                    stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                    close_fds=True, **options,
                )
            with _CHILDREN_GUARD:
                _CHILDREN[task_id] = process
            try:
                process.stdin.write(private_input)
                process.stdin.close()
            finally:
                if not process.stdin.closed:
                    process.stdin.close()
            self.update(task_id, pid=process.pid)
        except Exception as exc:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.update(task_id, running=False, status="error", can_stop=False,
                        finished_at=_timestamp(), message=f"后台任务启动失败：{exc}")
            raise
        return self.get(task_id)


def _run_noop(payload, state, store, stop_event):
    """A side-effect-free runner used to verify the process lifecycle."""
    duration = max(0.0, min(60.0, float(payload.get("duration", 0))))
    deadline = time.monotonic() + duration
    progress = 0
    print("后台任务生命周期检查已启动", flush=True)
    while time.monotonic() < deadline and not stop_event.is_set():
        progress += 1
        store.update(state["task_id"], progress=progress)
        stop_event.wait(min(0.1, max(0.0, deadline - time.monotonic())))
    print("后台任务生命周期检查已结束", flush=True)
    return {"status": "stopped" if stop_event.is_set() else "success", "progress": progress,
            "message": "后台任务生命周期检查完成"}


def run_worker(root, task_id):
    # Private parameters never appear in argv, database state, or a job file.
    restore_worker_stdio()
    input_error = False
    try:
        payload = json.load(sys.stdin)
        input_error = not isinstance(payload, dict)
    except (AttributeError, OSError, ValueError):
        input_error = True
    store = DurableTaskStore(root)
    task_id = _task_id(task_id)
    lease = store._lease(task_id)
    if not lease.acquire(timeout=0):
        return 1
    previous_signals = {}
    try:
        state = store.get(task_id, reconcile=False)
        if not state or not state.get("running"):
            return 1
        if input_error:
            store.update(task_id, running=False, status="error", can_stop=False,
                         finished_at=_timestamp(), message="后台任务私密参数读取失败")
            return 1
        stop_event = store.stop_event(task_id)
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_signals[sig] = signal.signal(sig, lambda signum, frame: stop_event.set())
        state = store.update(task_id, running=True, pid=os.getpid(),
                             status="stopping" if stop_event.is_set() else "running",
                             worker_started_at=time.time(), can_stop=True)
        try:
            os.environ["BIT_DURABLE_TASK_WORKER"] = "1"
            os.environ["BIT_BACKGROUND_SERVICES_DISABLED"] = "1"
            os.environ["BIT_EXECUTION_JOB_ID"] = task_id
            usage = dict(payload.get("_usage_context") or {})
            for field, key in (("actor_id", "AI_BILLING_ACTOR_ID"), ("actor_name", "AI_BILLING_ACTOR_NAME"),
                               ("organization_key", "AI_BILLING_ORGANIZATION_KEY")):
                if field in usage:
                    os.environ[key] = str(usage[field])
            if payload.get("execution_target"):
                os.environ["BIT_EXECUTION_TARGET"] = str(payload["execution_target"])
            for key, value in usage.items():
                if key in {"BIT_EXECUTION_TARGET", "BIT_EXECUTION_JOB_ID"} or key.startswith("AI_BILLING_ACTOR_"):
                    os.environ[key] = str(value)
            if state["job_type"] == "__noop__":
                result = _run_noop(payload, state, store, stop_event)
            elif state["job_type"] == "size_chart_batch":
                from bit.size_chart_batch import run
                result = run(payload, state, store, stop_event)
            else:
                os.environ["BIT_BACKGROUND_SERVICES_DISABLED"] = "1"
                from bit import bit_interface
                result = bit_interface.execute_durable_task(state["job_type"], payload, state, store, stop_event)
            result = dict(result or {})
            result.update(running=False, can_stop=False, finished_at=_timestamp(),
                          stop_requested=stop_event.is_set())
            result.setdefault("status", "stopped" if stop_event.is_set() else "success")
            result.setdefault("message", "后台任务已停止" if stop_event.is_set() else "后台任务执行完成")
            store.update(task_id, **result)
            return 0
        except Exception as exc:
            traceback.print_exc()
            store.update(task_id, running=False, status="stopped" if stop_event.is_set() else "error",
                         can_stop=False, finished_at=_timestamp(), stop_requested=stop_event.is_set(),
                         message="后台任务已停止" if stop_event.is_set() else str(exc))
            return 1
    finally:
        lease.release()
        for sig, previous in previous_signals.items():
            signal.signal(sig, previous)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", nargs=2, metavar=("ROOT", "TASK_ID"), required=True)
    arguments = parser.parse_args(argv)
    return run_worker(*arguments.run)


if __name__ == "__main__":
    raise SystemExit(main())
