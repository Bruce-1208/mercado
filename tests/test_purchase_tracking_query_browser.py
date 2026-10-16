"""Real Chromium exercises of the shipped content scripts, with buyer pages mocked."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

EXTENSION = Path(__file__).resolve().parents[1] / 'browser_extension/zeshun_collector'
ORDER = '261009-557800569231682'
OTHER = '261009-673028555121287'


@pytest.fixture
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch(headless=True)
        yield instance
        instance.close()


def open_page(browser, html, *, direct=False, capture=False, platform='pdd'):
    page = browser.new_page()
    host = 'trade.1688.com' if platform == '1688' else 'mobile.yangkeduo.com'
    page.route(f'https://{host}/**', lambda route: route.fulfill(
        content_type='text/html; charset=utf-8', body='<meta charset="utf-8">' + html))
    if capture:
        page.add_init_script(path=str(EXTENSION / 'purchase-tracking-page.js'))
    page.goto(f'https://{host}/' + (f'order.html?order_sn={ORDER}' if direct else 'orders.html'))
    page.evaluate('window.chrome={runtime:{onMessage:{addListener:fn=>window.purchaseListener=fn}}}')
    page.add_script_tag(path=str(EXTENSION / 'purchase-tracking-content.js'))
    return page


def fetch(page, **extra):
    return page.evaluate('msg => new Promise(resolve => purchaseListener(msg, null, resolve))',
                         {'type': 'FETCH_PURCHASE_TRACKING', 'purchaseOrder': ORDER, **extra})


def test_missing_search_is_failure_not_skipped(browser):
    page = open_page(browser, '<h1>我的订单</h1><section>其他商品</section>')
    result = fetch(page)
    assert result['status'] == 'failed'
    assert result['fatal']
    assert '未执行查询' in result['message']
    page.close()


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('field', [
    '<label for="buyer-query">订单编号：</label><input id="buyer-query">',
    '<div><span>订单号：</span><span><input></span></div>',
    '<input name="tradeId">',
    '<input aria-label="订单号码">',
    '<span id="caption">订单编号</span><input aria-labelledby="caption">',
])
def test_1688_delayed_labelled_search_ignores_product_search(browser, width, field):
    page = open_page(browser, f'''<input id="product" placeholder="搜索商品"><button onclick="window.wrongSearch=true">搜索</button>
      <section id="buyer"></section><section id="result"></section>
      <script>
      window.loadBuyer = () => document.querySelector('#buyer').innerHTML = {json.dumps(field + '<button onclick="searchOrder()">查 询</button>')};
      function searchOrder() {{
        window.searched = document.querySelector('#buyer input').value;
        document.querySelector('#result').innerHTML = '订单编号：{ORDER}<p>物流单号：SF123456789</p>';
      }}
      </script>''', platform='1688')
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate('setTimeout(window.loadBuyer, 750)')
    result = fetch(page)
    assert result['status'] == 'synced'
    assert page.evaluate('window.searched') == ORDER
    assert page.evaluate('Boolean(window.wrongSearch)') is False
    assert page.locator('#product').input_value() == ''
    page.close()


def test_1688_expands_filters_and_submits_order_field(browser):
    page = open_page(browser, f'''<button onclick="document.querySelector('#filters').hidden=false;window.expanded=(window.expanded||0)+1">更多筛选</button>
      <section id="filters" hidden><label>订单号<input></label>
        <input type="button" value="查询" onclick="document.querySelector('#result').innerHTML='订单编号：{ORDER}<p>待发货</p>'">
      </section><section id="result"></section>''', platform='1688')
    assert fetch(page)['status'] == 'pending'
    assert page.evaluate('window.expanded') == 1
    page.close()


def test_1688_no_order_search_waits_then_fails_without_using_product_search(browser):
    page = open_page(browser, '<input placeholder="搜索商品"><button onclick="window.wrongSearch=true">搜索</button><p>没有符合条件的订单</p>', platform='1688')
    page.clock.install()
    page.evaluate('order => { window.resultPromise = new Promise(resolve => purchaseListener({type:"FETCH_PURCHASE_TRACKING",purchaseOrder:order},null,resolve)); }', ORDER)
    page.clock.run_for(13000)
    result = page.evaluate('window.resultPromise')
    assert result['status'] == 'failed'
    assert result['fatal']
    assert '未执行查询' in result['message']
    assert page.evaluate('Boolean(window.wrongSearch)') is False
    page.close()


def test_1688_login_expiry_during_search_wait_is_reported(browser):
    page = open_page(browser, '<section>订单列表加载中</section>', platform='1688')
    page.evaluate("setTimeout(() => document.querySelector('section').textContent='请先登录', 500)")
    assert fetch(page)['status'] == 'not_logged_in'
    page.close()


def test_clicks_search_and_waits_for_slow_result(browser):
    page = open_page(browser, f'''<input placeholder="订单号"><button onclick="searchOrder()">搜索</button><section id="result"></section>
      <script>function searchOrder() {{ window.clicked=true; setTimeout(() => {{
        document.querySelector('#result').innerHTML = '订单编号：{ORDER}<p>物流单号：SF123456789</p>';
      }}, 2200); }}</script>''')
    result = fetch(page)
    assert page.evaluate('window.clicked')
    assert result['status'] == 'synced'
    assert result['tracking_number'] == 'SF123456789'
    page.close()


def test_explicit_search_empty_result_is_not_found(browser):
    page = open_page(browser, '''<input placeholder="订单号"><button onclick="document.querySelector('section').textContent='没有符合条件的订单'">查询</button><section>旧结果</section>''')
    assert fetch(page)['status'] == 'not_found'
    page.close()


def test_stale_empty_result_is_query_failure(browser):
    page = open_page(browser, '<input placeholder="订单号"><p>没有符合条件的订单</p>')
    page.clock.install()
    page.evaluate('window.resultPromise = new Promise(resolve => purchaseListener({type:"FETCH_PURCHASE_TRACKING",purchaseOrder:"' + ORDER + '"},null,resolve))')
    page.clock.run_for(16000)
    result = page.evaluate('window.resultPromise')
    assert result['status'] == 'failed'
    page.close()


@pytest.mark.parametrize('body,status', [
    (f'<section>订单编号：{ORDER}<p>待发货</p></section>', 'pending'),
    ('<p>订单不存在</p>', 'not_found'),
    ('<p>安全验证，请完成验证</p>', 'failed'),
    ('<p>请先登录</p>', 'not_logged_in'),
])
def test_direct_detail_results(browser, body, status):
    page = open_page(browser, body, direct=True)
    assert fetch(page)['status'] == status
    page.close()


def test_pdd_real_page_fetch_response_and_packages_are_bound_to_order(browser):
    page = open_page(browser, '<p>订单详情</p>', direct=True, capture=True)
    page.route('**/api/order-fixture', lambda route: route.fulfill(json={'result': {
        'order_sn': ORDER, 'status_desc': '已发货',
        'packages': [{'tracking_number': 'SF123456789'}, {'tracking_number': 'YT123456789'},
                     {'order_sn': OTHER, 'tracking_number': 'SF999999999'}],
        'recommendations': [{'order_sn': OTHER, 'tracking_number': 'SF888888888'}],
    }}))
    page.evaluate("fetch('/api/order-fixture').then(r=>r.json())")
    result = fetch(page)
    assert result['status'] == 'synced'
    assert result['tracking_number'] == 'SF123456789;YT123456789'
    page.close()


def test_pdd_xhr_and_initial_data_do_not_overwrite_loaded_tracking(browser):
    page = open_page(browser, '<p>订单详情</p>', direct=True, capture=True)
    page.evaluate('(order)=>window.rawData={order_sn:order,status_desc:"待发货"}', ORDER)
    page.route('**/api/order-fixture', lambda route: route.fulfill(json={
        'order_sn': ORDER, 'tracking_number': 'SF123456789', 'status_desc': '已发货'}))
    page.evaluate("() => new Promise(resolve=>{const x=new XMLHttpRequest();x.open('GET','/api/order-fixture');x.onload=resolve;x.send()})")
    assert fetch(page)['tracking_number'] == 'SF123456789'
    page.close()


def test_reads_new_logistics_dialog_and_rejects_another_order(browser):
    page = open_page(browser, f'''<section>订单编号：{ORDER}<button onclick="document.querySelector('dialog').showModal()">查看物流</button></section>
        <dialog>订单编号：{OTHER}<p>物流单号：SF999999999</p></dialog>''', direct=True)
    result = fetch(page)
    assert result['status'] == 'failed'
    assert '不匹配' in result['message']
    page.close()


def test_matched_order_link_requests_navigation_not_pending(browser):
    page = open_page(browser, f'<section>订单编号：{ORDER}<a href="/fixture-logistics.html?order_sn={ORDER}">查看物流</a></section>', direct=True)
    result = fetch(page)
    assert result['status'] == 'navigate'
    assert result['bound_order'] == ORDER
    assert result['url'].endswith(f'fixture-logistics.html?order_sn={ORDER}')
    page.close()


def test_shipped_order_without_number_is_failure_not_unshipped(browser):
    page = open_page(browser, f'<section>订单编号：{ORDER}<p>已发货</p></section>', direct=True)
    page.clock.install()
    page.evaluate('window.resultPromise = new Promise(resolve => purchaseListener({type:"FETCH_PURCHASE_TRACKING",purchaseOrder:"' + ORDER + '"},null,resolve))')
    page.clock.run_for(16000)
    assert page.evaluate('window.resultPromise')['status'] == 'failed'
    page.close()


def test_installed_extension_queries_two_pdd_details_and_reads_native_responses(tmp_path, browser):
    """Run the real background + MAIN/isolated scripts in an isolated extension profile."""
    context = browser.browser_type.launch_persistent_context(str(tmp_path / 'profile'), headless=True, channel='chromium',
        args=[f'--disable-extensions-except={EXTENSION}', f'--load-extension={EXTENSION}'])
    visited = []
    def serve(route):
        url = route.request.url
        if 'mobile.yangkeduo.com/order.html?' in url:
            from urllib.parse import urlparse, parse_qs
            order = parse_qs(urlparse(url).query)['order_sn'][0]
            visited.append(order)
            body = f'''<meta charset="utf-8"><h1>订单详情</h1>
                <script>setTimeout(()=>fetch('/api/fixture?order_sn={order}').then(r=>r.json()),600)</script>'''
            route.fulfill(content_type='text/html; charset=utf-8', body=body)
        elif 'mobile.yangkeduo.com/api/fixture?' in url:
            from urllib.parse import urlparse, parse_qs
            order = parse_qs(urlparse(url).query)['order_sn'][0]
            number = 'SF123456789' if order == ORDER else 'YT123456789'
            route.fulfill(json={'result': {'order_sn': order, 'tracking_number': number, 'status_desc': '已发货'}})
        else:
            route.abort()
    context.route('https://**/*', serve)
    worker = next((item for item in context.service_workers if item.url.endswith('/background.js')), None)
    if worker is None:
        worker = context.wait_for_event('serviceworker', predicate=lambda item: item.url.endswith('/background.js'))
    # Chromium emits serviceworker before its extension execution context is ready.
    import time
    deadline = time.monotonic() + 5
    while not worker.evaluate("Boolean(globalThis.chrome?.tabs) && typeof queryPurchaseTrackingOrder === 'function'"):
        assert time.monotonic() < deadline, 'extension worker not ready'
        context.pages[0].wait_for_timeout(100)
    tab_id = worker.evaluate('async () => (await chrome.tabs.create({url:"about:blank",active:false})).id')
    result = worker.evaluate('async ({tabId, orders}) => queryPurchaseTrackingRow(tabId,"pdd",{purchase_orders:orders})',
                             {'tabId': tab_id, 'orders': [ORDER, OTHER]})
    assert visited == [ORDER, OTHER]
    assert result['status'] == 'synced'
    assert result['tracking_number'] == 'SF123456789;YT123456789'
    context.close()


def test_installed_extension_resumes_after_search_form_navigation(tmp_path, browser):
    order = '5127480146121008319'
    context = browser.browser_type.launch_persistent_context(str(tmp_path / 'form-profile'),
        headless=True, channel='chromium',
        args=[f'--disable-extensions-except={EXTENSION}', f'--load-extension={EXTENSION}'])
    visited = []
    def serve(route):
        url = route.request.url
        if url.startswith('https://trade.1688.com/order/buyer_order_list.htm'):
            route.fulfill(content_type='text/html; charset=utf-8', body='''<meta charset="utf-8">
              <form action="/order/query.html" method="get"><input name="orderId" placeholder="订单号"><button>查询</button></form>''')
        elif url.startswith('https://trade.1688.com/order/query.html'):
            visited.append(url)
            route.fulfill(content_type='text/html; charset=utf-8', body=f'<meta charset="utf-8"><section>订单编号：{order}<p>物流单号：SF123456789</p></section>')
        else:
            route.abort()
    context.route('https://**/*', serve)
    worker = next((item for item in context.service_workers if item.url.endswith('/background.js')), None)
    if worker is None:
        worker = context.wait_for_event('serviceworker', predicate=lambda item: item.url.endswith('/background.js'))
    # Chromium emits serviceworker before its extension execution context is ready.
    import time
    deadline = time.monotonic() + 5
    while not worker.evaluate("Boolean(globalThis.chrome?.tabs) && typeof queryPurchaseTrackingOrder === 'function'"):
        assert time.monotonic() < deadline, 'extension worker not ready'
        context.pages[0].wait_for_timeout(100)
    tab_id = worker.evaluate('async () => (await chrome.tabs.create({url:"about:blank",active:false})).id')
    result = worker.evaluate('async ({tabId, order}) => queryPurchaseTrackingOrder(tabId,"1688",order)',
                            {'tabId': tab_id, 'order': order})
    assert len(visited) == 1
    assert f'orderId={order}' in visited[0]
    assert result['tracking_number'] == 'SF123456789'
    context.close()
