"""Offline publication layout and picker regression coverage."""
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390, 320])
def test_publish_settings_layout(console_page, width):
    page = console_page
    page.set_default_timeout(5000)
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate('''() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-mercado-products').classList.add('active');
        mercadoListMode = 'products';
        mercadoListRows = [{id:11, title:'布局测试商品', review_status:'approved', source_type:'collected'}];
        selectedMercadoCollectionIds = new Set([11]);
        renderMercadoPublishStores([
            {id:7, site_id:'CBT', display_name:'测试店铺甲 · 跨境商品上架账号'},
            {id:8, site_id:'CBT', display_name:'测试店铺乙 · 跨境商品上架账号'}
        ]);
        document.getElementById('mercado-product-actions').style.display='flex';
        const module = document.getElementById('mercado-list-module');
        module.classList.remove('collection-mode'); module.classList.add('product-mode');
        updateMercadoSelection();
        document.getElementById('mercado-product-actions').closest('.tab-page').classList.add('active');
    }''')
    page.locator('#mercado-publish-next').click()
    workspace = page.locator('#mercado-publish-workspace')
    workspace.scroll_into_view_if_needed()
    # Every field and control stays inside its grid cell and the viewport.
    assert workspace.evaluate('''el => [...el.querySelectorAll('.market-publish-field')]
        .filter(f => f.offsetWidth).every(f => {
            const r=f.getBoundingClientRect();
            const c=f.querySelector('input, .market-multi-trigger, select').getBoundingClientRect();
            return r.left >= 0 && r.right <= innerWidth && c.left >= r.left - 1 && c.right <= r.right + 1;
        })''')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    trigger = page.locator('#mercado-publish-store-trigger')
    page.locator('#mercado-publish-mode').focus()
    page.keyboard.press('Tab')
    assert trigger.evaluate("el => el === document.activeElement && getComputedStyle(el).outlineStyle !== 'none'")
    page.keyboard.press('Enter')
    panel = page.locator('#mercado-publish-store-picker .market-multi-panel')
    assert panel.is_visible()
    bounds = panel.bounding_box()
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
    options = panel.locator('input[type=checkbox]')
    options.nth(0).check()
    options.nth(1).check()
    assert page.locator('#mercado-publish-store option:checked').count() == 2
    output = ROOT / 'artifacts/publish-settings-layout-20261013'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'picker-{width}.png'))
    page.keyboard.press('Escape')
    assert not panel.is_visible()
    site = page.locator('#mercado-publish-site-trigger')
    site.click()
    site_panel = page.locator('#mercado-publish-site-picker .market-multi-panel')
    bounds = site_panel.bounding_box()
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
    page.keyboard.press('Escape')
    workspace.scroll_into_view_if_needed()
    workspace.evaluate("el => el.scrollIntoView({block: 'start'})")
    page.screenshot(path=str(output / f'settings-{width}.png'))
    page.get_by_role('button', name='返回选择商品', exact=True).click()
    assert workspace.get_attribute('open') is None
    assert page.locator('#mercado-publish-store option:checked').count() == 2
