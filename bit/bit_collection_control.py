"""声誉/侵权采集共用的批次控制、错峰和失败补跑判断。"""

import json
import os
import random
import time
from pathlib import Path

from bit.bit_runtime_lock import InterProcessLock, RUNTIME_LOCK_DIR


DEFAULT_COLLECTION_MAX_WORKERS = 10
DEFAULT_STAGGER_MIN_SECONDS = 5.0
DEFAULT_STAGGER_MAX_SECONDS = 10.0
DEFAULT_RATE_LIMIT_PAUSE_SECONDS = 300.0
DEFAULT_RETRY_LOCK_WAIT_SECONDS = 120.0

RATE_LIMIT_STATE_PATH = Path(
    os.environ.get("BIT_COLLECTION_RATE_LIMIT_STATE_PATH")
    or (RUNTIME_LOCK_DIR / "collection_rate_limit_state.json")
)

_PERMANENT_FAILURE_MARKERS = (
    "登录失效",
    "未配置站点",
    "验证码",
    "人机验证",
    "verification",
    "captcha",
)


def env_int(name, default, minimum=0):
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def env_float(name, default, minimum=0.0):
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return max(minimum, float(default))


def stagger_bounds(min_seconds=None, max_seconds=None):
    lower = env_float(
        "BIT_COLLECTION_STAGGER_MIN_SECONDS",
        DEFAULT_STAGGER_MIN_SECONDS if min_seconds is None else min_seconds,
    )
    upper = env_float(
        "BIT_COLLECTION_STAGGER_MAX_SECONDS",
        DEFAULT_STAGGER_MAX_SECONDS if max_seconds is None else max_seconds,
    )
    return (upper, lower) if upper < lower else (lower, upper)


def stagger_sleep(min_seconds=None, max_seconds=None, sleep=time.sleep, choose=random.uniform):
    lower, upper = stagger_bounds(min_seconds, max_seconds)
    delay = choose(lower, upper) if upper > lower else lower
    if delay > 0:
        sleep(delay)
    return delay


def _read_rate_limit_state():
    try:
        payload = json.loads(RATE_LIMIT_STATE_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def trip_batch_rate_limit(source, reason="", pause_seconds=None, now=None):
    """触发跨进程批次暂停；暂停期间的重复触发不会不断延长截止时间。"""
    now = time.time() if now is None else float(now)
    pause_seconds = env_float(
        "BIT_COLLECTION_RATE_LIMIT_PAUSE_SECONDS",
        DEFAULT_RATE_LIMIT_PAUSE_SECONDS if pause_seconds is None else pause_seconds,
    )
    lock = InterProcessLock(
        "collection_rate_limit_state_write",
        owner=f"collection_rate_limit:{source}",
        stale_seconds=300,
    )
    if not lock.acquire(timeout=10):
        # 写锁争用时，其他进程通常已经写入暂停状态；直接读取即可。
        return _read_rate_limit_state()
    try:
        current = _read_rate_limit_state()
        current_until = float(current.get("pause_until") or 0)
        pause_until = current_until if current_until > now else now + pause_seconds
        payload = {
            "pause_until": pause_until,
            "triggered_at": now,
            "source": str(source or "collection"),
            "reason": str(reason or "")[:500],
        }
        RATE_LIMIT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = RATE_LIMIT_STATE_PATH.with_suffix(
            f"{RATE_LIMIT_STATE_PATH.suffix}.{os.getpid()}.tmp"
        )
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_path, RATE_LIMIT_STATE_PATH)
        return payload
    finally:
        lock.release()


def batch_pause_remaining(now=None):
    now = time.time() if now is None else float(now)
    state = _read_rate_limit_state()
    try:
        return max(0.0, float(state.get("pause_until") or 0) - now)
    except (TypeError, ValueError):
        return 0.0


def wait_for_batch_resume(source="collection", sleep=time.sleep, now=time.time):
    """所有采集进程在窗口/站点边界调用，保证限频后整批一起暂停。"""
    announced = False
    while True:
        remaining = batch_pause_remaining(now=now())
        if remaining <= 0:
            return
        if not announced:
            print(
                f"{source} 检测到批次限频熔断，统一暂停约 {int(remaining + 0.999)} 秒",
                flush=True,
            )
            announced = True
        sleep(min(5.0, remaining))


def is_failure_status(status):
    text = str(status or "").strip()
    return bool(text) and text != "成功"


def outcome_failed(result_rows):
    return any(
        len(row) >= 4 and is_failure_status(row[3])
        for row in (result_rows or [])
        if isinstance(row, (list, tuple))
    )


def outcome_has_marker(result_rows, *markers):
    return any(
        marker in str(row[3] or "")
        for row in (result_rows or [])
        if isinstance(row, (list, tuple)) and len(row) >= 4
        for marker in markers
    )


def outcome_is_permanent_failure(result_rows):
    return outcome_has_marker(result_rows, *_PERMANENT_FAILURE_MARKERS)


def row_key(row):
    return (
        str(row[0] or "").strip(),
        str(row[1] or "").strip(),
        str(row[3] or "").strip(),
    )
