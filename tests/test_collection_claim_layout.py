import json
from unittest.mock import patch

import pytest
from erp import mercadolibre_collection_store as store
from bit import product_access
from test_console_ui_browser import ROOT, console_browser, console_page
from test_erp_mercadolibre_collection_store import _FakeConnection


@pytest.mark.parametrize('role,owner_id,allowed', [('member', 7, True), ('member', 8, False), ('super_admin', 8, True), ('enterprise_admin', 8, True), ('member', None, False)])
def test_claim_requires_collector(role, owner_id, allowed):
    user = dict(id=7, role_key=role, organization_key='test', display_name='业务甲')
    row = dict(id=91, task_id=1, organization_key='test', owner_user_id=owner_id,
               owner_name='业务甲' if owner_id == 7 else '', visibility='enterprise')
    connection = _FakeConnection()
    connection.fake_cursor.fetchall = lambda: [row]
    with product_access.scope(user), patch.object(store, 'ensure_collection_tables'):
        assert store._json_safe_row(row)['can_claim'] is allowed
        if not allowed:
            with pytest.raises(ValueError, match='只能认领自己采集'):
                store._add_collection_items_to_products_once([91], connection_factory=lambda: connection)
            assert not any(q.startswith('INSERT') for q, _ in connection.fake_cursor.queries)
        else:
            store._add_collection_items_to_products_once([91], connection_factory=lambda: connection)


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_collection_single_row_and_claim_action(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    rows = [dict(id=91, task_id=1, source_item_id='MLM91', title='模拟商品', collector_salesperson='业务甲', can_claim=True),
            dict(id=92, task_id=1, source_item_id='MLM92', title='其他业务员商品', collector_salesperson='业务乙', can_claim=False)]
    page.route('**/api/mercado-collection/items?*', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'status':'success','data':{'rows':rows,'total':2,'salespeople':['业务甲','业务乙']}})))
    page.evaluate("switchTab('mercado-collection')")
    page.wait_for_selector('.market-collection-row')
    button = page.locator('#mercado-add-selected')
    assert button.is_visible()
    assert button.evaluate("e => !e.closest('details')")
    assert '业务甲' in page.locator('[data-label="采集业务员"]').first.inner_text()
    bottoms = page.locator('.zs-common-filter-row > :is(.market-filter-field, .zs-common-date-filter):visible').evaluate_all('(els) => els.map(e => Math.round(e.getBoundingClientRect().bottom))')
    assert len(set(bottoms)) == 1
    page.locator('.mercado-item-select').first.check()
    assert button.is_enabled()
    page.locator('#mercado-select-all').check()
    assert button.is_disabled()
    page.locator('#mercado-select-all').uncheck()
    page.locator('.zs-common-filter-row select').first.focus()
    assert page.locator('.zs-common-filter-row select').first.evaluate('e => document.activeElement === e')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    path = ROOT / 'artifacts/collection-claim-20261012'
    path.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path / f'collection-{width}.png'))
