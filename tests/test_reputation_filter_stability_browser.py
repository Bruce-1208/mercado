"""Reputation filters must settle after entry and viewport changes (offline)."""
import pytest
from test_console_ui_browser import console_browser, console_page


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_reputation_filter_layout_settles(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(n => n.classList.toggle('active', n.id === 'tab-reputation'));
        reputationRows = [{'店铺名': '模拟店铺', '站点': '墨西哥', '七天单量': 12}];
        renderLatestReputationRows();
    }""")
    samples = page.evaluate("""async () => {
        const panel = document.querySelector('#tab-reputation .reputation-filter-controls');
        const samples = [];
        for (let i = 0; i < 40; i++) {
            await new Promise(requestAnimationFrame);
            samples.push([panel.clientWidth, panel.clientHeight, panel.style.gridTemplateColumns]);
        }
        return samples.slice(20);
    }""")
    assert len({tuple(sample) for sample in samples}) == 1, samples
    panel = page.locator('#tab-reputation .reputation-filter-controls')
    assert panel.evaluate('(p) => p.scrollWidth <= p.clientWidth + 2')
    search = page.locator('#reputation-name-search')
    if not search.is_visible():
        panel.locator('summary').click()
    search.fill('模拟')
    assert search.evaluate('(n) => n === document.activeElement')
    assert page.locator('#reputation-body tr').count() == 1
    checkbox = page.locator('#reputation-select-all')
    checkbox.check()
    assert '1' in page.locator('#reputation-selection-summary').inner_text()
    summary = panel.locator('summary')
    summary.focus()
    page.keyboard.press('Enter')
    page.keyboard.press('Escape')
    assert summary.evaluate('(n) => n === document.activeElement')


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_api_reputation_filter_layout_settles(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(n => n.classList.toggle('active', n.id === 'tab-api-reputation'));
        apiReputationRows = [{store_name: '模拟店铺', site_name: '墨西哥', sales_completed: 12}];
        renderApiReputationScopeOptions();
        renderApiReputationTable();
    }""")
    samples = page.evaluate("""async () => {
        const panel = document.querySelector('#tab-api-reputation .reputation-filter-controls');
        const samples = [];
        for (let i = 0; i < 40; i++) {
            await new Promise(requestAnimationFrame);
            samples.push([panel.clientWidth, panel.clientHeight, panel.style.gridTemplateColumns]);
        }
        return samples.slice(20);
    }""")
    assert len({tuple(sample) for sample in samples}) == 1, samples
    panel = page.locator('#tab-api-reputation .reputation-filter-controls')
    assert panel.evaluate('(p) => p.scrollWidth <= p.clientWidth + 2')
    search = page.locator('#api-reputation-search')
    if not search.is_visible():
        panel.locator('summary').click()
    search.fill('模拟')
    assert search.evaluate('(n) => n === document.activeElement')
    assert page.locator('#api-reputation-body tr').count() == 1
    picker = page.locator('#api-reputation-store-filter-picker')
    if not picker.is_visible():
        panel.locator('summary').click()
    picker.locator('.market-multi-trigger').click()
    picker.locator('.market-multi-options input[type=checkbox]').first.check()
    picker.locator('.market-multi-trigger').click()
    selected = page.locator('#api-reputation-store-filter').evaluate('(n) => [...n.selectedOptions].map(o => o.value)')
    assert selected == ['模拟店铺']
    page.locator('#api-reputation-select-visible').check()
    assert page.locator('#api-reputation-body input[type=checkbox]').first.is_checked()
    page.set_viewport_size({'width': 390 if width > 600 else 1440, 'height': 1000})
    page.evaluate("""() => {
        document.getElementById('tab-api-reputation').classList.remove('active');
        document.getElementById('tab-reputation').classList.add('active');
    }""")
    page.evaluate('() => new Promise(requestAnimationFrame)')
    page.evaluate("""() => {
        document.getElementById('tab-reputation').classList.remove('active');
        document.getElementById('tab-api-reputation').classList.add('active');
    }""")
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert page.locator('#api-reputation-store-filter').evaluate('(n) => [...n.selectedOptions].map(o => o.value)') == selected
    assert panel.evaluate('(p) => p.scrollWidth <= p.clientWidth + 2')
    summary = panel.locator('summary')
    summary.focus()
    if not panel.locator('details').evaluate('(n) => n.open'):
        page.keyboard.press('Enter')
    page.keyboard.press('Escape')
    assert summary.evaluate('(n) => n === document.activeElement')
    assert not panel.locator('details').evaluate('(n) => n.open')
