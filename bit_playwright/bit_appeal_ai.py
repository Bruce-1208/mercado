import time

from bit.bit_utils import getWindowidByName
from bit_playwright.bit_appeal import shensu as _shensu
from bit_playwright.common import (
    BitPlaywrightSession,
    open_mercado_backend_page,
    select_country,
)


SITE_OPTION_REPLY = {
    "MX": "Mexico (Direct to consumer)",
    "MLM": "Mexico (Direct to consumer)",
    "BR": "Brazil",
    "MLB": "Brazil",
    "CL": "Chile",
    "MLC": "Chile",
    "CO": "Colombia",
    "MCO": "Colombia",
    "AR": "Argentina",
    "MLA": "Argentina",
    "UY": "Uruguay",
    "MLU": "Uruguay",
}


def normalize_site_code(site):
    value = str(site or "").strip().upper()
    aliases = {
        "墨西哥": "MX",
        "MEXICO": "MX",
        "巴西": "BR",
        "BRAZIL": "BR",
        "智利": "CL",
        "CHILE": "CL",
        "哥伦比亚": "CO",
        "COLOMBIA": "CO",
        "阿根廷": "AR",
        "ARGENTINA": "AR",
        "乌拉圭": "UY",
        "URUGUAY": "UY",
    }
    return aliases.get(value, value)


def build_site_option_reply(site):
    return SITE_OPTION_REPLY.get(normalize_site_code(site), str(site or "").strip())


def contains_site_option_menu(text):
    value = str(text or "")
    options = [
        "Mexico (Direct to consumer)",
        "Mexico (Fulfillment)",
        "Brazil",
        "Chile",
        "Colombia",
        "Argentina",
        "Uruguay",
    ]
    return sum(1 for item in options if item in value) >= 3


def is_site_option_question(response_text):
    value = str(response_text or "")
    return contains_site_option_menu(value) and ("which" in value.lower() or "哪个" in value or "选项" in value)


def should_intervene_ai_response(response_text):
    return is_site_option_question(response_text)


def build_infraction_followup_message(infraction_ids, site):
    ids = ", ".join(str(item) for item in infraction_ids)
    return f"{build_site_option_reply(site)}\nPlease check these infraction IDs: {ids}"


def connect_bit_browser(window_id):
    session = BitPlaywrightSession(window_id)
    session.__enter__()
    return session


def select_site(page, name, site):
    return select_country(page, site)


def open_ai_contact_window(session, name, site):
    page = session.page
    access = open_mercado_backend_page(
        session,
        "https://global-selling.mercadolibre.com/help/hub/30928?source",
    )
    if not access.get("ok"):
        raise RuntimeError(access.get("message") or access.get("status"))
    select_country(page, site)
    page.get_by_text("Contact us").first.click(timeout=15000)
    return page


def shensu(name, site, form, message):
    return _shensu(name, site, form, message, mode="AI客服")


def appeal_ai_recollect_once(name, site="MX"):
    window_id = getWindowidByName(name)
    with BitPlaywrightSession(window_id) as session:
        open_ai_contact_window(session, name, site)
        return True


def appeal_ai_recollect_loop(name, site="MX", interval=1800):
    while True:
        appeal_ai_recollect_once(name, site)
        time.sleep(interval)
