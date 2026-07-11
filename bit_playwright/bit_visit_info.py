import time

from bit.bit_api import closeBrowser
from bit_playwright.common import BitPlaywrightSession, select_country


def get_visits_info(window_id, site):
    with BitPlaywrightSession(window_id) as session:
        page = session.page
        page.goto("https://global-selling.mercadolibre.com/metrics#sc-menu", wait_until="domcontentloaded", timeout=60000)
        page.reload(wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        select_country(page, site)
        print("成功选择站点")
        try:
            page.locator(
                "xpath=/html/body/main/div/div/div[3]/div/div/div[3]/section/div[2]/div[2]/div[2]/div[1]/div/div/div/div[4]"
            ).click(timeout=10000)
        except Exception as exc:
            print(exc)
    try:
        closeBrowser(window_id)
    except Exception:
        pass


if __name__ == "__main__":
    get_visits_info("", "")
