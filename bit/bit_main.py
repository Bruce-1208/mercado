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
SCHEDULE_INTERVAL_HOURS = 2
DEFAULT_CHAIN_REPEAT_SECONDS = 2 * 60 * 60
DEFAULT_MAIN_BROWSER_WORKER_LIMIT = 10
DEFAULT_MAIN_APPEAL_ROUNDS = 10
MAIN_APPEAL_TYPES = (
    bit_daily_task.APPEAL_TYPE_INFRACTION,
    bit_daily_task.APPEAL_TYPE_DELAY,
    bit_daily_task.APPEAL_TYPE_COMPLAINT,
    bit_daily_task.APPEAL_TYPE_CANCELLATION,
)
# 保留旧锁名，确保升级前后运行的调度进程仍然互斥。
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


def _main_browser_worker_limit():
    configured_limit = _get_int_env(
        "BIT_MAIN_BROWSER_WORKER_LIMIT",
        DEFAULT_MAIN_BROWSER_WORKER_LIMIT,
    )
    return min(DEFAULT_MAIN_BROWSER_WORKER_LIMIT, max(1, configured_limit))


def _memory_safe_worker_count(requested):
    return min(max(1, int(requested)), _main_browser_worker_limit())


def get_next_run_boundary(started_at=None):
    started_at = started_at or datetime.now()
    return started_at + timedelta(hours=SCHEDULE_INTERVAL_HOURS)


def _collection_options(prefix):
    requested_workers = max(
        1,
        _get_int_env(
            f"BIT_{prefix}_MAX_WORKERS",
            DEFAULT_COLLECTION_MAX_WORKERS,
        ),
    )
    return {
        "max_workers": _memory_safe_worker_count(requested_workers),
        "stagger_min_seconds": env_float("BIT_COLLECTION_STAGGER_MIN_SECONDS", 5),
        "stagger_max_seconds": env_float("BIT_COLLECTION_STAGGER_MAX_SECONDS", 10),
        "retry_failed": True,
    }


def _wait_between_collections():
    legacy_lower = env_float("BIT_REPUTATION_INFRACTION_WAIT_MIN_SECONDS", 180)
    legacy_upper = env_float("BIT_REPUTATION_INFRACTION_WAIT_MAX_SECONDS", 300)
    lower = env_float("BIT_INFRACTION_REPUTATION_WAIT_MIN_SECONDS", legacy_lower)
    upper = env_float("BIT_INFRACTION_REPUTATION_WAIT_MAX_SECONDS", legacy_upper)
    if upper < lower:
        lower, upper = upper, lower
    delay = random.uniform(lower, upper) if upper > lower else lower
    print(
        f"{get_now_time()} 侵权采集完成，等待 {delay:.1f} 秒后启动声誉采集<br>",
        flush=True,
    )
    if delay > 0:
        time.sleep(delay)
    # 若侵权末尾触发过全局限频，3–5 分钟冷却仍不足时继续等到批次熔断解除。
    wait_for_batch_resume("声誉启动")
    return delay


def _run_ai_appeal_loop(started_at):
    if not _get_bool_env("BIT_ENABLE_AI_APPEAL_LOOP", True):
        print(
            f"{get_now_time()} 已按 BIT_ENABLE_AI_APPEAL_LOOP=0 跳过 AI 申诉循环<br>",
            flush=True,
        )
        return None

    top_n = _get_int_env("BIT_DAILY_TOP_N", bit_daily_task.DEFAULT_DAILY_TOP_N)
    max_workers = _get_int_env(
        "BIT_DAILY_MAX_WORKERS",
        bit_daily_task.DEFAULT_DAILY_MAX_WORKERS,
    )
    max_workers = _memory_safe_worker_count(max_workers)
    recent_days = _get_int_env(
        "BIT_DAILY_RECENT_DAYS",
        bit_daily_task.DEFAULT_DAILY_RECENT_DAYS,
    )
    site_pause = _get_int_env("BIT_DAILY_SITE_PAUSE", 30)
    round_interval = _get_int_env("BIT_DAILY_ROUND_INTERVAL", 600)
    appeal_rounds = max(
        1,
        _get_int_env("BIT_DAILY_APPEAL_ROUNDS", DEFAULT_MAIN_APPEAL_ROUNDS),
    )
    min_rate = os.environ.get("BIT_DAILY_MIN_RATE", "0")
    appeal_labels = " -> ".join(MAIN_APPEAL_TYPES)
    print(
        f"{get_now_time()} 开始执行 AI 申诉循环：top_n={top_n}, "
        f"max_workers={max_workers}, recent_days={recent_days}, "
        f"round_interval={round_interval}，每轮 {appeal_labels}，"
        f"共执行 {appeal_rounds} 轮<br>"
    )
    task_lock = bit_daily_task.acquire_daily_task_lock(
        owner="bit_main.py:all_appeal_types",
        mode=f"{appeal_rounds}_rounds",
    )
    if task_lock is None:
        owner = bit_daily_task.get_daily_task_lock_owner()
        raise bit_daily_task.DailyTaskAlreadyRunning(
            f"bit_daily_task 已在其他进程运行：{owner}"
        )

    all_rounds = []
    try:
        for round_no in range(1, appeal_rounds + 1):
            round_started = time.time()
            round_result = {
                "round": round_no,
                "results": {},
                "errors": {},
            }
            print(
                f"{get_now_time()} 开始第 {round_no}/{appeal_rounds} 轮 "
                f"AI 申诉：{appeal_labels}<br>"
            )
            for appeal_type in MAIN_APPEAL_TYPES:
                try:
                    print(
                        f"{get_now_time()} 第 {round_no}/{appeal_rounds} 轮"
                        f"开始 {appeal_type} AI 申诉，最多 {max_workers} 个进程<br>"
                    )
                    round_result["results"][appeal_type] = (
                        bit_daily_task.run_ai_appeal_once(
                            appeal_type,
                            top_n=top_n,
                            max_workers=max_workers,
                            recent_days=recent_days,
                            site_pause=site_pause,
                            only_active=True,
                            min_rate=min_rate,
                            _task_lock=task_lock,
                        )
                    )
                    print(
                        f"{get_now_time()} 第 {round_no}/{appeal_rounds} 轮"
                        f"{appeal_type} AI 申诉完成<br>"
                    )
                except Exception as exc:
                    round_result["errors"][appeal_type] = str(exc)
                    print(
                        f"{get_now_time()} 第 {round_no}/{appeal_rounds} 轮"
                        f"{appeal_type} AI 申诉异常，继续下一类：{exc}<br>"
                    )
                    traceback.print_exc()
            all_rounds.append(round_result)

            if round_no >= appeal_rounds:
                break
            sleep_seconds = max(
                0,
                int(round_interval) - (time.time() - round_started),
            )
            print(
                f"{get_now_time()} 第 {round_no}/{appeal_rounds} 轮全部申诉类型完成，"
                f"等待 {sleep_seconds:.1f} 秒后重新计算 Top 店铺<br>"
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    finally:
        task_lock.release()

    print(f"{get_now_time()} 四类 AI 申诉循环已完成 {appeal_rounds} 轮<br>")
    return {
        "rounds": all_rounds,
        "round_count": appeal_rounds,
        "max_workers": max_workers,
        "appeal_types": list(MAIN_APPEAL_TYPES),
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def run_infraction_reputation_then_appeal():
    """侵权采集 -> 声誉采集 -> 四类 AI 申诉循环 10 轮。"""
    if not SCHEDULE_LOCK.acquire(blocking=False):
        print(f"{get_now_time()} 本进程调度任务仍在运行，本次跳过<br>")
        return None

    try:
        process_lock = InterProcessLock(
            SCHEDULE_PROCESS_LOCK_KEY,
            owner="bit_main_scheduler",
            metadata={"task_type": "infraction_reputation_appeal_chain"},
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
    reputation_options = _collection_options("REPUTATION")
    infraction_options = _collection_options("INFRACTION")
    print(
        f"{get_now_time()} 定时任务开始：侵权(并发{infraction_options['max_workers']}) "
        f"-> 冷却 -> 声誉(并发{reputation_options['max_workers']}) "
        f"-> 侵权/延误/投诉/取消率 AI 申诉 10 轮，"
        f"店铺错峰{infraction_options['stagger_min_seconds']:.0f}–"
        f"{infraction_options['stagger_max_seconds']:.0f}秒<br>"
    )
    try:
        phase_errors = {}
        reputation_result = None
        infraction_result = None

        print(f"{get_now_time()} 开始执行侵权采集：{infraction_options}<br>")
        try:
            infraction_result = bit_infractions_info.main(**infraction_options)
            print(f"{get_now_time()} 侵权采集执行完成<br>")
        except Exception as exc:
            phase_errors["infraction"] = str(exc)
            print(f"{get_now_time()} 侵权采集异常，将继续执行声誉采集：{exc}<br>")
            traceback.print_exc()

        _wait_between_collections()

        print(f"{get_now_time()} 开始执行声誉采集：{reputation_options}<br>")
        try:
            reputation_result = bit_reputation_info.main(**reputation_options)
            print(f"{get_now_time()} 声誉采集执行完成<br>")
        except Exception as exc:
            phase_errors["reputation"] = str(exc)
            print(f"{get_now_time()} 声誉采集异常，将继续执行 AI 申诉循环：{exc}<br>")
            traceback.print_exc()

        appeal_result = None
        try:
            appeal_result = _run_ai_appeal_loop(started_at)
        except Exception as exc:
            phase_errors["ai_appeal"] = str(exc)
            print(f"{get_now_time()} AI 申诉循环异常：{exc}<br>")
            traceback.print_exc()
        return {
            "infraction": infraction_result,
            "reputation": reputation_result,
            "ai_appeal": appeal_result,
            "errors": phase_errors,
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


def run_reputation_infraction_then_daily():
    """兼容旧入口；实际执行采集和四类 AI 申诉循环。"""
    return run_infraction_reputation_then_appeal()


def run_main_loop(repeat_interval_seconds=None, max_cycles=None):
    """循环执行完整任务链；每条链结束后再固定休息 2 小时。"""
    if repeat_interval_seconds is None:
        repeat_interval_seconds = max(
            0,
            _get_int_env(
                "BIT_MAIN_REPEAT_INTERVAL_SECONDS",
                DEFAULT_CHAIN_REPEAT_SECONDS,
            ),
        )
    cycle_limit = None if max_cycles is None else max(1, int(max_cycles))
    results = []
    cycle_no = 1
    while True:
        print(f"{get_now_time()} 开始执行第 {cycle_no} 条完整任务链<br>")
        results.append(run_infraction_reputation_then_appeal())
        if cycle_limit is not None and cycle_no >= cycle_limit:
            return results
        print(
            f"{get_now_time()} 第 {cycle_no} 条完整任务链已结束，"
            f"休息 {repeat_interval_seconds / 3600:.2f} 小时后重新执行<br>"
        )
        if repeat_interval_seconds > 0:
            time.sleep(repeat_interval_seconds)
        cycle_no += 1


def build_scheduler():
    """兼容旧调用；新的命令行入口使用 ``run_main_loop`` 按完成时间计时。"""
    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_infraction_reputation_then_appeal,
        "interval",
        hours=SCHEDULE_INTERVAL_HOURS,
        next_run_time=datetime.now(),
        id="infraction_reputation_appeal_chain",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    return scheduler


if __name__ == "__main__":
    print("------------------------------")
    print(f"{get_now_time()} bit_main 循环任务启动")
    print("启动后立即执行，每条完整任务链结束后休息 2 小时再重新执行")
    print(
        "任务顺序：侵权采集 -> 冷却 3–5 分钟 -> 声誉采集 -> "
        "侵权 -> 延误 -> 投诉 -> 取消率 AI 申诉，共 10 轮，最多 10 个进程"
    )
    run_main_loop()
