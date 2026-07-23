import os
import random
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta


# 兼容 python bit/bit_main.py 与 python -m bit.bit_main 两种启动方式。
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from apscheduler.schedulers.blocking import BlockingScheduler

from bit import bit_daily_task
from bit import bit_infractions_info
from bit import bit_reputation_info
from bit.bit_collection_control import (
    DEFAULT_COLLECTION_MAX_WORKERS,
    env_float,
    wait_for_batch_resume,
)
from bit.bit_runtime_lock import InterProcessLock
from bit.bit_utils import get_now_time


SCHEDULE_LOCK = threading.Lock()
SCHEDULE_INTERVAL_HOURS = 12
SCHEDULE_PROCESS_LOCK_KEY = "reputation_infraction_schedule"


def _get_int_env(name, default):
    value = os.environ.get(name, "")
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_bool_env(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return str(value).strip().casefold() in {"1", "true", "yes", "on", "enable", "enabled"}


def get_next_run_boundary(started_at=None):
    started_at = started_at or datetime.now()
    return started_at + timedelta(hours=SCHEDULE_INTERVAL_HOURS)


def _collection_options(prefix):
    return {
        "max_workers": max(
            1,
            _get_int_env(
                f"BIT_{prefix}_MAX_WORKERS",
                DEFAULT_COLLECTION_MAX_WORKERS,
            ),
        ),
        "stagger_min_seconds": env_float("BIT_COLLECTION_STAGGER_MIN_SECONDS", 5),
        "stagger_max_seconds": env_float("BIT_COLLECTION_STAGGER_MAX_SECONDS", 10),
        "retry_failed": True,
    }


def _wait_between_collections():
    lower = env_float("BIT_REPUTATION_INFRACTION_WAIT_MIN_SECONDS", 180)
    upper = env_float("BIT_REPUTATION_INFRACTION_WAIT_MAX_SECONDS", 300)
    if upper < lower:
        lower, upper = upper, lower
    delay = random.uniform(lower, upper) if upper > lower else lower
    print(
        f"{get_now_time()} 声誉采集完成，等待 {delay:.1f} 秒后启动侵权采集<br>",
        flush=True,
    )
    if delay > 0:
        time.sleep(delay)
    # 若声誉末尾触发过全局限频，3–5 分钟冷却仍不足时继续等到批次熔断解除。
    wait_for_batch_resume("侵权启动")
    return delay


def _run_ai_appeal_loop(started_at):
    if not _get_bool_env("BIT_ENABLE_AI_APPEAL_LOOP", False):
        print(
            f"{get_now_time()} AI 申诉循环保持暂停；"
            "如需恢复请显式设置 BIT_ENABLE_AI_APPEAL_LOOP=1<br>",
            flush=True,
        )
        return None

    top_n = _get_int_env("BIT_DAILY_TOP_N", bit_daily_task.DEFAULT_DAILY_TOP_N)
    max_workers = _get_int_env(
        "BIT_DAILY_MAX_WORKERS",
        bit_daily_task.DEFAULT_DAILY_MAX_WORKERS,
    )
    recent_days = _get_int_env(
        "BIT_DAILY_RECENT_DAYS",
        bit_daily_task.DEFAULT_DAILY_RECENT_DAYS,
    )
    site_pause = _get_int_env("BIT_DAILY_SITE_PAUSE", 30)
    round_interval = _get_int_env("BIT_DAILY_ROUND_INTERVAL", 600)
    stop_buffer_minutes = _get_int_env("BIT_DAILY_STOP_BUFFER_MINUTES", 10)
    next_boundary = get_next_run_boundary(started_at)
    stop_at = next_boundary - timedelta(minutes=max(0, stop_buffer_minutes))
    print(
        f"{get_now_time()} 开始执行 AI 申诉循环：top_n={top_n}, "
        f"max_workers={max_workers}, recent_days={recent_days}, "
        f"round_interval={round_interval}，计划在 {stop_at:%Y-%m-%d %H:%M:%S} 前停止<br>"
    )
    result = bit_daily_task.loop_top_infraction_ai_appeal(
        top_n=top_n,
        max_workers=max_workers,
        recent_days=recent_days,
        site_pause=site_pause,
        round_interval=round_interval,
        stop_at=stop_at,
    )
    print(f"{get_now_time()} AI 申诉循环已按计划停止<br>")
    return result


def run_reputation_infraction_then_daily():
    """声誉采集 -> 冷却 3–5 分钟 -> 侵权采集；AI 申诉默认暂停。"""
    if not SCHEDULE_LOCK.acquire(blocking=False):
        print(f"{get_now_time()} 本进程调度任务仍在运行，本次跳过<br>")
        return None

    try:
        process_lock = InterProcessLock(
            SCHEDULE_PROCESS_LOCK_KEY,
            owner="bit_main_scheduler",
            metadata={"task_type": "reputation_infraction_chain"},
            stale_seconds=24 * 60 * 60,
        )
        process_lock_acquired = process_lock.acquire(timeout=0)
    except Exception as exc:
        print(f"{get_now_time()} 创建调度进程锁失败，本次跳过：{exc}<br>")
        SCHEDULE_LOCK.release()
        return None
    if not process_lock_acquired:
        print(f"{get_now_time()} 另一调度进程仍占用采集链，本次跳过<br>")
        SCHEDULE_LOCK.release()
        return None

    started_at = datetime.now()
    print(
        f"{get_now_time()} 定时任务开始：声誉(并发10) -> 冷却3–5分钟 -> "
        "侵权(并发10)，店铺错峰5–10秒，AI申诉暂停<br>"
    )
    try:
        reputation_options = _collection_options("REPUTATION")
        print(f"{get_now_time()} 开始执行声誉采集：{reputation_options}<br>")
        reputation_result = bit_reputation_info.main(**reputation_options)
        print(f"{get_now_time()} 声誉采集执行完成<br>")

        _wait_between_collections()

        infraction_options = _collection_options("INFRACTION")
        print(f"{get_now_time()} 开始执行侵权采集：{infraction_options}<br>")
        infraction_result = bit_infractions_info.main(**infraction_options)
        print(f"{get_now_time()} 侵权采集执行完成<br>")

        appeal_result = _run_ai_appeal_loop(started_at)
        return {
            "reputation": reputation_result,
            "infraction": infraction_result,
            "ai_appeal": appeal_result,
        }
    except Exception as exc:
        print(f"{get_now_time()} 定时任务异常：{exc}<br>")
        traceback.print_exc()
        return None
    finally:
        elapsed = (datetime.now() - started_at).total_seconds()
        print(f"{get_now_time()} 定时任务结束，耗时 {elapsed:.1f} 秒，调度锁已释放<br>")
        process_lock.release()
        SCHEDULE_LOCK.release()


def build_scheduler():
    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_reputation_infraction_then_daily,
        "interval",
        hours=SCHEDULE_INTERVAL_HOURS,
        next_run_time=datetime.now(),
        id="reputation_infraction_daily_chain",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    return scheduler


if __name__ == "__main__":
    print("------------------------------")
    print(f"{get_now_time()} bit_main 定时任务启动")
    print("启动后立即执行一次，之后每 12 小时执行一次")
    print("任务顺序：声誉采集 -> 冷却 3–5 分钟 -> 侵权采集；AI 申诉默认暂停")
    build_scheduler().start()
