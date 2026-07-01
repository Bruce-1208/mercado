from bit.bit_utils import getWindowidByName
from playwright.common import BitPlaywrightSession, select_country


CHAT_INFO_API_URL = "https://zeshun.nat100.top/api/v1/chat"


def shensu(name, site, form, message, mode="人工客服"):
    window_id = getWindowidByName(name)
    print(f"{name} {site} 开始进行{form}申诉，话术为{message}<br>")
    with BitPlaywrightSession(window_id) as session:
        page = session.page
        page.goto("https://global-selling.mercadolibre.com/help/hub/30928?source", wait_until="domcontentloaded", timeout=60000)
        select_country(page, site)
        if mode == "AI客服":
            page.get_by_text("Contact us").first.click(timeout=15000)
        return True


def use_one_browser_run_task(info):
    name, site, form, message = info[:4]
    return shensu(name, site, form, message, "人工客服")


def use_all_browser_run_task(browser_list):
    return [use_one_browser_run_task(info) for info in browser_list]


def use_all_browser_run_task_with_thread_pool(browser_list, max_threads=10):
    return use_all_browser_run_task(browser_list)


def auto_appeal_delay():
    raise NotImplementedError("请改用 playwright.bit_appeal.shensu 或 playwright.bit_appeal_ai 的 AI 入口。")


def main():
    raise NotImplementedError("请从业务入口传入店铺、站点、表单和话术。")
