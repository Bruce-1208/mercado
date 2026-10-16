"""Offline real-template regression for a single ordinary filter row."""
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page


@pytest.mark.parametrize('width', [1440, 1024, 390, 320])
def test_all_console_filter_rows(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.wait_for_selector('.zs-compact-filters', state='attached')
    tabs = page.locator('.tab-page').evaluate_all('(nodes) => nodes.map(n => n.id)')
    failures = []
    count = 0
    for tab in tabs:
        page.evaluate('''id => {
            document.querySelectorAll('.tab-page').forEach(n => n.classList.toggle('active', n.id === id));
        }''', tab)
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        rows = page.locator('#' + tab + ' .zs-compact-filters').evaluate_all('''panels => panels.filter(p => p.getClientRects().length).map(p => {
            const nodes = [...p.querySelectorAll(':scope > .zs-compact-item, :scope > details > summary')].filter(n => n.getClientRects().length);
            const boxes = nodes.map(n => n.getBoundingClientRect());
            return {id: p.id || p.className, rows: boxes.every(b => Math.abs(b.bottom - boxes[0].bottom) < 3),
                overflow: p.scrollWidth > p.clientWidth + 2, controls: nodes.length};
        })''')
        count += len(rows)
        failures.extend((tab, row) for row in rows if not row['rows'] or row['overflow'])
        if tab in ['tab-orders', 'tab-ai-original-products', 'tab-1688-products']:
            out = ROOT / 'artifacts/compact-filters-20261012'
            out.mkdir(exist_ok=True)
            page.screenshot(path=str(out / f'{tab}-{width}.png'))
    assert count >= 20
    assert not failures, failures


def test_resize_preserves_values_focus_and_multiselect(console_page):
    page = console_page
    page.wait_for_selector('#tab-orders .zs-compact-filters')
    page.locator('#order-store-filter-trigger').click()
    page.locator('#order-store-filter-picker .market-multi-options input[type=checkbox]').first.check()
    page.locator('#order-store-filter-trigger').click()
    selected = page.locator('#order-store-filter').evaluate('(n) => [...n.selectedOptions].map(o => o.value)')
    page.set_viewport_size({'width': 390, 'height': 1000})
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    summary = page.locator('#tab-orders .zs-compact-filters > details > summary')
    summary.evaluate('(n) => n.parentElement.open = false')
    summary.focus()
    page.keyboard.press('Enter')
    assert page.locator('#order-store-filter-trigger').is_visible()
    assert page.locator('#order-store-filter').evaluate('(n) => [...n.selectedOptions].map(o => o.value)') == selected
    page.keyboard.press('Escape')
    assert summary.evaluate('(n) => n === document.activeElement')
    assert not page.locator('#tab-orders .zs-compact-filters > details').evaluate('(n) => n.open')
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert page.locator('#order-store-filter-trigger').is_visible()


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('template', [
    'mercado_promotions.html', 'mercado_action_center.html', 'ai_billing.html',
    'ai_weight_price.html', 'clearance_alerts.html', 'price_competition.html',
    'product_diagnosis.html', 'automation_center.html', 'infraction_dashboard.html',
])
def test_standalone_filter_rows(console_page, template, width):
    from jinja2 import Environment, FileSystemLoader
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    html = Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template(template).render(
        current_user={'id': 1, 'username': 'preview', 'role_key': 'super_admin'},
        can_view_all=True, billing_admin=True, can_execute=False, rules={},
        url_for=lambda endpoint, filename=None: '/static/' + filename if filename else '/',
    )
    page.route('**/compact-preview', lambda route: route.fulfill(body=html, content_type='text/html'))
    page.goto('http://console.test/compact-preview')
    page.wait_for_selector('.zs-compact-filters', state='attached')
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    panels = page.locator('.zs-compact-filters')
    for panel in panels.all():
        if not panel.is_visible():
            continue
        assert panel.evaluate('(p) => p.scrollWidth <= p.clientWidth + 2')
        summary = panel.locator(':scope > details > summary')
        summary.click()
        assert panel.evaluate('(p) => p.scrollWidth <= p.clientWidth + 2')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if template == 'mercado_promotions.html':
        out = ROOT / 'artifacts/compact-filters-20261012'
        page.screenshot(path=str(out / f'promotions-expanded-{width}.png'))
