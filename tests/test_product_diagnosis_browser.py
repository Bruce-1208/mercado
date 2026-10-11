"""Exercise the actual diagnosis UI offline, including permission and failure states."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader

from test_console_ui_browser import console_browser, console_page

ROOT = Path(__file__).resolve().parents[1]


def product():
    return {"link_id": 42, "token_id": 1, "site_id": "MLM", "item_id": "MLM123", "title": "便携式厨房电子秤",
        "store_name": "泽顺墨西哥旗舰店", "seller_sku": "SCALE-01", "risk": "high", "issue_count": 2,
        "quality_score": 69, "experience_score": 30, "checked_at": "2026-10-10T00:00:00+00:00",
        "available_quantity": 0, "listing_status": "active", "data_status": "partial", "stale": False,
        "sources": {"item": {"status": "ok"}, "quality": {"status": "ok"}, "experience": {"status": "restricted", "message": "店铺模式或应用权限不支持此接口"}},
        "issues": [{"key": "inventory:out_of_stock", "title": "在售商品可售库存为 0", "severity": "high",
            "source": "local_rule", "suggestion": "核对平台库存及在途补货，确认是否需要采购。", "evidence": "平台商品状态 active，可售库存 0。"},
            {"key": "quality:HAS_GTIN", "title": "商品编码待完善", "severity": "medium", "source": "official",
             "suggestion": "核对并补充真实 GTIN 商品编码。", "evidence": "Falta código", "official_url": "javascript:alert('BAD')"}]}


@pytest.fixture
def diagnosis_page(console_browser):
    context = console_browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    permissions = {"can_manage": True, "can_task": True}
    listing = {"rows": [product()], "total": 1, "summary": {"total": 1, "high": 1},
               "stores": [{"id": 1, "name": "泽顺墨西哥旗舰店"}], "runs": []}
    requests = []
    template = Environment(loader=FileSystemLoader(str(ROOT / "bit" / "templates")), autoescape=True).get_template("product_diagnosis.html")

    def route(route):
        path = urlsplit(route.request.url).path
        if path == "/product-diagnosis":
            html = template.render(url_for=lambda endpoint, filename: '/static/' + filename,
                                   current_user={"username": "operator"}, embedded=False, **permissions)
            route.fulfill(content_type="text/html", body=html)
        elif path.startswith('/static/'):
            file = ROOT / "bit" / path.lstrip('/')
            route.fulfill(content_type='text/css' if file.suffix == '.css' else 'application/javascript', body=file.read_text() if file.is_file() else '')
        elif path == '/api/product-diagnosis':
            requests.append(route.request.url)
            route.fulfill(content_type='application/json', body=json.dumps({"status": "success", "data": listing}))
        elif path == '/api/product-diagnosis/42':
            route.fulfill(content_type='application/json', body=json.dumps({"status": "success", "data": product()}))
        elif path.endswith('/today'):
            requests.append(route.request.url)
            route.fulfill(content_type='application/json', body=json.dumps({"status": "success", "data": {"task": {"id": 99}}}))
        elif path.endswith('/start'):
            requests.append(json.loads(route.request.post_data))
            route.fulfill(content_type='application/json', body=json.dumps({"status": "success", "data": {"run_id": "run-1"}}))
        else:
            route.fulfill(status=404, body='')

    context.route('**/*', route)
    page.goto('http://diagnosis.test/product-diagnosis')
    page.wait_for_selector('.pd-product')
    yield page, permissions, listing, requests
    context.close()


def test_detail_exposes_evidence_and_today_task(diagnosis_page):
    page, _, _, requests = diagnosis_page
    assert page.locator('#pd-high').inner_text() == '1'
    assert '部分数据待核对' in page.locator('.pd-product-risk').inner_text()
    page.get_by_role('button', name='查看诊断', exact=True).click()
    page.wait_for_selector('.pd-detail-issue')
    assert page.locator('#pd-dialog').is_visible()
    assert '接口受限' in page.locator('.pd-source-grid').inner_text()
    page.get_by_text('查看平台依据与原文', exact=True).first.click()
    assert '平台商品状态 active' in page.locator('.pd-detail-issue').first.inner_text()
    assert page.locator('#pd-dialog a[href^="javascript:"]').count() == 0
    page.get_by_role('button', name='加入今日必办', exact=True).click()
    page.wait_for_function("document.getElementById('pd-today').textContent === '已加入今日必办'")
    assert '内部建议处理时限' in page.locator('#pd-detail-message').inner_text()
    assert any('/42/today' in row for row in requests if isinstance(row, str))
    assert page.locator('#pd-today').is_disabled()
    assert '30 分' not in page.locator('.pd-source-grid').inner_text()


def test_filters_start_scope_and_readonly_permissions(diagnosis_page):
    page, permissions, _, requests = diagnosis_page
    page.locator('#pd-store').select_option('1')
    page.locator('#pd-site').select_option('MLM')
    page.get_by_role('button', name='诊断当前店铺范围', exact=True).click()
    page.wait_for_function("!document.getElementById('pd-start').disabled")
    assert {"token_ids": [1], "site_id": "MLM"} in requests
    page.locator('.pd-metric[data-risk="high"]').click()
    page.wait_for_function("document.querySelector('.pd-metric[data-risk=high]').getAttribute('aria-pressed') === 'true'")
    assert any(parse_qs(urlsplit(row).query).get('risk') == ['high'] for row in requests if isinstance(row,str))
    permissions.update(can_manage=False, can_task=False)
    page.reload()
    page.wait_for_selector('.pd-product')
    assert page.locator('#pd-start').is_disabled()
    page.get_by_role('button', name='查看诊断', exact=True).click()
    page.wait_for_selector('.pd-detail-issue')
    assert page.locator('#pd-today').is_disabled()


def test_mobile_layout_and_failure_preserves_existing_results(diagnosis_page, tmp_path):
    page, _, _, _ = diagnosis_page
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.get_by_role('button', name='查看诊断', exact=True).click()
    page.wait_for_selector('.pd-detail-issue')
    assert page.locator('#pd-dialog').bounding_box()['width'] <= 390
    page.locator('#pd-close').click()
    page.route('**/api/product-diagnosis?*', lambda route: route.fulfill(status=502, content_type='application/json', body=json.dumps({"status":"error","message":"平台数据暂时不可用"})))
    page.locator('#pd-refresh').click()
    page.wait_for_selector('#pd-error:visible')
    assert page.locator('.pd-product').count() == 1
    assert '平台数据暂时不可用' in page.locator('#pd-error').inner_text()


def test_desktop_mobile_visuals_and_untrusted_text(diagnosis_page, monkeypatch):
    page, _, listing, _ = diagnosis_page
    target = ROOT / 'artifacts' / 'product-diagnosis-20261010'
    target.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(target / 'desktop.png'), full_page=True)
    page.get_by_role('button', name='查看诊断', exact=True).click()
    page.wait_for_selector('.pd-detail-issue')
    page.locator('.pd-detail-issue details').first.click()
    page.screenshot(path=str(target / 'detail-desktop.png'), full_page=True)
    page.locator('#pd-close').click()
    page.set_viewport_size({'width': 390, 'height': 844})
    page.screenshot(path=str(target / 'mobile.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    listing['rows'][0]['title'] = '<img src=x onerror="window.injected=true">'
    page.locator('#pd-refresh').click()
    page.wait_for_function("document.querySelector('.pd-product-title').textContent.includes('onerror')")
    assert page.evaluate('window.injected === undefined')


def test_console_entry_is_grouped_searchable_and_lazy_loaded(console_page):
    page = console_page
    search = page.get_by_role('searchbox', name='搜索功能')
    search.fill('商品经营诊断')
    assert page.locator('.sidebar .tab-button:visible').count() == 1
    entry = page.locator('.sidebar [data-tab="product-diagnosis"]')
    assert entry.locator('..').locator('..').get_attribute('data-group') == 'products'
    assert not page.locator('#product-diagnosis-frame').get_attribute('src')
    entry.click()
    assert page.locator('#tab-product-diagnosis').is_visible()
    assert '/product-diagnosis?embedded=1' in page.locator('#product-diagnosis-frame').get_attribute('src')
