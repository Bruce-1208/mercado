"""Offline grouped navigation behavior using the real console template."""
import json
from urllib.parse import urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader

from test_console_ui_browser import ROOT, console_browser, console_page


def test_navigation_search_collapse_and_permissions(console_page):
    page = console_page
    page.evaluate('setWorkbenchImmersive(false)')
    groups = page.locator('.nav-section')
    assert groups.count() == 8
    page.get_by_role('button', name='全部收起', exact=True).click()
    assert page.locator('.nav-section[open]').count() == 0
    search = page.get_by_role('searchbox', name='搜索功能')
    search.fill('核重')
    assert page.locator('.sidebar .tab-button:visible').count() == 1
    assert page.locator('.sidebar .tab-button:visible').inner_text() == 'AI核重核价'
    search.fill('没有这个模块')
    assert page.locator('.nav-empty').is_visible()
    search.fill('')
    assert page.locator('.nav-section[open]').count() == 0
    page.evaluate("switchTab('orders')")
    assert page.locator('.nav-section[data-group="orders"]').get_attribute('open') is not None
    page.evaluate("document.querySelector('[data-tab=access]').hidden = true")
    # The settings link now also belongs to System; the group stays reachable.
    assert not page.locator('[data-group=system]').evaluate('n => n.hidden')
    page.locator('[data-group=system] .tab-button').evaluate_all('nodes => nodes.forEach(n => n.hidden = true)')
    page.wait_for_function("document.querySelector('[data-group=system]').hidden")
    page.get_by_role('button', name='全部展开', exact=True).click()
    page.wait_for_function("JSON.parse(localStorage.getItem('zeshun-navigation-groups-v1')).products === true")
    page.reload()
    assert page.locator('.nav-section[data-group="products"]').get_attribute('open') is not None
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('#sidebar-toggle').click()
    assert page.locator('.sidebar .nav-label:visible').count() > 0
    assert page.evaluate('document.documentElement.scrollWidth') == 390


def test_mobile_navigation_survives_saved_desktop_collapse(console_page):
    page = console_page
    page.evaluate('setWorkbenchImmersive(false)')
    toggle = page.locator('#sidebar-toggle')
    toggle.click()
    assert page.evaluate("localStorage.getItem('zeshun-sidebar-collapsed')") == '1'
    page.set_viewport_size({'width': 390, 'height': 844})
    page.wait_for_function("document.getElementById('console-navigation').inert")
    toggle.click()
    assert toggle.get_attribute('aria-expanded') == 'true'
    search = page.get_by_role('searchbox', name='搜索功能')
    search.fill('订单管理')
    page.locator('.sidebar .tab-button:visible').click()
    assert page.locator('#tab-orders').is_visible()
    assert toggle.get_attribute('aria-expanded') == 'false'
    assert page.evaluate("localStorage.getItem('zeshun-sidebar-collapsed')") == '1'
    page.set_viewport_size({'width': 1440, 'height': 1000})
    assert page.locator('body').evaluate("el => el.classList.contains('sidebar-collapsed')")


def test_mobile_navigation_focus_escape_and_backdrop(console_page):
    page = console_page
    page.evaluate('setWorkbenchImmersive(false)')
    page.set_viewport_size({'width': 390, 'height': 844})
    original_overflow = page.evaluate('getComputedStyle(document.body).overflow')
    toggle = page.locator('#sidebar-toggle')
    toggle.click()
    page.wait_for_function("document.activeElement.matches('.nav-tools input')")
    page.locator('.sidebar-footer').evaluate("el => el.querySelector('a, button')?.focus()")
    for _ in range(12):
        page.keyboard.press('Tab')
        assert page.evaluate("document.activeElement.id === 'sidebar-toggle' || !!document.activeElement.closest('.sidebar')")
    page.keyboard.press('Escape')
    assert toggle.get_attribute('aria-expanded') == 'false'
    assert toggle.evaluate('el => el === document.activeElement')
    toggle.click()
    page.locator('.zs-nav-backdrop').click(position={'x': 360, 'y': 420})
    assert toggle.get_attribute('aria-expanded') == 'false'
    assert page.evaluate('getComputedStyle(document.body).overflow') == original_overflow


@pytest.mark.parametrize('width', [1440, 390])
def test_standalone_modules_open_new_tabs_and_keep_console_page(console_browser, width):
    env = Environment(loader=FileSystemLoader(ROOT / 'bit/templates'))
    html = env.get_template('index.html').render(
        current_user={
            'id': 1, 'username': 'preview', 'display_name': '界面预览',
            'role_key': 'super_admin', 'role_name': '超级管理员',
            'permissions': ['*'], 'is_platform_admin': False,
        },
        can_change_task_workers=False, can_access_shop_status=True,
        runtime_role='server', url_for=lambda endpoint, filename: '/static/' + filename,
    )
    destinations = {
        '今日必办': '/mercado/today',
        '员工任务': '/employee-tasks',
        '支付管理': '/billing',
        'AI 账单': '/ai-billing',
    }
    context = console_browser.new_context(viewport={'width': width, 'height': 900})

    def route(request):
        path = urlsplit(request.request.url).path
        if path == '/':
            request.fulfill(body=html, content_type='text/html')
        elif path.startswith('/static/'):
            asset = ROOT / 'bit' / path.lstrip('/')
            if asset.is_file():
                request.fulfill(path=str(asset))
            else:
                request.fulfill(status=404, body='missing test asset')
        elif path in destinations.values():
            request.fulfill(body=f'<h1>{path}</h1>', content_type='text/html')
        else:
            request.fulfill(content_type='application/json', body=json.dumps({
                'status': 'success', 'success': True, 'data': [], 'items': [],
                'stores': [], 'groups': [], 'total': 0,
            }))

    context.route('**/*', route)
    page = context.new_page()
    page.goto('http://console.test/')
    page.locator('.nav-section').first.wait_for(state='attached')
    original_url = page.url

    for label, path in destinations.items():
        toggle = page.locator('#sidebar-toggle')
        if width <= 760 and toggle.get_attribute('aria-expanded') != 'true':
            toggle.click()
        page.get_by_role('searchbox', name='搜索功能').fill(label)
        link = page.locator(f'.sidebar .tab-button[href="{path}"]')
        assert link.is_visible()
        assert link.get_attribute('target') == '_blank'
        assert 'noopener' in (link.get_attribute('rel') or '').split()
        link.focus()
        assert link.evaluate('element => element === document.activeElement')
        with page.expect_popup() as opened:
            page.keyboard.press('Enter')
        popup = opened.value
        popup.wait_for_load_state('domcontentloaded')
        assert urlsplit(popup.url).path == path
        assert popup.evaluate('window.opener') is None
        assert page.url == original_url
        if width <= 760:
            assert toggle.get_attribute('aria-expanded') == 'false'
            assert toggle.evaluate('element => element === document.activeElement')
        popup.close()

    context.close()
