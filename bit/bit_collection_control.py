"""声誉/侵权采集共用的批次控制、错峰和失败补跑判断。"""

import json
import os
import random
import re
import time
import csv
from datetime import datetime
from pathlib import Path

from bit.bit_mercado_limit import is_mercado_rate_limited_text
from bit.bit_runtime_lock import InterProcessLock, RUNTIME_LOCK_DIR


DEFAULT_COLLECTION_MAX_WORKERS = 3
DEFAULT_STAGGER_MIN_SECONDS = 5.0
DEFAULT_STAGGER_MAX_SECONDS = 10.0
DEFAULT_RATE_LIMIT_PAUSE_SECONDS = 300.0
DEFAULT_RETRY_LOCK_WAIT_SECONDS = 120.0
CONFIG_SITE_SEPARATOR_PATTERN = re.compile(r"[，,、/;；|\s]+")

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


def is_rate_limited_text(value):
    """兼容旧调用点；限频识别统一交给 ``bit_mercado_limit``。"""
    return is_mercado_rate_limited_text(value)


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


def _selection_values(values):
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    return tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        )
    )


def split_config_sites(value):
    """拆分数据库配置中的站点，保持原始顺序并去重。"""
    return list(
        dict.fromkeys(
            site.strip()
            for site in CONFIG_SITE_SEPARATOR_PATTERN.split(str(value or ""))
            if site.strip()
        )
    )


def filter_config_rows(rows, selected_shops=None, selected_sites=None):
    """按前端选择过滤店铺配置，并把每行站点缩小到实际选中的交集。"""
    shop_names = set(_selection_values(selected_shops))
    site_names = set(_selection_values(selected_sites))
    filtered = []
    for raw_row in rows or []:
        if not isinstance(raw_row, (list, tuple)) or len(raw_row) < 4:
            continue
        shop_name = str(raw_row[1] or "").strip()
        if shop_names and shop_name not in shop_names:
            continue
        configured_sites = split_config_sites(raw_row[3])
        target_sites = [
            site for site in configured_sites if not site_names or site in site_names
        ]
        if not target_sites:
            continue
        row = list(raw_row)
        row[3] = "，".join(target_sites)
        filtered.append(tuple(row))
    return filtered


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


def failed_result_rows(result_rows):
    """返回最终未成功读取的站点记录。"""
    return [
        row
        for row in (result_rows or [])
        if isinstance(row, (list, tuple))
        and len(row) >= 4
        and is_failure_status(row[3])
    ]


def failed_sites(result_rows):
    """按出现顺序返回最终失败的站点，供失败补跑精确缩小范围。"""
    sites = []
    for row in failed_result_rows(result_rows):
        site = str(row[2] or "").strip() if len(row) > 2 else ""
        if site and site not in sites:
            sites.append(site)
    return sites


def merge_site_retry_outcome(original_outcome, retry_outcome):
    """用补跑结果替换对应站点，保留同店铺其他已成功站点的数据。"""
    original_row, original_data, original_results = original_outcome
    _retry_row, retry_data, retry_results = retry_outcome
    retried_sites = {
        str(row[2] or "").strip()
        for row in (retry_results or [])
        if isinstance(row, (list, tuple)) and len(row) > 2 and str(row[2] or "").strip()
    }
    if not retried_sites:
        return original_outcome

    merged_data = [
        row
        for row in (original_data or [])
        if not (
            isinstance(row, (list, tuple))
            and len(row) > 1
            and str(row[1] or "").strip() in retried_sites
        )
    ]
    merged_data.extend(retry_data or [])
    merged_results = [
        row
        for row in (original_results or [])
        if not (
            isinstance(row, (list, tuple))
            and len(row) > 2
            and str(row[2] or "").strip() in retried_sites
        )
    ]
    merged_results.extend(retry_results or [])
    return original_row, merged_data, merged_results


def write_unreadable_site_report(
    collection_name,
    result_rows,
    output_dir=None,
    recorded_at=None,
):
    """把本轮最终仍无法读取的站点写入独立 CSV，避免邮件或数据库异常时丢失。"""
    failed_rows = failed_result_rows(result_rows)
    if not failed_rows:
        return None

    recorded_at = recorded_at or datetime.now()
    output_dir = Path(
        output_dir
        or (Path(__file__).resolve().parent / "采集失败记录")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in str(collection_name or "采集")
    ).strip("_") or "采集"
    output_path = output_dir / (
        f"无法读取站点-{safe_name}-{recorded_at:%Y%m%d-%H%M%S}-{os.getpid()}.csv"
    )

    with output_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("任务类型", "店铺名", "站点", "状态", "记录时间"))
        for row in failed_rows:
            writer.writerow(
                (
                    row[0] if len(row) > 0 else collection_name,
                    row[1] if len(row) > 1 else "",
                    row[2] if len(row) > 2 else "",
                    row[3] if len(row) > 3 else "",
                    row[4] if len(row) > 4 else recorded_at.strftime("%Y-%m-%d %H:%M:%S"),
                )
            )
    return output_path


def row_key(row):
    return (
        str(row[0] or "").strip(),
        str(row[1] or "").strip(),
        str(row[3] or "").strip(),
    )
