"""Share BitBrowser request pacing and explicit rate-limit backoff across workers."""

import logging
import math
import os
import sqlite3
import time
from contextlib import contextmanager

from bit import bit_runtime_lock as locks

log = logging.getLogger(__name__)
_now = time.time
_sleep = time.sleep


def request_interval():
    try:
        value = float(os.environ.get("BIT_BROWSER_API_MIN_INTERVAL_SECONDS", "0.25"))
        return max(0.1, value) if math.isfinite(value) else 0.25
    except (TypeError, ValueError):
        return 0.25


@contextmanager
def _database(timeout=5):
    locks.RUNTIME_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    # Separate from lifecycle admission: reserve_window holds its own transaction
    # while probing PIDs, so sharing that database would deadlock.
    db = sqlite3.connect(str(locks.RUNTIME_LOCK_DIR / "browser_api_rate.sqlite3"),
                         timeout=max(0, timeout))
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS rate_state (
            id INTEGER PRIMARY KEY CHECK (id=1), next_request REAL NOT NULL,
            next_open REAL NOT NULL, blocked_until REAL NOT NULL,
            open_failures INTEGER NOT NULL, api_failures INTEGER NOT NULL
        )""")
        db.execute("INSERT OR IGNORE INTO rate_state VALUES (1, 0, 0, 0, 0, 0)")
        db.commit()
        yield db
        db.commit()
    finally:
        db.close()


def try_request(endpoint, open_interval=0, timeout=5):
    """Reserve only an immediately available slot, otherwise return its delay."""
    with _database(timeout) as db:
        db.execute("BEGIN IMMEDIATE")
        next_request, next_open, blocked = db.execute(
            "SELECT next_request, next_open, blocked_until FROM rate_state WHERE id=1"
        ).fetchone()
        now = _now()
        delay = max(next_request, blocked, next_open if endpoint == "open" else 0) - now
        if delay > 0:
            return delay
        db.execute("UPDATE rate_state SET next_request=?, next_open=? WHERE id=1",
                   (now + request_interval(),
                    max(next_open, now + open_interval) if endpoint == "open" else next_open))
        return 0


def wait_for_request(endpoint, timeout, open_interval=0):
    """Reserve an immediate request slot; never sleep inside a DB transaction."""
    deadline = time.monotonic() + max(0, float(timeout))
    while True:
        remaining = max(0, deadline - time.monotonic())
        delay = try_request(endpoint, open_interval, min(5, remaining))
        if delay == 0:
            return
        remaining = max(0, deadline - time.monotonic())
        if delay > remaining:
            raise TimeoutError("等待 BitBrowser 共享限频冷却超时；本次未发送接口请求")
        _sleep(min(delay, 0.5))


def record_response(endpoint, result=None, http_status=None):
    """Only explicit throttling changes backoff; ordinary failures remain errors."""
    message = str(result.get("msg") or result.get("message") or "").lower() if isinstance(result, dict) else ""
    throttled = http_status == 429 or (
        isinstance(result, dict) and result.get("success") is False
        and any(marker in message for marker in (
            "降低接口请求频率", "请求过于频繁", "请求频率", "too many requests", "rate limit",
        ))
    )
    if not throttled and not (isinstance(result, dict) and result.get("success") is True):
        return
    with _database() as db:
        db.execute("BEGIN IMMEDIATE")
        open_failures, api_failures = db.execute(
            "SELECT open_failures, api_failures FROM rate_state WHERE id=1"
        ).fetchone()
        if throttled:
            # An open refusal cools every opener, but lets PID checks and cleanup
            # proceed. HTTP 429 (or a throttled read) cools the entire local API.
            global_limit = http_status == 429 or endpoint != "open"
            failures = min(4, (api_failures if global_limit else open_failures) + 1)
            delay = min(60, 10 * 2 ** (failures - 1))
            column, counter = ("blocked_until", "api_failures") if global_limit else ("next_open", "open_failures")
            db.execute(f"UPDATE rate_state SET {column}=MAX({column}, ?), {counter}=? WHERE id=1",
                       (_now() + delay, failures))
            log.warning("BitBrowser %s 接口限频，共享冷却 %s 秒", endpoint, delay)
        else:
            db.execute("UPDATE rate_state SET api_failures=0 WHERE id=1")
            if endpoint == "open":
                db.execute("UPDATE rate_state SET open_failures=0 WHERE id=1")
