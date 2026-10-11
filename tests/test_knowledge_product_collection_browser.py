import json
from urllib.parse import urlsplit

import pytest
from test_console_ui_browser import console_browser, console_page  # noqa: F401
from test_infringement_knowledge_browser import open_knowledge
from bit.knowledge_product_collection import PageReader


@pytest.mark.parametrize('source,width', [('zying', 1440), ('lanjing', 390)])
def test_collection_ui_requires_confirmation_and_renders_results(console_page, source, width):
    page = console_page
    open_knowledge(page)
    task = {'phase': 'idle', 'running': False}
    writes, errors = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    def api(route):
        path = urlsplit(route.request.url).path
        if route.request.method == 'POST':
            action = path.rsplit('/', 1)[1]; writes.append(action)
            task.update(source=source, running=True, phase='waiting_login' if action == 'open' else 'collecting', message='请确认登录' if action == 'open' else '正在读取')
            data = dict(task)
        elif path.endswith('status'):
            data = dict(task)
        else:
            data = {'rows': [{'source_name': '蓝鲸选品' if source == 'lanjing' else '智赢',
                'product_id': 'MLM123', 'title': '<script>安全标题</script>', 'sales': 0, 'price': 0,
                'currency': 'MXN', 'brand': 'Sample Brand', 'listed_at': '2026-10-09',
                'subcategory': 'Accessories', 'site': 'MLM', 'storage_mode': '自发货',
                'main_image_url': 'javascript:alert(1)', 'product_url': 'javascript:alert(1)'}],
                'page': 1, 'pages': 1, 'total': 1}
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': data}))
    page.route('**/api/infringement-knowledge/collection/**', api)
    page.locator('#knowledge-collection-tab').click()
    page.set_viewport_size({'width': width, 'height': 1000})
    page.locator('#knowledge-collection-source').select_option(source)
    assert page.locator('#knowledge-collection-confirm').is_disabled()
    page.locator('#knowledge-collection-open').click()
    page.wait_for_function("!document.getElementById('knowledge-collection-confirm').disabled")
    assert writes == ['open']
    page.locator('#knowledge-collection-confirm').click()
    page.wait_for_function("document.getElementById('knowledge-collection-status').textContent.includes('正在读取')")
    assert writes == ['open', 'confirm-login']
    page.wait_for_function("document.getElementById('knowledge-collection-body').textContent.includes('Sample Brand')")
    cells = page.locator('#knowledge-collection-body td')
    assert cells.nth(1).inner_text() == '0'
    assert cells.nth(2).inner_text() == '0 MXN'
    assert not page.locator('#knowledge-collection-body script, #knowledge-collection-body a, #knowledge-collection-body img').count()
    assert not errors
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


@pytest.mark.parametrize('source', ['zying', 'lanjing'])
def test_live_page_adapters_use_official_clients_and_exact_filters(console_browser, source):
    page = console_browser.new_page()
    calls = []
    origin = 'https://meli.zying.net' if source == 'zying' else 'https://xp.lingdongsz.com'
    main = '''export const api={getMeliItemsByPage: async filters => {
        const response = await fetch('/listing', {method:'POST',body:JSON.stringify(filters)});
        return response.json(); }};'''
    blue = '''export function p(data) { return fetch(data.url,{method:'POST',body:JSON.stringify(data)}).then(r=>r.json()); }'''
    table = """import{p as client}from'./main.js';
        const resDataMap = x=>x, pageNo=1;
        function query(){return client({'url':'/web/items/search','siteId':'MLM',pageNo,pageSize:50});}
        document.querySelector('button').onclick=query;"""
    def route(request):
        path = urlsplit(request.request.url).path
        if path == '/':
            html = '<div>仓储模式</div><button>自发货</button>' if source == 'zying' else '<button>查询</button>'
            html = '<meta charset="utf-8">' + html + '<script type="module" src="/assets/main.js"></script>'
            if source == 'lanjing': html += '<script type="module" src="/assets/table.js"></script>'
            request.fulfill(content_type='text/html', body=html)
        elif path == '/assets/main.js':
            request.fulfill(content_type='application/javascript', body=main if source == 'zying' else blue)
        elif path == '/assets/table.js':
            request.fulfill(content_type='application/javascript', body=table)
        else:
            calls.append(json.loads(request.request.post_data))
            if path == '/web/items/findOne':
                payload = {'code': 0, 'data': {'id': '1', 'brand_id': ['Detail Brand'],
                           'pictures': ['https://img.test/1.jpg'], 'category_id': 'leaf',
                           'path_from_root': [{'id': 'root', 'name': 'Parent'}, {'id': 'leaf', 'name': 'Child'}]}}
            else:
                payload = {'code': 200, 'data': {'Datas': [{'Sku': '1', 'Title': 'Product'}], 'Total': 1}} if source == 'zying' else {'code': 0, 'data': {'list': [{'id': '1', 'title': 'Product'}], 'total': 1}}
            request.fulfill(content_type='application/json', body=json.dumps(payload))
    page.route('**/*', route)
    try:
        page.goto(origin)
        reader = PageReader(page, source)
        reader.prepare()
        rows, total = reader.listing(2)
        assert total == 1
        if source == 'zying':
            assert calls == [{'Status': 8, 'StockType': 1, 'page': 2, 'pagesize': 50}]
        else:
            assert calls[-2] == {'url': '/web/items/search', 'siteId': 'MLM', 'storeStatusVol': 'under_review',
                                'sortKey': 'sold_his', 'sortOrder': 'desc', 'pageNo': 2, 'pageSize': 50}
            assert calls[-1] == {'url': '/web/items/findOne', 'siteId': 'MLM', 'itemId': '1'}
            assert rows[0]['brand_id'] == ['Detail Brand']
            assert rows[0]['pictures'] == ['https://img.test/1.jpg']
    finally:
        page.close()
