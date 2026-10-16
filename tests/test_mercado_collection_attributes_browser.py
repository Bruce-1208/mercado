import json
from urllib.parse import parse_qs, urlsplit

import pytest

from erp.mercadolibre_collection_attributes import check_required_attributes
from erp.mercadolibre_collection_store import _json_safe_row
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


def samples():
    rows = [_json_safe_row({
        "id": index, "task_id": 1, "category_id": "MLM123", "source_item_id": f"MLM{index}",
        "title": title, "weight_g": 120, "weight_basis": "plugin_actual",
        "package_length_cm": 10, "package_width_cm": 8, "package_height_cm": 6,
        "sale_price_usd": 20, "shipping_fee_usd": 5,
        "main_image_url": "https://images.test/sample.svg",
        "source_json": {"condition": "new", "attributes": [{"id": "BRAND", "value_name": "Acme"}]},
    }) for index, title in enumerate(["缺少型号的商品", "属性齐全的商品", "暂未核验的商品"], 91)]
    return rows


@pytest.mark.parametrize("width", [1440, 1024, 390])
def test_automatic_check_displays_missing_fields_preserves_focus_and_selection(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    rows = samples()
    results = [check_required_attributes(rows[0], [{"id": "MODEL", "tags": {"required": True}}]),
               check_required_attributes(rows[1], [{"id": "BRAND", "tags": {"required": True}}]),
               check_required_attributes(rows[2], None)]
    # Render collected values as literal text, even when upstream names contain markup.
    results[0]["missing"][0]["name"] = '型号 <img src=x onerror="alert(1)">'
    pending = []
    page.route("**/api/catalog/attributes/check?*", lambda route: pending.append(route))
    page.route("https://images.test/**", lambda route: route.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#ddd"/></svg>'))
    page.route("**/api/mercado-collection/items?*", lambda route: route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": {"rows": rows, "total": 3}})))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.evaluate("switchTab('mercado-collection')")
    page.wait_for_selector('[data-collection-attribute-check="91"]')
    page.wait_for_function("document.getElementById('collection-attribute-summary').textContent.includes('核验中 3 件')")
    first = page.locator('.mercado-item-select[value="91"]')
    first.focus()
    page.keyboard.press('Space')
    assert first.is_checked()
    # Let the asynchronous response arrive while keyboard focus stays on the checkbox.
    assert len(pending) == 1
    pending[0].fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": {"results": results}}))
    page.wait_for_function("document.getElementById('collection-attribute-summary').textContent.includes('核验中 0 件')")
    assert first.is_checked() and first.evaluate("el => document.activeElement === el")
    assert first.evaluate("el => getComputedStyle(el).outlineStyle") != "none"
    summary = page.locator('#collection-attribute-summary').inner_text()
    assert "缺失 1 件" in summary and "齐全 1 件" in summary and "暂未核验 1 件" in summary
    missing = page.locator('[data-collection-attribute-check="91"]')
    assert "MODEL" in missing.inner_text() and "<img" in missing.inner_text()
    assert missing.locator('img').count() == 0
    assert page.locator('[data-collection-attribute-check="92"]').get_attribute('data-status') == 'complete'
    assert page.locator('[data-collection-attribute-check="93"]').get_attribute('data-status') == 'unavailable'
    page.locator('#mercado-select-all').check()
    assert page.locator('.mercado-item-select:checked').count() == 3
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    missing.scroll_into_view_if_needed()
    artifact = ROOT / 'artifacts/mercado-collection-attributes-20261014'
    artifact.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact / f'attributes-{width}.png'))
    page.evaluate("switchTab('mercado-products')")
    assert not page.locator('#collection-attribute-summary').is_visible()
    assert not errors


def test_stale_result_does_not_overwrite_new_list_and_failures_are_explicit(console_page):
    page = console_page
    pending = []
    page.route('**/api/catalog/attributes/check?*', lambda route: pending.append(route))
    page.evaluate("rows => { mercadoListMode='collection'; renderMercadoListRows(rows); }", samples())
    page.wait_for_function("document.querySelector('[data-catalog-attributes]')?.dataset.checkStarted === 'true'")
    assert len(pending) == 1
    changed = samples()[1]
    changed['updated_at'] = '2026-10-14 23:00:00'
    page.evaluate("row => renderMercadoListRows([row])", changed)
    pending[0].fulfill(content_type='application/json', body=json.dumps({'status':'success','data':{'results':[
        {'id':92,'status':'missing','message':'过期结果'}]}}))
    page.wait_for_timeout(100)
    assert len(pending) == 2
    pending[1].fulfill(status=503, content_type='application/json',body='{}')
    page.wait_for_function("document.querySelector('[data-collection-attribute-check]').dataset.status === 'unavailable'")
    assert '过期结果' not in page.locator('#mercado-list-body').inner_text()
    assert '齐全 0 件' in page.locator('#collection-attribute-summary').inner_text()


def test_large_page_uses_bounded_batches_and_checks_every_row(console_page):
    page = console_page
    batches = []
    def respond(route):
        ids = [int(value) for value in parse_qs(urlsplit(route.request.url).query)['ids'][0].split(',')]
        batches.append(ids)
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": {"results": [
            {"id": value, "status": "complete", "message": "类目必填属性齐全"} for value in ids
        ]}}))
    page.route("**/api/catalog/attributes/check?*", respond)
    page.evaluate("rows => { mercadoListMode='collection'; renderMercadoListRows(rows); }", [
        {**samples()[0], "id": index} for index in range(1, 14)
    ])
    page.wait_for_function("document.getElementById('collection-attribute-summary').textContent.includes('齐全 13 件')")
    assert sorted(value for batch in batches for value in batch) == list(range(1, 14))
    assert max(map(len, batches)) <= 5
