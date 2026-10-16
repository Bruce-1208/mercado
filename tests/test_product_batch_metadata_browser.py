"""Offline product batch editing and ordinary keyword filtering."""
import json
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_batch_editor_fields_search_and_keyboard(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.set_default_timeout(5000)
    writes = []
    page.route('**/api/access/users', lambda route: route.fulfill(content_type='application/json', body=json.dumps({
        'status': 'success', 'data': [{'id': 20, 'display_name': '小李', 'is_active': True}],
    })))
    def save(route):
        writes.append(route.request.post_data_json)
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': {'requested': 2, 'changed': 2}}))
    page.route('**/api/mercado-products/bulk-edit', save)
    page.evaluate('''() => {
        currentWorkbenchUser.role_key = 'enterprise_admin';
        currentWorkbenchUser.permissions = ['*'];
        switchTab('mercado-products');
        selectedMercadoCollectionIds.clear();
        selectedMercadoCollectionIds.add(1); selectedMercadoCollectionIds.add(2);
        mercadoManagementCategoryBulk.add(new Option('家居', '5'));
        updateMercadoSelection();
    }''')
    search = page.locator('#mercado-list-search')
    assert search.is_visible()
    assert page.evaluate("document.getElementById('mercado-list-search').closest('details') === null")
    search.fill('收纳')
    with page.expect_request(lambda request: '/api/mercado-products?' in request.url and 'search=' in request.url) as request:
        search.press('Enter')
    assert '%E6%94%B6%E7%BA%B3' in request.value.url
    # Search resets selection; emulate selecting two returned mock products.
    page.evaluate('''() => { selectedMercadoCollectionIds.add(1); selectedMercadoCollectionIds.add(2); updateMercadoSelection(); }''')
    page.locator('.zs-bulk-menu > summary').click()
    page.locator('#mercado-bulk-edit-selected').click()
    dialog = page.locator('#mercado-bulk-edit-dialog')
    for field, value in [('weight_g', '650'), ('review_status', 'risk'), ('owner_user_id', '20'), ('management_category_id', '5'), ('visibility', 'private')]:
        dialog.locator(f'[data-bulk-field="{field}"]').check()
        control = page.locator('#mercado-bulk-edit-weight' if field == 'weight_g' else f'#mercado-bulk-edit-{field}')
        if field == 'weight_g': control.fill(value)
        else: control.select_option(value)
    dialog.locator('[data-bulk-field="dimensions"]').check()
    for name, value in [('length', '30'), ('width', '20'), ('height', '10')]:
        page.locator(f'#mercado-bulk-edit-{name}').fill(value)
    page.locator('#mercado-bulk-edit-save').focus()
    page.keyboard.press('Tab')
    assert page.evaluate("document.getElementById('mercado-bulk-edit-dialog').contains(document.activeElement)")
    bounds = dialog.bounding_box()
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
    out = ROOT / 'artifacts/product-batch-metadata-20261014'
    out.mkdir(exist_ok=True)
    page.locator('#mercado-bulk-edit-heading').scroll_into_view_if_needed()
    page.screenshot(path=str(out / f'batch-{width}.png'))
    page.locator('#mercado-bulk-edit-save').click()
    page.wait_for_function("!document.getElementById('mercado-bulk-edit-dialog').open")
    assert writes == [{'product_item_ids': [1, 2], 'changes': {
        'weight_g': 650, 'package_length_cm': 30, 'package_width_cm': 20, 'package_height_cm': 10,
        'review_status': 'risk', 'owner_user_id': 20, 'management_category_id': 5, 'visibility': 'private',
    }}]
    page.screenshot(path=str(out / f'filters-{width}.png'))
