"""Shared ERP density must keep every workspace reachable and within its viewport."""
import pytest
from test_console_ui_browser import console_browser, console_page


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_all_console_workspaces_fit_viewport(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    tabs = page.locator('.tab-page').evaluate_all('(els) => els.map(el => el.id)')
    for tab in tabs:
        page.evaluate('''id => {
            document.querySelectorAll('.tab-page').forEach(el => el.classList.toggle('active', el.id === id));
            window.scrollTo(0, 0);
        }''', tab)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), tab


@pytest.mark.parametrize('width', [1440, 390])
def test_dark_navigation_contrast_search_and_toggle(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    if width <= 760:
        page.locator('.sidebar-toggle').click()
    sidebar = page.locator('.sidebar')
    assert sidebar.is_visible()
    contrast = sidebar.evaluate('''el => {
        const lum = color => {
            const rgb = color.match(/[\\d.]+/g).slice(0, 3).map(Number).map(v => {
                v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
            });
            return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
        };
        const bg = lum(getComputedStyle(el).backgroundColor);
        const text = lum(getComputedStyle(el.querySelector('.tab-button:not(.active)')).color);
        return {bg, ratio: (text + .05) / (bg + .05)};
    }''')
    assert contrast['bg'] < .05
    assert contrast['ratio'] >= 4.5
    search = page.get_by_role('searchbox', name='搜索功能')
    search.fill('库存')
    assert sidebar.locator('[data-tab="inventory"]').is_visible()
    assert not sidebar.locator('[data-tab="ai-video"]').is_visible()
    search.fill('')
    if width <= 760:
        page.keyboard.press('Escape')
        sidebar.wait_for(state='hidden')
    else:
        page.locator('.sidebar-toggle').click()
        assert sidebar.bounding_box()['width'] <= 74
        page.locator('.sidebar-toggle').click()
        assert search.is_visible()
