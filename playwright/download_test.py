from playwright.common import BitPlaywrightSession


def open_report(window_id, url):
    with BitPlaywrightSession(window_id) as session:
        page = session.page
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(10000)
        return page.title()


if __name__ == "__main__":
    open_report(
        "1495e31cb630406bb690ba187f264fe7",
        "https://global-selling.mercadolibre.com/reputation?reportType=handling_time",
    )
