"""Validate shared date controls using the real console and offline API fixtures."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


def respond(route, data):
    route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))


def test_all_filter_ranges_are_in_advanced_filters(console_page):
    page = console_page
    ids = [
        "order-start-date", "mercado-date-from", "products-1688-date-from", "ai-original-date-from",
        "wdr-filter-date-from", "publish-record-start-date", "salesperson-stats-start", "store-analysis-start",
        "after-sale-date-from", "profit-date-from", "window-anomaly-date-from", "ai-video-date-from",
        "store-link-audit-from", "ad-analysis-date-from", "prohibited-occurred-from", "pre-sale-start-date",
        "customer-service-start-date", "ai-appeal-date-from", "print-wallet-record-date-from",
    ]
    present = [input_id for input_id in ids if page.locator('#' + input_id).count()]
    assert len(present) >= 16
    for input_id in present:
        assert page.locator('#' + input_id).evaluate("el => !!el.closest('details.zs-advanced-filters')"), input_id
        preset = page.locator('#' + input_id + '-preset')
        assert preset.count() == 1, input_id
        assert not preset.evaluate("el => !!el.closest('details')"), input_id
    # These times belong to operations and editors rather than list filters.
    for input_id in ['order-sync-start-date', 'mercado-publish-schedule-at', 'inventory-movement-time']:
        assert not page.locator('#' + input_id).evaluate("el => !!el.closest('details.zs-advanced-filters')")


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_collection_salesperson_and_date_queries_and_reset(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    queries = []

    def collection(route):
        query = parse_qs(urlsplit(route.request.url).query)
        queries.append(query)
        selected = query.get('salesperson', [''])[0]
        rows = [{'id': i + 1, 'title': name + '的采集商品', 'salesperson': name, 'source_item_id': 'MLM' + str(i)}
                for i, name in enumerate(['业务甲', '业务乙']) if not selected or selected == name]
        respond(route, {'rows': rows, 'total': len(rows), 'salespeople': ['业务甲', '业务乙']})

    page.route('**/api/mercado-collection/items?*', collection)
    page.route('**/api/mercado-products?*', lambda route: respond(route, {'rows': [], 'total': 0}))
    page.evaluate("switchTab('mercado-collection')")
    salesperson = page.locator('#mercado-salesperson-filter')
    page.wait_for_function("document.getElementById('mercado-salesperson-filter').options.length === 3")
    assert salesperson.is_visible()
    assert page.locator('#mercado-date-from').is_hidden()
    assert page.locator('#mercado-date-from-preset').is_visible()
    salesperson.select_option('业务乙')
    page.wait_for_function("mercadoListRows.length === 1")
    assert queries[-1]['salesperson'] == ['业务乙']
    assert salesperson.locator('option').count() == 3  # Directory survives filtering.
    preset = page.locator('#mercado-date-from-preset')
    preset.select_option('yesterday')
    page.wait_for_function("!!mercadoDateFrom.value && mercadoCollectionLoaded")
    assert queries[-1]['date_from'][0].endswith('T00:00')
    assert queries[-1]['date_to'][0].endswith('T23:59')
    assert queries[-1]['date_from'][0][:10] == queries[-1]['date_to'][0][:10]
    preset.select_option('day')
    day = page.locator('#mercado-date-from-day')
    day.fill('2026-10-01')
    page.wait_for_function("mercadoDateFrom.value === '2026-10-01T00:00' && mercadoCollectionLoaded")
    assert queries[-1]['date_to'] == ['2026-10-01T23:59']
    preset.select_option('custom')
    page.locator('#mercado-date-from').fill('2026-10-01T09:15')
    page.locator('#mercado-date-to').fill('2026-10-02T10:30')
    page.evaluate('applyMercadoProductFilters()')
    page.wait_for_function('mercadoCollectionLoaded')
    assert queries[-1]['date_from'] == ['2026-10-01T09:15']
    assert queries[-1]['date_to'] == ['2026-10-02T10:30']
    assert preset.input_value() == 'custom'
    for selector in [salesperson, preset, page.locator('#mercado-date-from'), page.locator('#mercado-date-to')]:
        bounds = selector.bounding_box()
        assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width + 1
    artifact = ROOT / 'artifacts/common-date-filters-20261008'
    artifact.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact / f'collection-{width}.png'), full_page=True)
    page.evaluate('resetMercadoProductFilters()')
    page.wait_for_function('mercadoCollectionLoaded && mercadoListRows.length === 2')
    assert salesperson.input_value() == ''
    assert preset.input_value() == ''
    assert 'salesperson' not in queries[-1] and 'date_from' not in queries[-1] and 'date_to' not in queries[-1]
    page.evaluate("switchTab('mercado-products')")
    assert salesperson.is_hidden()


def test_programmatic_defaults_and_order_reset_sync_common_date_filter(console_page):
    page = console_page
    page.locator('#order-start-date-preset').select_option('7')
    assert page.locator('#order-start-date-preset').input_value() == '7'
    page.evaluate("orderStartDate.value = '2026-01-01T09:15'; orderEndDate.value = '2026-01-02T10:30'")
    assert page.locator('#order-start-date-preset').input_value() == 'custom'
    page.evaluate('resetOrderFilters()')
    assert page.locator('#order-start-date-preset').input_value() == ''


@pytest.mark.parametrize('template,from_id', [
    ('ai_billing.html', 'ai-billing-start-date'),
    ('account_billing.html', 'print-wallet-record-date-from'),
])
@pytest.mark.parametrize('width', [1440, 390, 320])
def test_standalone_billing_date_filters(console_page, template, from_id, width):
    from jinja2 import Environment, FileSystemLoader
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    html = Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template(template).render(
        current_user={'id': 1}, wallet_admin=True, billing_admin=True,
        url_for=lambda endpoint, filename=None: '/static/' + filename if filename else '/',
    )
    page.route('**/date-preview', lambda route: route.fulfill(body=html, content_type='text/html'))
    page.goto('http://console.test/date-preview')
    if template == 'account_billing.html':
        page.locator('#print-wallet-tab').click()
    preset = page.locator('#' + from_id + '-preset')
    assert preset.is_visible()
    assert page.locator('#' + from_id).is_hidden()
    preset.select_option('today')
    assert page.locator('#' + from_id).input_value()
    preset.select_option('custom')
    assert page.locator('#' + from_id).is_visible()
    for control in [preset, page.locator('#' + from_id)]:
        bounds = control.bounding_box()
        assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width + 1
    artifact = ROOT / 'artifacts/common-date-filters-20261008'
    artifact.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact / f'{template}-{width}.png'), full_page=True)


def test_admin_filter_ranges_and_quick_queries(console_page):
    from jinja2 import Environment, FileSystemLoader
    page = console_page
    html = Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template('index.html').render(
        current_user={'id': 1, 'username': 'preview', 'is_admin': True, 'is_platform_admin': True, 'role_key': 'super_admin'},
        can_change_task_workers=False, runtime_role='server',
        url_for=lambda endpoint, filename=None: '/static/' + filename if filename else '/',
    )
    page.route('**/date-preview', lambda route: route.fulfill(body=html, content_type='text/html'))
    page.goto('http://console.test/date-preview')
    for input_id in ['store-link-audit-from', 'salesperson-stats-start', 'ad-analysis-date-from']:
        assert page.locator('#' + input_id).evaluate("el => !!el.closest('details.zs-advanced-filters')")
    # Execute every quick callback once; no business API can escape the offline routes.
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.evaluate("""() => {
        for (const preset of document.querySelectorAll('.zs-common-date-filter select')) {
            preset.value = 'today';
            preset.dispatchEvent(new Event('change', {bubbles: true}));
        }
    }""")
    page.wait_for_timeout(200)
    assert not errors


@pytest.mark.parametrize('width', [1440, 320])
def test_infraction_period_buttons_move_to_advanced_and_common_date_stays_in_sync(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-infractions').classList.add('active');
        document.querySelector('.legacy-infractions-module').hidden = false;
    }""")
    preset = page.locator('#infraction-date-preset')
    assert preset.is_visible()
    assert page.locator('#tab-infractions .range-switch').is_hidden()
    preset.select_option('7')
    assert page.locator('#tab-infractions .range-button.active').get_attribute('data-days') == '7'
    page.evaluate('setInfractionDays(30)')
    assert preset.input_value() == '30'
    bounds = preset.bounding_box()
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width + 1


def test_standalone_infraction_period_filter(console_page):
    from jinja2 import Environment, FileSystemLoader
    page = console_page
    html = Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template('infraction_dashboard.html').render(
        current_user={'id': 1}, ip_rights_mode=False,
        url_for=lambda endpoint, filename=None: '/static/' + filename if filename else '/',
    )
    page.route('**/date-preview', lambda route: route.fulfill(body=html, content_type='text/html'))
    page.goto('http://console.test/date-preview')
    preset = page.locator('#days-select-preset')
    assert preset.is_visible()
    assert page.locator('#days-select').is_hidden()
    preset.select_option('7')
    assert page.locator('#days-select').input_value() == '7'
