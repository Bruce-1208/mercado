"""Read only the Pago home balance card, never activity amounts."""
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def _usd_number(value):
    text = re.sub(r"\s+", "", str(value or ""))
    if not re.fullmatch(r"-?\d[\d,.]*", text):
        return ""
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".") if len(text.rsplit(",", 1)[1]) == 2 else text.replace(",", "")
    try:
        amount = Decimal(text)
        return f"{amount:.2f}" if amount.is_finite() else ""
    except InvalidOperation:
        return ""


def parse_pago_home(html):
    soup = BeautifulSoup(html or "", "html.parser")
    card = next((node for node in soup.select('a[href]')
                 if urlparse(node.get('href', '')).path == '/banking/balance'
                 and re.search(r'Money to be transferred|to be released', node.get_text(' ', strip=True), re.I)), None)
    if card is None:
        return {"released_usd": "", "unreleased_usd": "", "raw_text": "未找到 Pago 首页余额卡片，请检查页面是否加载完整", "candidate_count": 0}
    raw = card.get_text(' ', strip=True)
    if re.search(r'Show balance', str(card), re.I):
        return {"released_usd": "", "unreleased_usd": "", "raw_text": "余额已隐藏，请在 Pago 页面显示余额后重新采集", "candidate_count": 0}
    released = ""
    amount = card.select_one('.banking-balance__amount [data-andes-money-amount], .banking-balance__amount .andes-money-amount')
    if amount is not None:
        currency = amount.select_one('[data-andes-money-amount-currency], .andes-money-amount__currency')
        fraction = amount.select_one('[data-andes-money-amount-fraction], .andes-money-amount__fraction')
        cents = amount.select_one('[data-andes-money-amount-cents], .andes-money-amount__cents')
        if currency and re.search(r'US\$|USD', currency.get_text()) and fraction:
            whole = re.sub(r'[,\.\s]', '', fraction.get_text())
            decimal = cents.get_text(strip=True) if cents else '00'
            if re.fullmatch(r'-?\d+', whole) and re.fullmatch(r'\d{2}', decimal):
                released = _usd_number(whole + '.' + decimal)
    pending_match = re.search(r'(?:US\$|USD)\s*(-?\d[\d,.]*(?:\s+[\d,.]+)?)\s+to be released', raw, re.I)
    pending = _usd_number(pending_match.group(1)) if pending_match else ''
    return {"released_usd": released, "unreleased_usd": pending,
            "raw_text": raw[:1000], "candidate_count": int(bool(released)) + int(bool(pending))}
