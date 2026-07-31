from bit_playwright.bit_summary_info import get_reputation_info_all
from bit_playwright.bit_visit_info import get_visits_info
from bit_playwright.common import (
    BitPlaywrightSession,
    open_mercado_backend_page,
    select_country,
)


def get_reputation_info(window_id, name="", site="", driver=None):
    from bit_playwright.bit_summary_info import get_reputation_info as get_summary_reputation

    data = get_summary_reputation(window_id, site)
    if isinstance(data, list) and name and site:
        return [name, site] + data
    return data


def get_recent_visits_info(driver_or_page, window_id, name="", site="", days=8):
    return get_visits_info(window_id, site)


def get_reputation_page(window_id, site=""):
    session = BitPlaywrightSession(window_id)
    session.__enter__()
    access = open_mercado_backend_page(
        session,
        "https://global-selling.mercadolibre.com/reputation",
    )
    if not access.get("ok"):
        session.__exit__(None, None, None)
        raise RuntimeError(access.get("message") or access.get("status"))
    if site:
        select_country(session.page, site)
    return session
