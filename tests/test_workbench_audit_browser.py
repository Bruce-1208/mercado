import json
from urllib.parse import parse_qs, urlsplit

from test_console_ui_browser import console_browser, console_page


def test_missing_cost_never_shows_full_profit(console_page):
    page = console_page
    page.evaluate('''() => {
        renderOrderRows([{id:'123', pack_id:'P123', store_id:1, cost:null, income:100,
            profit:null, amount:20, title:'缺少成本', quantity:1}]);
    }''')
    text = page.locator('#order-table-body').inner_text()
    assert '待核算' in text
    assert '100.0%' not in text
    page.evaluate('showOrderDetail(0)')
    assert '待核算' in page.locator('#order-detail-dialog').inner_text()
    # One incomplete child keeps the entire pack pending.
    assert page.evaluate('''() => orderFinancePending(mergeVisibleOrders([
        {id:'1',pack_id:'P',store_id:1,cost:10,income:100,profit:90},
        {id:'2',pack_id:'P',store_id:1,cost:null,income:100,profit:null}
    ])[0])''')
    assert not page.evaluate('orderFinancePending({cost:0,income:100,profit:100})')


def test_ad_server_pages_and_full_summary(console_page):
    page = console_page
    queries = []
    def respond(route):
        args = parse_qs(urlsplit(route.request.url).query)
        queries.append(args)
        number = int(args.get('page', ['1'])[0])
        route.fulfill(content_type='application/json', body=json.dumps({'status':'success','data': {
            'server_paged':True, 'page':number, 'pages':3, 'page_size':100, 'total':250,
            'snapshot_available':True, 'summary':{'link_count':250, 'account_count':1},
            'accounts':[], 'links':[{'token_id':1,'site_id':'MLM','ad_group_id':number,'item_id':f'ITEM{number}','title':f'第{number}页'}]
        }}))
    page.route('**/api/mercado-tokens**', lambda route: route.fulfill(content_type='application/json', body='{"status":"success","data":{"rows":[]}}'))
    page.route('**/api/ad-analysis?**', respond)
    page.evaluate('loadAdAnalysis()')
    assert page.locator('#ad-analysis-link-count').inner_text() == '250'
    assert page.locator('#ad-analysis-page-indicator').inner_text() == '1 / 3'
    page.evaluate('changeAdAnalysisPage(1)')
    page.wait_for_function("document.getElementById('ad-analysis-page-indicator').textContent === '2 / 3'")
    assert '第2页' in page.locator('#ad-analysis-link-body').inner_text()
    assert queries[-1]['page'] == ['2']
    assert queries[-1]['page_size'] == ['100']


def test_today_link_locates_exact_order(console_page):
    page = console_page
    seen = []
    def respond(route):
        args = parse_qs(urlsplit(route.request.url).query)
        seen.append(args)
        route.fulfill(content_type='application/json', body=json.dumps({'status':'success','data': {
            'rows':[{'id':'123','pack_id':'PACK123','store_id':9,'title':'目标订单','cost':None,'income':100,'profit':None}],
            'total':1,'page':1,'pages':1
        }}))
    page.route('**/api/orders?**', respond)
    page.goto('http://console.test/?tab=orders&order_id=123&store_id=9')
    page.evaluate("currentWorkbenchPermissions.add('*'); document.querySelector('.tab-button[data-tab=orders]').hidden = false; switchTab('orders')")
    page.wait_for_function("document.getElementById('order-detail-dialog').open")
    assert seen[-1]['search'] == ['123']
    assert seen[-1]['store_id'] == ['9']
    assert 'PACK123' in page.locator('#order-detail-title').inner_text()
