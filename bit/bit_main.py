import os
import sys
import threading
import traceback
from datetime import datetime, timedelta


# Make this entry file runnable both as:
# 1) python bit/bit_main.py
# 2) python -m bit.bit_main
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
from bit.bit_utils import get_now_time


SCHEDULE_LOCK = threading.Lock()


def _get_int_env(name, default):
    value = os.environ.get(name, "")
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_next_daily_boundary(now=None):
    now = now or datetime.now()
    today_midday = now.replace(hour=12, minute=0, second=0, microsecond=0)
    today_midnight = now.replace(hour=2, minute=1, second=0, microsecond=0)
    tomorrow_midnight = today_midnight + timedelta(days=1)
    if now < today_midday:
        return today_midday
    return tomorrow_midnight


def run_reputation_infraction_then_daily():
    """每天 00:00 和 12:00 执行：声誉采集 -> 侵权采集 -> AI 申诉循环。"""
    if not SCHEDULE_LOCK.acquire(blocking=False):
        print(f"{get_now_time()} 定时任务仍在运行，本次跳过，避免声誉/侵权/申诉任务互相冲突<br>")
        return

    started_at = datetime.now()
    print(f"{get_now_time()} 定时任务开始：声誉采集 -> 侵权采集 -> bit_daily_task<br>")
    try:
        print(f"{get_now_time()} 开始执行 bit_reputation_info<br>")
        bit_reputation_info.main()
        print(f"{get_now_time()} bit_reputation_info 执行完成<br>")

        print(f"{get_now_time()} 开始执行 bit_infractions_info<br>")
        bit_infractions_info.main()
        print(f"{get_now_time()} bit_infractions_info 执行完成<br>")

        top_n = _get_int_env("BIT_DAILY_TOP_N", 20)
        max_workers = _get_int_env("BIT_DAILY_MAX_WORKERS", top_n)
        recent_days = _get_int_env("BIT_DAILY_RECENT_DAYS", 30)
        site_pause = _get_int_env("BIT_DAILY_SITE_PAUSE", 30)
        round_interval = _get_int_env("BIT_DAILY_ROUND_INTERVAL", 600)
        stop_buffer_minutes = _get_int_env("BIT_DAILY_STOP_BUFFER_MINUTES", 10)
        next_boundary = get_next_daily_boundary()
        stop_at = next_boundary - timedelta(minutes=max(0, stop_buffer_minutes))
        print(
            f"{get_now_time()} 开始执行 bit_daily_task："
            f"top_n={top_n}, max_workers={max_workers}, recent_days={recent_days}, "
            f"round_interval={round_interval}，将在 {stop_at.strftime('%Y-%m-%d %H:%M:%S')} 前停止<br>"
        )
        bit_daily_task.loop_top_infraction_ai_appeal(
            top_n=top_n,
            max_workers=max_workers,
            recent_days=recent_days,
            site_pause=site_pause,
            round_interval=round_interval,
            stop_at=stop_at,
        )
        print(f"{get_now_time()} bit_daily_task 循环已按计划停止，等待下一次采集任务<br>")
    except Exception as e:
        print(f"{get_now_time()} 定时任务异常：{e}<br>")
        traceback.print_exc()
    finally:
        elapsed = (datetime.now() - started_at).total_seconds()
        print(f"{get_now_time()} 定时任务结束，耗时 {elapsed:.1f} 秒<br>")
        SCHEDULE_LOCK.release()


def build_scheduler():
    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_reputation_infraction_then_daily,
        "cron",
        hour="0,12",
        minute=0,
        id="reputation_infraction_daily_chain",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    return scheduler


if __name__ == "__main__":
    print("------------------------------")
    print(f"{get_now_time()} bit_main 定时任务启动")
    print("默认执行时间：每天 00:00 和 12:00")
    print("任务顺序：bit_reputation_info -> bit_infractions_info -> bit_daily_task 循环到下一次 00/12 点前")
    scheduler = build_scheduler()
    scheduler.start()
