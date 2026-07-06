import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from bit import bit_appeal_ai
from bit.bit_db_api import get_latest_infraction_info
from bit.bit_utils import get_now_time


def build_latest_infraction_appeal_plan(top_n=5, recent_days=30, only_active=True):
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


def appeal_one_shop_infractions(shop_plan, site_pause=30, message=""):
    """单个店铺内，按侵权数量最多的站点开始，依次调用 AI 客服侵权申诉。"""
    name = shop_plan["name"]
    results = []
    for site in shop_plan["sites"]:
        site_code = site["site_code"]
        count = site["count"]
        try:
            print(f"{get_now_time()} {name} {site_code} 开始 AI 客服侵权申诉，站点侵权数 {count}<br>")
            result = bit_appeal_ai.shensu(name, site_code, "侵权", message)
            results.append({"site": site_code, "count": count, "result": result})
            print(f"{get_now_time()} {name} {site_code} AI 客服侵权申诉完成：{result}<br>")
        except Exception as e:
            results.append({"site": site_code, "count": count, "error": str(e)})
            print(f"{get_now_time()} {name} {site_code} AI 客服侵权申诉失败：{e}<br>")
            traceback.print_exc()

        if site_pause > 0:
            time.sleep(site_pause)

    return {"name": name, "total": shop_plan["total"], "results": results}


def run_top_infraction_ai_appeal_once(top_n=5, max_workers=None, recent_days=30, site_pause=30, message="", only_active=True):
    """并发处理侵权总数最多的 N 家店铺；每个店铺内部按站点侵权数降序串行处理。"""
    plan = build_latest_infraction_appeal_plan(
        top_n=top_n,
        recent_days=recent_days,
        only_active=only_active,
    )
    if not plan:
        print(f"{get_now_time()} 没有找到可处理的侵权店铺<br>")
        return []

    worker_count = max_workers if max_workers is not None else top_n
    worker_count = max(1, min(int(worker_count), len(plan)))
    results = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(appeal_one_shop_infractions, shop, site_pause, message)
            for shop in plan
        ]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                results.append({"error": str(e)})
                traceback.print_exc()

    print(f"{get_now_time()} Top 侵权店铺 AI 客服申诉一轮完成：{results}<br>")
    return results


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


def loop_top_infraction_ai_appeal(
    top_n=5,
    max_workers=None,
    recent_days=30,
    round_interval=600,
    site_pause=30,
    message="",
    only_active=True,
    stop_at=None,
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


if __name__ == "__main__":
    loop_top_infraction_ai_appeal(top_n=20, max_workers=20, round_interval=600)
