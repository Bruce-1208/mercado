"""逐店验证配置站点的 Mercado Libre 流量读取，不写入数据库。"""

import argparse
import json
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from bit.bit_api import closeBrowser
from bit.bit_config import list_config_rows
from bit.bit_reputation_info import (
    MercadoAuthenticationError,
    _connect_browser,
    _failure_status,
    _split_sites,
    get_recent_visits_info,
)
from bit.bit_runtime_lock import create_window_lease


def _check_shop(row, lease_wait_seconds=300, attempts=2):
    window_id, shop_name = row[0], row[1]
    sites = _split_sites(row[3])
    lease = create_window_lease(
        window_id,
        owner=f"traffic_check:{shop_name}",
        shop_name=shop_name,
        task_type="traffic_check",
    )
    if not lease.acquire(timeout=max(0, float(lease_wait_seconds))):
        return [
            {
                "shop": shop_name,
                "site": site,
                "ok": False,
                "status": "失败：窗口被其他任务占用",
                "visits": [],
            }
            for site in sites
        ]

    driver = None
    results = []
    try:
        driver = _connect_browser(window_id, max_retries=2, retry_delay=10)
        fatal_error = None
        for site in sites:
            if fatal_error is not None:
                results.append(
                    {
                        "shop": shop_name,
                        "site": site,
                        "ok": False,
                        "status": _failure_status(fatal_error),
                        "visits": [],
                    }
                )
                continue
            last_error = None
            succeeded = False
            for attempt in range(1, max(1, int(attempts)) + 1):
                try:
                    visits = get_recent_visits_info(
                        driver,
                        window_id,
                        shop_name,
                        site,
                        days=8,
                    )
                    results.append(
                        {
                            "shop": shop_name,
                            "site": site,
                            "ok": bool(visits),
                            "status": "成功" if visits else "失败：流量为空",
                            "visits": visits,
                            "days": len(visits),
                        }
                    )
                    succeeded = True
                    break
                except Exception as exc:
                    last_error = exc
                    if isinstance(exc, MercadoAuthenticationError):
                        fatal_error = exc
                        break
                    if attempt < max(1, int(attempts)):
                        time.sleep(3)
            if not succeeded:
                results.append(
                    {
                        "shop": shop_name,
                        "site": site,
                        "ok": False,
                        "status": _failure_status(last_error),
                        "visits": [],
                    }
                )
    except Exception as exc:
        status = _failure_status(exc)
        results.extend(
            {
                "shop": shop_name,
                "site": site,
                "ok": False,
                "status": status,
                "visits": [],
            }
            for site in sites
        )
    finally:
        # 只有成功附加到本次打开的 WebDriver 才关闭窗口。若 BitBrowser 返回
        # “浏览器正在打开中”，窗口可能属于未遵守本地 lease 的后台任务，
        # 诊断工具不能替它关闭。
        if driver is not None:
            try:
                closeBrowser(window_id, lease=lease)
            except Exception as exc:
                print(f"{shop_name}关闭浏览器失败：{exc}", flush=True)
        lease.release()
    return results


def check_all_traffic(
    *,
    workers=3,
    selected_shops=None,
    selected_sites=None,
    start_shop=None,
    lease_wait_seconds=300,
    attempts=2,
    output_path=None,
):
    selected_shops = {str(value).strip() for value in selected_shops or [] if str(value).strip()}
    selected_sites = {str(value).strip() for value in selected_sites or [] if str(value).strip()}
    rows = []
    start_shop = str(start_shop or "").strip()
    start_reached = not start_shop
    for row in list_config_rows(include_ignored=False):
        if not row or not row[0] or not row[3]:
            continue
        if not start_reached:
            start_reached = str(row[1]).strip() == start_shop
            if not start_reached:
                continue
        if selected_shops and str(row[1]).strip() not in selected_shops:
            continue
        sites = _split_sites(row[3])
        if selected_sites:
            sites = [site for site in sites if site in selected_sites]
            if not sites:
                continue
            row = list(row)
            row[3] = "，".join(sites)
            row = tuple(row)
        rows.append(row)

    results = []
    completed_shops = 0
    with ProcessPoolExecutor(max_workers=max(1, min(int(workers), len(rows) or 1))) as executor:
        future_map = {
            executor.submit(
                _check_shop,
                row,
                lease_wait_seconds,
                attempts,
            ): row
            for row in rows
        }
        for future in as_completed(future_map):
            row = future_map[future]
            try:
                shop_results = future.result()
            except Exception as exc:
                shop_results = [
                    {
                        "shop": row[1],
                        "site": site,
                        "ok": False,
                        "status": _failure_status(exc),
                        "visits": [],
                    }
                    for site in _split_sites(row[3])
                ]
            results.extend(shop_results)
            completed_shops += 1
            for result in shop_results:
                print(
                    "TRAFFIC_CHECK "
                    + json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    flush=True,
                )
            print(
                f"TRAFFIC_CHECK_PROGRESS={completed_shops}/{len(rows)}",
                flush=True,
            )

    results.sort(key=lambda item: (item["shop"], item["site"]))
    failure_reasons = Counter(
        str(item.get("status") or "失败：未知异常")
        for item in results
        if not item["ok"]
    )
    summary = {
        "shop_count": len({item["shop"] for item in results}),
        "site_count": len(results),
        "success_count": sum(bool(item["ok"]) for item in results),
        "failure_count": sum(not item["ok"] for item in results),
        "partial_count": sum(
            bool(item["ok"]) and int(item.get("days", 0)) < 8 for item in results
        ),
        "failures": [item for item in results if not item["ok"]],
        "partials": [
            item
            for item in results
            if item["ok"] and int(item.get("days", 0)) < 8
        ],
        "failure_reasons": dict(failure_reasons.most_common()),
    }
    if output_path is None:
        output_path = (
            Path(__file__).resolve().parent
            / "采集失败记录"
            / f"全店流量测试-{datetime.now():%Y%m%d-%H%M%S}.json"
        )
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary["report_path"] = str(output_path)
    report = {
        "tested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "results": results,
    }
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "TRAFFIC_CHECK_SUMMARY=" + json.dumps(summary, ensure_ascii=False),
        flush=True,
    )
    return summary, results


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--shop", action="append", default=[])
    parser.add_argument("--site", action="append", default=[])
    parser.add_argument("--start-shop")
    parser.add_argument("--lease-wait-seconds", type=float, default=300)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--output")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    summary, _results = check_all_traffic(
        workers=args.workers,
        selected_shops=args.shop,
        selected_sites=args.site,
        start_shop=args.start_shop,
        lease_wait_seconds=args.lease_wait_seconds,
        attempts=args.attempts,
        output_path=args.output,
    )
    return 1 if summary["failure_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
