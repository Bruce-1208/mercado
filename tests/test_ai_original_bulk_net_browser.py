"""Role visibility and batch controls on the real console, offline."""
import json
from urllib.parse import urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader

from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('role', ['super_admin', 'enterprise_admin', 'member'])
@pytest.mark.parametrize('width', [1440, 390])
def test_summary_removed_and_model_settings_only_for_super_admin(console_browser, role, width):
    html = Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template('index.html').render(
        current_user={'username': 'preview', 'role_key': role}, can_change_task_workers=False,
        runtime_role='server', url_for=lambda endpoint, filename: '/static/' + filename)
    page = console_browser.new_page(viewport={'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    def route(request):
        path = urlsplit(request.request.url).path
        if path == '/': request.fulfill(body=html, content_type='text/html')
        elif path.startswith('/static/'):
            request.fulfill(path=str(ROOT / 'bit' / path.lstrip('/')))
        else:
            request.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': [], 'items': [], 'total': 0}))
    page.route('**/*', route)
    page.goto('http://console.test/')
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
    }""")
    panel = page.locator('#tab-ai-original-products')
    assert panel.locator('.ai-original-metrics').count() == 0
    assert '商品生成说明' not in panel.inner_text()
    assert panel.locator('.ai-original-settings-disclosure').count() == (1 if role == 'super_admin' else 0)
    if role == 'super_admin':
        panel.locator('.ai-original-settings-disclosure > summary').click()
        assert panel.locator('#ai-original-model').is_visible()
    else:
        assert panel.locator('#ai-original-model').count() == 0
    assert panel.locator('#ai-original-process').is_visible()
    panel.locator('.ai-original-bulk-net').scroll_into_view_if_needed()
    assert panel.locator('#ai-original-bulk-net-set').is_disabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = ROOT / 'artifacts/ai-original-bulk-net-20261010'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'{role}-{width}.png'))
    assert not errors
    page.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_selected_amount_and_ratio_save_and_refresh(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    rows = [{'id': i, 'net_proceeds_usd': amount, 'original_1688': {'title': f'产品{i}'}}
            for i, amount in [(1, 10), (2, 20), (3, 30), (4, None)]]
    requests = []
    def api(route):
        path = urlsplit(route.request.url).path
        if path.endswith('/bulk-net-proceeds'):
            body = route.request.post_data_json
            requests.append(body)
            changed = skipped = 0
            for row in rows:
                if row['id'] not in body['product_item_ids']: continue
                changes = body['changes']
                if 'net_proceeds_usd' in changes: row['net_proceeds_usd'] = changes['net_proceeds_usd']
                elif row['net_proceeds_usd'] is None:
                    skipped += 1
                    continue
                else: row['net_proceeds_usd'] *= changes['net_proceeds_ratio'] / 100
                changed += 1
            data = {'changed': changed, 'skipped': skipped}
        else: data = {'rows': rows, 'total': len(rows)}
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': data}))
    page.route('**/api/ai-original-products', api)
    page.route('**/api/ai-original-products?*', api)
    page.route('**/api/ai-original-products/bulk-net-proceeds', api)
    page.evaluate("""async () => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        await loadAiOriginalProducts();
    }""")
    # Select individual products through the checkboxes, leaving product 3 alone.
    for row_id in [1, 2, 4]:
        page.locator(f'[data-original-product-id="{row_id}"] input[type="checkbox"]').first.check()
    page.locator('#ai-original-bulk-net-ratio').fill('150')
    page.locator('#ai-original-bulk-net-scale').click()
    page.wait_for_function('!aiOriginalSubmitting')
    assert requests[0] == {'product_item_ids': [1, 2, 4], 'changes': {'net_proceeds_ratio': 150}}
    assert [row['net_proceeds_usd'] for row in rows] == [15, 30, 30, None]
    assert '跳过 1 件' in page.locator('#ai-original-task-status').inner_text()
    assert page.locator('#ai-original-net-1').input_value() == '15'
    assert page.locator('#ai-original-net-2').input_value() == '30'
    page.locator('#ai-original-bulk-net-amount').fill('12.5')
    page.locator('#ai-original-bulk-net-set').click()
    page.wait_for_function('!aiOriginalSubmitting')
    assert requests[-1]['changes'] == {'net_proceeds_usd': 12.5}
    assert [row['net_proceeds_usd'] for row in rows] == [12.5, 12.5, 30, 12.5]
    page.locator('#ai-original-bulk-net-ratio').fill('0')
    page.locator('#ai-original-bulk-net-scale').click()
    assert len(requests) == 2
    assert '大于 0' in page.locator('#ai-original-task-status').inner_text()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
