"""Offline UI inventory: expand every filter and visit each standalone template.

Use real templates and existing catalog fixtures. Every request is intercepted;
this audit never contacts a business service or submits an operation.
"""
import json
import re

import pytest
from jinja2 import Environment, FileSystemLoader

from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401
from test_catalog_workspace_browser import show_workspace
from test_mercado_collection_detail import detail_row  # noqa: F401
from bit.mercado_collection_detail import build_collection_detail

OUTPUT = ROOT / 'artifacts/module-ui-20261016/verified'
CATALOGS = {'1688-products', 'mercado-collection', 'mercado-products', 'ai-original-products'}
EMBEDDED = {
    'size-charts': 'size_charts.html', 'automation-center': 'automation_center.html',
    'clearance-alerts': 'clearance_alerts.html', 'product-diagnosis': 'product_diagnosis.html',
    'promotions': 'mercado_promotions.html', 'price-competition': 'price_competition.html',
    'ai-weight-price': 'ai_weight_price.html', 'infringement-dashboard': 'infraction_dashboard.html',
    'ip-rights-dashboard': 'infraction_dashboard.html',
}


def render_preview(template, **extra):
    return Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template(template).render(
        current_user={'id': 1, 'username': 'preview', 'display_name': '界面预览',
                      'role_key': 'super_admin', 'permissions': ['*']},
        can_view_all=True, billing_admin=True, can_execute=False, rules={}, providers=[],
        job_id='preview', detail=None, error='模拟资料暂无数据', csrf_token='offline',
        url_for=lambda endpoint, filename=None, **kwargs: '/static/' + filename if filename else '/',
        **extra,
    )


def settle(page):
    page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')


def inspect_filters(page, root, name, width):
    for summary in root.locator('.zs-advanced-filters > summary').all():
        if summary.is_visible() and not summary.evaluate('n => n.parentElement.open'):
            summary.click()
    settle(page)
    fields = []
    for control in root.locator('.zs-compact-filters input, .zs-compact-filters select, .zs-compact-filters .market-multi-trigger, .zs-compact-filters .store-trigger').all():
        if not control.is_visible() or control.is_disabled():
            continue
        info = control.evaluate('''n => ({id:n.id, type:n.type, tag:n.tagName,
            value:n.value, width:n.getBoundingClientRect().width})''')
        control.focus()
        if info['tag'] == 'SELECT':
            control.press('Alt+ArrowDown')
            control.press('Escape')
            assert control.input_value() == info['value'], (name, info)
        elif info['tag'] == 'BUTTON':
            control.click()
            for panel in root.locator('.market-multi-panel:visible, .store-menu.open').all():
                box = panel.bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1, (name, info, box)
            control.press('Escape')
        assert info['width'] > 0
        fields.append(info)
    settle(page)
    overflow = root.locator('.zs-compact-filters, .zs-advanced-grid').evaluate_all('''nodes => nodes
        .filter(n => n.getClientRects().length && n.scrollWidth > n.clientWidth + 2)
        .map(n => n.id || n.className)''')
    assert not overflow, (name, width, overflow)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (name, width)
    page.evaluate('window.scrollTo(0,0)')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUTPUT / f'{name}-{width}.png'), full_page=True)
    return {'page': name, 'width': width, 'fields': fields}


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_each_workspace_expanded_filters_and_pickers(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.emulate_media(reduced_motion='reduce')
    page.set_default_timeout(5000)
    for path, template in EMBEDDED.items():
        html = render_preview(template, embedded=True, ip_rights_mode=path == 'ip-rights-dashboard')
        page.route(re.compile(r'^http://console\.test/' + path + r'(?:\?.*)?$'),
                   lambda route, request, html=html: route.fulfill(body=html, content_type='text/html'))
    reports = []
    for tab in page.locator('.tab-page').evaluate_all('(ns) => ns.map(n => n.id)'):
        key = tab.removeprefix('tab-')
        if key in CATALOGS:
            show_workspace(page, key)
        else:
            page.evaluate('id => document.querySelectorAll(".tab-page").forEach(n => n.classList.toggle("active", n.id === id))', tab)
        settle(page)
        for frame in page.locator('#' + tab + ' iframe[data-src]').all():
            frame.evaluate('n => n.src = n.dataset.src')
            frame.content_frame.locator('body.zs-standalone').wait_for(state='attached')
        reports.append(inspect_filters(page, page.locator('#' + tab), tab, width))
        for subtab in page.locator('#' + tab + ' .inventory-view-tabs button, #' + tab + ' .knowledge-tabs button').all():
            if subtab.is_visible() and not subtab.is_disabled():
                name = subtab.get_attribute('data-inventory-view') or subtab.get_attribute('id')
                subtab.click()
                reports.append(inspect_filters(page, page.locator('#' + tab), tab + '-' + name, width))
    (OUTPUT / f'workspaces-{width}.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2))


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_standalone_templates_and_secondary_tabs(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.emulate_media(reduced_motion='reduce')
    page.set_default_timeout(5000)
    reports = []
    for template in sorted((ROOT / 'bit/templates').glob('*.html')):
        if template.name == 'index.html' or '<body' not in template.read_text():
            continue
        html = render_preview(template.name)
        page.route('**/ui-preview', lambda route: route.fulfill(body=html, content_type='text/html'))
        page.goto('http://console.test/ui-preview')
        settle(page)
        reports.append(inspect_filters(page, page.locator('body'), template.stem, width))
        # These controls only switch local views; never click submit/sync actions.
        tabs = page.locator('.tabs button[data-view], .awp-tabs button, .billing-tabs [role=tab]')
        for index in range(tabs.count()):
            tab = tabs.nth(index)
            if not tab.is_visible() or tab.is_disabled():
                continue
            tab.click()
            settle(page)
            reports.append(inspect_filters(page, page.locator('body'), f'{template.stem}-view-{index}', width))
    (OUTPUT / f'standalone-{width}.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2))


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_activity_store_picker_keyboard_and_selection(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    html = Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template('mercado_promotions.html').render(
        current_user={'id': 1, 'username': 'preview'},
        url_for=lambda endpoint, filename: '/static/' + filename,
    )
    page.route('**/ui-preview', lambda r: r.fulfill(body=html, content_type='text/html'))
    page.goto('http://console.test/ui-preview')
    page.evaluate("state.stores = [{id:1,name:'模拟墨西哥店铺',sites:[]}]; renderStores()")
    settle(page)
    trigger = page.locator('#storeTrigger')
    if not trigger.is_visible():
        page.locator('.zs-advanced-filters > summary').first.click()
    trigger.focus()
    trigger.press('ArrowDown')
    checkbox = page.locator('#storeMenu input')
    assert checkbox.evaluate('n => n === document.activeElement')
    checkbox.press('Space')
    assert checkbox.is_checked()
    checkbox.press('Escape')
    assert trigger.get_attribute('aria-expanded') == 'false'
    assert trigger.evaluate('n => n === document.activeElement')
    trigger.press('Enter')
    assert checkbox.is_checked()
    box = page.locator('#storeMenu').bounding_box()
    assert box['x'] >= 0 and box['x'] + box['width'] <= width
    trigger.press('Escape')


@pytest.mark.parametrize('width', [1440, 390])
def test_each_console_dialog_fits_and_closes(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.emulate_media(reduced_motion='reduce')
    reports = []
    for dialog_id in page.locator('dialog[id]').evaluate_all('nodes => nodes.map(n => n.id)'):
        dialog = page.locator('#' + dialog_id)
        if dialog_id == 'aip-amazon-config':
            # Simulate the platform choice that normally reveals this parent panel.
            page.evaluate('''() => {
                const select = document.getElementById('ai-original-platform');
                select.value = 'amazon';
                select.dispatchEvent(new Event('change', {bubbles:true}));
            }''')
        dialog.evaluate('''n => {
            const tab = n.closest('.tab-page');
            if (tab) document.querySelectorAll('.tab-page').forEach(p => p.classList.toggle('active', p === tab));
            n.showModal();
        }''')
        settle(page)
        box = dialog.bounding_box()
        assert box and box['x'] >= -1 and box['x'] + box['width'] <= width + 1, (dialog_id, box)
        assert box['y'] >= -1 and box['y'] + box['height'] <= 1001, (dialog_id, box)
        assert page.evaluate('id => !!document.activeElement.closest("#" + id)', dialog_id), dialog_id
        OUTPUT.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(OUTPUT / f'{dialog_id}-{width}.png'))
        page.keyboard.press('Escape')
        assert not dialog.evaluate('n => n.open'), dialog_id
        reports.append({'dialog': dialog_id, 'width': width, 'bounds': box})
    (OUTPUT / f'dialogs-{width}.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2))


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_collection_secondary_gallery_and_shared_finish(console_page, detail_row, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    html = Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template('mercado_collection_detail.html').render(
        detail=build_collection_detail(detail_row), error='',
        url_for=lambda endpoint, filename: '/static/' + filename,
    )
    page.route('**/ui-preview', lambda r: r.fulfill(body=html, content_type='text/html'))
    page.route('https://images.test/**', lambda r: r.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"><rect x="30" y="15" width="60" height="90" rx="10" fill="#2563eb"/></svg>'))
    page.goto('http://console.test/ui-preview')
    assert page.locator('body').get_attribute('class') == 'zs-standalone zs-collection-detail'
    thumbnail = page.locator('.detail-thumbnail').nth(1)
    thumbnail.focus()
    thumbnail.press('Enter')
    assert thumbnail.get_attribute('aria-pressed') == 'true'
    assert page.locator('#collection-image-position').inner_text().startswith('第 2 / 16 张')
    assert thumbnail.evaluate('n => getComputedStyle(n).outlineStyle') != 'none'
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert '完整描述最后一行' in page.locator('#description').inner_text()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.evaluate('window.scrollTo(0,0)')
    page.screenshot(path=str(OUTPUT / f'collection-secondary-gallery-{width}.png'))
