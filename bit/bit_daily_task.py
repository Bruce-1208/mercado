import os
import re
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
    get_latest_reputation_info,
    resolve_window_anomaly,
)
from bit.bit_runtime_lock import InterProcessLock, create_window_lease, get_lock_owner
from bit.bit_mercado_limit import is_mercado_rate_limited_text
from bit.bit_mercado_login import (
    is_human_verification_result,
    is_login_blocking_result,
    record_human_verification_anomaly,
)
from bit.bit_utils import get_now_time


DEFAULT_DAILY_TOP_N = 30
DEFAULT_DAILY_MAX_WORKERS = 10
DEFAULT_DAILY_BROWSER_WORKER_LIMIT = 10
DEFAULT_DAILY_RECENT_DAYS = 100
DEFAULT_LOGIN_RETRY_ATTEMPTS = 3
DEFAULT_LOGIN_RETRY_SECONDS = 180
DEFAULT_SITE_RETRY_ATTEMPTS = int(os.getenv("BIT_DAILY_SITE_RETRY_ATTEMPTS", "2"))
DEFAULT_SITE_RETRY_SECONDS = int(os.getenv("BIT_DAILY_SITE_RETRY_SECONDS", "25"))
DEFAULT_RATE_LIMIT_RETRIES = int(os.getenv("BIT_DAILY_RATE_LIMIT_RETRIES", "3"))
DEFAULT_RATE_LIMIT_RETRY_SECONDS = int(os.getenv("BIT_DAILY_RATE_LIMIT_RETRY_SECONDS", "300"))
DEFAULT_START_STAGGER_SECONDS = float(os.getenv("BIT_DAILY_START_STAGGER_SECONDS", "10"))
DAILY_TASK_LOCK_KEY = "bit_daily_task_singleton"

APPEAL_TYPE_INFRACTION = "侵权"
APPEAL_TYPE_DELAY = "延误"
APPEAL_TYPE_CANCELLATION = "取消率"
APPEAL_TYPE_COMPLAINT = "投诉"
SUPPORTED_APPEAL_TYPES = (
    APPEAL_TYPE_INFRACTION,
    APPEAL_TYPE_DELAY,
    APPEAL_TYPE_CANCELLATION,
    APPEAL_TYPE_COMPLAINT,
)
REPUTATION_RATE_FIELDS = {
    APPEAL_TYPE_DELAY: "延误率",
    APPEAL_TYPE_CANCELLATION: "取消率",
    APPEAL_TYPE_COMPLAINT: "投诉率",
}


class DailyTaskAlreadyRunning(RuntimeError):
    pass


def _daily_browser_worker_limit():
    try:
        return max(
            1,
            int(
                os.getenv(
                    "BIT_DAILY_BROWSER_WORKER_LIMIT",
                    str(DEFAULT_DAILY_BROWSER_WORKER_LIMIT),
                )
            ),
        )
    except (TypeError, ValueError):
        return DEFAULT_DAILY_BROWSER_WORKER_LIMIT


def normalize_appeal_type(appeal_type):
    """把 daily_task 的申诉类型统一为 ``bit_appeal_ai.shensu`` 接受的值。"""
    text = str(appeal_type or "").strip()
    aliases = {
        "侵权": APPEAL_TYPE_INFRACTION,
        "infraction": APPEAL_TYPE_INFRACTION,
        "infringement": APPEAL_TYPE_INFRACTION,
        "延误": APPEAL_TYPE_DELAY,
        "延误率": APPEAL_TYPE_DELAY,
        "delay": APPEAL_TYPE_DELAY,
        "delay_rate": APPEAL_TYPE_DELAY,
        "取消": APPEAL_TYPE_CANCELLATION,
        "取消率": APPEAL_TYPE_CANCELLATION,
        "cancellation": APPEAL_TYPE_CANCELLATION,
        "cancellation_rate": APPEAL_TYPE_CANCELLATION,
        "投诉": APPEAL_TYPE_COMPLAINT,
        "投诉率": APPEAL_TYPE_COMPLAINT,
        "complaint": APPEAL_TYPE_COMPLAINT,
        "complaints": APPEAL_TYPE_COMPLAINT,
        "complaint_rate": APPEAL_TYPE_COMPLAINT,
    }
    normalized = aliases.get(text.casefold())
    if normalized is None:
        raise ValueError(
            f"不支持的申诉类型：{appeal_type}，仅支持侵权、延误率、取消率、投诉"
        )
    return normalized


def _appeal_type_label(appeal_type):
    normalized = normalize_appeal_type(appeal_type)
    return "延误率" if normalized == APPEAL_TYPE_DELAY else normalized


def _parse_rate(value):
    """把 ``7.5%``、``0.075`` 等声誉比率统一为 0 到 1 范围的小数。"""
    text = str(value or "").strip().replace("，", ",").replace("％", "%")
    if not text or text.casefold() in {"-", "--", "—", "n/a", "none", "null"}:
        return 0.0
    match = re.search(r"-?\d+(?:[.,]\d+)?", text)
    if not match:
        return 0.0
    try:
        number = float(match.group(0).replace(",", "."))
    except ValueError:
        return 0.0
    return number / 100 if "%" in text or number > 1 else number


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
        is_login_blocking_result(text)
        or "登录失效" in text
        or "登录态失效" in text
        or "触发自动登录" in text
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
    return is_mercado_rate_limited_text(value)


def _is_failed_appeal_result(value):
    text = str(value or "").strip().casefold()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "执行异常",
            "执行失败",
            "打开比特浏览器失败",
            "窗口页面打开验证失败",
            "窗口页面未正常打开",
            "没有找到 ai 客服",
            "没有找到ai客服",
            "没有找到输入框",
            "进入了人工客服页面",
            "chrome not reachable",
            "session not created",
            "timed out",
            "timeout",
        )
    )


def _close_ai_appeal_browser(window_id, window_lease, name, reason):
    """在异常等待或退出前关闭完整浏览器窗口，避免并发任务积累内存。"""
    try:
        close_result = closeBrowser(window_id, lease=window_lease)
        print(
            f"{get_now_time()} {name} 因{reason}关闭浏览器窗口："
            f"{close_result}<br>"
        )
        return close_result
    except Exception as exc:
        print(f"{get_now_time()} {name} 因{reason}关闭浏览器窗口失败：{exc}<br>")
        return None


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


def build_latest_reputation_appeal_plan(
    appeal_type,
    top_n=DEFAULT_DAILY_TOP_N,
    only_active=True,
    min_rate=0,
):
    """按最新声誉批次生成延误率、取消率或投诉申诉计划。

    只选择比率大于 0 且不低于 ``min_rate`` 的站点；同一店铺内按比率降序，
    店铺之间按最高站点比率和比率总和排序。
    """
    normalized_type = normalize_appeal_type(appeal_type)
    rate_field = REPUTATION_RATE_FIELDS.get(normalized_type)
    if not rate_field:
        raise ValueError("声誉申诉计划仅支持延误率、取消率或投诉")

    threshold = max(0, _parse_rate(min_rate))
    data = get_latest_reputation_info()
    rows = data.get("rows") or []
    active_config = bit_appeal_ai.load_active_shop_site_config() if only_active else {}
    shop_map = {}

    for row in rows:
        name = str(row.get("店铺名") or "").strip()
        site = str(row.get("站点") or "").strip()
        rate_text = str(row.get(rate_field) or "").strip()
        rate = _parse_rate(rate_text)
        if not name or not site or rate <= 0 or rate < threshold:
            continue
        if only_active and name not in active_config:
            continue

        site_code = bit_appeal_ai.normalize_site_code(site)
        if only_active and active_config.get(name) and site_code not in active_config[name]:
            continue

        shop = shop_map.setdefault(name, {"name": name, "total": 0.0, "sites": []})
        shop["total"] += rate
        shop["sites"].append({
            "site": site,
            "site_code": site_code,
            "count": rate,
            "rate": rate,
            "rate_text": rate_text,
        })

    plan = []
    for shop in shop_map.values():
        shop["sites"].sort(key=lambda item: item["rate"], reverse=True)
        if shop["sites"]:
            plan.append(shop)

    plan.sort(
        key=lambda item: (
            item["sites"][0]["rate"] if item["sites"] else 0,
            item["total"],
            item["name"],
        ),
        reverse=True,
    )
    selected = plan[:max(1, int(top_n))]
    label = _appeal_type_label(normalized_type)
    print(f"{get_now_time()} 最新声誉数据时间：{data.get('latest_submit_time', '')}<br>")
    print(
        f"{get_now_time()} Top {top_n} {label}店铺计划（最低比率 {threshold:.2%}）："
        f"{selected}<br>"
    )
    return selected


def build_appeal_plan(
    appeal_type,
    top_n=DEFAULT_DAILY_TOP_N,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    only_active=True,
    min_rate=0,
):
    """按申诉类型分发到侵权计划或声誉比率计划。"""
    normalized_type = normalize_appeal_type(appeal_type)
    if normalized_type == APPEAL_TYPE_INFRACTION:
        return build_latest_infraction_appeal_plan(
            top_n=top_n,
            recent_days=recent_days,
            only_active=only_active,
        )
    return build_latest_reputation_appeal_plan(
        normalized_type,
        top_n=top_n,
        only_active=only_active,
        min_rate=min_rate,
    )


def _save_login_anomaly(window_id, name, site_code, reason):
    """兼容旧调用：只允许人机验证进入店铺状态。"""
    if not is_human_verification_result(reason):
        return False
    try:
        return record_human_verification_anomaly(
            reason,
            window_id,
            name,
            site=site_code,
            source="AI申诉",
        )
    except Exception as e:
        print(f"{get_now_time()} {name} 写入窗口异常失败：{e}<br>")
        return False


def _resolve_login_anomaly(window_id, name):
    try:
        resolve_window_anomaly(window_id)
    except Exception as e:
        print(f"{get_now_time()} {name} 更新窗口登录状态失败：{e}<br>")


def _appeal_one_shop_locked(
    shop_plan,
    window_id,
    window_lease,
    appeal_type=APPEAL_TYPE_INFRACTION,
    site_pause=30,
    message="",
    site_retry_attempts=DEFAULT_SITE_RETRY_ATTEMPTS,
    site_retry_seconds=DEFAULT_SITE_RETRY_SECONDS,
    rate_limit_retries=DEFAULT_RATE_LIMIT_RETRIES,
    rate_limit_retry_seconds=DEFAULT_RATE_LIMIT_RETRY_SECONDS,
):
    normalized_type = normalize_appeal_type(appeal_type)
    appeal_label = _appeal_type_label(normalized_type)
    name = shop_plan["name"]
    results = []
    exit_shop = False

    for site in shop_plan["sites"]:
        if exit_shop:
            break

        site_code = site["site_code"]
        count = site["count"]
        metric_text = site.get("rate_text") or count
        general_attempt = 1
        rate_retry_count = 0
        result = ""

        while True:
            try:
                print(
                    f"{get_now_time()} {name} {site_code} 开始 AI 客服{appeal_label}申诉，"
                    f"站点指标 {metric_text}，普通尝试 {general_attempt}/{site_retry_attempts}，"
                    f"限频重试 {rate_retry_count}/{rate_limit_retries}<br>"
                )
                result = bit_appeal_ai.shensu(
                    name,
                    site_code,
                    normalized_type,
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
                    f"立即终止该店铺任务<br>"
                )
                _close_ai_appeal_browser(
                    window_id,
                    window_lease,
                    name,
                    "登录失效或触发自动登录",
                )
                exit_shop = True
                break

            if _is_rate_limited_result(result):
                _close_ai_appeal_browser(
                    window_id,
                    window_lease,
                    name,
                    "访问限频",
                )
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
                _close_ai_appeal_browser(
                    window_id,
                    window_lease,
                    name,
                    "自动找客服报错",
                )
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
            retryable_failure = _is_retryable_site_result(result)
            appeal_failure = retryable_failure or _is_failed_appeal_result(result)
            if appeal_failure:
                _close_ai_appeal_browser(
                    window_id,
                    window_lease,
                    name,
                    "自动找客服报错",
                )
            if retryable_failure:
                print(f"{get_now_time()} {name} {site_code} 多次重试后仍失败：{result}<br>")
            elif appeal_failure:
                print(f"{get_now_time()} {name} {site_code} 自动找客服执行失败：{result}<br>")
            else:
                _resolve_login_anomaly(window_id, name)
                print(
                    f"{get_now_time()} {name} {site_code} "
                    f"AI 客服{appeal_label}申诉完成：{result}<br>"
                )
            break

        if not exit_shop and site_pause > 0:
            time.sleep(site_pause)

    return {
        "name": name,
        "total": shop_plan["total"],
        "appeal_type": appeal_label,
        "results": results,
        "exit_reason": "未登录" if exit_shop else "",
    }


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
    """兼容旧的店铺侵权执行入口。"""
    return _appeal_one_shop_locked(
        shop_plan,
        window_id,
        window_lease,
        appeal_type=APPEAL_TYPE_INFRACTION,
        site_pause=site_pause,
        message=message,
        site_retry_attempts=site_retry_attempts,
        site_retry_seconds=site_retry_seconds,
        rate_limit_retries=rate_limit_retries,
        rate_limit_retry_seconds=rate_limit_retry_seconds,
    )


def appeal_one_shop(
    shop_plan,
    appeal_type=APPEAL_TYPE_INFRACTION,
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
    normalized_type = normalize_appeal_type(appeal_type)
    appeal_label = _appeal_type_label(normalized_type)
    name = shop_plan["name"]
    try:
        window_id = bit_appeal_ai.get_window_id_by_shop_name(name)
    except Exception as e:
        return {
            "name": name,
            "total": shop_plan.get("total", 0),
            "appeal_type": appeal_label,
            "results": [{"error": str(e)}],
            "exit_reason": "未找到窗口",
        }

    lease = create_window_lease(
        window_id,
        owner=f"bit_daily_task:{appeal_label}:{name}",
        shop_name=name,
        task_type=f"bit_daily_task:{appeal_label}",
    )
    if not lease.acquire(timeout=0):
        print(f"{get_now_time()} {name} 窗口已被其他任务占用，跳过本店铺<br>")
        return {
            "name": name,
            "total": shop_plan.get("total", 0),
            "appeal_type": appeal_label,
            "results": [],
            "exit_reason": "窗口被其他任务占用",
        }
    try:
        return _appeal_one_shop_locked(
            shop_plan,
            window_id,
            lease,
            appeal_type=normalized_type,
            site_pause=site_pause,
            message=message,
            site_retry_attempts=site_retry_attempts,
            site_retry_seconds=site_retry_seconds,
            rate_limit_retries=rate_limit_retries,
            rate_limit_retry_seconds=rate_limit_retry_seconds,
        )
    finally:
        # shensu 会关闭当前标签页，但不会关闭比特浏览器窗口。bit_main 会连续
        # 跑多轮申诉，如果这里不关窗口，Chromium 主进程和渲染进程会跨轮累积。
        _close_ai_appeal_browser(
            window_id,
            lease,
            name,
            "店铺 AI 申诉任务结束",
        )
        lease.release()


def appeal_one_shop_infractions(
    shop_plan,
    site_pause=30,
    message="",
    **kwargs,
):
    """兼容旧入口：按店铺执行侵权申诉。"""
    return appeal_one_shop(
        shop_plan,
        appeal_type=APPEAL_TYPE_INFRACTION,
        site_pause=site_pause,
        message=message,
        **kwargs,
    )


def _appeal_one_shop_worker_for_type(shop, appeal_type, site_pause, message, start_delay):
    if start_delay > 0:
        print(f"{get_now_time()} {shop.get('name', '')} 启动错峰等待 {start_delay:.1f} 秒<br>")
        time.sleep(start_delay)
    return appeal_one_shop(
        shop,
        appeal_type=appeal_type,
        site_pause=site_pause,
        message=message,
    )


def _appeal_one_shop_worker(shop, site_pause, message, start_delay):
    """兼容旧的侵权多进程 worker。"""
    return _appeal_one_shop_worker_for_type(
        shop,
        APPEAL_TYPE_INFRACTION,
        site_pause,
        message,
        start_delay,
    )


def _run_ai_appeal_once_locked(
    appeal_type,
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    site_pause=30,
    message="",
    only_active=True,
    min_rate=0,
):
    """用多进程并发处理指定类型的 Top 店铺；店铺内部按站点指标降序串行处理。"""
    normalized_type = normalize_appeal_type(appeal_type)
    appeal_label = _appeal_type_label(normalized_type)
    plan = build_appeal_plan(
        normalized_type,
        top_n=top_n,
        recent_days=recent_days,
        only_active=only_active,
        min_rate=min_rate,
    )
    if not plan:
        print(f"{get_now_time()} 没有找到可处理的{appeal_label}店铺<br>")
        return []

    requested_workers = (
        max_workers if max_workers is not None else DEFAULT_DAILY_MAX_WORKERS
    )
    worker_limit = _daily_browser_worker_limit()
    worker_count = max(
        1,
        min(int(requested_workers), len(plan), worker_limit),
    )
    if int(requested_workers) > worker_count:
        print(
            f"{get_now_time()} AI 申诉并发已从 {requested_workers} 限制为 "
            f"{worker_count}，可通过 BIT_DAILY_BROWSER_WORKER_LIMIT 调整<br>"
        )
    results = []
    print(
        f"{get_now_time()} bit_daily_task 本轮使用 {worker_count} 个进程"
        f"并发处理 {len(plan)} 个{appeal_label}店铺<br>"
    )
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                _appeal_one_shop_worker_for_type,
                shop,
                normalized_type,
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

    print(f"{get_now_time()} Top {appeal_label}店铺 AI 客服申诉一轮完成：{results}<br>")
    return results


def _run_top_infraction_ai_appeal_once_locked(
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    site_pause=30,
    message="",
    only_active=True,
):
    """兼容旧的内部侵权单轮入口。"""
    return _run_ai_appeal_once_locked(
        APPEAL_TYPE_INFRACTION,
        top_n=top_n,
        max_workers=max_workers,
        recent_days=recent_days,
        site_pause=site_pause,
        message=message,
        only_active=only_active,
    )


def run_ai_appeal_once(
    appeal_type,
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    site_pause=30,
    message="",
    only_active=True,
    min_rate=0,
    _task_lock=None,
):
    normalized_type = normalize_appeal_type(appeal_type)
    appeal_label = _appeal_type_label(normalized_type)
    owned_lock = None
    task_lock = _task_lock
    if task_lock is None:
        owned_lock = acquire_daily_task_lock(
            owner=f"bit_daily_task.py:{appeal_label}",
            mode="once",
        )
        task_lock = owned_lock
    if task_lock is None or not task_lock.acquired:
        owner = get_daily_task_lock_owner()
        raise DailyTaskAlreadyRunning(f"bit_daily_task 已在其他进程运行：{owner}")
    try:
        return _run_ai_appeal_once_locked(
            normalized_type,
            top_n=top_n,
            max_workers=max_workers,
            recent_days=recent_days,
            site_pause=site_pause,
            message=message,
            only_active=only_active,
            min_rate=min_rate,
        )
    finally:
        if owned_lock is not None:
            owned_lock.release()


def run_top_infraction_ai_appeal_once(
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    site_pause=30,
    message="",
    only_active=True,
    _task_lock=None,
):
    """兼容旧入口：自动执行一轮侵权申诉。"""
    return run_ai_appeal_once(
        APPEAL_TYPE_INFRACTION,
        top_n=top_n,
        max_workers=max_workers,
        recent_days=recent_days,
        site_pause=site_pause,
        message=message,
        only_active=only_active,
        _task_lock=_task_lock,
    )


def auto_appeal_infraction(**kwargs):
    """自动对侵权进行一轮申诉。"""
    return run_ai_appeal_once(APPEAL_TYPE_INFRACTION, **kwargs)


def auto_appeal_delay(**kwargs):
    """自动对延误率进行一轮申诉。"""
    return run_ai_appeal_once(APPEAL_TYPE_DELAY, **kwargs)


def auto_appeal_cancellation(**kwargs):
    """自动对取消率进行一轮申诉。"""
    return run_ai_appeal_once(APPEAL_TYPE_CANCELLATION, **kwargs)


def auto_appeal_complaint(**kwargs):
    """自动对投诉进行一轮申诉。"""
    return run_ai_appeal_once(APPEAL_TYPE_COMPLAINT, **kwargs)


def auto_appeal_infringement(**kwargs):
    """``auto_appeal_infraction`` 的语义化别名。"""
    return auto_appeal_infraction(**kwargs)


def auto_appeal_delay_rate(**kwargs):
    """``auto_appeal_delay`` 的语义化别名。"""
    return auto_appeal_delay(**kwargs)


def auto_appeal_cancellation_rate(**kwargs):
    """``auto_appeal_cancellation`` 的语义化别名。"""
    return auto_appeal_cancellation(**kwargs)


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


def _loop_ai_appeal_locked(
    appeal_type,
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    round_interval=600,
    site_pause=30,
    message="",
    only_active=True,
    min_rate=0,
    stop_at=None,
    max_rounds=None,
    task_lock=None,
):
    """循环执行指定类型的 Top 店铺 AI 客服申诉。"""
    normalized_type = normalize_appeal_type(appeal_type)
    appeal_label = _appeal_type_label(normalized_type)
    round_limit = None if max_rounds is None else max(1, int(max_rounds))
    round_no = 1
    if round_limit is not None:
        print(
            f"{get_now_time()} Top {appeal_label}店铺 AI 客服申诉循环"
            f"共执行 {round_limit} 轮<br>"
        )
    if stop_at:
        print(
            f"{get_now_time()} Top {appeal_label}店铺 AI 客服申诉循环将在 "
            f"{_format_stop_at(stop_at)} 前停止<br>"
        )
    while True:
        remaining = _seconds_until_stop(stop_at)
        if remaining is not None and remaining <= 0:
            print(
                f"{get_now_time()} 已到达停止时间，"
                f"结束 Top {appeal_label}店铺 AI 客服申诉循环<br>"
            )
            return

        started = time.time()
        try:
            print(
                f"{get_now_time()} 开始第 {round_no} 轮 "
                f"Top {appeal_label}店铺 AI 客服申诉<br>"
            )
            run_ai_appeal_once(
                normalized_type,
                top_n=top_n,
                max_workers=max_workers,
                recent_days=recent_days,
                site_pause=site_pause,
                message=message,
                only_active=only_active,
                min_rate=min_rate,
                _task_lock=task_lock,
            )
        except Exception as e:
            print(
                f"{get_now_time()} 第 {round_no} 轮 Top {appeal_label}店铺 "
                f"AI 客服申诉异常：{e}<br>"
            )
            traceback.print_exc()

        if round_limit is not None and round_no >= round_limit:
            print(
                f"{get_now_time()} 已完成 {round_limit} 轮，"
                f"结束 Top {appeal_label}店铺 AI 客服申诉循环<br>"
            )
            return

        sleep_seconds = max(0, int(round_interval) - (time.time() - started))
        remaining = _seconds_until_stop(stop_at)
        if remaining is not None:
            if remaining <= 0:
                print(
                    f"{get_now_time()} 已到达停止时间，"
                    f"结束 Top {appeal_label}店铺 AI 客服申诉循环<br>"
                )
                return
            sleep_seconds = min(sleep_seconds, remaining)
        print(f"{get_now_time()} 第 {round_no} 轮结束，等待 {sleep_seconds:.1f} 秒后重新计算 Top 店铺<br>")
        time.sleep(sleep_seconds)
        round_no += 1


def _loop_top_infraction_ai_appeal_locked(
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    round_interval=600,
    site_pause=30,
    message="",
    only_active=True,
    stop_at=None,
    max_rounds=None,
    task_lock=None,
):
    """兼容旧的内部侵权循环入口。"""
    return _loop_ai_appeal_locked(
        APPEAL_TYPE_INFRACTION,
        top_n=top_n,
        max_workers=max_workers,
        recent_days=recent_days,
        round_interval=round_interval,
        site_pause=site_pause,
        message=message,
        only_active=only_active,
        stop_at=stop_at,
        max_rounds=max_rounds,
        task_lock=task_lock,
    )


def loop_ai_appeal(
    appeal_type,
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    round_interval=600,
    site_pause=30,
    message="",
    only_active=True,
    min_rate=0,
    stop_at=None,
    max_rounds=None,
    _task_lock=None,
):
    normalized_type = normalize_appeal_type(appeal_type)
    appeal_label = _appeal_type_label(normalized_type)
    owned_lock = None
    task_lock = _task_lock
    if task_lock is None:
        owned_lock = acquire_daily_task_lock(
            owner=f"bit_daily_task.py:{appeal_label}",
            mode="loop",
        )
        task_lock = owned_lock
    if task_lock is None or not task_lock.acquired:
        owner = get_daily_task_lock_owner()
        raise DailyTaskAlreadyRunning(f"bit_daily_task 已在其他进程运行：{owner}")
    try:
        return _loop_ai_appeal_locked(
            normalized_type,
            top_n=top_n,
            max_workers=max_workers,
            recent_days=recent_days,
            round_interval=round_interval,
            site_pause=site_pause,
            message=message,
            only_active=only_active,
            min_rate=min_rate,
            stop_at=stop_at,
            max_rounds=max_rounds,
            task_lock=task_lock,
        )
    finally:
        if owned_lock is not None:
            owned_lock.release()


def loop_top_infraction_ai_appeal(
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    round_interval=600,
    site_pause=30,
    message="",
    only_active=True,
    stop_at=None,
    max_rounds=None,
    _task_lock=None,
):
    """兼容旧入口：循环执行侵权申诉。"""
    return loop_ai_appeal(
        APPEAL_TYPE_INFRACTION,
        top_n=top_n,
        max_workers=max_workers,
        recent_days=recent_days,
        round_interval=round_interval,
        site_pause=site_pause,
        message=message,
        only_active=only_active,
        stop_at=stop_at,
        max_rounds=max_rounds,
        _task_lock=_task_lock,
    )


def loop_delay_ai_appeal(**kwargs):
    """循环自动对延误率进行申诉。"""
    return loop_ai_appeal(APPEAL_TYPE_DELAY, **kwargs)


def loop_cancellation_ai_appeal(**kwargs):
    """循环自动对取消率进行申诉。"""
    return loop_ai_appeal(APPEAL_TYPE_CANCELLATION, **kwargs)


def loop_complaint_ai_appeal(**kwargs):
    """循环自动对投诉进行申诉。"""
    return loop_ai_appeal(APPEAL_TYPE_COMPLAINT, **kwargs)


if __name__ == "__main__":
    loop_top_infraction_ai_appeal(top_n=30, max_workers=5, round_interval=60)
