import time

from bit.bit_api import closeBrowser
from bit.bit_config import list_config_rows, split_config_sites
from bit.bit_mysql import insert_task_record
from bit.bit_utils import get_now_time
from bit_playwright.common import BitPlaywrightSession, select_country


ORDERS_URL = (
    "https://global-selling.mercadolibre.com/orders/omni/list?"
    "filters=&subFilters=&search=&limit=50&offset=0&"
    "startPeriod=WITH_DATE_CLOSED_2M_OLD&selectedTab=TAB_TODAY_CBT"
)


def print_orders(window_id, site):
    with BitPlaywrightSession(window_id) as session:
        page = session.page
        page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=60000)
        page.reload(wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        select_country(page, site)
        page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=60000)
        page.reload(wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        try:
            page.locator(
                "xpath=/html/body/main/div/div[3]/div/div/div[3]/div/div[2]/div/div/"
                "section/div/div[1]/div/div/div[1]/div[1]/div/div/span/input"
            ).click(timeout=10000)
        except Exception as exc:
            print("无法勾选打印订单", exc)

        try:
            page.locator(
                "xpath=/html/body/main/div/div[3]/div/div/div[3]/div/div[2]/div/div/"
                "section/div/div[1]/div/div/div[2]/div/button"
            ).click(timeout=10000)
        except Exception as exc:
            print("没有可以打印的订单", exc)
        return True


def print_orders_all():
    start = int(time.time())
    result = []
    for row in list_config_rows(include_ignored=False):
        browser_id, name, remark, sites = row[:4]
        if not browser_id or not sites:
            continue
        for site in split_config_sites(sites):
            site = site.strip()
            if not site:
                continue
            try:
                if print_orders(browser_id, site):
                    result.append(("后台打印订单", name, site, "成功", get_now_time()))
            except Exception as exc:
                print(f"窗口{name}{site}执行失败", exc)
                result.append(("后台打印订单", name, site, "失败", get_now_time()))
            time.sleep(5)
        try:
            closeBrowser(str(browser_id))
        except Exception:
            pass
    print("总花费", int(time.time()) - start)
    insert_task_record(result)


if __name__ == "__main__":
    print_orders_all()
