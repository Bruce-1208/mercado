import multiprocessing
import os
import time

import pytest

from bit import bit_api as api, bit_browser_lifecycle as lifecycle, bit_runtime_lock as locks


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(locks, "RUNTIME_LOCK_DIR", tmp_path)
    monkeypatch.setattr(lifecycle, "ensure_browser_reaper", lambda: None)
    monkeypatch.setattr(lifecycle, "available_memory_percent", lambda: 80)
    monkeypatch.setattr(lifecycle, "_LIVE_SNAPSHOT_TTL", 0)
    monkeypatch.setattr(api, "getAllBrowserPids", lambda: {})
    monkeypatch.setattr(api, "getBrowserPids", lambda ids: {})
    monkeypatch.setattr(api, "_post_browser_mutation", lambda *a, **kw: {"success": True})
    monkeypatch.setenv("BIT_BROWSER_MAX_OPEN_WINDOWS", "3")
    yield
    for key, lease in list(api._AUTO_LEASES.items()):
        lease.release()
        api._AUTO_LEASES.pop(key, None)


def test_open_is_registered_before_request_and_close_verified(monkeypatch):
    def request(endpoint, *args, **kwargs):
        assert lifecycle._rows()[0][0] == "one"
        return {"success": True}

    monkeypatch.setattr(api, "_post_browser_mutation", request)
    api.openBrowser("one")
    assert lifecycle._rows() == [("one", "open", 0)]
    assert api.closeBrowser("one")["success"] is True
    assert lifecycle._rows() == []


def test_close_success_with_live_process_keeps_capacity(monkeypatch):
    api.openBrowser("one")
    monkeypatch.setattr(api, "getBrowserPids", lambda ids: {"one": 123})
    monkeypatch.setattr(api.time, "sleep", lambda _: None)
    assert api.closeBrowser("one")["success"] is False
    assert lifecycle._rows() == [("one", "open", 0)]
    assert locks.current_thread_window_lease("one") is None


def test_close_failure_survives_for_reaper(monkeypatch):
    api.openBrowser("one")

    def fail(*args, **kwargs):
        raise ConnectionError("BitBrowser offline")

    monkeypatch.setattr(api, "_post_browser_mutation", fail)
    with pytest.raises(ConnectionError):
        api.closeBrowser("one")
    lifecycle.reap_orphan_windows()
    assert len(lifecycle._rows()) == 1
    monkeypatch.setattr(api, "_post_browser_mutation", lambda *a, **kw: {"success": True})
    lifecycle.reap_orphan_windows()
    assert lifecycle._rows() == []


def test_open_timeout_cannot_be_forgotten_before_late_launch_settles(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError("open response lost")

    monkeypatch.setattr(api, "_post_browser_mutation", fail)
    with pytest.raises(TimeoutError):
        api.openBrowser("one")
    assert locks.current_thread_window_lease("one") is None
    monkeypatch.setattr(api, "_post_browser_mutation", lambda *a, **kw: {"success": True})
    assert api.closeBrowser("one")["success"] is False
    lifecycle.reap_orphan_windows()
    assert len(lifecycle._rows()) == 1
    with lifecycle._database() as db:
        db.execute("UPDATE windows SET settle_until=0")
    lifecycle.reap_orphan_windows()
    assert lifecycle._rows() == []


def test_reaper_never_closes_active_or_handed_off_windows(monkeypatch):
    api.openBrowser("active")
    api.openBrowser("manual")
    api.releaseBrowserLease("manual")
    monkeypatch.setattr(api, "getBrowserPids", lambda ids: {"manual": 123})
    monkeypatch.setattr(api, "_post_browser_mutation", lambda *a, **kw: pytest.fail("unsafe close"))
    lifecycle.reap_orphan_windows()
    assert {row[0] for row in lifecycle._rows()} == {"active", "manual"}


def test_closed_manual_window_stops_consuming_capacity():
    api.openBrowser("manual")
    api.releaseBrowserLease("manual")
    lifecycle.reap_orphan_windows()
    assert lifecycle._rows() == []


def test_pending_and_unmanaged_windows_both_count_toward_limit(monkeypatch):
    monkeypatch.setenv("BIT_BROWSER_MAX_OPEN_WINDOWS", "2")
    lifecycle.reserve_window("pending", lambda: {})
    with pytest.raises(TimeoutError, match="窗口上限"):
        lifecycle.reserve_window("new", lambda: {"manual": 321}, timeout=0)
    assert len(lifecycle._rows()) == 1


def test_window_limit_defaults_to_five_and_never_exceeds_ten(monkeypatch):
    monkeypatch.delenv("BIT_BROWSER_MAX_OPEN_WINDOWS", raising=False)
    assert lifecycle.browser_window_limit() == 5

    monkeypatch.setenv("BIT_BROWSER_MAX_OPEN_WINDOWS", "10")
    assert lifecycle.browser_window_limit() == 10

    monkeypatch.setenv("BIT_BROWSER_MAX_OPEN_WINDOWS", "99")
    assert lifecycle.browser_window_limit() == 10

    monkeypatch.setenv("BIT_BROWSER_MAX_OPEN_WINDOWS", "invalid")
    assert lifecycle.browser_window_limit() == 5


def test_memory_pressure_blocks_new_window_but_allows_existing_connection(monkeypatch):
    monkeypatch.setattr(lifecycle, "available_memory_percent", lambda: 5)
    with pytest.raises(TimeoutError, match="可用内存 5"):
        lifecycle.reserve_window("new", lambda: {}, timeout=0)
    lifecycle.reserve_window("existing", lambda: {"existing": 123}, timeout=0)


def test_pid_api_failure_never_admits_unbounded_opens(monkeypatch):
    def fail():
        raise ConnectionError("cannot inspect capacity")

    monkeypatch.setattr(api, "getAllBrowserPids", fail)
    monkeypatch.setattr(api, "_post_browser_mutation", lambda *a, **kw: pytest.fail("unsafe open"))
    with pytest.raises(ConnectionError):
        api.openBrowser("one")
    assert lifecycle._rows() == []
    assert locks.current_thread_window_lease("one") is None


def test_waiters_share_pid_and_memory_snapshot(monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle, "_LIVE_SNAPSHOT_TTL", 2)
    for index in range(3):
        lifecycle.reserve_window(str(index), lambda: calls.append("probe") or {}, timeout=0)
    for index in range(3, 15):
        with pytest.raises(TimeoutError):
            lifecycle.reserve_window(str(index), lambda: calls.append("probe") or {}, timeout=0)
    assert calls == ["probe"]


def test_pid_verification_failure_keeps_record(monkeypatch):
    api.openBrowser("one")

    def fail(ids):
        raise RuntimeError("PID status unknown")

    monkeypatch.setattr(api, "getBrowserPids", fail)
    with pytest.raises(RuntimeError):
        api.closeBrowser("one")
    assert len(lifecycle._rows()) == 1


def test_finished_thread_with_forgotten_auto_lease_can_be_reclaimed(monkeypatch):
    import threading
    worker = threading.Thread(target=lambda: api.openBrowser("forgotten"))
    worker.start()
    worker.join(timeout=5)
    lock_path = locks.InterProcessLock(locks.window_lock_key("forgotten")).lock_path
    os.utime(lock_path, (time.time() - 10, time.time() - 10))
    lifecycle.reap_orphan_windows()
    assert lifecycle._rows() == []


def _reserve_in_child(directory, window_id, results):
    from pathlib import Path
    locks.RUNTIME_LOCK_DIR = Path(directory)
    lifecycle.available_memory_percent = lambda: 80
    try:
        lifecycle.reserve_window(window_id, lambda: {}, timeout=0)
        results.put(True)
    except TimeoutError:
        results.put(False)


def test_capacity_is_shared_across_independent_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    workers = [context.Process(target=_reserve_in_child, args=(str(tmp_path), str(i), results))
               for i in range(6)]
    try:
        for worker in workers:
            worker.start()
        admitted = [results.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
        assert sum(admitted) == 3
        assert len(lifecycle._rows()) == 3
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        results.close()


def _open_in_child(directory, ready):
    from pathlib import Path
    locks.RUNTIME_LOCK_DIR = Path(directory)
    lifecycle.available_memory_percent = lambda: 80
    lease = locks.create_window_lease("orphan", owner="worker")
    assert lease.acquire(timeout=0)
    lifecycle.reserve_window("orphan", lambda: {})
    lifecycle.mark_open("orphan")
    ready.set()
    time.sleep(60)


def test_killed_worker_is_recovered_from_durable_registry(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    worker = context.Process(target=_open_in_child, args=(str(tmp_path), ready))
    worker.start()
    try:
        assert ready.wait(15)
    finally:
        worker.terminate()
        worker.join(timeout=5)
    lock_path = locks.InterProcessLock(locks.window_lock_key("orphan")).lock_path
    os.utime(lock_path, (time.time() - 10, time.time() - 10))
    lifecycle.reap_orphan_windows()
    assert lifecycle._rows() == []


@pytest.mark.parametrize("payload, expected", [
    ({"a": 123}, {"a": 123}),
    ({"success": True, "data": {"a": 123, "b": None, "c": 0}}, {"a": 123}),
])
def test_pid_response_accepts_documented_shapes(monkeypatch, payload, expected):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr(api.requests, "post", lambda *a, **kw: Response())
    assert api._pid_response("pids/all", {}) == expected
