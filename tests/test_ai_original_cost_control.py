"""Offline scheduling and distributed-lock checks for per-product budgets."""

from datetime import datetime, timedelta, timezone

import pytest

from erp.ai_original_cost_control import product_generation_lock, wait_for_low_cost_window


def test_peak_wait_announces_once_and_sleeps_in_interruptible_chunks():
    # Monday 11:59:10 Beijing. Never rely on the machine's local timezone.
    clock = [datetime(2026, 10, 5, 3, 59, 10, tzinfo=timezone.utc)]
    sleeps = []
    announcements = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    wait_for_low_cost_window(announcements.append, now=lambda: clock[0], sleep=sleep)
    assert sleeps == [30, 20]
    assert announcements == ["2026-10-05T12:00:00+08:00"]
    assert clock[0] == datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)


def test_weekend_does_not_wait_or_announce():
    wait_for_low_cost_window(
        lambda value: pytest.fail("weekends are offpeak"),
        now=lambda: datetime(2026, 10, 3, 2, tzinfo=timezone.utc),
        sleep=lambda seconds: pytest.fail("must not wait"),
    )


def test_wait_can_be_interrupted_without_starting_paid_work():
    def stop(seconds):
        raise InterruptedError("stop server")
    with pytest.raises(InterruptedError):
        wait_for_low_cost_window(
            now=lambda: datetime(2026, 10, 5, 2, tzinfo=timezone.utc), sleep=stop)


class FakeConnection:
    def __init__(self, acquired=1, owned=1):
        self.acquired = acquired
        self.owned = owned
        self.closed = False
        self.queries = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.queries.append((sql, params))

    def fetchone(self):
        sql = self.queries[-1][0]
        if "GET_LOCK" in sql:
            return {"acquired": self.acquired}
        if "IS_USED_LOCK" in sql:
            return {"owned": self.owned}
        return {}

    def close(self):
        self.closed = True


def test_product_lock_verifies_owner_and_releases_same_connection():
    connection = FakeConnection()
    with product_generation_lock(42, connection_factory=lambda: connection) as check_owner:
        assert connection.closed is False
        check_owner()
    assert connection.closed is True
    queries = connection.queries
    assert "GET_LOCK(%s, 0)" in queries[0][0]
    assert "IS_USED_LOCK(%s) = CONNECTION_ID()" in queries[1][0]
    assert "RELEASE_LOCK(%s)" in queries[2][0]
    assert [params for _, params in queries] == [("mercado-ai-budget:42",)] * 3


@pytest.mark.parametrize("acquired", [0, None])
def test_busy_lock_never_enters_generation_and_closes_connection(acquired):
    connection = FakeConnection(acquired=acquired)
    with pytest.raises(RuntimeError, match="已有生成任务"):
        with product_generation_lock(42, connection_factory=lambda: connection):
            pytest.fail("duplicate worker must not process product")
    assert connection.closed
    assert len(connection.queries) == 1


def test_lost_connection_ownership_stops_before_next_request():
    connection = FakeConnection()
    with pytest.raises(RuntimeError, match="生成锁已失效"):
        with product_generation_lock(42, connection_factory=lambda: connection) as check_owner:
            check_owner()
            connection.owned = 0
            check_owner()
    assert connection.closed
    assert "RELEASE_LOCK" in connection.queries[-1][0]


def test_generation_exception_still_releases_lock():
    connection = FakeConnection()
    with pytest.raises(ValueError, match="invalid source"):
        with product_generation_lock(42, connection_factory=lambda: connection):
            raise ValueError("invalid source")
    assert connection.closed
    assert "RELEASE_LOCK" in connection.queries[-1][0]


@pytest.mark.parametrize("limit,reserved,expected", [(6, 1, 2), (12, 1, 5), (6, 2, 1)])
def test_generation_workers_leave_room_for_checkpoints_and_ui(monkeypatch, limit, reserved, expected):
    from erp.ai_original_cost_control import generation_worker_limit
    monkeypatch.setenv("MYSQL_POOL_MAX_CONNECTIONS", str(limit))
    monkeypatch.setenv("MYSQL_POOL_AUTH_RESERVED", str(reserved))
    assert generation_worker_limit() == expected


def test_generation_admission_waits_before_taking_database_connection(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from erp import ai_original_cost_control as control
    monkeypatch.setenv("MYSQL_POOL_MAX_CONNECTIONS", "6")
    monkeypatch.setenv("MYSQL_POOL_AUTH_RESERVED", "1")
    started = threading.Event()
    opened = threading.Event()
    def queued():
        started.set()
        def connect():
            opened.set()
            return FakeConnection()
        with product_generation_lock(3, connection_factory=connect):
            pass
    with ThreadPoolExecutor(max_workers=1) as executor:
        with product_generation_lock(1, connection_factory=FakeConnection):
            with product_generation_lock(2, connection_factory=FakeConnection):
                future = executor.submit(queued)
                assert started.wait(2)
                assert not opened.wait(.1)
            future.result(timeout=2)
        assert opened.is_set()
    assert control._active_generations == 0


def test_connection_failure_returns_generation_slot():
    from erp import ai_original_cost_control as control
    def unavailable():
        raise TimeoutError("pool busy")
    with pytest.raises(TimeoutError):
        with product_generation_lock(1, connection_factory=unavailable):
            pass
    assert control._active_generations == 0


def test_task_reports_budgeted_concurrency_and_finishes_queue(monkeypatch):
    from test_ai_original_batch_isolation import worker, row
    monkeypatch.setenv('MYSQL_POOL_MAX_CONNECTIONS', '6')
    monkeypatch.setenv('MYSQL_POOL_AUTH_RESERVED', '1')
    items = [row(pid) for pid in range(1, 5)]
    fixture = worker(monkeypatch, items)
    fixture.run(items, workers=3, model='qwen3.7-flash')
    assert fixture.state['worker_count'] == 2
    assert fixture.state['completed_count'] == 4
    assert fixture.state['failed_count'] == 0
    assert not fixture.state['running']
