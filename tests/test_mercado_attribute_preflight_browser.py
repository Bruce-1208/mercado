import json

from test_console_ui_browser import console_browser, ROOT
from urllib.parse import urlsplit
import pytest
from jinja2 import Environment, FileSystemLoader


@pytest.fixture
def console_page(console_browser):
    html = Environment(loader=FileSystemLoader(ROOT / "bit/templates")).get_template("index.html").render(
        current_user={"username":"preview", "is_admin":True}, runtime_role="server",
        can_change_task_workers=False, url_for=lambda endpoint, filename: "/static/" + filename)
    page = console_browser.new_page(viewport={"width":1440,"height":1000})
    def route(route):
        path = urlsplit(route.request.url).path
        if path == "/":
            route.fulfill(body=html, content_type="text/html")
        elif path.startswith("/static/"):
            file = ROOT / "bit" / path.lstrip("/")
            if file.exists():
                route.fulfill(path=str(file))
            else:
                route.fulfill(status=404, body="")
        else:
            route.fulfill(content_type="application/json", body='{"status":"success","data":{},"items":[],"stores":[],"groups":[],"total":0}')
    page.route("**/*", route)
    page.goto("http://console.test/")
    yield page
    page.close()



@pytest.mark.parametrize("count,width", [(1, 1440), (51, 1440), (100, 390)])
def test_preflight_shows_reports_and_invalidates_after_settings_change(console_page, count, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    calls = []
    def preview(route):
        calls.append(route.request.post_data_json)
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": {
            "results": [{"product_id": 1, "source_item_id": "MLM123", "revision": "saved-revision",
                         "preflight": {"status": "ready", "issues": [{"code": "virtual_ean", "scope": "商品",
                             "attribute_id": "GTIN", "severity": "warning",
                             "message": "自动生成虚拟 EAN-13：2000000000015；未经 GS1 注册"}], "mappings": [
                             {"scope": "商品", "source": {"id": "BRAND", "value_name": "Real brand"},
                              "target": {"id": "BRAND", "value_name": "Real brand"}, "rule": "source_schema_to_target_id"}]}}]}}))
    page.route("**/api/mercado-products/attribute-preflight", preview)
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-mercado-collection').classList.add('active');
        mercadoListMode = 'products';
        selectedMercadoCollectionIds.add(1);
        mercadoStoreTokenRows = [{id:7, site_id:'CBT', display_name:'测试店铺', site_settings:[]}];
        mercadoPublishMode.value = 'accounts';
        mercadoPublishStore.innerHTML = '<option value="7" selected>测试店铺</option>';
        mercadoPublishSite.innerHTML = '<option value="MLM" selected>墨西哥</option>';
        mercadoPublishQuantity.value = 1; mercadoPublishRatio.value = 100;
        window.syncConsoleWorkflow();
        const card = document.getElementById('mercado-product-actions');
        card.closest('.tab-page').classList.add('active');
        card.style.display = 'block';
        card.closest('#mercado-list-module').classList.add('products-mode');
        document.getElementById('mercado-publish-workspace').open = true;
    }""")
    page.evaluate("count => { for (let id = 1; id <= count; id++) selectedMercadoCollectionIds.add(id); }", count)
    page.locator("#mercado-attribute-preflight-run").focus()
    page.keyboard.press("Enter")
    page.wait_for_function("document.body.textContent.includes('属性预检：可上架')")
    page.locator('.publish-record-details summary').first.focus()
    page.keyboard.press('Enter')
    assert '自动生成虚拟 EAN-13：2000000000015' in page.locator('body').inner_text()
    assert calls[0]["product_item_ids"] == list(range(1, count + 1))
    assert page.evaluate("window.mercadoAttributeDraftRevisions()") == {"1:7:MLM": "saved-revision"}
    page.evaluate("mercadoPublishQuantity.value = 2")
    assert page.evaluate("() => {try {window.mercadoAttributeDraftRevisions();} catch(e) {return e.message;}}") == '所选商品或上架设置已变化，请重新检查上架属性'


def test_report_escapes_untrusted_collected_values(console_page):
    page = console_page
    html = page.evaluate("""() => window.renderMercadoAttributeReport({preflight:{status:'mapping_conflict',
        issues:[{scope:'SKU 1', message:'<img src=x onerror=alert(1)>',severity:'error'}], mappings:[]}})""")
    assert '<img' not in html
    assert '&lt;img' in html
    assert '映射冲突' in html


@pytest.mark.parametrize("width", [1440, 390])
def test_user_can_complete_a_missing_attribute_and_recheck(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    calls = []
    def preview(route):
        data = route.request.post_data_json
        calls.append(data)
        corrected = bool(data.get('corrections'))
        result = {"product_id": 1, "source_item_id": "MLM123", "preflight": {
            "status": "ready" if corrected else "needs_completion", "mappings": [],
            "issues": [] if corrected else [{"scope":"商品", "attribute_id":"MODEL", "severity":"error", "message":"缺少型号"}]}}
        if corrected:
            result['revision'] = 'corrected-draft'
        else:
            result['correction_fields'] = [{"category_id":"MLM1", "attribute_id":"MODEL", "sku_index":None,
                                           "scope":"商品", "name":"型号", "choices":[], "combination":False}]
        route.fulfill(content_type='application/json', body=json.dumps({'status':'success','data':{'results':[result]}}))
    page.route('**/api/mercado-products/attribute-preflight', preview)
    page.evaluate("""() => {
        mercadoListMode = 'products'; selectedMercadoCollectionIds.add(1);
        mercadoStoreTokenRows = [{id:7, site_id:'CBT', display_name:'测试店铺', site_settings:[]}];
        mercadoPublishMode.value = 'accounts';
        mercadoPublishStore.innerHTML = '<option value="7" selected>测试店铺</option>';
        mercadoPublishSite.innerHTML = '<option value="MLM" selected>墨西哥</option>';
        mercadoPublishQuantity.value = 1; mercadoPublishRatio.value = 100;
        window.syncConsoleWorkflow();
        const card = document.getElementById('mercado-product-actions');
        card.closest('.tab-page').classList.add('active'); card.style.display = 'block';
        card.closest('#mercado-list-module').classList.add('products-mode');
        document.getElementById('mercado-publish-workspace').open = true;
    }""")
    page.locator('#mercado-attribute-preflight-run').click()
    page.get_by_label('型号', exact=True).fill('M-123')
    page.get_by_label('属性依据', exact=True).fill('包装标签')
    folder = ROOT / 'artifacts' / 'mercado-attribute-preflight'
    folder.mkdir(parents=True, exist_ok=True)
    panel = page.locator('.market-attribute-preflight')
    panel.screenshot(path=str(folder / f'correction-{width}.png'))
    assert panel.evaluate('el => el.scrollWidth <= el.clientWidth + 1')
    page.locator('#mercado-attribute-preflight-run').click()
    page.wait_for_function("document.body.textContent.includes('属性预检：可上架')")
    correction = calls[-1]['corrections']['1'][0]
    assert correction['value_name'] == 'M-123'
    assert correction['evidence'] == '包装标签'
    assert page.evaluate('window.mercadoAttributeDraftRevisions()') == {'1:7:MLM': 'corrected-draft'}
