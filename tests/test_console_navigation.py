"""Offline grouped navigation behavior using the real console template."""
from test_console_ui_browser import console_browser, console_page


def test_navigation_search_collapse_and_permissions(console_page):
    page = console_page
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
    page.set_viewport_size({'width': 390, 'height': 844})
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
    assert page.evaluate("getComputedStyle(document.body).overflow !== 'hidden'")
