import json
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from urllib.parse import urlsplit

import pytest
from test_console_ui_browser import ROOT, console_browser, console_page

OUTPUT=ROOT/'artifacts/report-audit-20261011'


def test_risk_scan_defaults_to_one_source_and_one_hundred_items(console_page):
    assert console_page.locator('#risk-check-limit').input_value()=='100'
    assert console_page.locator('.risk-check-source:checked').evaluate_all('rows=>rows.map(row=>row.value)')==['product_list']


def test_analysis_polling_deadline_and_user_abort_end_loading(console_page):
    result=console_page.evaluate('''async () => {
      const original = window.fetch;
      window.fetch = async () => ({status:202});
      const started = Date.now();
      let timeout, cancelled;
      try { await fetchAnalysis('/pending', {deadlineMs:40}); } catch(e) { timeout=e.message; }
      const elapsed=Date.now()-started;
      const controller=new AbortController();controller.abort();
      try { await fetchAnalysis('/cancelled', {signal:controller.signal}); } catch(e) { cancelled=e.name; }
      window.fetch=original;
      return {timeout,cancelled,elapsed};
    }''')
    assert '稍后重新查询' in result['timeout']
    assert result['cancelled']=='AbortError'
    assert result['elapsed'] < 1000


def test_ads_currency_suppression_and_recovered_get(console_page):
    page=console_page
    calls=[]
    page.route('**/api/mercado-tokens',lambda r:r.fulfill(json={'status':'success','data':{'rows':[]}}))
    def serve(route):
        calls.append(route.request.headers)
        if len(calls)==1:return route.fulfill(status=502,body='temporary gateway failure')
        route.fulfill(json={'status':'success','data':{'server_paged':True,'snapshot_available':True,
            'page':1,'pages':1,'page_size':100,'total':1,
            'generated_at':'2026-09-29 16:03:09','summary':{'link_count':1,'account_count':1,
            'currencies':[],'unknown_currency_count':1},
            'accounts':[{'token_id':1,'advertiser_id':2,'site_id':'MLM','store_name':'测试广告账户',
                'currency_id':'','metrics':{'cost':675.54,'total_amount':2686.81,'roas':2686.81/675.54}}],
            'links':[{'token_id':1,'advertiser_id':2,'site_id':'MLM','ad_group_id':3,'item_id':'MLM123',
                'title':'示例广告商品','currency_id':'','metrics':{'cost':675.54,'total_amount':2686.81,'roas':2686.81/675.54}}]}})
    page.route('**/api/ad-analysis?*',serve)
    page.evaluate("document.querySelectorAll('.tab-page').forEach(e=>e.classList.remove('active')); document.getElementById('tab-ad-analysis').classList.add('active')")
    page.evaluate('loadAdAnalysis()')
    assert len(calls)==2 and calls[0]['x-analysis-async']=='1'
    assert '币种待核实' in page.locator('#ad-analysis-cost').inner_text()
    assert '暂不汇总' in page.locator('#ad-analysis-roas').inner_text()
    assert '已过期' in page.locator('#ad-analysis-message-freshness').inner_text()
    OUTPUT.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(OUTPUT/'ads-unknown-currency.png'),full_page=True)


def test_freshness_counts_mixed_old_and_unknown_and_uses_beijing(console_page):
    page=console_page
    result=page.evaluate('''() => DataFreshness.render('fund-latest-time',
      ['2026-07-27 08:00:00','2026-10-11T12:00:00Z',null],
      {hours:24,now:Date.parse('2026-10-11T14:00:00Z')})''')
    assert result=={'fresh':1,'stale':1,'unknown':1,'total':3}
    text=page.locator('#fund-latest-time-freshness').inner_text()
    assert '1/3' in text and '1 条数据已过期' in text and '1 条时间未知' in text
    assert '20:00:00' in text and '北京时间' in text


@pytest.mark.parametrize('width,height',[(1440,900),(1280,720),(373,844)])
def test_order_primary_layout_has_one_vertical_content_scroller(console_page,width,height):
    page=console_page
    page.set_viewport_size({'width':width,'height':height})
    page.evaluate("renderOrderRows(Array.from({length:50},(_,i)=>({id:String(i),title:'测试订单',amount:20})))")
    page.wait_for_timeout(80)
    assert page.locator('.order-filter-region').evaluate('e=>e.scrollHeight<=e.clientHeight+1')
    assert page.locator('#tab-orders .zs-advanced-filters').get_attribute('open') is None
    footer=page.locator('.order-footer-metrics').bounding_box()
    assert footer['y']+footer['height']<=height
    assert page.locator('.order-table-wrap').bounding_box()['height']>=100
    heading=page.locator('#tab-orders .order-heading-tools').bounding_box()
    assert heading['height']>=28 and heading['width']>=100
    sync=page.locator('#order-sync-open-button').bounding_box()
    assert sync['x']+sync['width']<=width
    OUTPUT.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(OUTPUT/f'orders-{width}.png'))


def test_collection_repair_queue_and_empty_copy(console_page):
    page=console_page
    page.evaluate('''() => { mercadoListMode='collection';renderMercadoListRows([
      {id:3,title:'旧规格商品',collection_quality_issues:[{field:'variations',message:'属性误当变体，请重新采集详情'}]}
    ]); }''')
    assert '本页待修复 1 件' in page.locator('#collection-repair-queue').inner_text()
    assert page.locator('#collection-repair-queue a').get_attribute('href')=='/mercado-collection/items/3'
    page.evaluate('renderMercadoListRows([])')
    assert '没有符合当前条件' in page.locator('#mercado-list-body').inner_text()
    assert page.locator('#collection-repair-queue').is_hidden()


def test_unread_risk_is_unknown_and_coverage_is_visible(console_browser):
    html=Environment(loader=FileSystemLoader(ROOT/'bit/templates')).get_template('infraction_dashboard.html').render(
      current_user={'username':'preview'},ip_rights_mode=False,embedded=True,url_for=lambda endpoint,filename:'/static/'+filename)
    page=console_browser.new_page()
    stores=[dict(token_id=1,store_name='待读取店铺',detection_count=0,rights_holder_count=0,total=0,last_read_at=None,last_synced_at=None,sites=[{'site_id':'MLM','total':0}])]
    def route(r):
        path=urlsplit(r.request.url).path
        if path=='/':return r.fulfill(body=html,content_type='text/html')
        if path.startswith('/static/'):return r.fulfill(path=str(ROOT/'bit'/path.lstrip('/')))
        if path.endswith('/dashboard'):
            return r.fulfill(json={'status':'success','data':{'summary':{'total':0},'account_groups':[{'group_name':'测试','salespeople':[{'salesperson':'甲','stores':stores}]}],'rows':[],'total':0,'filters':{}}})
        r.fulfill(json={'status':'success','data':{}})
    page.route('**/*',route);page.goto('http://risk.test/')
    page.wait_for_function("document.getElementById('metric-problems').textContent.includes('已成功读取')")
    assert '0/1' in page.locator('#metric-problems').inner_text()
    row=page.locator('.person-block tbody tr').first
    assert row.locator('td.count').all_inner_texts()==['—','—','—']
    assert '风险未知' in row.inner_text()
    page.close()
