from bit_playwright.common import BitPlaywrightSession


def connect_browser(window_id):
    session = BitPlaywrightSession(window_id)
    session.__enter__()
    return session
