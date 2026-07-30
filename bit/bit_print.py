"""Mercado Libre 订单标签单次打印。

该模块既可以独立运行，也供 ``bit_interface`` 服务台调用。
打印任务按店铺串行执行，并与其他 BitBrowser 任务共用窗口锁，
避免重复打印或关闭正在被其他任务使用的窗口。
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from typing import Callable, Iterable

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from bit.bit_api import closeBrowser, openBrowser
from bit.bit_config import list_config_rows, split_config_sites
from bit.bit_db_api import insert_task_record
from bit.bit_mercado_login import open_mercado_backend_page
from bit.bit_runtime_lock import InterProcessLock, create_window_lease, get_lock_owner
from bit.bit_switch_country import force_select_country, oepn_country_switch
from bit.bit_utils import get_now_time


ORDERS_URL = (
    "https://global-selling.mercadolibre.com/orders/omni/list?"
    "filters=&subFilters=&search=&limit=50&offset=0&"
    "startPeriod=WITH_DATE_CLOSED_2M_OLD&selectedTab=TAB_TODAY_CBT"
)
ORDER_PRINT_LOCK_KEY = "bit_order_print_task"
SITE_COUNTRY_NAMES = {
    "墨西哥": "Mexico",
    "巴西": "Brazil",
    "哥伦比亚": "Colombia",
    "智利": "Chile",
    "阿根廷": "Argentina",
    "乌拉圭": "Uruguay",
}

# 最后两个 XPath 保留旧页面结构作为兜底；前面优先使用语义选择器。
SELECT_ALL_LOCATORS = (
    (By.CSS_SELECTOR, "input[type='checkbox'][aria-label*='select all' i]"),
    (By.CSS_SELECTOR, "input[type='checkbox'][data-testid*='select-all' i]"),
    (
        By.XPATH,
        "//input[@type='checkbox' and contains(translate(@aria-label, "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'all')]",
    ),
    (
        By.XPATH,
        "/html/body/main/div/div[3]/div/div/div[3]/div/div[2]/div/div/"
        "section/div/div[1]/div/div/div[1]/div[1]/div/div/span/input",
    ),
)
PRINT_BUTTON_LOCATORS = (
    (By.CSS_SELECTOR, "button[data-testid*='print' i]"),
    (By.CSS_SELECTOR, "button[aria-label*='print' i]"),
    (
        By.XPATH,
        "//button[contains(translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'print') "
        "or contains(translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'imprimir')]",
    ),
    (
        By.XPATH,
        "/html/body/main/div/div[3]/div/div/div[3]/div/div[2]/div/div/"
        "section/div/div[1]/div/div/div[2]/div/button",
    ),
)
NO_ORDER_MARKERS = (
    "no orders",
    "no sales",
    "no hay ventas",
    "no encontramos ventas",
    "não há vendas",
    "nenhuma venda",
    "sem vendas",
)


class PrintTaskStopped(RuntimeError):
    """用于在站点切换、重试等安全边界结束任务。"""


def _now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _emit(logger: Callable[[str], None] | None, message: str):
    text = f"{get_now_time()} {message}"
    if logger is None:
        print(text)
    else:
        logger(text)


def _interruptible_wait(seconds, stop_event=None):
    seconds = max(0, float(seconds or 0))
    if not seconds:
        return False
    if stop_event is not None:
        return bool(stop_event.wait(seconds))
    time.sleep(seconds)
    return False


def acquire_order_print_lock(owner="bit_print", mode="once"):
    task_lock = InterProcessLock(
        ORDER_PRINT_LOCK_KEY,
        owner=owner,
        metadata={"mode": str(mode or "once"), "task_type": "order_print"},
    )
    return task_lock if task_lock.acquire(timeout=0) else None


def get_order_print_lock_owner():
    return get_lock_owner(ORDER_PRINT_LOCK_KEY)


def _normalized_selection(values: Iterable[str] | str | None):
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


def _normalized_targets(targets):
    normalized = []
    seen = set()
    for target in targets or ():
        if isinstance(target, dict):
            shop_name = str(target.get("shop_name") or "").strip()
            site = str(target.get("site") or "").strip()
        elif isinstance(target, (list, tuple)) and len(target) >= 2:
            shop_name = str(target[0] or "").strip()
            site = str(target[1] or "").strip()
        else:
            continue
        key = (shop_name, site)
        if not shop_name or not site or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return tuple(normalized)


def build_print_jobs(
    rows=None,
    selected_shops=None,
    selected_sites=None,
    selected_targets=None,
):
    """把数据库店铺配置转为待执行的店铺/站点任务。"""

    shop_filter = set(_normalized_selection(selected_shops))
    site_filter = set(_normalized_selection(selected_sites))
    target_filter = set(_normalized_targets(selected_targets))
    jobs = []
    seen_window_sites = set()
    for row in rows if rows is not None else list_config_rows(include_ignored=False):
        values = tuple(row or ()) + ("",) * 7
        window_id, shop_name, _status, configured_sites = values[:4]
        window_id = str(window_id or "").strip()
        shop_name = str(shop_name or "").strip()
        if not window_id or not shop_name:
            continue
        if shop_filter and shop_name not in shop_filter:
            continue
        sites = []
        for site in split_config_sites(configured_sites):
            if target_filter:
                if (shop_name, site) not in target_filter:
                    continue
            elif site_filter and site not in site_filter:
                continue
            sites.append(site)
        sites = [
            site
            for site in sites
            if (window_id, site) not in seen_window_sites
        ]
        if sites:
            seen_window_sites.update((window_id, site) for site in sites)
            jobs.append(
                {
                    "window_id": window_id,
                    "shop_name": shop_name,
                    "sites": sites,
                }
            )
    return jobs


def _connect_driver(open_response):
    if not isinstance(open_response, dict) or open_response.get("success") is False:
        message = (open_response or {}).get("msg") if isinstance(open_response, dict) else ""
        raise RuntimeError(message or "BitBrowser 窗口打开失败")
    data = open_response.get("data") or {}
    driver_path = data.get("driver")
    debugger_address = data.get("http")
    if not driver_path or not debugger_address:
        raise RuntimeError("BitBrowser 打开结果缺少 driver 或 http 地址")

    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_experimental_option("debuggerAddress", debugger_address)
    return webdriver.Chrome(
        service=Service(driver_path),
        options=chrome_options,
    )


def _wait_for_page(driver, timeout):
    WebDriverWait(driver, timeout).until(
        lambda current: current.execute_script("return document.readyState") in ("interactive", "complete")
    )


def _find_clickable(driver, locators, timeout):
    """先快速尝试语义选择器，再对旧 XPath 做一次显式等待。"""

    for by, value in locators:
        try:
            for element in driver.find_elements(by, value):
                if element.is_displayed() and element.is_enabled():
                    return element
        except Exception:
            continue
    last_by, last_value = locators[-1]
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((last_by, last_value))
    )


def _page_has_no_orders(driver):
    try:
        text = str(driver.find_element(By.TAG_NAME, "body").text or "").casefold()
    except Exception:
        text = str(getattr(driver, "page_source", "") or "").casefold()
    return any(marker in text for marker in NO_ORDER_MARKERS)


def _switch_site(driver, site, timeout, logger=None):
    country_name = SITE_COUNTRY_NAMES.get(str(site or "").strip())
    if not country_name:
        raise ValueError(f"不支持的站点：{site}")
    _wait_for_page(driver, timeout)
    if not oepn_country_switch(driver):
        raise RuntimeError("未找到站点选择器")
    if not force_select_country(driver, country_name):
        raise RuntimeError(f"站点切换失败：{site}")
    _emit(logger, f"已切换到 {site} 站点")


def _open_orders_page(
    driver,
    shop_name,
    window_id,
    *,
    settle_seconds=5,
    stop_event=None,
):
    def guarded_sleep(seconds):
        if _interruptible_wait(seconds, stop_event):
            raise PrintTaskStopped("已收到停止请求")

    result = open_mercado_backend_page(
        driver,
        ORDERS_URL,
        shop_name,
        window_id,
        settle_seconds=settle_seconds,
        sleep=guarded_sleep,
    )
    if not result.get("ok"):
        raise RuntimeError(result.get("message") or result.get("status"))
    return result


def _print_current_site(
    driver,
    site,
    timeout=15,
    settle_seconds=5,
    stop_event=None,
    logger=None,
    shop_name="",
    window_id="",
):
    if stop_event is not None and stop_event.is_set():
        raise PrintTaskStopped("已收到停止请求")

    _open_orders_page(
        driver,
        shop_name,
        window_id,
        settle_seconds=settle_seconds,
        stop_event=stop_event,
    )
    _wait_for_page(driver, timeout)
    _switch_site(driver, site, timeout, logger=logger)

    _open_orders_page(
        driver,
        shop_name,
        window_id,
        settle_seconds=settle_seconds,
        stop_event=stop_event,
    )
    _wait_for_page(driver, timeout)

    try:
        select_all = _find_clickable(driver, SELECT_ALL_LOCATORS, timeout)
    except Exception as exc:
        if _page_has_no_orders(driver):
            return {
                "status": "no_orders",
                "message": "当前站点没有待打印订单",
                "selected_count": 0,
            }
        raise RuntimeError("页面已加载，但未找到订单全选控件") from exc

    if not select_all.is_selected():
        driver.execute_script("arguments[0].click();", select_all)
    try:
        selected_count = int(
            driver.execute_script(
                "return Array.from(document.querySelectorAll('input[type=checkbox]:checked'))"
                ".filter((element) => !String(element.getAttribute('aria-label') || '')"
                ".toLowerCase().includes('all') && !String(element.getAttribute('data-testid') || '')"
                ".toLowerCase().includes('select-all')).length;"
            )
            or 0
        )
    except Exception:
        selected_count = 0

    try:
        print_button = _find_clickable(driver, PRINT_BUTTON_LOCATORS, timeout)
    except Exception as exc:
        raise RuntimeError("已勾选订单，但未找到打印按钮") from exc

    driver.execute_script("arguments[0].click();", print_button)
    _emit(logger, f"{site} 已提交打印请求")
    return {
        "status": "printed",
        "message": "已提交打印",
        "selected_count": selected_count,
    }


def _result_row(shop_name, site, status, message, attempts=0, selected_count=0):
    return {
        "shop_name": str(shop_name or ""),
        "site": str(site or ""),
        "status": str(status or "failed"),
        "message": str(message or ""),
        "attempts": int(attempts or 0),
        "selected_count": int(selected_count or 0),
        "finished_at": _now_text(),
    }


def _task_record(result):
    status = result["status"]
    if status == "printed":
        outcome = "成功"
    elif status == "no_orders":
        outcome = "成功：无待打印订单"
    elif status == "skipped":
        outcome = f"跳过：{result['message']}"
    else:
        outcome = f"失败：{result['message']}"
    return ("后台打印订单", result["shop_name"], result["site"], outcome, result["finished_at"])


def _run_shop_job(
    job,
    *,
    max_retries=3,
    retry_delay_seconds=300,
    page_timeout=15,
    settle_seconds=5,
    stop_event=None,
    logger=None,
):
    window_id = job["window_id"]
    shop_name = job["shop_name"]
    sites = list(job["sites"])
    lease = create_window_lease(
        window_id,
        owner=f"bit_print:{shop_name}",
        shop_name=shop_name,
        task_type="order_print",
    )
    if not lease.acquire(timeout=0):
        owner = get_lock_owner(lease.key)
        owner_name = owner.get("owner") or "其他任务"
        message = f"窗口正在被 {owner_name} 使用"
        _emit(logger, f"{shop_name} {message}，本轮跳过")
        return [_result_row(shop_name, site, "skipped", message) for site in sites]

    driver = None
    browser_opened = False
    results = []
    try:
        if stop_event is not None and stop_event.is_set():
            return results
        _emit(logger, f"开始打开店铺窗口：{shop_name}")
        open_response = openBrowser(window_id)
        browser_opened = bool(
            isinstance(open_response, dict)
            and open_response.get("success") is not False
            and open_response.get("data")
        )
        driver = _connect_driver(open_response)
        driver.implicitly_wait(2)

        for site in sites:
            if stop_event is not None and stop_event.is_set():
                break
            last_error = ""
            for attempt in range(1, max(1, int(max_retries)) + 1):
                try:
                    _emit(logger, f"正在处理 {shop_name} / {site}（第 {attempt} 次）")
                    outcome = _print_current_site(
                        driver,
                        site,
                        timeout=page_timeout,
                        settle_seconds=settle_seconds,
                        stop_event=stop_event,
                        logger=logger,
                        shop_name=shop_name,
                        window_id=window_id,
                    )
                    results.append(
                        _result_row(
                            shop_name,
                            site,
                            outcome["status"],
                            outcome["message"],
                            attempts=attempt,
                            selected_count=outcome.get("selected_count", 0),
                        )
                    )
                    break
                except PrintTaskStopped:
                    raise
                except Exception as exc:
                    last_error = str(exc) or exc.__class__.__name__
                    _emit(logger, f"{shop_name} / {site} 第 {attempt} 次失败：{last_error}")
                    if attempt < max(1, int(max_retries)):
                        if _interruptible_wait(retry_delay_seconds, stop_event):
                            raise PrintTaskStopped("已收到停止请求")
            else:
                results.append(
                    _result_row(
                        shop_name,
                        site,
                        "failed",
                        last_error or "打印失败",
                        attempts=max(1, int(max_retries)),
                    )
                )
    except PrintTaskStopped:
        _emit(logger, f"{shop_name} 已在安全边界停止")
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        _emit(logger, f"{shop_name} 窗口初始化失败：{message}")
        completed_sites = {result["site"] for result in results}
        results.extend(
            _result_row(shop_name, site, "failed", message)
            for site in sites
            if site not in completed_sites
        )
    finally:
        if browser_opened:
            try:
                close_result = closeBrowser(window_id, lease=lease)
                if isinstance(close_result, dict) and close_result.get("success") is False:
                    _emit(logger, f"{shop_name} 窗口关闭失败：{close_result.get('msg') or close_result}")
                else:
                    _emit(logger, f"{shop_name} 窗口已关闭")
            except Exception as exc:
                _emit(logger, f"{shop_name} 窗口关闭失败：{exc}")
        lease.release()
    return results


def _summary(results, started_at, stopped=False):
    counts = {
        "printed": sum(result["status"] == "printed" for result in results),
        "no_orders": sum(result["status"] == "no_orders" for result in results),
        "failed": sum(result["status"] == "failed" for result in results),
        "skipped": sum(result["status"] == "skipped" for result in results),
    }
    finished_at = _now_text()
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "stopped": bool(stopped),
        "total": len(results),
        **counts,
        "results": results,
    }


def print_orders_all(
    selected_shops=None,
    selected_sites=None,
    selected_targets=None,
    *,
    max_retries=3,
    retry_delay_seconds=300,
    page_timeout=15,
    settle_seconds=5,
    stop_event=None,
    logger=None,
    persist=True,
):
    """执行一轮订单打印并返回可供服务台展示的结构化结果。"""

    started_at = _now_text()
    jobs = build_print_jobs(
        selected_shops=selected_shops,
        selected_sites=selected_sites,
        selected_targets=selected_targets,
    )
    _emit(
        logger,
        f"开始订单打印：{len(jobs)} 家店铺，"
        f"{sum(len(job['sites']) for job in jobs)} 个店铺站点",
    )
    results = []
    for job in jobs:
        if stop_event is not None and stop_event.is_set():
            break
        results.extend(
            _run_shop_job(
                job,
                max_retries=max_retries,
                retry_delay_seconds=retry_delay_seconds,
                page_timeout=page_timeout,
                settle_seconds=settle_seconds,
                stop_event=stop_event,
                logger=logger,
            )
        )

    if persist and results:
        try:
            insert_task_record([_task_record(result) for result in results])
        except Exception as exc:
            _emit(logger, f"打印结果写入任务记录失败：{exc}")

    summary = _summary(
        results,
        started_at,
        stopped=bool(stop_event is not None and stop_event.is_set()),
    )
    _emit(
        logger,
        "本轮打印完成："
        f"已提交 {summary['printed']}，无订单 {summary['no_orders']}，"
        f"失败 {summary['failed']}，跳过 {summary['skipped']}",
    )
    return summary


def print_orders(window_id, site):
    """保留旧调用入口，单窗口单站点执行一次打印。"""

    results = _run_shop_job(
        {"window_id": str(window_id), "shop_name": str(window_id), "sites": [site]},
        max_retries=1,
    )
    return bool(results and results[0]["status"] in ("printed", "no_orders"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mercado Libre 订单标签单次打印")
    parser.add_argument("--shop", action="append", default=[], help="只执行指定店铺，可重复")
    parser.add_argument("--site", action="append", default=[], help="只执行指定站点，可重复")
    parser.add_argument("--max-retries", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--retry-delay-seconds", type=int, default=300)
    args = parser.parse_args(argv)

    task_lock = acquire_order_print_lock(
        owner="bit_print.py",
        mode="once",
    )
    if task_lock is None:
        owner = get_order_print_lock_owner()
        raise RuntimeError(f"订单打印任务已在运行：{owner}")
    kwargs = {
        "selected_shops": args.shop or None,
        "selected_sites": args.site or None,
        "max_retries": args.max_retries,
        "retry_delay_seconds": max(0, args.retry_delay_seconds),
    }
    try:
        print_orders_all(**kwargs)
    finally:
        task_lock.release()


if __name__ == "__main__":
    main()
