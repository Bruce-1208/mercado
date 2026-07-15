import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from bit import bit_appeal_ai
from bit.bit_api import closeBrowser
from bit.bit_db_api import (
    get_latest_infraction_info,
    resolve_window_anomaly,
    upsert_window_anomaly,
)
from bit.bit_runtime_lock import InterProcessLock, create_window_lease, get_lock_owner
from bit.bit_utils import get_now_time


DEFAULT_DAILY_TOP_N = 30
DEFAULT_DAILY_MAX_WORKERS = 30
DEFAULT_DAILY_RECENT_DAYS = 100
DEFAULT_LOGIN_RETRY_ATTEMPTS = 3
DEFAULT_LOGIN_RETRY_SECONDS = 180
DEFAULT_SITE_RETRY_ATTEMPTS = int(os.getenv("BIT_DAILY_SITE_RETRY_ATTEMPTS", "2"))
DEFAULT_SITE_RETRY_SECONDS = int(os.getenv("BIT_DAILY_SITE_RETRY_SECONDS", "25"))
DEFAULT_RATE_LIMIT_RETRIES = int(os.getenv("BIT_DAILY_RATE_LIMIT_RETRIES", "3"))
DEFAULT_RATE_LIMIT_RETRY_SECONDS = int(os.getenv("BIT_DAILY_RATE_LIMIT_RETRY_SECONDS", "300"))
DEFAULT_START_STAGGER_SECONDS = float(os.getenv("BIT_DAILY_START_STAGGER_SECONDS", "10"))
DAILY_TASK_LOCK_KEY = "bit_daily_task_singleton"


class DailyTaskAlreadyRunning(RuntimeError):
    pass


def acquire_daily_task_lock(owner="bit_daily_task", mode="once"):
    lock = InterProcessLock(
        DAILY_TASK_LOCK_KEY,
        owner=owner,
        metadata={"task_type": "bit_daily_task", "mode": mode},
    )
    if not lock.acquire(timeout=0):
        return None
    return lock


def get_daily_task_lock_owner():
    return get_lock_owner(DAILY_TASK_LOCK_KEY)


def _is_login_required_result(value):
    text = str(value or "")
    return (
        "未登录" in text
        or "Fill out your e-mail address to log in" in text
        or "Fill out your email address to log in" in text
    )


def _is_retryable_site_result(value):
    text = str(value or "")
    retry_markers = (
        "AI 客服悬浮窗",
        "AI客服悬浮窗",
        "新版内嵌 AI 助手",
        "没有找到 AI 客服",
        "没有找到AI客服",
        "没有找到输入框",
        "没有找到 AI 客服输入框",
        "没有找到 AI 客服聊天窗口",
        "about:blank",
        "打开比特浏览器失败",
        "打开窗口失败",
        "窗口页面打开验证失败",
        "窗口页面未正常打开",
        "请求太过频繁",
        "timeout",
        "timed out",
        "Read timed out",
        "chrome not reachable",
        "session not created",
        "aborted",
    )
    return any(marker.lower() in text.lower() for marker in retry_markers)


def _is_rate_limited_result(value):
    text = str(value or "").lower()
    markers = (
        "429",
        "too many requests",
        "rate limit",
        "request limit",
        "限频",
        "请求太过频繁",
        "请求过于频繁",
        "访问过于频繁",
        "操作太频繁",
        "每秒最多可以发起",
        "西语错误页持续出现",
        "页面访问异常",
        "demasiadas solicitudes",
        "muitas solicitações",
    )
    return any(marker in text for marker in markers)


def build_latest_infraction_appeal_plan(top_n=DEFAULT_DAILY_TOP_N, recent_days=DEFAULT_DAILY_RECENT_DAYS, only_active=True):
    """从最新一次侵权列表中选出侵权总数最多的 N 家店铺，并按站点侵权数降序排列。"""
    data = get_latest_infraction_info(recent_days)
    summary_rows = data.get("summary") or []
    active_config = bit_appeal_ai.load_active_shop_site_config() if only_active else {}
    shop_map = {}

    for row in summary_rows:
        name = str(row.get("店铺名") or "").strip()
        site = str(row.get("站点") or "").strip()
        count = int(row.get("总数") or 0)
        if not name or not site or count <= 0:
            continue
        if only_active and name not in active_config:
            continue

        site_code = bit_appeal_ai.normalize_site_code(site)
        if only_active and active_config.get(name) and site_code not in active_config[name]:
            continue

        shop = shop_map.setdefault(name, {"name": name, "total": 0, "sites": []})
        shop["total"] += count
        shop["sites"].append({
            "site": site,
            "site_code": site_code,
            "count": count,
        })

    plan = []
    for shop in shop_map.values():
        shop["sites"].sort(key=lambda item: item["count"], reverse=True)
        if shop["sites"]:
            plan.append(shop)

    plan.sort(
        key=lambda item: (
            item["total"],
            item["sites"][0]["count"] if item["sites"] else 0,
            item["name"],
        ),
        reverse=True,
    )
    selected = plan[:max(1, int(top_n))]
    print(f"{get_now_time()} 最新侵权数据时间：{data.get('latest_submit_time', '')}<br>")
    print(f"{get_now_time()} Top {top_n} 侵权店铺计划：{selected}<br>")
    return selected


def _save_login_anomaly(window_id, name, site_code, reason):
    try:
        upsert_window_anomaly(
            window_id,
            name,
            site=site_code,
            anomaly_type="需要登录",
            reason=str(reason or "检测到登录失效"),
            source="bit_daily_task",
        )
    except Exception as e:
        print(f"{get_now_time()} {name} 写入窗口异常失败：{e}<br>")


def _resolve_login_anomaly(window_id, name):
    try:
        resolve_window_anomaly(window_id)
    except Exception as e:
        print(f"{get_now_time()} {name} 更新窗口登录状态失败：{e}<br>")


def _appeal_one_shop_infractions_locked(
    shop_plan,
    window_id,
    window_lease,
    site_pause=30,
    message="",
    site_retry_attempts=DEFAULT_SITE_RETRY_ATTEMPTS,
    site_retry_seconds=DEFAULT_SITE_RETRY_SECONDS,
    rate_limit_retries=DEFAULT_RATE_LIMIT_RETRIES,
    rate_limit_retry_seconds=DEFAULT_RATE_LIMIT_RETRY_SECONDS,
):
    name = shop_plan["name"]
    results = []
    exit_shop = False

    for site in shop_plan["sites"]:
        if exit_shop:
            break

        site_code = site["site_code"]
        count = site["count"]
        general_attempt = 1
        rate_retry_count = 0
        result = ""

        while True:
            try:
                print(
                    f"{get_now_time()} {name} {site_code} 开始 AI 客服侵权申诉，"
                    f"站点侵权数 {count}，普通尝试 {general_attempt}/{site_retry_attempts}，"
                    f"限频重试 {rate_retry_count}/{rate_limit_retries}<br>"
                )
                result = bit_appeal_ai.shensu(
                    name,
                    site_code,
                    "侵权",
                    message,
                    validate_open=True,
                )
            except Exception as e:
                result = f"执行异常：{e}"
                traceback.print_exc()

            if _is_login_required_result(result):
                results.append({"site": site_code, "count": count, "result": result})
                print(
                    f"{get_now_time()} {name} {site_code} 检测到登录失效，"
                    f"立即终止该店铺任务并关闭浏览器窗口<br>"
                )
                try:
                    close_result = closeBrowser(window_id, lease=window_lease)
                    print(f"{get_now_time()} {name} 关闭窗口结果：{close_result}<br>")
                except Exception as e:
                    print(f"{get_now_time()} {name} 登录失效后关闭窗口失败：{e}<br>")
                _save_login_anomaly(window_id, name, site_code, result)
                exit_shop = True
                break

            if _is_rate_limited_result(result):
                if rate_retry_count < max(0, int(rate_limit_retries)):
                    rate_retry_count += 1
                    print(
                        f"{get_now_time()} {name} {site_code} 遇到限频，"
                        f"等待 {rate_limit_retry_seconds} 秒后进行第 "
                        f"{rate_retry_count}/{rate_limit_retries} 次限频重试：{result}<br>"
                    )
                    time.sleep(max(0, int(rate_limit_retry_seconds)))
                    continue
                results.append({
                    "site": site_code,
                    "count": count,
                    "result": result,
                    "rate_limit_retries": rate_retry_count,
                })
                print(
                    f"{get_now_time()} {name} {site_code} 限频重试 {rate_retry_count} 次后仍失败："
                    f"{result}<br>"
                )
                break

            if _is_retryable_site_result(result) and general_attempt < max(1, int(site_retry_attempts)):
                general_attempt += 1
                print(
                    f"{get_now_time()} {name} {site_code} 遇到瞬时失败，"
                    f"{site_retry_seconds} 秒后重试：{result}<br>"
                )
                time.sleep(max(0, int(site_retry_seconds)))
                continue

            results.append({
                "site": site_code,
                "count": count,
                "result": result,
                "site_attempts": general_attempt,
                "rate_limit_retries": rate_retry_count,
            })
            if _is_retryable_site_result(result):
                print(f"{get_now_time()} {name} {site_code} 多次重试后仍失败：{result}<br>")
            else:
                _resolve_login_anomaly(window_id, name)
                print(f"{get_now_time()} {name} {site_code} AI 客服侵权申诉完成：{result}<br>")
            break

        if not exit_shop and site_pause > 0:
            time.sleep(site_pause)

    return {
        "name": name,
        "total": shop_plan["total"],
        "results": results,
        "exit_reason": "未登录" if exit_shop else "",
    }


def appeal_one_shop_infractions(
    shop_plan,
    site_pause=30,
    message="",
    login_retry_attempts=DEFAULT_LOGIN_RETRY_ATTEMPTS,
    login_retry_seconds=DEFAULT_LOGIN_RETRY_SECONDS,
    site_retry_attempts=DEFAULT_SITE_RETRY_ATTEMPTS,
    site_retry_seconds=DEFAULT_SITE_RETRY_SECONDS,
    rate_limit_retries=DEFAULT_RATE_LIMIT_RETRIES,
    rate_limit_retry_seconds=DEFAULT_RATE_LIMIT_RETRY_SECONDS,
):
    """按店铺执行 AI 申诉；整个店铺期间独占该浏览器窗口。"""
    del login_retry_attempts, login_retry_seconds  # 登录失效现在立即终止，不再原地重试。
    name = shop_plan["name"]
    try:
        window_id = bit_appeal_ai.get_window_id_by_shop_name(name)
    except Exception as e:
        return {
            "name": name,
            "total": shop_plan.get("total", 0),
            "results": [{"error": str(e)}],
            "exit_reason": "未找到窗口",
        }

    lease = create_window_lease(
        window_id,
        owner=f"bit_daily_task:{name}",
        shop_name=name,
        task_type="bit_daily_task",
    )
    if not lease.acquire(timeout=0):
        print(f"{get_now_time()} {name} 窗口已被其他任务占用，跳过本店铺<br>")
        return {
            "name": name,
            "total": shop_plan.get("total", 0),
            "results": [],
            "exit_reason": "窗口被其他任务占用",
        }
    try:
        return _appeal_one_shop_infractions_locked(
            shop_plan,
            window_id,
            lease,
            site_pause=site_pause,
            message=message,
            site_retry_attempts=site_retry_attempts,
            site_retry_seconds=site_retry_seconds,
            rate_limit_retries=rate_limit_retries,
            rate_limit_retry_seconds=rate_limit_retry_seconds,
        )
    finally:
        lease.release()


def _appeal_one_shop_worker(shop, site_pause, message, start_delay):
    if start_delay > 0:
        print(f"{get_now_time()} {shop.get('name', '')} 启动错峰等待 {start_delay:.1f} 秒<br>")
        time.sleep(start_delay)
    return appeal_one_shop_infractions(shop, site_pause, message)


def _run_top_infraction_ai_appeal_once_locked(
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    site_pause=30,
    message="",
    only_active=True,
):
    """用多进程并发处理侵权总数最多的 N 家店铺；每个店铺内部按站点侵权数降序串行处理。"""
    plan = build_latest_infraction_appeal_plan(
        top_n=top_n,
        recent_days=recent_days,
        only_active=only_active,
    )
    if not plan:
        print(f"{get_now_time()} 没有找到可处理的侵权店铺<br>")
        return []

    worker_count = max_workers if max_workers is not None else DEFAULT_DAILY_MAX_WORKERS
    worker_count = max(1, min(int(worker_count), len(plan)))
    results = []
    print(f"{get_now_time()} bit_daily_task 本轮使用 {worker_count} 个进程并发处理 {len(plan)} 个店铺<br>")
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                _appeal_one_shop_worker,
                shop,
                site_pause,
                message,
                index * max(0, DEFAULT_START_STAGGER_SECONDS),
            )
            for index, shop in enumerate(plan)
        ]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                results.append({"error": str(e)})
                traceback.print_exc()

    print(f"{get_now_time()} Top 侵权店铺 AI 客服申诉一轮完成：{results}<br>")
    return results


def run_top_infraction_ai_appeal_once(
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    site_pause=30,
    message="",
    only_active=True,
    _task_lock=None,
):
    owned_lock = None
    task_lock = _task_lock
    if task_lock is None:
        owned_lock = acquire_daily_task_lock(owner="bit_daily_task.py", mode="once")
        task_lock = owned_lock
    if task_lock is None or not task_lock.acquired:
        owner = get_daily_task_lock_owner()
        raise DailyTaskAlreadyRunning(f"bit_daily_task 已在其他进程运行：{owner}")
    try:
        return _run_top_infraction_ai_appeal_once_locked(
            top_n=top_n,
            max_workers=max_workers,
            recent_days=recent_days,
            site_pause=site_pause,
            message=message,
            only_active=only_active,
        )
    finally:
        if owned_lock is not None:
            owned_lock.release()


def _format_stop_at(stop_at):
    if not stop_at:
        return ""
    if isinstance(stop_at, datetime):
        return stop_at.strftime("%Y-%m-%d %H:%M:%S")
    return str(stop_at)


def _seconds_until_stop(stop_at):
    if not stop_at:
        return None
    if isinstance(stop_at, datetime):
        return (stop_at - datetime.now()).total_seconds()
    return float(stop_at) - time.time()


def _loop_top_infraction_ai_appeal_locked(
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    round_interval=600,
    site_pause=30,
    message="",
    only_active=True,
    stop_at=None,
    task_lock=None,
):
    """循环执行 Top 侵权店铺 AI 客服申诉。"""
    round_no = 1
    if stop_at:
        print(f"{get_now_time()} Top 侵权店铺 AI 客服申诉循环将在 {_format_stop_at(stop_at)} 前停止<br>")
    while True:
        remaining = _seconds_until_stop(stop_at)
        if remaining is not None and remaining <= 0:
            print(f"{get_now_time()} 已到达停止时间，结束 Top 侵权店铺 AI 客服申诉循环<br>")
            return

        started = time.time()
        try:
            print(f"{get_now_time()} 开始第 {round_no} 轮 Top 侵权店铺 AI 客服申诉<br>")
            run_top_infraction_ai_appeal_once(
                top_n=top_n,
                max_workers=max_workers,
                recent_days=recent_days,
                site_pause=site_pause,
                message=message,
                only_active=only_active,
                _task_lock=task_lock,
            )
        except Exception as e:
            print(f"{get_now_time()} 第 {round_no} 轮 Top 侵权店铺 AI 客服申诉异常：{e}<br>")
            traceback.print_exc()

        sleep_seconds = max(0, int(round_interval) - (time.time() - started))
        remaining = _seconds_until_stop(stop_at)
        if remaining is not None:
            if remaining <= 0:
                print(f"{get_now_time()} 已到达停止时间，结束 Top 侵权店铺 AI 客服申诉循环<br>")
                return
            sleep_seconds = min(sleep_seconds, remaining)
        print(f"{get_now_time()} 第 {round_no} 轮结束，等待 {sleep_seconds:.1f} 秒后重新计算 Top 店铺<br>")
        time.sleep(sleep_seconds)
        round_no += 1


def loop_top_infraction_ai_appeal(
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    round_interval=600,
    site_pause=30,
    message="",
    only_active=True,
    stop_at=None,
    _task_lock=None,
):
    owned_lock = None
    task_lock = _task_lock
    if task_lock is None:
        owned_lock = acquire_daily_task_lock(owner="bit_daily_task.py", mode="loop")
        task_lock = owned_lock
    if task_lock is None or not task_lock.acquired:
        owner = get_daily_task_lock_owner()
        raise DailyTaskAlreadyRunning(f"bit_daily_task 已在其他进程运行：{owner}")
    try:
        return _loop_top_infraction_ai_appeal_locked(
            top_n=top_n,
            max_workers=max_workers,
            recent_days=recent_days,
            round_interval=round_interval,
            site_pause=site_pause,
            message=message,
            only_active=only_active,
            stop_at=stop_at,
            task_lock=task_lock,
        )
    finally:
        if owned_lock is not None:
            owned_lock.release()


if __name__ == "__main__":
    loop_top_infraction_ai_appeal(top_n=30, max_workers=5, round_interval=60)
