"""Product developer filters read saved products without a live ZYing login."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


def respond(route, data, *, status=200, message=""):
    route.fulfill(status=status, content_type="application/json", body=json.dumps({
        "status": "success" if status == 200 else "error", "data": data, "message": message,
    }))


def prepare(page):
    requests = []
    remote_requests = []

    def products(route):
        query = parse_qs(urlsplit(route.request.url).query)
        requests.append(query)
        developers = [("17", "开发甲"), ("18", "开发乙")]
        selected = query.get("product_developer_id", [""])[0]
        rows = [{"id": index + 1, "source_type": "zying", "title": name + "的产品",
                 "source_item_id": "ZY" + developer_id, "product_developer_id": developer_id,
                 "product_developer_name": name}
                for index, (developer_id, name) in enumerate(developers)
                if not selected or selected == developer_id]
        respond(route, {"rows": rows, "total": len(rows)})

    def unavailable_zying(route):
        remote_requests.append(route.request.url)
        respond(route, {}, status=400, message="智赢登录凭证为空，请重新登录")

    page.route("**/api/mercado-products?*", products)
    page.route("**/api/zying-collection/developers", unavailable_zying)
    page.evaluate("""() => {
        currentWorkbenchUser.permissions = ['*'];
        document.querySelector('.tab-button[data-tab="mercado-products"]').hidden = false;
    }""")
    return requests, remote_requests


@pytest.mark.parametrize("width", [1440, 390])
def test_saved_developers_are_visible_and_filter_products_without_zying_login(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    requests, remote_requests = prepare(page)
    page.route("**/api/mercado-products/developers", lambda route: respond(route, {
        "rows": [{"id": "17", "name": "开发甲"}, {"id": "18", "name": "开发乙"}],
    }))
    page.evaluate("switchTab('mercado-products')")
    page.wait_for_function("mercadoProductDeveloperFilter.options.length === 3 && mercadoListRows.length === 2")
    page.locator("#mercado-product-filters .zs-advanced-filters > summary").click()
    selector = page.locator("#mercado-product-developer-filter")
    assert selector.is_visible()
    assert selector.locator("option").all_text_contents() == ["全部产品开发", "开发甲", "开发乙"]
    bounds = selector.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width

    selector.select_option("18")
    page.wait_for_function("mercadoListRows.length === 1 && mercadoListRows[0].product_developer_id === '18'")
    assert requests[-1]["product_developer_id"] == ["18"]
    assert "开发乙的产品" in page.locator("#mercado-list-body").inner_text()

    # Opening the separate ZYing collection directory cannot clear this selection.
    page.evaluate("renderZyingProductDevelopers([{id: '99', name: '智赢实时目录'}])")
    assert selector.input_value() == "18"
    assert selector.locator("option").count() == 3
    selector.select_option("")
    page.wait_for_function("mercadoListRows.length === 2")
    assert "product_developer_id" not in requests[-1]
    assert not remote_requests
    artifacts = ROOT / "artifacts/product-developer-filter-20261007"
    artifacts.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifacts / f"products-{width}.png"), full_page=True)


def test_directory_failure_can_retry_and_keeps_the_selected_filter(console_page):
    page = console_page
    prepare(page)
    reads = []

    def directory(route):
        reads.append(route.request.url)
        if len(reads) == 2:
            respond(route, {}, status=503, message="人员读取暂不可用")
        else:
            respond(route, {"rows": [{"id": "17", "name": "开发甲"}]})

    page.route("**/api/mercado-products/developers", directory)
    page.evaluate("switchTab('mercado-products')")
    page.wait_for_function("mercadoProductDeveloperFilter.options.length === 2")
    page.locator("#mercado-product-filters .zs-advanced-filters > summary").click()
    selector = page.locator("#mercado-product-developer-filter")
    selector.select_option("17")
    page.wait_for_function("mercadoListRows.length === 1")
    page.evaluate("searchMercadoList()")
    retry = page.locator("#mercado-product-developer-retry")
    retry.wait_for(state="visible")
    assert "人员读取暂不可用" in page.locator("#mercado-product-developer-status").inner_text()
    assert selector.input_value() == "17"
    retry.click()
    retry.wait_for(state="hidden")
    assert len(reads) == 3
    assert selector.input_value() == "17"


def test_empty_directory_is_refreshed_after_products_arrive(console_page):
    page = console_page
    prepare(page)
    reads = []

    def directory(route):
        reads.append(route.request.url)
        respond(route, {"rows": [] if len(reads) == 1 else [{"id": "17", "name": "开发甲"}]})

    page.route("**/api/mercado-products/developers", directory)
    page.evaluate("switchTab('mercado-products')")
    page.locator("#mercado-product-filters .zs-advanced-filters > summary").click()
    page.locator("#mercado-product-developer-status").filter(has_text="暂无已记录的产品开发").wait_for()
    page.evaluate("searchMercadoList()")
    page.wait_for_function("mercadoProductDeveloperFilter.options.length === 2")
    assert len(reads) == 2
    assert page.locator("#mercado-product-developer-status").is_hidden()
