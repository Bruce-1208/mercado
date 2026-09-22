"""Durable ownership and capacity for BitBrowser (which outlives Python workers).

Only registered automation windows may be reclaimed. A successful HTTP close is
not proof that Chromium exited; bit_api verifies the live PID before forgetting it.
"""

import logging
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

from bit import bit_runtime_lock as locks

log = logging.getLogger(__name__)
_reaper_guard = threading.Lock()
_reaper_pid = None
_LIVE_SNAPSHOT_TTL = 2.0


def _setting(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def browser_window_limit():
    """Share the admission limit with schedulers before they start workers."""
    return _setting("BIT_BROWSER_MAX_OPEN_WINDOWS", 3)


@contextmanager
def _database():
    locks.RUNTIME_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(locks.RUNTIME_LOCK_DIR / "browser_lifecycle.sqlite3"), timeout=15,
    )
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS windows (
            window_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            settle_until REAL NOT NULL DEFAULT 0
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS observation (
            id INTEGER PRIMARY KEY CHECK (id=1), checked_at REAL NOT NULL,
            pids TEXT NOT NULL, available REAL
        )""")
        yield connection
        connection.commit()
    finally:
        connection.close()


def _rows():
    with _database() as db:
        return db.execute("SELECT window_id, state, settle_until FROM windows").fetchall()


def available_memory_percent():
    """Use OS counters without adding a dependency to packaged local agents."""
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
                    (name, ctypes.c_uint64) for name in (
                        "total", "available", "page_total", "page_available",
                        "virtual_total", "virtual_available", "extended_available",
                    )
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return 100 * status.available / status.total
        elif sys.platform == "darwin":
            output = subprocess.check_output(
                ["/usr/bin/memory_pressure", "-Q"], text=True, timeout=3,
            )
            for line in output.splitlines():
                if "System-wide memory free percentage:" in line:
                    return float(line.rsplit(":", 1)[1].strip().rstrip("%"))
        else:
            from pathlib import Path
            values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
            return 100 * int(values["MemAvailable"].split()[0]) / int(values["MemTotal"].split()[0])
    except (OSError, ValueError, KeyError, ZeroDivisionError, subprocess.SubprocessError):
        log.warning("无法读取系统可用内存，继续执行窗口数量上限保护", exc_info=True)
    return None


def _observe(db, live_windows):
    # Waiting workers must not each poll /pids/all once per second: BitBrowser
    # limits the whole local API to 10 requests/s. Share one short-lived snapshot.
    row = db.execute("SELECT checked_at, pids, available FROM observation WHERE id=1").fetchone()
    if row and 0 <= time.time() - row[0] < _LIVE_SNAPSHOT_TTL:
        return set(json.loads(row[1])), row[2]
    alive = set(live_windows())
    available = available_memory_percent()
    db.execute("INSERT OR REPLACE INTO observation VALUES (1, ?, ?, ?)",
               (time.time(), json.dumps(sorted(alive)), available))
    return alive, available


def reserve_window(window_id, live_windows, timeout=180):
    """Persist BEFORE /open; pending, failed-close and manual windows use capacity.

    SQLite serializes admission across all worker processes, including independent
    task pools. Do not hold its transaction while waiting for capacity.
    """
    window_id = str(window_id)
    deadline = time.monotonic() + max(0, float(timeout))
    limit = browser_window_limit()
    announced = False
    while True:
        with _database() as db:
            db.execute("BEGIN IMMEDIATE")
            tracked = {row[0] for row in db.execute("SELECT window_id FROM windows")}
            alive, available = _observe(db, live_windows)  # Failure must fail closed.
            memory_ok = available is None or available >= _setting("BIT_BROWSER_MIN_AVAILABLE_MEMORY_PERCENT", 15)
            if window_id in alive or (memory_ok and (window_id in tracked or len(tracked | alive) < limit)):
                db.execute(
                    "INSERT OR REPLACE INTO windows VALUES (?, 'opening', ?)",
                    (window_id, time.time() + _setting("BIT_BROWSER_OPEN_SETTLE_SECONDS", 120)),
                )
                return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"等待比特浏览器内存容量超时：窗口上限 {limit}，可用内存 {available}%")
        if not announced:
            log.warning("等待比特浏览器资源释放：窗口上限 %s，可用内存 %s%%，窗口 %s", limit, available, window_id)
            announced = True
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def mark_open(window_id):
    with _database() as db:
        db.execute("UPDATE windows SET state='open', settle_until=0 WHERE window_id=?",
                   (str(window_id),))


def mark_launch_started(window_id, request_timeout):
    with _database() as db:
        db.execute("UPDATE windows SET state='opening', settle_until=? WHERE window_id=?",
                   (time.time() + max(1, float(request_timeout)) +
                    _setting("BIT_BROWSER_OPEN_SETTLE_SECONDS", 120), str(window_id)))


def forget_window(window_id):
    with _database() as db:
        db.execute("DELETE FROM windows WHERE window_id=?", (str(window_id),))


def mark_manual(window_id):
    """Explicit handoff: still counts toward capacity, never auto-close it."""
    with _database() as db:
        db.execute("UPDATE windows SET state='manual' WHERE window_id=?", (str(window_id),))


def confirm_closed(window_id):
    """Keep uncertain launches until they settle, even if no PID exists yet."""
    with _database() as db:
        row = db.execute("SELECT state, settle_until FROM windows WHERE window_id=?",
                         (str(window_id),)).fetchone()
        if row and row[0] == "opening" and row[1] > time.time():
            return False
        db.execute("DELETE FROM windows WHERE window_id=?", (str(window_id),))
    return True


def reap_orphan_windows():
    from bit.bit_api import closeBrowser, getBrowserPids, release_finished_thread_leases

    release_finished_thread_leases()

    # One sweeper per lock directory, no duplicate API bursts from 15 workers.
    sweep = locks.InterProcessLock("bit_browser_reaper", owner="browser_reaper")
    if not sweep.acquire(timeout=0):
        return
    try:
        for window_id, state, settle_until in _rows():
            lease = locks.create_window_lease(window_id, owner="browser_reaper", task_type="cleanup")
            if not lease.acquire(timeout=0):
                continue  # An active task owns this window, regardless of age.
            try:
                # Re-read after acquiring the lease: it may have been handed to
                # the user between the snapshot and this lock acquisition.
                with _database() as db:
                    row = db.execute("SELECT state, settle_until FROM windows WHERE window_id=?",
                                     (window_id,)).fetchone()
                if row is None:
                    continue
                state, settle_until = row
                if settle_until > time.time():
                    continue
                if state == "manual":
                    if window_id not in getBrowserPids([window_id]):
                        forget_window(window_id)
                    continue
                result = closeBrowser(window_id, lease=lease, request_timeout=5, api_lock_timeout=5)
                if result.get("success") is True:
                    log.info("已回收自动任务遗留的比特窗口：%s", window_id)
                else:
                    log.warning("比特窗口尚未释放，保留登记并稍后重试：%s %s", window_id, result)
            except Exception:
                log.exception("回收比特窗口失败，保留登记：%s", window_id)
            finally:
                lease.release()
    finally:
        sweep.release()


def ensure_browser_reaper():
    """Run in both the long-lived server and workers; survives worker termination."""
    global _reaper_pid
    with _reaper_guard:
        if _reaper_pid == os.getpid():
            return
        _reaper_pid = os.getpid()

        def run():
            while True:
                try:
                    reap_orphan_windows()
                except Exception:
                    log.exception("比特窗口回收巡检失败，将自动重试")
                time.sleep(_setting("BIT_BROWSER_REAPER_INTERVAL_SECONDS", 15))

        threading.Thread(target=run, name="bit-browser-reaper", daemon=True).start()
