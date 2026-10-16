import json

import pytest
from jinja2 import Environment, FileSystemLoader

from bit.mercado_collection_detail import build_collection_detail
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401
from test_mercado_collection_detail import detail_row  # noqa: F401


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_list_opens_right_drawer_with_all_images_and_descriptions(console_page, detail_row, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    html = Environment(loader=FileSystemLoader(ROOT / "bit/templates")).get_template(
        "mercado_collection_detail.html").render(
            detail=build_collection_detail(detail_row), error="", retry=False,
            url_for=lambda endpoint, filename: "/static/" + filename,
        )
    page.route("**/mercado-collection/items/91", lambda route: route.fulfill(body=html, content_type="text/html"))
    page.route("**/api/mercado-collection/items?*", lambda route: route.fulfill(
        body=json.dumps({"status": "success", "data": {"rows": [detail_row], "total": 1}}), content_type="application/json"))
    page.route("**/api/mercado-collection/attribute-check?*", lambda route: route.fulfill(
        body=json.dumps({"status": "success", "data": {"results": [{"id": 91, "status": "missing",
            "message": "缺少 1 项必填属性", "category_id": "MLM123", "missing": [
                {"scope": "规格 1", "name": "型号", "attribute_id": "MODEL"}
            ]}]}}), content_type="application/json"))
    # Offline product drawings make the gallery visually reviewable without external requests.
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600" viewBox="0 0 600 600">
      <rect width="600" height="600" fill="#fafbfd"/><ellipse cx="300" cy="475" rx="155" ry="22" fill="#e2e8f0"/>
      <rect x="160" y="135" width="280" height="325" rx="56" fill="#406a9f"/>
      <rect x="175" y="150" width="250" height="292" rx="45" fill="#22456c"/>
      <circle cx="300" cy="294" r="90" fill="#152f4d"/><circle cx="300" cy="294" r="67" fill="#315779"/>
      <circle cx="275" cy="182" r="5" fill="#82ceff"/><circle cx="300" cy="182" r="5" fill="#82ceff"/>
      <circle cx="325" cy="182" r="5" fill="#82ceff"/><text x="300" y="414" fill="#a9c2da" font-size="17" font-family="sans-serif" text-anchor="middle">PORTABLE SPEAKER</text></svg>'''
    page.route("https://images.test/**", lambda route: route.fulfill(body=svg, content_type="image/svg+xml"))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.evaluate("switchTab('mercado-collection')")
    page.wait_for_selector(".market-collection-detail-link")
    assert page.locator(".market-collection-details").count() == 0
    page.locator(".market-collection-detail-link").click()
    page.wait_for_selector("#product-detail-drawer .detail-shell")
    assert page.url == "http://console.test/"
    drawer = page.locator("#product-detail-drawer")
    page.wait_for_function("document.getElementById('product-detail-drawer').getAnimations().every(animation => animation.playState === 'finished')")
    bounds = drawer.bounding_box()
    assert abs(bounds["x"] + bounds["width"] - width) < 1
    assert abs(bounds["height"] - 1000) < 1
    assert page.locator("#product-detail-drawer-title").inner_text() == detail_row["title"]
    assert page.locator(".detail-thumbnail").count() == 16
    assert "材质" in page.locator("#attributes").inner_text()
    page.wait_for_selector('#product-detail-drawer [data-collection-attribute-check][data-status="missing"]')
    assert "MODEL" in drawer.locator('[data-collection-attribute-check]').inner_text()
    assert "完整描述最后一行" in page.locator("#description").inner_text()
    assert "原始采集描述，完整保留" in page.locator("#description").inner_text()
    assert "SPEAKER-BLUE" in page.locator("#variants").inner_text()
    assert drawer.locator("details").count() == 0
    page.locator('.detail-thumbnail').nth(13).click()
    assert page.locator("#collection-main-image").get_attribute("src").endswith("product-14.jpg")
    assert page.locator("#collection-main-image-link").get_attribute("href").endswith("product-14.jpg")
    assert page.locator('.detail-thumbnail[aria-pressed="true"]').count() == 1
    assert "第 14 / 16 张" in page.locator("#collection-image-position").inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#variants").scroll_into_view_if_needed()
    page.wait_for_function("[...document.querySelectorAll('.detail-variant-images img')].every(img => img.complete && img.naturalWidth > 0)")
    page.evaluate("document.querySelector('.product-drawer-body').scrollTop = 0")
    artifact = ROOT / "artifacts/mercado-collection-detail-20261008"
    artifact.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact / f"detail-{width}.png"))
    assert not errors
    page.locator(".product-drawer-close").click()
    assert not drawer.is_visible()
    assert page.locator(".market-collection-detail-link").evaluate("el => el === document.activeElement")
    assert page.url.startswith("http://console.test/") and "/mercado-collection/items/" not in page.url
    page.wait_for_function("document.getElementById('tab-mercado-collection').classList.contains('active')")


def test_direct_secondary_page_empty_states_and_return_link(console_page):
    page = console_page
    html = Environment(loader=FileSystemLoader(ROOT / "bit/templates")).get_template(
        "mercado_collection_detail.html").render(detail=build_collection_detail({"id": 7, "title": "资料尚未齐全"}),
        error="", retry=False, url_for=lambda endpoint, filename: "/static/" + filename)
    page.route("**/mercado-collection/items/7", lambda route: route.fulfill(body=html, content_type="text/html"))
    page.goto("http://console.test/mercado-collection/items/7")
    assert "未采集到商品图片" in page.locator("#pictures").inner_text()
    assert "未采集到商品属性" in page.locator("#attributes").inner_text()
    assert "未采集到商品描述" in page.locator("#description").inner_text()
    assert "未采集到规格变体" in page.locator("#variants").inner_text()
    assert page.locator("#collection-detail-back").get_attribute("href") == "/?tab=mercado-collection"
    page.locator("#collection-detail-back").click()
    page.wait_for_url("**/?tab=mercado-collection")
    page.wait_for_function("document.getElementById('tab-mercado-collection').classList.contains('active')")


def test_drawer_load_error_can_retry_without_losing_list(console_page, detail_row):
    page = console_page
    calls = []
    env = Environment(loader=FileSystemLoader(ROOT / "bit/templates"))

    def detail_response(route):
        calls.append(route.request.url)
        failed = len(calls) == 1
        html = env.get_template("mercado_collection_detail.html").render(
            detail=None if failed else build_collection_detail(detail_row),
            error="读取商品资料失败，请稍后重新加载" if failed else "", retry=failed,
            url_for=lambda endpoint, filename: "/static/" + filename)
        route.fulfill(status=503 if failed else 200, body=html, content_type="text/html")

    page.route("**/mercado-collection/items/91", detail_response)
    page.evaluate("openMercadoCollectionDetail(91)")
    page.wait_for_selector(".product-drawer-message[role=alert]")
    assert "读取商品资料失败" in page.locator(".product-drawer-message").inner_text()
    page.locator(".product-drawer-message button").click()
    page.wait_for_selector("#product-detail-drawer .detail-shell")
    assert len(calls) == 2
    assert page.locator("#product-detail-drawer-title").inner_text() == detail_row["title"]
    page.keyboard.press("Escape")
    assert not page.locator("#product-detail-drawer").is_visible()
    assert page.url == "http://console.test/"


def test_late_response_cannot_overwrite_another_product_drawer(console_page):
    page = console_page
    page.evaluate("""() => {
        window.savedDrawerFetch = window.fetch;
        window.drawerRequests = [];
        window.fetch = (url, options) => new Promise(resolve => drawerRequests.push({url, options, resolve}));
        openMercadoCollectionDetail(91);
        const content = document.createElement('p'); content.textContent = '另一件产品资料';
        openProductDetailDrawer({title: '另一件产品', subtitle: '产品列表', content});
        drawerRequests[0].resolve(new Response('<main class="detail-shell"><div class="detail-title"><h1>过期资料</h1></div></main>'));
    }""")
    page.wait_for_function("drawerRequests[0].options.signal.aborted")
    assert page.locator("#product-detail-drawer-title").inner_text() == "另一件产品"
    assert page.locator(".product-drawer-body").inner_text() == "另一件产品资料"
    page.evaluate("window.fetch = savedDrawerFetch")
