"""Shared category manager on the real console, with offline API data only."""
import json
import re
from urllib.parse import urlsplit

import pytest
from test_console_ui_browser import ROOT, console_browser, console_page


@pytest.mark.parametrize("width", [1440, 1024, 390])
def test_shared_category_manager_crud_and_navigation(console_page, width):
    page = console_page
    page.set_default_timeout(5000)
    page.set_viewport_size({"width": width, "height": 1000})
    categories = [{"id": 7, "name": "测试家居", "listing_ratio_percent": 100}]
    rows = [{"id": 41, "title": "模拟保温杯", "management_category_id": 7,
             "management_category_name": "测试家居", "original_1688": {}}]
    mutations = []

    def api(route):
        request = route.request
        path = urlsplit(request.url).path
        method = request.method
        if path.startswith('/api/mercado-management-categories'):
            if method != 'GET':
                body = request.post_data_json if method != 'DELETE' else None
                mutations.append((method, body))
                if method == 'POST':
                    categories.append({"id": 8, **body})
                elif method == 'PATCH':
                    categories[0].update(body)
                    rows[0]['management_category_name'] = body['name']
                elif method == 'DELETE':
                    categories.pop(0)
                    rows[0].update(management_category_id=None, management_category_name=None)
            data = {"rows": categories}
        elif path == '/api/1688-products/options':
            data = {"collector_salespeople": []}
        else:
            data = {"rows": rows, "total": len(rows)}
        route.fulfill(content_type='application/json', body=json.dumps({"status": "success", "data": data}))

    page.route(re.compile(r'/api/mercado-management-categories(?:[/?]|$)'), api)
    page.route(re.compile(r'/api/1688-products(?:[/?]|$)'), api)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.evaluate('''async () => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-1688-products').classList.add('active');
        await load1688Products();
    }''')
    page.locator('#products-1688-select-all').check()
    trigger = page.locator('[onclick="products1688OpenCategoryManager()"]')
    if not trigger.is_visible():
        page.locator('.p1688-toolbar .zs-advanced-filters summary').click()
    trigger.click()
    dialog = page.locator('#mercado-category-dialog')
    assert dialog.is_visible()
    page.wait_for_function("document.activeElement.id === 'mercado-category-new-name'")
    bounds = dialog.bounding_box()
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
    assert dialog.locator('.mercado-category-list').evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
    assert page.locator('[data-mercado-category-id="7"]').bounding_box()['width'] > 100
    output = ROOT / 'artifacts/1688-category-manager'
    output.mkdir(exist_ok=True)
    page.screenshot(path=str(output / f'manager-{width}.png'))
    page.locator('#mercado-category-new-name').fill('测试配件')
    page.locator('#mercado-category-new-ratio').fill('120')
    page.locator('#mercado-category-create').click()
    page.wait_for_function("document.querySelector('#products-1688-batch-category option[value=\"8\"]')")
    assert '测试配件' in page.locator('#products-1688-category-filter').inner_text()
    page.locator('[data-mercado-category-id="7"]').fill('测试家居改名')
    page.locator('[data-mercado-category-ratio-id="7"]').fill('85')
    dialog.locator('.mercado-category-row').first.get_by_role('button', name='保存').click()
    page.wait_for_function("document.querySelector('#products-1688-body').textContent.includes('测试家居改名')")
    assert page.locator('#products-1688-select-all').is_checked()
    page.once('dialog', lambda prompt: prompt.accept())
    dialog.locator('.mercado-category-row').first.get_by_role('button', name='删除').click()
    page.wait_for_function("document.querySelector('#products-1688-body').textContent.includes('未分类')")
    assert page.locator('#products-1688-batch-category option[value="7"]').count() == 0
    page.wait_for_function('!mercadoCategorySaving')
    page.keyboard.press('Escape')
    assert not dialog.is_visible()
    assert trigger.evaluate('(el) => el === document.activeElement')
    trigger.click()
    dialog.get_by_role('button', name='完成', exact=True).click()
    page.evaluate('''() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.querySelector('.market-category-manage-button').closest('.tab-page').classList.add('active');
    }''')
    page.locator('.market-category-manage-button').click()
    assert dialog.is_visible()
    assert page.locator('[data-mercado-category-id="8"]').input_value() == '测试配件'
    assert mutations == [('POST', {'name': '测试配件', 'listing_ratio_percent': 120}),
                         ('PATCH', {'name': '测试家居改名', 'listing_ratio_percent': 85}),
                         ('DELETE', None)]
    assert not errors
