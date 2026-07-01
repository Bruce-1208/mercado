from playwright.common import BitPlaywrightSession


def open_page(window_id, url=None, close_on_exit=False):
    session = BitPlaywrightSession(window_id, close_on_exit=close_on_exit)
    session.__enter__()
    if url:
        session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
    return session


def goto(window_id, url):
    with BitPlaywrightSession(window_id) as session:
        session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        return session.page.title()
