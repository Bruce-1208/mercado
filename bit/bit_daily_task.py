import contextlib
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timedelta


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from bit import bit_appeal_ai, mercado_infraction_sync
from bit.bit_api import closeBrowser
from bit.bit_collection_control import terminate_process_pool
from bit.bit_db_api import (
    get_latest_reputation_info,
    list_mercado_store_tokens,
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
DEFAULT_DAILY_MAX_WORKERS = 15
MAX_DAILY_TASK_WORKERS = 30
DEFAULT_DAILY_BROWSER_WORKER_LIMIT = MAX_DAILY_TASK_WORKERS
DEFAULT_DAILY_RECENT_DAYS = 100
DEFAULT_LOGIN_RETRY_ATTEMPTS = 3
DEFAULT_LOGIN_RETRY_SECONDS = 180
DEFAULT_SITE_RETRY_ATTEMPTS = int(os.getenv("BIT_DAILY_SITE_RETRY_ATTEMPTS", "2"))
DEFAULT_SITE_RETRY_SECONDS = int(os.getenv("BIT_DAILY_SITE_RETRY_SECONDS", "25"))
DEFAULT_RATE_LIMIT_RETRIES = int(os.getenv("BIT_DAILY_RATE_LIMIT_RETRIES", "3"))
DEFAULT_RATE_LIMIT_RETRY_SECONDS = int(os.getenv("BIT_DAILY_RATE_LIMIT_RETRY_SECONDS", "300"))
DEFAULT_START_STAGGER_SECONDS = float(os.getenv("BIT_DAILY_START_STAGGER_SECONDS", "0"))
DAILY_TASK_LOCK_KEY = "bit_daily_task_singleton"

APPEAL_TYPE_INFRACTION = "侵权"
APPEAL_TYPE_DELAY = "延误"
APPEAL_TYPE_CANCELLATION = "取消率"
APPEAL_TYPE_COMPLAINT = "投诉"
APPEAL_TYPE_MIXED = "混合模式"
DAILY_APPEAL_TASK_TYPES = (
    APPEAL_TYPE_INFRACTION,
    APPEAL_TYPE_DELAY,
    APPEAL_TYPE_COMPLAINT,
    APPEAL_TYPE_CANCELLATION,
)
SUPPORTED_APPEAL_TYPES = (
    APPEAL_TYPE_INFRACTION,
    APPEAL_TYPE_DELAY,
    APPEAL_TYPE_CANCELLATION,
    APPEAL_TYPE_COMPLAINT,
    APPEAL_TYPE_MIXED,
)
MIXED_APPEAL_SEQUENCE = (
    APPEAL_TYPE_INFRACTION,
    APPEAL_TYPE_DELAY,
    APPEAL_TYPE_INFRACTION,
    APPEAL_TYPE_COMPLAINT,
    APPEAL_TYPE_INFRACTION,
    APPEAL_TYPE_CANCELLATION,
)
ALL_APPEAL_SITE_CODES = frozenset(("MX", "BR", "CL", "CO", "AR", "UY"))
REPUTATION_RATE_FIELDS = {
    APPEAL_TYPE_DELAY: "延误率",
    APPEAL_TYPE_CANCELLATION: "取消率",
    APPEAL_TYPE_COMPLAINT: "投诉率",
}


class DailyTaskAlreadyRunning(RuntimeError):
    pass


def _normalize_appeal_plan_limit(top_n):
    try:
        return int(top_n)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_TOP_N


def _select_appeal_plan(plan, top_n):
    """按数量限制申诉计划；top_n <= 0 表示执行全部符合条件的店铺。"""
    limit = _normalize_appeal_plan_limit(top_n)
    return list(plan) if limit <= 0 else list(plan)[:limit]


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
        "混合": APPEAL_TYPE_MIXED,
        "混合模式": APPEAL_TYPE_MIXED,
        "mixed": APPEAL_TYPE_MIXED,
    }
    normalized = aliases.get(text.casefold())
    if normalized is None:
        raise ValueError(
            f"不支持的申诉类型：{appeal_type}，仅支持侵权、延误率、取消率、投诉、混合模式"
        )
    return normalized


def _appeal_type_label(appeal_type):
    normalized = normalize_appeal_type(appeal_type)
    return "延误率" if normalized == APPEAL_TYPE_DELAY else normalized


def normalize_appeal_types(appeal_types):
    """规范化任务开关值，并按任务模块的固定顺序去重。"""
    raw_values = (
        [appeal_types]
        if isinstance(appeal_types, str)
        else list(appeal_types or ())
    )
    normalized_values = [normalize_appeal_type(value) for value in raw_values]
    if APPEAL_TYPE_MIXED in normalized_values:
        return (APPEAL_TYPE_MIXED,)
    selected = set(normalized_values)
    if not selected:
        raise ValueError("请至少开启一个任务")
    return tuple(
        appeal_type
        for appeal_type in DAILY_APPEAL_TASK_TYPES
        if appeal_type in selected
    )


def appeal_type_sequence(appeal_types):
    """返回单轮执行顺序；混合模式使用固定的六项任务序列。"""
    selected = normalize_appeal_types(appeal_types)
    if selected == (APPEAL_TYPE_MIXED,):
        return MIXED_APPEAL_SEQUENCE
    if len(selected) == 1:
        return selected
    if APPEAL_TYPE_INFRACTION not in selected:
        return selected
    sequence = [APPEAL_TYPE_INFRACTION]
    for appeal_type in selected:
        if appeal_type != APPEAL_TYPE_INFRACTION:
            sequence.extend((appeal_type, APPEAL_TYPE_INFRACTION))
    return tuple(sequence)


def _normalize_salespeople(salespeople):
    raw_values = [salespeople] if isinstance(salespeople, str) else list(salespeople or ())
    selected = []
    for value in raw_values:
        name = str(value or "").strip()
        if not name or name in ("全部业务员", "所有业务员", "all", "*"):
            continue
        if name not in selected:
            selected.append(name)
    return tuple(selected)


def _normalize_group_names(group_names):
    raw_values = (
        [group_names]
        if isinstance(group_names, str)
        else list(group_names or ())
    )
    selected = []
    for value in raw_values:
        name = str(value or "").strip()
        if not name or name in ("全部店铺组", "所有店铺组", "all", "*"):
            continue
        if name not in selected:
            selected.append(name)
    return tuple(selected)


def _setting_flag_enabled(value):
    if isinstance(value, str):
        return value.strip().casefold() not in ("", "0", "false", "no", "off")
    return bool(value)


def _load_authorized_appeal_shop_site_data(salespeople=None, group_names=None):
    """返回申诉授权的别名范围及用于浏览器遍历的规范店铺范围。"""
    selected_salespeople = set(_normalize_salespeople(salespeople))
    selected_group_names = set(_normalize_group_names(group_names))
    token_data = list_mercado_store_tokens() or {}
    alias_scope = {}
    collection_targets = {}
    for token in token_data.get("rows") or ():
        if not bool(token.get("enabled", True)):
            continue
        settings = [dict(setting or {}) for setting in (token.get("site_settings") or ())]
        enabled_sites = set()
        for setting in settings:
            salesperson = str(setting.get("salesperson") or "").strip()
            if selected_salespeople and salesperson not in selected_salespeople:
                continue
            group_name = str(setting.get("group_name") or "").strip()
            if selected_group_names and group_name not in selected_group_names:
                continue
            if not _setting_flag_enabled(setting.get("appeal_enabled")):
                continue
            site_code = bit_appeal_ai.normalize_site_code(setting.get("site_id"))
            if site_code in ALL_APPEAL_SITE_CODES:
                enabled_sites.add(site_code)
        if not enabled_sites:
            continue
        aliases = [
            str(alias or "").strip()
            for alias in (token.get("display_name"), token.get("nickname"))
            if str(alias or "").strip()
        ]
        if not aliases:
            continue
        collection_targets.setdefault(aliases[0], set()).update(enabled_sites)
        for alias in aliases:
            alias_key = str(alias or "").strip().casefold()
            if alias_key:
                alias_scope.setdefault(alias_key, set()).update(enabled_sites)
    return alias_scope, collection_targets


def load_authorized_appeal_shop_site_config(salespeople=None, group_names=None):
    """从店铺授权读取允许申诉的店铺/站点，并按业务员、店铺组缩小范围。"""
    scope, _targets = _load_authorized_appeal_shop_site_data(
        salespeople,
        group_names,
    )
    return scope


def load_authorized_appeal_collection_targets(salespeople=None, group_names=None):
    """返回实时侵权 API 读取使用的规范店铺名和站点码。"""
    _scope, targets = _load_authorized_appeal_shop_site_data(
        salespeople,
        group_names,
    )
    return targets


def load_authorized_appeal_api_targets(salespeople=None, group_names=None):
    """Return token-backed stores/sites selected by the appeal authorization switches."""

    selected_salespeople = set(_normalize_salespeople(salespeople))
    selected_group_names = set(_normalize_group_names(group_names))
    token_data = list_mercado_store_tokens() or {}
    targets = []
    for token in token_data.get("rows") or ():
        if not bool(token.get("enabled", True)):
            continue
        site_ids = set()
        for raw_setting in token.get("site_settings") or ():
            setting = dict(raw_setting or {})
            salesperson = str(setting.get("salesperson") or "").strip()
            if selected_salespeople and salesperson not in selected_salespeople:
                continue
            group_name = str(setting.get("group_name") or "").strip()
            if selected_group_names and group_name not in selected_group_names:
                continue
            if not _setting_flag_enabled(setting.get("appeal_enabled")):
                continue
            site_id = str(setting.get("site_id") or "").strip().upper()
            if bit_appeal_ai.normalize_site_code(site_id) in ALL_APPEAL_SITE_CODES:
                site_ids.add(site_id)
        if not site_ids:
            continue
        try:
            token_id = int(token.get("id") or 0)
        except (TypeError, ValueError):
            token_id = 0
        aliases = [
            str(value or "").strip()
            for value in (token.get("display_name"), token.get("nickname"))
            if str(value or "").strip()
        ]
        if token_id <= 0 or not aliases:
            continue
        targets.append(
            {
                "token_id": token_id,
                "name": aliases[0],
                "aliases": aliases,
                "site_ids": sorted(site_ids),
            }
        )
    return targets


def _appeal_scope(only_active=None, salespeople=None, group_names=None):
    """旧参数只保留调用兼容性，站点范围始终由店铺授权开关决定。"""
    return load_authorized_appeal_shop_site_config(salespeople, group_names)


def _appeal_site_is_enabled(scope, shop_name, site_code):
    if scope is None:
        return True
    allowed_sites = scope.get(str(shop_name or "").strip().casefold())
    return bool(allowed_sites and site_code in allowed_sites)


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


def _parse_nonnegative_count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _stop_requested(stop_event=None):
    try:
        return bool(stop_event is not None and stop_event.is_set())
    except (BrokenPipeError, EOFError, OSError):
        return True


def _wait_or_stop(seconds, stop_event=None):
    seconds = max(0, float(seconds or 0))
    if _stop_requested(stop_event):
        return True
    if stop_event is not None:
        try:
            return bool(stop_event.wait(seconds))
        except (BrokenPipeError, EOFError, OSError):
            return True
    time.sleep(seconds)
    return False


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


def _parse_live_infraction_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    candidates = []
    try:
        candidates.append(datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None))
    except ValueError:
        pass
    for date_format in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%y",
        "%m/%d/%Y",
        "%d/%m/%y",
        "%d/%m/%Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
    ):
        try:
            candidates.append(datetime.strptime(text, date_format))
        except ValueError:
            continue
    if not candidates:
        return None
    now = datetime.now()
    not_future = [item for item in candidates if item <= now]
    return max(not_future or candidates)


def _live_infraction_row_values(row):
    if isinstance(row, dict):
        return (
            row.get("店铺名"),
            row.get("站点"),
            row.get("编号"),
            row.get("侵权时间"),
            row.get("类型") or "侵权",
        )
    if isinstance(row, (list, tuple)) and len(row) >= 5:
        return (
            row[0],
            row[1],
            row[2] if len(row) > 2 else "",
            row[4],
            row[7] if len(row) > 7 else "侵权",
        )
    return ("", "", "", "", "侵权")


def build_latest_infraction_appeal_plan(
    top_n=DEFAULT_DAILY_TOP_N,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    only_active=None,
    salespeople=None,
    group_names=None,
    min_infraction_count=0,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    log_path=None,
    stop_event=None,
):
    """通过官方 API 遍历申诉授权站点，再按各站点侵权数量生成计划。

    这里不读取数据库侵权快照，也不打开浏览器读取网页。每轮通过店铺
    Access Token 并发调用 Mercado Moderations API，最后只保留本站点
    侵权数严格大于执行标准的站点。
    """
    del only_active  # 保留旧调用兼容性；实时范围始终来自店铺授权。
    threshold = _parse_nonnegative_count(min_infraction_count)
    recent_days = max(1, int(recent_days or DEFAULT_DAILY_RECENT_DAYS))
    api_targets = load_authorized_appeal_api_targets(
        salespeople,
        group_names,
    )
    if not api_targets:
        print(f"{get_now_time()} 店铺授权中没有符合筛选条件的申诉站点<br>")
        return []

    collection_targets = {
        target["name"]: {
            bit_appeal_ai.normalize_site_code(site_id)
            for site_id in target["site_ids"]
        }
        for target in api_targets
    }
    site_count = sum(len(target["site_ids"]) for target in api_targets)
    print(
        f"{get_now_time()} 开始通过官方 API 遍历 {len(api_targets)} 家店铺、"
        f"{site_count} 个申诉授权站点的侵权数据；不打开浏览器、不读取网页<br>"
    )
    collection_result = mercado_infraction_sync.collect_live_detection_infractions(
        api_targets,
        recent_days=recent_days,
        max_workers=max_workers,
        stop_event=stop_event,
    ) or {}
    failures = collection_result.get("failed_stores") or []
    if failures:
        print(
            f"{get_now_time()} 侵权 API 读取有 {len(failures)} 家异常："
            f"{failures}<br>"
        )
    results = collection_result.get("results") or []
    if results and all(result.get("status") == "error" for result in results):
        details = "；".join(
            f"{result.get('store')}：{result.get('message')}"
            for result in results
        )
        raise RuntimeError(f"全部店铺的侵权 API 读取失败：{details}")
    live_rows = collection_result.get("data") or []
    cutoff = datetime.now() - timedelta(days=recent_days)
    target_lookup = {
        name.casefold(): set(sites)
        for name, sites in collection_targets.items()
    }
    counts = {}
    item_ids = {}
    seen_rows = set()
    for row_index, row in enumerate(live_rows):
        raw_name, raw_site, row_id, infraction_date, infraction_type = (
            _live_infraction_row_values(row)
        )
        name = str(raw_name or "").strip()
        name_key = name.casefold()
        site_code = bit_appeal_ai.normalize_site_code(raw_site)
        if not name or site_code not in target_lookup.get(name_key, set()):
            continue
        normalized_row_type = str(infraction_type or "侵权").strip().casefold()
        if normalized_row_type not in {
            "侵权",
            "infringement",
            "infringements",
            "detection",
            "detections",
        }:
            continue
        parsed_date = _parse_live_infraction_date(infraction_date)
        if parsed_date is None or parsed_date < cutoff:
            continue
        dedupe_key = (
            name_key,
            site_code,
            str(row_id or f"row-{row_index}"),
            str(infraction_type or "侵权"),
        )
        if dedupe_key in seen_rows:
            continue
        seen_rows.add(dedupe_key)
        target_key = (name_key, site_code)
        counts[target_key] = counts.get(target_key, 0) + 1
        normalized_item_id = str(row_id or "").strip().upper()
        if normalized_item_id:
            item_ids.setdefault(target_key, []).append(normalized_item_id)

    plan = []
    for name, authorized_sites in collection_targets.items():
        name_key = name.casefold()
        all_site_counts = {
            site_code: counts.get((name_key, site_code), 0)
            for site_code in authorized_sites
        }
        sites = [
            {
                "site": bit_appeal_ai.normalize_site_name(site_code),
                "site_code": site_code,
                "count": count,
                "infraction_ids": list(item_ids.get((name_key, site_code), ())),
            }
            for site_code, count in all_site_counts.items()
            if count > threshold
        ]
        sites.sort(
            key=lambda item: (item["count"], item["site_code"]),
            reverse=True,
        )
        if sites:
            plan.append({
                "name": name,
                "total": sum(all_site_counts.values()),
                "sites": sites,
            })

    plan.sort(
        key=lambda item: (
            item["total"],
            item["sites"][0]["count"] if item["sites"] else 0,
            item["name"],
        ),
        reverse=True,
    )
    limit = _normalize_appeal_plan_limit(top_n)
    selected = _select_appeal_plan(plan, limit)
    scope_label = "全部" if limit <= 0 else f"Top {limit}"
    print(
        f"{get_now_time()} 官方 API 遍历完成，{scope_label}侵权店铺计划"
        f"（各站点最近 {recent_days} 天侵权数 > {threshold}）：{selected}<br>"
    )
    return selected


def build_latest_reputation_appeal_plan(
    appeal_type,
    top_n=DEFAULT_DAILY_TOP_N,
    only_active=None,
    min_rate=0,
    salespeople=None,
    group_names=None,
):
    """按最新声誉批次生成延误率、取消率或投诉申诉计划。

    只选择比率严格大于 ``min_rate`` 的站点；同一店铺内按比率降序，
    店铺之间按最高站点比率和比率总和排序。
    """
    normalized_type = normalize_appeal_type(appeal_type)
    rate_field = REPUTATION_RATE_FIELDS.get(normalized_type)
    if not rate_field:
        raise ValueError("声誉申诉计划仅支持延误率、取消率或投诉")

    threshold = max(0, _parse_rate(min_rate))
    data = get_latest_reputation_info()
    rows = data.get("rows") or []
    enabled_scope = _appeal_scope(only_active, salespeople, group_names)
    shop_map = {}

    for row in rows:
        name = str(row.get("店铺名") or "").strip()
        site = str(row.get("站点") or "").strip()
        rate_text = str(row.get(rate_field) or "").strip()
        rate = _parse_rate(rate_text)
        if not name or not site or rate <= threshold:
            continue
        site_code = bit_appeal_ai.normalize_site_code(site)
        if not _appeal_site_is_enabled(enabled_scope, name, site_code):
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
    limit = _normalize_appeal_plan_limit(top_n)
    selected = _select_appeal_plan(plan, limit)
    scope_label = "全部" if limit <= 0 else f"Top {limit} "
    label = _appeal_type_label(normalized_type)
    print(f"{get_now_time()} 最新声誉数据时间：{data.get('latest_submit_time', '')}<br>")
    print(
        f"{get_now_time()} {scope_label}{label}店铺计划（比率 > {threshold:.2%}）："
        f"{selected}<br>"
    )
    return selected


def build_appeal_plan(
    appeal_type,
    top_n=DEFAULT_DAILY_TOP_N,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    only_active=None,
    min_rate=0,
    min_infraction_count=0,
    min_delay_rate=None,
    min_cancellation_rate=None,
    min_complaint_rate=None,
    salespeople=None,
    group_names=None,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    log_path=None,
    stop_event=None,
):
    """按申诉类型分发到侵权计划或声誉比率计划。"""
    normalized_type = normalize_appeal_type(appeal_type)
    if normalized_type == APPEAL_TYPE_MIXED:
        raise ValueError("混合模式必须按任务序列执行，不能生成单一申诉计划")
    if normalized_type == APPEAL_TYPE_INFRACTION:
        return build_latest_infraction_appeal_plan(
            top_n=top_n,
            recent_days=recent_days,
            only_active=only_active,
            salespeople=salespeople,
            group_names=group_names,
            min_infraction_count=min_infraction_count,
            max_workers=max_workers,
            log_path=log_path,
            stop_event=stop_event,
        )
    task_rate_thresholds = {
        APPEAL_TYPE_DELAY: min_delay_rate,
        APPEAL_TYPE_CANCELLATION: min_cancellation_rate,
        APPEAL_TYPE_COMPLAINT: min_complaint_rate,
    }
    task_min_rate = task_rate_thresholds.get(normalized_type)
    if task_min_rate is None:
        task_min_rate = min_rate
    return build_latest_reputation_appeal_plan(
        normalized_type,
        top_n=top_n,
        only_active=only_active,
        min_rate=task_min_rate,
        salespeople=salespeople,
        group_names=group_names,
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
    stop_event=None,
):
    normalized_type = normalize_appeal_type(appeal_type)
    appeal_label = _appeal_type_label(normalized_type)
    name = shop_plan["name"]
    results = []
    exit_shop = False
    stopped = False

    for site in shop_plan["sites"]:
        if exit_shop or _stop_requested(stop_event):
            stopped = _stop_requested(stop_event)
            break

        site_code = site["site_code"]
        count = site["count"]
        metric_text = site.get("rate_text") or count
        general_attempt = 1
        rate_retry_count = 0
        result = ""

        while True:
            if _stop_requested(stop_event):
                stopped = True
                exit_shop = True
                break
            try:
                print(
                    f"{get_now_time()} {name} {site_code} 开始 AI 客服{appeal_label}申诉，"
                    f"站点指标 {metric_text}，普通尝试 {general_attempt}/{site_retry_attempts}，"
                    f"限频重试 {rate_retry_count}/{rate_limit_retries}<br>"
                )
                appeal_kwargs = {"validate_open": True}
                if (
                    normalized_type == APPEAL_TYPE_INFRACTION
                    and "infraction_ids" in site
                ):
                    appeal_kwargs["infraction_ids"] = site.get("infraction_ids")
                result = bit_appeal_ai.shensu(
                    name,
                    site_code,
                    normalized_type,
                    message,
                    **appeal_kwargs,
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
                    if _wait_or_stop(rate_limit_retry_seconds, stop_event):
                        stopped = True
                        exit_shop = True
                        break
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
                if _wait_or_stop(site_retry_seconds, stop_event):
                    stopped = True
                    exit_shop = True
                    break
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

        if not exit_shop and site_pause > 0 and _wait_or_stop(site_pause, stop_event):
            stopped = True
            exit_shop = True

    return {
        "name": name,
        "total": shop_plan["total"],
        "appeal_type": appeal_label,
        "results": results,
        "exit_reason": "已停止" if stopped else ("未登录" if exit_shop else ""),
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
    stop_event=None,
):
    """按店铺执行 AI 申诉；整个店铺期间独占该浏览器窗口。"""
    del login_retry_attempts, login_retry_seconds  # 登录失效现在立即终止，不再原地重试。
    normalized_type = normalize_appeal_type(appeal_type)
    appeal_label = _appeal_type_label(normalized_type)
    name = shop_plan["name"]
    if _stop_requested(stop_event):
        return {
            "name": name,
            "total": shop_plan.get("total", 0),
            "appeal_type": appeal_label,
            "results": [],
            "exit_reason": "已停止",
        }
    try:
        window_id = bit_appeal_ai.get_window_id_by_shop_name(name)
    except Exception as e:
        print(
            f"{get_now_time()} {name} 未能启动浏览器进程：{e}；"
            "请核对店铺授权名称与 BitBrowser 窗口名称<br>"
        )
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
            stop_event=stop_event,
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


def _appeal_one_shop_worker_for_type(
    shop,
    appeal_type,
    site_pause,
    message,
    start_delay,
    log_path=None,
    stop_event=None,
):
    if log_path:
        with open(log_path, "a", encoding="utf-8", buffering=1) as log_file:
            with contextlib.redirect_stdout(log_file), contextlib.redirect_stderr(log_file):
                return _appeal_one_shop_worker_for_type(
                    shop,
                    appeal_type,
                    site_pause,
                    message,
                    start_delay,
                    stop_event=stop_event,
                )
    if start_delay > 0:
        print(f"{get_now_time()} {shop.get('name', '')} 启动错峰等待 {start_delay:.1f} 秒<br>")
        if _wait_or_stop(start_delay, stop_event):
            return {
                "name": shop.get("name", ""),
                "results": [],
                "exit_reason": "已停止",
            }
    print(
        f"{get_now_time()} {shop.get('name', '')} 店铺 worker 已并发启动，"
        "开始解析 BitBrowser 窗口<br>"
    )
    return appeal_one_shop(
        shop,
        appeal_type=appeal_type,
        site_pause=site_pause,
        message=message,
        stop_event=stop_event,
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


def _force_close_appeal_plan_windows(plan):
    shops = [dict(shop) for shop in (plan or ())]

    def close_windows():
        for shop in shops:
            name = str(shop.get("name") or "")
            try:
                window_id = bit_appeal_ai.get_window_id_by_shop_name(name)
                closeBrowser(window_id, force=True, request_timeout=3)
                print(f"{get_now_time()} {name} 停止任务并强制关闭浏览器窗口<br>")
            except Exception as exc:
                print(f"{get_now_time()} {name} 停止时关闭浏览器窗口失败：{exc}<br>")

    threading.Thread(
        target=close_windows,
        name="daily-task-window-cleanup",
        daemon=True,
    ).start()


def _run_ai_appeal_once_locked(
    appeal_type,
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    site_pause=30,
    message="",
    only_active=None,
    min_rate=0,
    min_infraction_count=0,
    min_delay_rate=None,
    min_cancellation_rate=None,
    min_complaint_rate=None,
    salespeople=None,
    group_names=None,
    stop_event=None,
    log_path=None,
):
    """用多进程并发处理已开启的任务；店铺内部按站点指标降序串行处理。"""
    selected_types = normalize_appeal_types(appeal_type)
    sequence = appeal_type_sequence(selected_types)
    if stop_event is not None and stop_event.is_set():
        print(f"{get_now_time()} 已收到停止请求，本轮任务不再启动<br>")
        return []
    if len(sequence) > 1:
        print(
            f"{get_now_time()} 开始多任务模式，执行顺序："
            f"{' → '.join(_appeal_type_label(item) for item in sequence)}<br>"
        )
        task_results = []
        for index, current_type in enumerate(sequence, start=1):
            if stop_event is not None and stop_event.is_set():
                print(f"{get_now_time()} 已收到停止请求，剩余任务不再执行<br>")
                break
            current_label = _appeal_type_label(current_type)
            print(
                f"{get_now_time()} 多任务第 {index}/{len(sequence)} 项："
                f"{current_label}<br>"
            )
            task_results.append({
                "appeal_type": current_label,
                "results": _run_ai_appeal_once_locked(
                    current_type,
                    top_n=top_n,
                    max_workers=max_workers,
                    recent_days=recent_days,
                    site_pause=site_pause,
                    message=message,
                    only_active=only_active,
                    min_rate=min_rate,
                    min_infraction_count=min_infraction_count,
                    min_delay_rate=min_delay_rate,
                    min_cancellation_rate=min_cancellation_rate,
                    min_complaint_rate=min_complaint_rate,
                    salespeople=salespeople,
                    group_names=group_names,
                    stop_event=stop_event,
                    log_path=log_path,
                ),
            })
        print(f"{get_now_time()} 多任务一轮执行完成<br>")
        return task_results
    normalized_type = selected_types[0]
    appeal_label = _appeal_type_label(normalized_type)
    plan_limit = _normalize_appeal_plan_limit(top_n)
    plan_scope = (
        f"Top {plan_limit} {appeal_label}店铺"
        if plan_limit > 0
        else f"全部符合执行标准的{appeal_label}店铺"
    )
    plan = build_appeal_plan(
        normalized_type,
        top_n=top_n,
        recent_days=recent_days,
        only_active=only_active,
        min_rate=min_rate,
        min_infraction_count=min_infraction_count,
        min_delay_rate=min_delay_rate,
        min_cancellation_rate=min_cancellation_rate,
        min_complaint_rate=min_complaint_rate,
        salespeople=salespeople,
        group_names=group_names,
        max_workers=max_workers,
        log_path=log_path,
        stop_event=stop_event,
    )
    if not plan:
        print(f"{get_now_time()} 没有找到可处理的{appeal_label}店铺<br>")
        return []
    if stop_event is not None and stop_event.is_set():
        print(f"{get_now_time()} 已收到停止请求，{appeal_label}任务不再启动<br>")
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
    executor = ProcessPoolExecutor(max_workers=worker_count)
    future_map = {
        executor.submit(
            _appeal_one_shop_worker_for_type,
            shop,
            normalized_type,
            site_pause,
            message,
            index * max(0, DEFAULT_START_STAGGER_SECONDS),
            log_path,
            stop_event,
        ): shop
        for index, shop in enumerate(plan)
    }
    pending = set(future_map)
    forced_stop = False
    try:
        while pending:
            completed, pending = wait(
                pending,
                timeout=0.5,
                return_when=FIRST_COMPLETED,
            )
            for future in completed:
                shop = future_map[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    shop_name = str(shop.get("name") or "")
                    results.append({"name": shop_name, "error": str(e)})
                    print(
                        f"{get_now_time()} {shop_name}店铺任务失败，"
                        f"不影响其他店铺继续执行：{e}<br>"
                    )
                    traceback.print_exc()
            if _stop_requested(stop_event):
                for future in pending:
                    future.cancel()
                print(
                    f"{get_now_time()} 已收到停止请求，立即终止正在执行的"
                    f"{appeal_label}店铺进程<br>"
                )
                forced_stop = True
                break
    finally:
        if forced_stop:
            terminate_process_pool(executor)
            _force_close_appeal_plan_windows(plan)
        else:
            executor.shutdown(wait=True, cancel_futures=True)

    if normalized_type == APPEAL_TYPE_INFRACTION:
        print(
            f"{get_now_time()} {plan_scope}全部店铺、全部授权站点已发送完毕，"
            "本轮侵权申诉完成<br>"
        )
    print(f"{get_now_time()} {plan_scope} AI 客服申诉一轮完成：{results}<br>")
    return results


def _run_top_infraction_ai_appeal_once_locked(
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    site_pause=30,
    message="",
    only_active=None,
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
    only_active=None,
    min_rate=0,
    min_infraction_count=0,
    min_delay_rate=None,
    min_cancellation_rate=None,
    min_complaint_rate=None,
    salespeople=None,
    group_names=None,
    stop_event=None,
    log_path=None,
    _task_lock=None,
):
    selected_types = normalize_appeal_types(appeal_type)
    appeal_label = "、".join(_appeal_type_label(item) for item in selected_types)
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
            selected_types,
            top_n=top_n,
            max_workers=max_workers,
            recent_days=recent_days,
            site_pause=site_pause,
            message=message,
            only_active=only_active,
            min_rate=min_rate,
            min_infraction_count=min_infraction_count,
            min_delay_rate=min_delay_rate,
            min_cancellation_rate=min_cancellation_rate,
            min_complaint_rate=min_complaint_rate,
            salespeople=salespeople,
            group_names=group_names,
            stop_event=stop_event,
            log_path=log_path,
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
    only_active=None,
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
    only_active=None,
    min_rate=0,
    min_infraction_count=0,
    min_delay_rate=None,
    min_cancellation_rate=None,
    min_complaint_rate=None,
    salespeople=None,
    group_names=None,
    stop_at=None,
    max_rounds=None,
    task_lock=None,
    stop_event=None,
    log_path=None,
):
    """循环执行已开启的店铺 AI 客服申诉任务。"""
    selected_types = normalize_appeal_types(appeal_type)
    appeal_label = "、".join(_appeal_type_label(item) for item in selected_types)
    plan_limit = _normalize_appeal_plan_limit(top_n)
    plan_scope = (
        f"Top {plan_limit} {appeal_label}店铺"
        if plan_limit > 0
        else f"全部符合执行标准的{appeal_label}店铺"
    )
    round_limit = None if max_rounds is None else max(1, int(max_rounds))
    round_no = 1
    if round_limit is not None:
        print(
            f"{get_now_time()} {plan_scope} AI 客服申诉循环"
            f"共执行 {round_limit} 轮<br>"
        )
    if stop_at:
        print(
            f"{get_now_time()} {plan_scope} AI 客服申诉循环将在 "
            f"{_format_stop_at(stop_at)} 前停止<br>"
        )
    while True:
        if stop_event is not None and stop_event.is_set():
            print(
                f"{get_now_time()} 已收到停止请求，"
                f"结束{plan_scope} AI 客服申诉循环<br>"
            )
            return
        remaining = _seconds_until_stop(stop_at)
        if remaining is not None and remaining <= 0:
            print(
                f"{get_now_time()} 已到达停止时间，"
                f"结束{plan_scope} AI 客服申诉循环<br>"
            )
            return

        started = time.time()
        try:
            api_refresh_text = (
                "，先重新读取官方 API 侵权列表"
                if APPEAL_TYPE_INFRACTION in selected_types
                else ""
            )
            print(
                f"{get_now_time()} 开始第 {round_no} 轮 "
                f"{plan_scope} AI 客服申诉{api_refresh_text}<br>"
            )
            run_ai_appeal_once(
                selected_types,
                top_n=top_n,
                max_workers=max_workers,
                recent_days=recent_days,
                site_pause=site_pause,
                message=message,
                only_active=only_active,
                min_rate=min_rate,
                min_infraction_count=min_infraction_count,
                min_delay_rate=min_delay_rate,
                min_cancellation_rate=min_cancellation_rate,
                min_complaint_rate=min_complaint_rate,
                salespeople=salespeople,
                group_names=group_names,
                stop_event=stop_event,
                log_path=log_path,
                _task_lock=task_lock,
            )
        except Exception as e:
            print(
                f"{get_now_time()} 第 {round_no} 轮{plan_scope} "
                f"AI 客服申诉异常：{e}<br>"
            )
            traceback.print_exc()

        if round_limit is not None and round_no >= round_limit:
            print(
                f"{get_now_time()} 已完成 {round_limit} 轮，"
                f"结束{plan_scope} AI 客服申诉循环<br>"
            )
            return
        if stop_event is not None and stop_event.is_set():
            print(
                f"{get_now_time()} 已收到停止请求，"
                f"结束{plan_scope} AI 客服申诉循环<br>"
            )
            return

        sleep_seconds = max(0, int(round_interval) - (time.time() - started))
        remaining = _seconds_until_stop(stop_at)
        if remaining is not None:
            if remaining <= 0:
                print(
                    f"{get_now_time()} 已到达停止时间，"
                    f"结束{plan_scope} AI 客服申诉循环<br>"
                )
                return
            sleep_seconds = min(sleep_seconds, remaining)
        refresh_text = (
            "重新读取官方 API 侵权列表并生成下一轮计划"
            if APPEAL_TYPE_INFRACTION in selected_types
            else f"重新计算{plan_scope}"
        )
        print(
            f"{get_now_time()} 第 {round_no} 轮全部站点结束，"
            f"等待 {sleep_seconds:.1f} 秒后{refresh_text}<br>"
        )
        if stop_event is not None:
            if stop_event.wait(sleep_seconds):
                print(
                    f"{get_now_time()} 已收到停止请求，"
                    f"结束{plan_scope} AI 客服申诉循环<br>"
                )
                return
        else:
            time.sleep(sleep_seconds)
        round_no += 1


def _loop_top_infraction_ai_appeal_locked(
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    round_interval=600,
    site_pause=30,
    message="",
    only_active=None,
    stop_at=None,
    max_rounds=None,
    task_lock=None,
    stop_event=None,
    log_path=None,
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
        stop_event=stop_event,
        log_path=log_path,
    )


def loop_ai_appeal(
    appeal_type,
    top_n=DEFAULT_DAILY_TOP_N,
    max_workers=DEFAULT_DAILY_MAX_WORKERS,
    recent_days=DEFAULT_DAILY_RECENT_DAYS,
    round_interval=600,
    site_pause=30,
    message="",
    only_active=None,
    min_rate=0,
    min_infraction_count=0,
    min_delay_rate=None,
    min_cancellation_rate=None,
    min_complaint_rate=None,
    salespeople=None,
    group_names=None,
    stop_at=None,
    max_rounds=None,
    stop_event=None,
    log_path=None,
    _task_lock=None,
):
    selected_types = normalize_appeal_types(appeal_type)
    appeal_label = "、".join(_appeal_type_label(item) for item in selected_types)
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
            selected_types,
            top_n=top_n,
            max_workers=max_workers,
            recent_days=recent_days,
            round_interval=round_interval,
            site_pause=site_pause,
            message=message,
            only_active=only_active,
            min_rate=min_rate,
            min_infraction_count=min_infraction_count,
            min_delay_rate=min_delay_rate,
            min_cancellation_rate=min_cancellation_rate,
            min_complaint_rate=min_complaint_rate,
            salespeople=salespeople,
            group_names=group_names,
            stop_at=stop_at,
            max_rounds=max_rounds,
            task_lock=task_lock,
            stop_event=stop_event,
            log_path=log_path,
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
    only_active=None,
    stop_at=None,
    max_rounds=None,
    stop_event=None,
    log_path=None,
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
        stop_event=stop_event,
        log_path=log_path,
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
