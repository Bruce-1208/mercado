"""Verify Mercado card data in Chromium with only local, mocked responses."""

import json
import os
from pathlib import Path

import pytest


EXTENSION = Path(__file__).resolve().parents[1] / "browser_extension/zeshun_collector"
LIST_URL = "https://listado.mercadolibre.com.mx/test"
SKU = "MLM385775246"
PRODUCT = {
    "sku": SKU,
    "seller": "TREASURE PRODUCTS",
    "seller_id": 12345678,
    "seller_country": "MX",
    "seller_status": "领导铂金",
    "seller_level": 5,
    "fulfillment_type": "self_ship",
    "fulfillment_label": "本土仓",
    "fulfillment_eligible": True,
    "sales": 0,
    "commission_percent": 20,
    "estimate_commission_percent": 15,
    "brand": "KCHOOH",
    "weight_g": 210,
    "size_cm": [30, 24, 4],
    "volumetric_weight_g": 480,
    "weight_freight_usd": 3.15,
    "size_freight_usd": 5.13,
    "effective_freight_source": "weight",
    "low_freight": True,
    "listed_at": "2026-05-22T00:00:00Z",
    "net_proceeds_usd": 8.89,
    "net_proceeds_cny": 59.70,
}


def listing_markup(layout="grid", sku=SKU, card_id="product"):
    title = (
        f'<a class="poly-component__title ui-search-link" '
        f'href="https://articulo.mercadolibre.com.mx/{sku[:3]}-{sku[3:]}-test-_JM">'
        "Anime Kpop Demon Hunter Cosplay Rumi Zoey Mira Cosplay</a>"
    )
    price = (
        '<div class="poly-price__current ui-search-price__second-line">'
        '<span class="andes-money-amount"><span class="andes-money-amount__fraction">255</span>'
        '<span class="andes-money-amount__cents">16</span></span></div>'
    )
    image = '<div class="native-image" aria-label="Product image"></div>'
    if layout == "classic":
        card = (
            f'<div class="ui-search-result" id="{card_id}">{image}'
            f'<div class="ui-search-result__content">{title}{price}</div></div>'
        )
    elif layout == "horizontal":
        card = (
            f'<div class="poly-card poly-card--list" id="{card_id}">{image}'
            f'<div class="poly-card__content">{title}{price}</div></div>'
        )
    else:
        card = (
            f'<div class="poly-card" id="{card_id}">{image}'
            f'<div class="poly-card__content">{title}{price}</div></div>'
        )
    return f'<li class="ui-search-layout__item">{card}</li>'


@pytest.fixture(scope="module")
def browser():
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as pw:
        candidates = [
            os.environ.get("CONSOLE_TEST_BROWSER", ""),
            pw.chromium.executable_path,
            "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        ]
        executable = next((p for p in candidates if p and Path(p).is_file()), None)
        if not executable:
            pytest.skip("Chromium is required")
        instance = pw.chromium.launch(executable_path=executable, headless=True)
        yield instance
        instance.close()


@pytest.fixture
def mercado_page(browser):
    contexts = []

    def create(body=None, *, width=284, url=LIST_URL, products=None, mode="success", content=True):
        context = browser.new_context(viewport={"width": 1000, "height": 1000})
        contexts.append(context)
        page = context.new_page()
        markup = body if body is not None else f'<ol id="results">{listing_markup()}</ol>'
        html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Local Mercado fixture</title>
          <style>
            body {{ margin: 16px; font: 14px Arial, sans-serif; }}
            ol {{ padding: 0; list-style: none; }}
            li {{ margin-bottom: 12px; }}
            .poly-card, .ui-search-result {{ width: {width}px; background: #fff;
                border-radius: 6px; box-shadow: 0 0 0 1px #ddd; }}
            .native-image {{ height: 90px; background: #eee; }}
            .poly-card__content, .ui-search-result__content {{ min-width: 0; }}
            .poly-component__title {{ display: block; padding: 8px; color: #333; }}
            .poly-price__current {{ padding: 0 8px 8px; }}
            .poly-card--list, .ui-search-result {{ display: flex; width: 680px; }}
            .poly-card--list .native-image, .ui-search-result .native-image {{ flex: 0 0 120px; }}
            .poly-card--list .poly-card__content, .ui-search-result__content {{ flex: 1; }}
            .ui-pdp-container__col {{ width: 400px; }}
          </style></head><body>{markup}</body></html>"""
        # Every request, including a shop tab opened by a click, is fulfilled locally.
        context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body=html))
        overrides = products or {}
        page.add_init_script(f"""
          window.mockProducts = {json.dumps(overrides, ensure_ascii=False)};
          window.defaultProduct = {json.dumps(PRODUCT, ensure_ascii=False)};
          window.mockMode = {json.dumps(mode)};
          window.messages = [];
          window.pendingResponses = [];
          window.chrome = {{runtime: {{
            onMessage: {{addListener: listener => {{ window.extensionListener = listener; }}}},
            sendMessage: (message, callback) => {{
              messages.push(structuredClone(message));
              if (message.type !== 'GET_MERCADO_PRODUCT_INFO') {{
                callback({{ok:false, error:'Unexpected fixture message'}}); return;
              }}
              const response = {{ok:true, warnings:[], items:message.items.map(item => ({{
                sku:item.sku, requested_price:item.price > 0 ? item.price : null,
                fetched_at:1790899200, cached:false,
                product:{{...structuredClone(defaultProduct), sku:item.sku,
                  ...structuredClone(mockProducts[item.sku] || {{}})}}
              }}))}};
              if (mockMode === 'hold') pendingResponses.push(callback);
              else setTimeout(() => callback(response), 0);
            }}
          }}}};
        """)
        page.goto(url)
        page.add_style_tag(path=str(EXTENSION / "content.css"))
        for filename in ["collector-core.js", "mercado-data.js"] + (["content.js"] if content else []):
            page.add_script_tag(path=str(EXTENSION / filename))
        return page

    yield create
    for context in contexts:
        context.close()


def wait_for_card(page, card_id="product"):
    page.wait_for_function(
        "id => document.querySelector(`#${id} .zs-info-id`) !== null", arg=card_id
    )
    return page.locator(f"#{card_id} .zeshun-product-info")


def row(panel, label):
    return panel.locator(".zs-info-row").filter(has=panel.page.locator(".zs-info-label", has_text=label))


@pytest.mark.parametrize("layout,host", [
    ("grid", "#product"),
    ("horizontal", "#product > .poly-card__content"),
    ("classic", "#product > .ui-search-result__content"),
])
def test_panel_belongs_to_native_card_and_survives_mutations(mercado_page, layout, host):
    page = mercado_page(f'<ol id="results">{listing_markup(layout)}</ol>')
    panel = wait_for_card(page)
    assert page.locator(f"{host} > .zeshun-product-info").count() == 1
    assert page.locator("li > .zeshun-product-info").count() == 0
    assert page.locator("#product > .zeshun-card-collect").count() == 1
    bounds = page.locator("#product").bounding_box()
    panel_bounds = panel.bounding_box()
    assert panel_bounds["x"] >= bounds["x"] - 1
    assert panel_bounds["x"] + panel_bounds["width"] <= bounds["x"] + bounds["width"] + 1
    assert panel_bounds["y"] + panel_bounds["height"] <= bounds["y"] + bounds["height"] + 1
    page.evaluate("""() => {
      document.querySelector('#product').dataset.marker = 'updated';
      document.querySelector('#product').append(document.createElement('span'));
    }""")
    page.wait_for_timeout(400)
    assert page.locator(".zeshun-product-info").count() == 1
    assert page.locator(".zeshun-card-collect").count() == 1
    assert page.evaluate("messages.filter(m => m.type === 'GET_MERCADO_PRODUCT_INFO').length") == 1


def test_added_cards_and_changed_prices_get_their_own_data(mercado_page):
    page = mercado_page(products={"MLM385775247": {"sales": 42, "commission_percent": 0}})
    wait_for_card(page)
    page.evaluate("markup => document.querySelector('#results').insertAdjacentHTML('beforeend', markup)",
                  listing_markup(sku="MLM385775247", card_id="second-product"))
    second = wait_for_card(page, "second-product")
    assert row(second, "销量：").locator(".zs-info-value").inner_text() == "42"
    assert row(second, "佣金：").locator(".zs-info-value").inner_text() == "0%"
    page.evaluate("""() => {
      mockProducts.MLM385775246 = {sales:17, commission_percent:12};
      document.querySelector('#product .andes-money-amount__fraction').textContent = '266';
    }""")
    page.wait_for_function("document.querySelector('#product .zeshun-product-info').dataset.productKey === 'MLM385775246:266.16'")
    first = wait_for_card(page)
    page.wait_for_function("document.querySelector('#product .zs-info-stats').textContent.includes('17')")
    assert row(first, "佣金：").locator(".zs-info-value").inner_text() == "12%"
    assert page.locator(".zeshun-product-info").count() == 2
    assert page.evaluate("messages.some(m => m.items?.some(i => i.sku === 'MLM385775246' && i.price === 266.16))")


def test_reused_card_collects_the_current_listing(mercado_page):
    page = mercado_page(products={"MLM385775247": {"seller": "NEW SELLER"}})
    wait_for_card(page)
    next_url = "https://articulo.mercadolibre.com.mx/MLM-385775247-new-product-_JM"
    page.evaluate("""url => {
      window.open = (url, target, features) => {
        window.openedListing = {url, target, features}; return {};
      };
      document.querySelector('#product a.poly-component__title').href = url;
      document.querySelector('#product a.poly-component__title').textContent = 'New product';
    }""", next_url)
    page.wait_for_function("document.querySelector('#product .zs-info-id')?.textContent === 'MLM385775247'")
    assert page.locator(".zeshun-product-info").count() == 1
    assert page.locator(".zeshun-card-collect").count() == 1
    assert page.locator(".zs-info-shop").inner_text() == "NEW SELLER"
    page.locator("#product > .zeshun-card-collect").click()
    assert page.evaluate("openedListing") == {
        "url": next_url, "target": "_blank", "features": "noopener"
    }
    assert page.evaluate("messages.every(m => m.type === 'GET_MERCADO_PRODUCT_INFO')")


def test_zero_sales_actual_commission_and_refresh(mercado_page):
    page = mercado_page()
    panel = wait_for_card(page)
    assert row(panel, "销量：").locator(".zs-info-value").inner_text() == "0"
    assert row(panel, "佣金：").locator(".zs-info-value").inner_text() == "20%"
    assert panel.locator(".zs-info-profit-rule").inner_text() == "按15%佣金估算"
    page.evaluate("""() => {
      window.nativeClicks = 0;
      document.querySelector('#product').addEventListener('click', () => nativeClicks++);
      mockProducts.MLM385775246 = {sales:99, commission_percent:0};
    }""")
    panel.locator(".zs-info-refresh").click()
    page.wait_for_function("document.querySelector('.zs-info-stats')?.textContent.includes('99')")
    assert row(panel, "佣金：").locator(".zs-info-value").inner_text() == "0%"
    assert page.evaluate("messages.length") == 2
    assert page.evaluate("nativeClicks") == 0
    assert panel.get_attribute("aria-busy") == "false"


@pytest.mark.parametrize("width", [284, 220])
def test_long_seller_and_data_fit_narrow_product_cards(mercado_page, width):
    page = mercado_page(width=width, products={SKU: {
        "seller": "TREASURE_" + "VERY_LONG_SELLER_NAME_" * 12,
        "brand": "A brand with a longer descriptive name",
    }})
    panel = wait_for_card(page)
    metrics = panel.evaluate("""panel => {
      const bounds = panel.getBoundingClientRect();
      return {clientWidth:panel.clientWidth, scrollWidth:panel.scrollWidth,
        outside:[...panel.querySelectorAll('*')].filter(node => {
          const r = node.getBoundingClientRect();
          return r.width > 0 && (r.left < bounds.left - 1 || r.right > bounds.right + 1);
        }).map(node => node.className)};
    }""")
    assert metrics["scrollWidth"] <= metrics["clientWidth"] + 1
    assert metrics["outside"] == []
    assert panel.bounding_box()["width"] <= width
    assert panel.locator(".zs-info-shop").get_attribute("title").startswith("查看 TREASURE_")


def test_seller_link_is_safe_and_does_not_trigger_native_card(mercado_page):
    seller = '<img src=x onerror="window.injected=true"> Store'
    page = mercado_page(products={SKU: {"seller": seller}})
    panel = wait_for_card(page)
    link = panel.locator("a.zs-info-shop")
    assert link.inner_text() == seller
    assert panel.locator("img").count() == 0
    assert link.get_attribute("href") == "https://listado.mercadolibre.com.mx/_CustId_12345678"
    assert link.get_attribute("target") == "_blank"
    assert {"noopener", "noreferrer"}.issubset(set(link.get_attribute("rel").split()))
    page.evaluate("""() => {
      window.nativeClicks = 0;
      document.querySelector('#product').addEventListener('click', () => nativeClicks++);
    }""")
    with page.context.expect_page() as opened:
        link.click()
    shop = opened.value
    shop.wait_for_load_state()
    assert shop.url == "https://listado.mercadolibre.com.mx/_CustId_12345678"
    assert shop.evaluate("window.opener === null")
    assert page.evaluate("nativeClicks") == 0
    assert page.evaluate("typeof window.injected") == "undefined"
    assert page.evaluate("messages.every(m => m.type === 'GET_MERCADO_PRODUCT_INFO')")


@pytest.mark.parametrize("freight_source,label,amount", [
    ("weight", "实际重量", "$3.15"),
    ("size", "体积重量", "$5.13"),
])
def test_freight_selection_matches_the_data(mercado_page, freight_source, label, amount):
    page = mercado_page(products={SKU: {"effective_freight_source": freight_source}})
    panel = wait_for_card(page)
    selected = panel.locator(".zs-info-freight.is-selected")
    assert selected.count() == 1
    assert selected.locator(".zs-info-freight-amount").inner_text() == amount
    assert label in selected.get_attribute("aria-label")
    assert "净收益采用此项" in selected.get_attribute("aria-label")
    assert panel.locator(".zs-info-low").count() == 2


def test_missing_data_has_placeholders_and_no_unsafe_seller_link(mercado_page):
    missing = {key: None for key in PRODUCT if key != "sku"}
    missing.update(seller_id="123/javascript:alert(1)", seller_level=None)
    page = mercado_page(products={SKU: missing})
    panel = wait_for_card(page)
    assert panel.locator("a.zs-info-shop").count() == 0
    assert panel.locator(".zs-info-shop").inner_text() == "—"
    assert panel.locator(".zs-info-tags").count() == 0
    assert panel.locator(".zs-info-country").count() == 0
    assert panel.locator(".zs-info-fulfillment").inner_text() == "未识别"
    for label in ["销量：", "佣金：", "品牌：", "重量：", "尺寸：", "上架："]:
        assert row(panel, label).locator(".zs-info-value").inner_text() == "—"
    assert row(panel, "净收益：").locator(".zs-info-value").inner_text() == "— / —"
    assert panel.locator(".zs-info-freight-amount").all_text_contents() == ["—", "—"]
    assert panel.locator(".zs-info-freight.is-selected").count() == 0
    assert panel.locator(".zs-info-low").count() == 0
    assert all(value not in panel.inner_text() for value in ["undefined", "null", "NaN"])


def test_loading_failure_can_retry_without_collecting(mercado_page):
    page = mercado_page(mode="hold")
    page.wait_for_function("pendingResponses.length === 1")
    panel = page.locator(".zeshun-product-info")
    assert panel.get_attribute("aria-busy") == "true"
    assert "正在读取商品数据" in panel.locator("[role=status]").inner_text()
    page.evaluate("""() => {
      pendingResponses.shift()({ok:false, error:'测试服务暂不可用'});
      mockMode = 'success';
      window.nativeClicks = 0;
      document.querySelector('#product').addEventListener('click', () => nativeClicks++);
    }""")
    page.wait_for_function("document.querySelector('.zs-info-refresh')?.textContent === '重试'")
    assert "测试服务暂不可用" in panel.locator("[role=status]").inner_text()
    assert panel.get_attribute("aria-busy") == "false"
    panel.locator(".zs-info-refresh").click()
    wait_for_card(page)
    assert panel.locator(".zs-info-id").inner_text() == SKU
    assert panel.locator(".zs-info-state").count() == 0
    assert panel.get_attribute("aria-busy") == "false"
    assert page.evaluate("nativeClicks") == 0
    assert page.evaluate("messages.map(m => m.type)") == [
        "GET_MERCADO_PRODUCT_INFO", "GET_MERCADO_PRODUCT_INFO"
    ]


def test_detail_card_uses_native_detail_column(mercado_page):
    page = mercado_page(
        '<main class="ui-pdp-container"><div class="ui-pdp-container__col col-2" id="product">'
        '<h1 class="ui-pdp-title">Test detail product</h1>'
        '<div class="ui-pdp-price__second-line"><span class="andes-money-amount">'
        '<span class="andes-money-amount__fraction">255</span>'
        '<span class="andes-money-amount__cents">16</span></span></div></div></main>',
        url="https://articulo.mercadolibre.com.mx/MLM-385775246-test-_JM",
    )
    panel = wait_for_card(page)
    assert page.locator("#product > .zeshun-product-info--detail").count() == 1
    assert panel.locator(".zs-info-id").inner_text() == SKU
    assert page.locator("#zeshun-collector-floating").count() == 1
    assert page.locator(".zeshun-card-collect").count() == 0
    assert page.evaluate("messages[0].items") == [{"sku": SKU, "price": 255.16}]


@pytest.mark.parametrize('message_type', ['EXTRACT_PRODUCT', 'EXTRACT_BATCH_PRODUCT'])
@pytest.mark.parametrize('with_zying', [False, True])
def test_collection_reuses_zeshun_card_data(mercado_page, message_type, with_zying):
    page = mercado_page(
        '<main class="ui-pdp-container"><div class="ui-pdp-container__col col-2" id="product">'
        '<h1 class="ui-pdp-title">Source isolation</h1>'
        '<meta property="og:image" content="https://example.test/product.jpg">'
        '</div></main>',
        url='https://articulo.mercadolibre.com.mx/MLM-385775246-test-_JM',
    )
    wait_for_card(page)
    if with_zying:
        page.evaluate("""() => {
          const host=document.createElement('div');document.body.append(host);
          host.attachShadow({mode:'open'}).innerHTML=
            '<div id="zyCardWrap">重量 9999g 尺寸 99 x 99 x 99 cm 销量 999 请先登录智赢</div>';
        }""")
    before = page.evaluate('messages.length')
    result = page.evaluate("""type => new Promise(resolve =>
      extensionListener({type}, {}, resolve))""", message_type)
    assert result['ok'] is True
    product = result['product']
    assert product['weight_g'] == PRODUCT['weight_g']
    assert [product[k] for k in ('package_length_cm', 'package_width_cm', 'package_height_cm')] == PRODUCT['size_cm']
    assert product['net_proceeds_usd'] == PRODUCT['net_proceeds_usd']
    assert product['source']['sold_quantity'] == 0
    assert product['plugin_snapshot']['read_method'] == 'zeshun_server_api'
    assert page.evaluate('messages.length') == before


def test_missing_zeshun_dimensions_never_fall_back_to_zying(mercado_page):
    page = mercado_page(
        '<main class="ui-pdp-container"><div id="product">'
        '<h1 class="ui-pdp-title">Missing dimensions</h1>'
        '<meta property="og:image" content="https://example.test/product.jpg">'
        '<div id="zyCardWrap">重量 500g 尺寸 20 x 30 x 40 cm</div>'
        '</div></main>',
        url='https://articulo.mercadolibre.com.mx/MLM-385775246-test-_JM',
        products={SKU: {'size_cm': None}},
    )
    result = page.evaluate("""() => new Promise(resolve =>
      extensionListener({type:'EXTRACT_PRODUCT'}, {}, resolve))""")
    assert result['ok'] is False
    assert '包装尺寸' in result['error']


@pytest.mark.parametrize('offer_markup', [
    '<script type="application/ld+json">{"@type":"Product","sku":"MLM385775246"}</script>',
    '<div class="ui-pdp-buybox"><input name="item_id" value="MLM385775246"></div>',
    '<div class="ui-pdp-buybox"><a href="?wid=MLM385775246">Selected seller</a></div>',
    '<script type="application/json">{"initialState":{"item":{"id":"MLM385775246"},"recommendations":[{"item_id":"MLM999999999"}]}}</script>',
    '<script>window.__PRELOADED_STATE__ = {"initialState":{"track":{"melidata":{"event_data":{"item_id":"MLM385775246"}}}}};</script>',
])
def test_catalog_uses_seller_listing_for_data_query(mercado_page, offer_markup):
    page = mercado_page(
        '<main class="ui-pdp-container"><h1 class="ui-pdp-title">Catalog product</h1>'
        '<meta property="og:image" content="https://example.test/product.jpg">'
        '<div class="recommendations" data-item-id="MLM999999999">Other product</div>'
        + offer_markup + '</main>',
        url='https://www.mercadolibre.com.mx/test/p/MLM2062246084',
    )
    result = page.evaluate("""() => new Promise(resolve =>
      extensionListener({type:'EXTRACT_BATCH_PRODUCT'}, {}, resolve))""")
    assert result['ok'] is True
    assert result['product']['source_item_id'] == SKU
    queries = page.evaluate("messages.filter(m=>m.type==='GET_MERCADO_PRODUCT_INFO')")
    assert all(item['sku'] == SKU for query in queries for item in query['items'])


def test_catalog_without_selected_seller_never_queries_catalog_or_recommendations(mercado_page):
    page = mercado_page(
        '<main class="ui-pdp-container"><h1 class="ui-pdp-title">Catalog product</h1>'
        '<script type="application/ld+json">{"@type":"Product","sku":"MLM2062246084"}</script>'
        '<div data-item-id="MLM999999999">Recommendation</div></main>',
        url='https://www.mercadolibre.com.mx/test/p/MLM2062246084',
    )
    result = page.evaluate("""() => new Promise(resolve =>
      extensionListener({type:'EXTRACT_BATCH_PRODUCT'}, {}, resolve))""")
    assert result['ok'] is False
    assert '卖家刊登编号' in result['error']
    assert page.evaluate('messages') == []


def test_batch_data_error_is_terminal_and_preserves_reason(mercado_page):
    page = mercado_page(
        '<main><h1 class="ui-pdp-title">Test</h1>'
        '<meta property="og:image" content="https://example.test/product.jpg"></main>',
        url='https://articulo.mercadolibre.com.mx/MLM-385775246-test-_JM',
        content=False,
    )
    page.evaluate("() => { chrome.runtime.sendMessage=(_message,callback)=>callback({ok:false,error:'商品数据服务繁忙，请稍后重试'}); }")
    page.add_script_tag(path=str(EXTENSION / 'content.js'))
    result = page.evaluate("""() => new Promise(resolve =>
      extensionListener({type:'EXTRACT_BATCH_PRODUCT'}, {}, resolve))""")
    assert result == {'ok': False, 'retryable': False, 'error': '商品数据服务繁忙，请稍后重试'}
