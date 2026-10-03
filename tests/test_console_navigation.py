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
    assert page.locator('.sidebar .nav-label:visible').count() > 0
    assert page.evaluate('document.documentElement.scrollWidth') == 390
