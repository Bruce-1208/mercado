"""Exercise durable sync transitions across independent database connections."""

from copy import deepcopy
from datetime import datetime, timedelta
import re
import sqlite3
from types import SimpleNamespace

import pytest

from bit import bit_runtime_lock, bit_store_link_sync as sync
from erp import mercadolibre_store_link_store as store


class SQLiteCursor:
    """Run the compact MySQL queue statements against a real persisted test DB."""

    def __init__(self, connection):
        self.cursor = connection.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.cursor.close()

    @staticmethod
    def sql(sql):
        sql = sql.replace("%s", "?")
        sql = sql.replace(
            "ON DUPLICATE KEY UPDATE", "ON CONFLICT(token_id) DO UPDATE SET"
        )
        return re.sub(r"VALUES\((`\w+`)\)", r"excluded.\1", sql)

    def execute(self, sql, params=()):
        return self.cursor.execute(self.sql(sql), params)

    def executemany(self, sql, rows):
        return self.cursor.executemany(self.sql(sql), rows)

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]


class SQLiteConnection:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row

    def cursor(self):
        return SQLiteCursor(self.connection)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


@pytest.fixture
def queue_db(tmp_path, monkeypatch):
    path = tmp_path / "store-sync.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE mercado_store_tokens (id INTEGER PRIMARY KEY, enabled INTEGER)")
        database.executemany("INSERT INTO mercado_store_tokens VALUES (?, 1)", [(1,), (2,), (3,)])
        database.execute(f"""CREATE TABLE {store.STORE_LINK_SYNC_STATE_TABLE} (
            token_id INTEGER PRIMARY KEY, requested_at TEXT, priority INTEGER NOT NULL DEFAULT 0,
            request_version INTEGER NOT NULL DEFAULT 0,
            started_request_version INTEGER NOT NULL DEFAULT 0,
            last_started_at TEXT, last_completed_at TEXT,
            last_status TEXT NOT NULL DEFAULT 'pending', last_error TEXT
        )""")
    monkeypatch.setattr(store, "_connect", lambda: SQLiteConnection(path))
    monkeypatch.setattr(store, "ensure_store_link_table", lambda _cursor: None)
    monkeypatch.setattr(store, "ensure_store_link_sync_state_table", lambda _cursor: None)
    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path / "locks")
    initial = deepcopy(sync._sync_state)
    initial.update(running=False, status="idle", task_id="", results=[], logs=[])
    monkeypatch.setattr(sync, "_sync_state", initial)
    return path


def saved_state(path, token_id):
    with sqlite3.connect(path) as database:
        database.row_factory = sqlite3.Row
        return dict(database.execute(
            f"SELECT * FROM {store.STORE_LINK_SYNC_STATE_TABLE} WHERE token_id = ?",
            (token_id,),
        ).fetchone())


def completed_result(record):
    return dict(store=f"Store {record['id']}", token_id=record["id"], status="success",
                discovered=0, stored=0, inserted=0, updated=0, details=0, failed=0, products=0)


def test_restart_resumes_recently_interrupted_store_and_preserves_completed_store(queue_db, monkeypatch):
    store.request_store_link_sync([1, 2])
    store.mark_store_link_sync_started(1)
    store.mark_store_link_sync_finished(1, "success")
    store.mark_store_link_sync_started(2)
    # The worker dies here. New service memory has no running task, while the
    # persisted store is still running, even though its latest start is recent.
    assert store.list_due_store_link_token_ids() == [2, 3]
    synced = []
    monkeypatch.setattr(sync, "_token_records", lambda ids: [{"id": value} for value in ids])
    monkeypatch.setattr(sync, "_sync_store", lambda record: synced.append(record["id"]) or completed_result(record))

    class ImmediateThread:
        def __init__(self, *, target, args=(), **_kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(sync, "threading", SimpleNamespace(Thread=ImmediateThread))
    result = sync.start_due_store_link_sync()

    assert result["started"] is True
    assert sorted(synced) == [2, 3]
    assert store.list_due_store_link_token_ids() == []
    assert saved_state(queue_db, 1)["request_version"] == 1
    assert saved_state(queue_db, 2)["last_status"] == "success"


@pytest.mark.parametrize("status", ["success", "error"])
def test_request_arriving_in_same_second_is_retained_for_next_scan(queue_db, monkeypatch, status):
    moment = datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(store, "_now", lambda: moment)
    store.request_store_link_sync([1])
    store.mark_store_link_sync_started(1)
    store.request_store_link_sync([1])
    store.mark_store_link_sync_finished(1, status, "remote error" if status == "error" else "")

    state = saved_state(queue_db, 1)
    assert state["request_version"] == 2
    assert state["started_request_version"] == 1
    assert state["requested_at"] == moment
    assert state["last_status"] == "queued"
    assert 1 in store.list_due_store_link_token_ids()

    store.mark_store_link_sync_started(1)
    store.mark_store_link_sync_finished(1, "success")
    assert saved_state(queue_db, 1)["requested_at"] is None
    assert 1 not in store.list_due_store_link_token_ids()


def test_failed_scan_keeps_backoff_until_a_new_request(queue_db):
    store.request_store_link_sync([1])
    store.mark_store_link_sync_started(1)
    store.mark_store_link_sync_finished(1, "error", "network unavailable")
    assert saved_state(queue_db, 1)["requested_at"] is not None
    assert 1 not in store.list_due_store_link_token_ids()

    with sqlite3.connect(queue_db) as database:
        database.execute(f"UPDATE {store.STORE_LINK_SYNC_STATE_TABLE} SET last_started_at = ? WHERE token_id = 1",
                         ((datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),))
    assert 1 in store.list_due_store_link_token_ids()

    store.mark_store_link_sync_started(1)
    store.mark_store_link_sync_finished(1, "error", "still unavailable")
    store.request_store_link_sync([1])
    assert 1 in store.list_due_store_link_token_ids()


def test_live_worker_prevents_scheduler_from_requeueing_running_rows(queue_db, monkeypatch):
    store.request_store_link_sync([1])
    store.mark_store_link_sync_started(1)
    lease = bit_runtime_lock.InterProcessLock(sync.STORE_LINK_SYNC_LOCK_KEY, owner="existing-worker")
    assert lease.acquire()
    monkeypatch.setattr(sync, "start_store_link_sync", lambda *_args: pytest.fail("live task must retain ownership"))
    try:
        result = sync.start_due_store_link_sync()
        assert result["started"] is False
        assert result["state"]["running"] is True
        assert saved_state(queue_db, 1)["request_version"] == 1
    finally:
        lease.release()


def test_worker_launch_failure_leaves_a_recoverable_queue(queue_db, monkeypatch):
    monkeypatch.setattr(sync, "_token_records", lambda ids: [{"id": value} for value in ids])

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("no thread available")

    monkeypatch.setattr(sync, "threading", SimpleNamespace(Thread=BrokenThread))
    with pytest.raises(RuntimeError, match="no thread available"):
        sync.start_store_link_sync([1])

    assert sync._sync_state["running"] is False
    assert saved_state(queue_db, 1)["last_status"] == "queued"
    assert 1 in store.list_due_store_link_token_ids()


def test_existing_queue_schema_adds_request_generations(monkeypatch):
    statements = []

    class Cursor:
        def execute(self, sql, params=()):
            statements.append(sql)

        def fetchall(self):
            return [{"COLUMN_NAME": name} for name in (
                "token_id", "requested_at", "last_started_at", "last_completed_at", "last_status", "last_error"
            )]

    monkeypatch.setattr(store, "_sync_state_schema_ready", False)
    store.ensure_store_link_sync_state_table(Cursor())
    alterations = [sql for sql in statements if "ALTER TABLE" in sql]
    assert len(alterations) == 3
    assert "`request_version`" in alterations[0]
    assert "`started_request_version`" in alterations[1]
    assert "`priority`" in alterations[2]


def test_authorization_priority_survives_normal_requests_and_restart(queue_db):
    store.request_store_link_sync([1, 2])
    store.request_store_link_sync([3], priority=True)
    store.request_store_link_sync([3])
    assert store.list_due_store_link_token_ids()[0] == 3
    assert store.order_store_link_token_ids_for_full_sync([1, 2, 3])[0] == 3
    assert store.list_due_store_link_token_ids(priority_only=True) == [3]
    store.mark_store_link_sync_started(3)
    store.mark_store_link_sync_finished(3, "success")
    assert saved_state(queue_db, 3)["priority"] == 0
    assert store.list_due_store_link_token_ids(priority_only=True) == []


def test_new_authorization_overtakes_waiting_stores(queue_db, monkeypatch):
    store.request_store_link_sync([1, 2])
    monkeypatch.setattr(sync, "STORE_LINK_STORE_WORKERS", 1)
    monkeypatch.setattr(sync, "_token_records", lambda ids: [{"id": value} for value in ids])
    monkeypatch.setattr(sync, "run_capacity_store", lambda _kind, _id, fn, record: fn(record))
    seen = []
    def scan(record):
        seen.append(record["id"])
        if record["id"] == 1:
            store.request_store_link_sync([3], priority=True)
        return completed_result(record)
    monkeypatch.setattr(sync, "_sync_store", scan)
    result = sync.run_store_link_sync([1, 2])
    assert seen == [1, 3, 2]
    assert result["total_stores"] == result["processed_stores"] == 3
