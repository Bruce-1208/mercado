"""Price preference through real detail extraction with mocked data service."""
import pytest
from test_zeshun_data_card_browser import browser, mercado_page


@pytest.mark.parametrize("message_type", ["EXTRACT_PRODUCT", "EXTRACT_BATCH_PRODUCT"])
@pytest.mark.parametrize("saved,explicit,previous,expected", [
    (None, None, True, 1200.50),
    ("discount", None, True, 899.99),
    ("discount", "original", True, 1200.50),
    ("original", "discount", True, 899.99),
    (None, None, False, 899.99),
    (None, "discount", False, 899.99),
    ("invalid", None, True, 1200.50),
])
def test_price_selection(mercado_page, message_type, saved, explicit, previous, expected):
    original = ('<s class="andes-money-amount andes-money-amount--previous">'
                '<span class="andes-money-amount__fraction">1,200</span>'
                '<span class="andes-money-amount__cents">50</span></s>') if previous else ""
    page = mercado_page(
        '<main class="ui-pdp-container"><h1 class="ui-pdp-title">Price fixture</h1>'
        '<meta property="og:image" content="https://example.test/product.jpg">'
        '<meta itemprop="price" content="888">'
        '<div class="ui-pdp-price"><div class="ui-pdp-price__second-line">' + original +
        '<span class="andes-money-amount"><span class="andes-money-amount__fraction">899</span>'
        '<span class="andes-money-amount__cents">99</span></span></div></div></main>',
        url='https://articulo.mercadolibre.com.mx/MLM-385775246-test-_JM')
    page.evaluate("""saved => {chrome.storage = {local: {get: async () =>
      ({productBatchOptions: {'price-mode': saved}})}};}""", saved)
    message = {"type": message_type}
    if explicit is not None:
        message["price_mode"] = explicit
    result = page.evaluate("""message => new Promise(resolve =>
      extensionListener(message, {}, resolve))""", message)
    assert result['ok'], result
    assert result['product']['price'] == expected
    assert result['product']['source']['price'] == expected

