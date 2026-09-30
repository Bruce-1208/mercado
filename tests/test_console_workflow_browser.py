"""Offline regression coverage for filter folding and the publishing review step."""
import pytest
from test_console_ui_browser import console_browser, console_page  # noqa: F401


@pytest.fixture
def product_page(console_page):
    page = console_page
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-mercado-products').classList.add('active');
        document.getElementById('mercado-list-products-host').append(mercadoListModule);
        mercadoListMode = 'products';
        mercadoListModule.classList.remove('collection-mode');
        mercadoListModule.classList.add('product-mode');
        mercadoProductActions.style.display = 'flex';
        mercadoCollectionOnlyActions.style.display = 'none';
        mercadoProductReviewActions.style.display = 'inline-flex';
        mercadoProductFilters.querySelectorAll('.market-product-only-filter').forEach(el => el.hidden = false);
        renderMercadoPublishStores([{id: 1, display_name: '测试店铺', site_id: 'MLM', site_settings: [{site_id:'MLM', group_name:'测试组'}]}]);
        renderMercadoListRows([{id: 101, title: '测试商品', review_status:'approved', weight_g:100, net_proceeds_usd:8}]);
    }""")
    return page


def test_advanced_filters_keep_values_and_show_active_count(console_page):
    page = console_page
    field = page.locator('#order-remark-search')
    summary = page.locator('#tab-orders .zs-advanced-filters > summary')
    assert not field.is_visible()
    summary.click()
    field.fill('待复核')
    summary.click()
    assert not field.is_visible()
    assert '已设置 1 项' in summary.inner_text()
    assert field.input_value() == '待复核'
    page.locator('#tab-orders .order-filter-actions').get_by_role('button', name='重置').click()
    assert summary.inner_text() == '高级筛选'


@pytest.mark.parametrize('width', [1440, 390])
def test_product_filters_and_selection_gate(product_page, width):
    page = product_page
    page.set_viewport_size({'width': width, 'height': 1000})
    assert page.locator('#mercado-review-filter').is_visible()
    assert not page.locator('#mercado-weight-min').is_visible()
    assert not page.locator('#mercado-publish-quantity').is_visible()
    assert page.locator('#mercado-publish-next').is_disabled()
    assert not page.locator('.zs-bulk-menu').is_visible()
    page.locator('.mercado-item-select').check()
    assert page.locator('#mercado-publish-next').is_enabled()
    assert page.locator('.zs-bulk-menu').is_visible()
    assert not page.locator('#mercado-delete-selected').is_visible()
    page.locator('#mercado-publish-next').click()
    assert page.locator('#mercado-publish-quantity').is_visible()
    assert not page.locator('#mercado-publish-selected').is_visible()
    box = page.locator('.zs-publish-workspace').bounding_box()
    assert box['x'] >= 0 and box['x'] + box['width'] <= width


def configure(page):
    page.locator('.mercado-item-select').check()
    page.locator('#mercado-publish-next').click()
    page.locator('#mercado-publish-store-trigger').click()
    page.locator('#mercado-publish-store-picker .market-multi-option input').check()
    page.locator('#mercado-publish-quantity').fill('20')


def test_review_preserves_settings_and_selection_changes_invalidate_it(product_page):
    page = product_page
    configure(page)
    page.locator('#mercado-publish-review-next').click()
    review = page.locator('#mercado-publish-review')
    assert review.is_visible()
    assert '测试店铺' in review.inner_text()
    assert '20' in review.inner_text()
    assert page.locator('#mercado-publish-selected').is_visible()
    assert not page.locator('#mercado-publish-quantity').is_visible()
    page.get_by_role('button', name='返回修改配置', exact=True).click()
    assert page.locator('#mercado-publish-quantity').input_value() == '20'
    page.locator('#mercado-publish-review-next').click()
    page.locator('.mercado-item-select').uncheck()
    assert not review.is_visible()
    assert not page.locator('#mercado-publish-selected').is_visible()
    assert page.locator('#mercado-publish-next').is_disabled()


def test_invalid_stock_cannot_reach_review(product_page):
    page = product_page
    configure(page)
    page.locator('#mercado-publish-quantity').fill('0')
    page.locator('#mercado-publish-review-next').click()
    assert not page.locator('#mercado-publish-review').is_visible()
    assert page.locator('#mercado-publish-quantity').evaluate('el => el === document.activeElement')


def test_group_review_and_cancel_do_not_publish(product_page):
    page = product_page
    requests = []
    page.on('request', lambda request: requests.append(request) if request.method == 'POST' else None)
    page.locator('.mercado-item-select').check()
    page.locator('#mercado-publish-next').click()
    page.locator('#mercado-publish-mode').select_option('groups')
    page.locator('#mercado-publish-group-trigger').click()
    page.locator('#mercado-publish-group-picker .market-multi-option input').check()
    page.locator('#mercado-group-publish-mode').select_option('polling')
    page.locator('#mercado-publish-review-next').click()
    assert '测试组' in page.locator('#mercado-publish-review').inner_text()
    assert '轮询' in page.locator('#mercado-publish-review').inner_text()
    dialogs = []
    def cancel(dialog):
        dialogs.append(dialog.type)
        dialog.dismiss()
    page.on('dialog', cancel)
    page.locator('#mercado-publish-selected').click()
    assert dialogs == ['confirm']
    assert not requests


def test_past_schedule_stays_in_settings(product_page):
    page = product_page
    configure(page)
    page.locator('#mercado-publish-schedule-at').fill('2020-01-01T12:00')
    page.locator('#mercado-publish-review-next').click()
    assert not page.locator('#mercado-publish-review').is_visible()
    assert '晚于当前时间' in page.locator('#mercado-publish-schedule-at').evaluate('el => el.validationMessage')
