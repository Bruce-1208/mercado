"""Membership navigation with the real template and intercepted offline APIs."""
import json
from urllib.parse import urlsplit
import pytest
from jinja2 import Environment, FileSystemLoader
from test_console_ui_browser import ROOT, console_browser  # noqa: F401


@pytest.fixture
def membership_page(console_browser, role, width):
    permissions = ['order_print.view', 'order_print.execute']
    if role != 'basic_member':
        permissions += [f'{module}.{action}' for module in ('catalog', 'ai_content', 'order_analysis')
                        for action in ('view', 'execute')]
    if role == 'enterprise_admin':
        permissions += ['access.view', 'access.manage']
    html = Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template('index.html').render(
        current_user={'id': 31, 'username': 'preview', 'display_name': '权限预览', 'role_key': role,
                      'role_name': {'basic_member': '基础会员', 'premium_member': '高级会员', 'enterprise_admin': '企业管理员'}[role],
                      'permissions': permissions, 'own_store_only': role != 'enterprise_admin'},
        can_change_task_workers=False, can_access_shop_status=role == 'enterprise_admin',
        runtime_role='server', url_for=lambda endpoint, filename: '/static/' + filename)
    page = console_browser.new_page(viewport={'width': width, 'height': 1000}, reduced_motion='reduce')
    def route(request):
        path = urlsplit(request.request.url).path
        if path == '/':
            request.fulfill(body=html, content_type='text/html')
        elif path.startswith('/static/'):
            request.fulfill(path=str(ROOT / 'bit' / path.lstrip('/')))
        else:
            request.fulfill(content_type='application/json', body=json.dumps(
                {'status': 'success', 'data': [], 'items': [], 'stores': [], 'groups': [], 'total': 0}))
    page.route('**/*', route)
    page.goto('http://console.test/')
    yield page
    page.close()


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('role', ['basic_member', 'premium_member', 'enterprise_admin'])
def test_membership_navigation(membership_page, width, role):
    page = membership_page
    page.set_default_timeout(5000)
    page.emulate_media(reduced_motion='reduce')
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate('''role => {
        currentWorkbenchUser.role_key = role;
        currentWorkbenchUser.own_store_only = role !== 'enterprise_admin';
        ordersLoaded = true;
        applyWorkbenchPermissions();
        switchTab('orders');
    }''', role)
    for tab in ['mercado-products', 'ai-original-products', 'ai-video']:
        button = page.locator(f'.sidebar [data-tab="{tab}"]')
        assert button.evaluate('e => e.hidden') == (role == 'basic_member')
    assert page.locator('.sidebar [data-tab="access"]').evaluate('e => e.hidden') == (role != 'enterprise_admin')
    if role == 'basic_member':
        assert page.locator('.topbar a[href="/settings/integrations"]').count() == 0
        assert not page.locator('#order-sync-open-button').is_visible()
        assert not page.locator('#order-zying-sync-open').is_visible()
    page.evaluate("switchTab('ai-original-products')")
    assert page.locator('#tab-ai-original-products').evaluate("e => e.classList.contains('active')") == (role != 'basic_member')
    page.evaluate("""() => {
        switchTab('orders');
        const select = document.getElementById('order-store-filter');
        select.innerHTML = '<option value="mx">墨西哥旗舰店</option><option value="br">巴西精选店</option>';
        renderMercadoPublishPicker(select);
    }""")
    # Existing keyboard-accessible multi-select must continue working for all tiers.
    from test_common_date_filters_browser import reveal_filter
    if page.locator('#order-filter-region').is_hidden():
        page.locator('#order-list-focus-toggle').click()
    reveal_filter(page.locator('#order-store-filter-trigger'))
    trigger = page.locator('#order-store-filter-trigger')
    trigger.scroll_into_view_if_needed()
    # The shared picker closes on scroll; let the mobile anchor finish moving.
    page.wait_for_timeout(350)
    trigger.focus()
    page.keyboard.press('ArrowDown')
    picker = page.locator('#order-store-filter-picker')
    search = picker.locator('input[type="search"]')
    search.fill('巴西')
    assert picker.locator('.market-multi-option:visible').count() == 1
    picker.locator('.market-multi-option:visible input').check()
    assert page.locator('#order-store-filter').evaluate('e => [...e.selectedOptions].map(o => o.value)') == ['br']
    search.focus()
    assert search.evaluate('e => e === document.activeElement')
    search.press('Escape')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = ROOT / 'artifacts/membership-permissions'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'{role}-{width}.png'))
