"""USD labels against local Mercado DOM fixtures, no external requests."""
import pytest
from test_zeshun_data_card_browser import browser, mercado_page, listing_markup


@pytest.mark.parametrize("domain,expected", [
    ("com.mx", "12.76"), ("com.ar", "0.26"), ("cl", "0.28"),
    ("com.co", "0.06"), ("com.uy", "6.38"),
])
def test_site_currency_and_fractional_price(mercado_page, domain, expected):
    page = mercado_page(url=f"https://listado.mercadolibre.{domain}/test")
    badge = page.locator(".zeshun-usd-price")
    page.wait_for_function("document.querySelector('.zeshun-usd-price')?.textContent.includes('US$')")
    assert badge.inner_text() == f"≈ US$ {expected}"
    assert "读取时间" in badge.get_attribute("title")
    assert page.locator(".andes-money-amount__fraction").inner_text() == "255"
    assert page.evaluate("exchangeMessages.length") == 1


def test_brazil_dynamic_prices_and_already_usd(mercado_page):
    page = mercado_page(url="https://lista.mercadolivre.com.br/test")
    badge = page.locator(".zeshun-usd-price")
    page.wait_for_function("document.querySelector('.zeshun-usd-price')?.textContent === '≈ US$ 51.03'")
    page.locator(".andes-money-amount__fraction").evaluate("el => el.firstChild.nodeValue='1.234'")
    page.wait_for_function("document.querySelector('.zeshun-usd-price')?.textContent === '≈ US$ 246.83'")
    page.locator(".andes-money-amount").evaluate("el => el.insertAdjacentHTML('afterbegin', '<span class=andes-money-amount__currency-symbol>US$</span>')")
    page.wait_for_function("document.querySelector('.zeshun-usd-price')?.textContent === '≈ US$ 1,234.16'")
    assert badge.count() == 1
    assert page.evaluate("exchangeMessages.length") == 1


def test_detail_and_failed_rate_never_show_invented_amount(mercado_page):
    body = '<main class="ui-pdp-container"><h1 class="ui-pdp-title">Test</h1><div class="ui-pdp-price__second-line"><span class="andes-money-amount"><span class="andes-money-amount__fraction">100</span></span></div></main>'
    page = mercado_page(body=body, content=False)
    page.evaluate("window.exchangeResponse={ok:false,error:'Unavailable'}")
    from test_zeshun_data_card_browser import EXTENSION
    page.add_script_tag(path=str(EXTENSION / "content.js"))
    page.wait_for_function("document.querySelector('.zeshun-usd-price')?.textContent === '美元汇率暂不可用'")
    assert page.locator(".andes-money-amount__fraction").inner_text() == "100"
    assert page.evaluate("exchangeMessages.length") == 1


def test_detail_price_renders_and_skips_installments(mercado_page):
    page = mercado_page(body='<main class="ui-pdp-container"><h1 class="ui-pdp-title">测试商品</h1><div class="ui-pdp-price__second-line"><s class="andes-money-amount"><span class="andes-money-amount__fraction">300</span></s><span class="andes-money-amount"><span class="andes-money-amount__fraction">255</span><span class="andes-money-amount__cents">16</span></span><div class="ui-pdp-installments"><span class="andes-money-amount"><span class="andes-money-amount__fraction">25</span></span></div></div></main>')
    page.wait_for_function("document.querySelector('.zeshun-usd-price')?.textContent === '≈ US$ 12.76'")
    assert page.locator('.zeshun-usd-price').count() == 1
    from pathlib import Path
    output = Path(__file__).resolve().parents[1] / 'artifacts/zeshun-usd-prices-1.8.76'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / 'detail.png'))
