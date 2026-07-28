from bit_playwright.common import BitPlaywrightSession, open_mercado_backend_page


def _is_mercado_url(url):
    return "mercadolibre.com" in str(url or "") or "mercadopago.com" in str(url or "")


def open_page(window_id, url=None, close_on_exit=False):
    session = BitPlaywrightSession(window_id, close_on_exit=close_on_exit)
    session.__enter__()
    if url:
        if _is_mercado_url(url):
            result = open_mercado_backend_page(session, url)
            if not result.get("ok"):
                session.__exit__(None, None, None)
                raise RuntimeError(result.get("message") or result.get("status"))
        else:
            session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
    return session


def goto(window_id, url):
    with BitPlaywrightSession(window_id) as session:
        if _is_mercado_url(url):
            result = open_mercado_backend_page(session, url)
            if not result.get("ok"):
                raise RuntimeError(result.get("message") or result.get("status"))
        else:
            session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        return session.page.title()
