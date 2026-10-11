import json

import pytest

from erp.mercadolibre_collection_store import _json_safe_row
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_collection_hover_preview_and_visible_missing_fields(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    row = _json_safe_row({"id": 91, "task_id": 1, "title": "待补资料商品", "source_item_id": "MLM91",
                          "main_image_url": "https://images.test/product.svg", "weight_g": 0,
                          "package_length_cm": 10, "package_height_cm": 6})
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600"><rect width="600" height="600" fill="#dbeafe"/><circle cx="300" cy="300" r="180" fill="#2563eb"/></svg>'
    page.route("https://images.test/**", lambda route: route.fulfill(body=svg, content_type="image/svg+xml"))
    page.route("**/api/mercado-collection/items?*", lambda route: route.fulfill(
        body=json.dumps({"status": "success", "data": {"rows": [row], "total": 1, "salespeople": ["业务甲"]}}),
        content_type="application/json"))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.evaluate("switchTab('mercado-collection')")
    page.wait_for_selector(".market-collection-row")
    assert "缺少有效实际重量" in page.locator(".market-collection-quality").inner_text()
    assert "缺少宽" in page.locator(".market-collection-quality").inner_text()
    assert "净收益未计算" in page.locator(".market-collection-quality").inner_text()
    assert "未采集到商品属性" in page.locator(".market-collection-quality").inner_text()
    image = page.locator(".market-collection-image-link img")
    image.scroll_into_view_if_needed()
    page.wait_for_function("document.querySelector('.market-collection-image-link img').naturalWidth > 0")
    image.hover()
    preview = page.locator(".collection-image-preview.visible")
    preview.wait_for(state="visible")
    assert preview.is_visible()
    assert preview.locator("img").get_attribute("src") == row["main_image_url"]
    bounds = preview.bounding_box()
    assert bounds["width"] > 250
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 1000
    artifact = ROOT / "artifacts/mercado-collection-quality-20261010"
    artifact.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact / f"hover-{width}.png"))
    page.mouse.move(0, 0)
    assert not page.locator(".collection-image-preview").is_visible()
    page.screenshot(path=str(artifact / f"missing-fields-{width}.png"))
    page.locator(".market-collection-image-link").focus()
    assert preview.is_visible()
    page.keyboard.press("Escape")
    assert not page.locator(".collection-image-preview").is_visible()
    assert not errors


def test_stale_salesperson_option_is_removed_and_query_resets(console_page):
    page = console_page
    requests = []

    def respond(route):
        requests.append(route.request.url)
        route.fulfill(body=json.dumps({"status": "success", "data": {
            "rows": [], "total": 0, "salespeople": ["业务甲", "业务乙"],
        }}), content_type="application/json")

    page.route("**/api/mercado-collection/items?*", respond)
    page.evaluate("""() => {
        const select = document.getElementById('mercado-salesperson-filter');
        select.add(new Option('script-test', 'script-test', true, true));
        switchTab('mercado-collection');
    }""")
    page.wait_for_function("mercadoCollectionLoaded")
    assert page.locator("#mercado-salesperson-filter option").all_inner_texts() == ["全部业务员", "业务甲", "业务乙"]
    assert page.locator("#mercado-salesperson-filter").input_value() == ""
    assert "salesperson=script-test" in requests[0]
    assert "salesperson=" not in requests[-1]
