import json
from pathlib import Path
import pytest
from test_console_ui_browser import console_browser, console_page


@pytest.mark.parametrize('width',[1440,390])
def test_progressive_metrics_preserves_focus_and_selection(console_page,width):
    page=console_page
    page.set_viewport_size({'width':width,'height':1000})
    page.evaluate('''() => {
        document.querySelectorAll('.tab-page').forEach(el=>el.classList.remove('active'));
        document.getElementById('tab-store-links').classList.add('active');
        storeLinkLoadRequestId = 100;
        renderStoreLinkRows([{id:1,item_id:'MLM1',title:'模拟商品',status:'active',sold_quantity:0}]);
        selectedStoreLinkIds.add(1);
        renderStoreLinkRows(storeLinkRows);
        document.querySelector('.store-link-row-select').focus();
    }''')
    page.route('**/api/store-links/metrics?*', lambda route:route.fulfill(content_type='application/json',body=json.dumps({
        'status':'success','data':[{'id':1,'sold_quantity':25,'total_visits':0,'visits_error':'',
        'quality_score':89,'quality_error':'','available_promotions':[{'promotion_fk':1,'promotion_id':'P1','promotion_type':'DEAL','name':'模拟可报名活动','status_raw':'candidate'}],
        'enrolled_promotions':[{'promotion_fk':2,'promotion_id':'P2','promotion_type':'DEAL','name':'模拟已参与活动','status_raw':'started'}]}]})))
    page.evaluate('StoreLinkMetrics.load(storeLinkRows,100)')
    assert page.locator('[data-store-link-metric="sales"]').inner_text()=='25'
    assert page.locator('[data-store-link-metric="visits"]').inner_text()=='0'
    assert '89' in page.locator('[data-store-link-metric="quality"]').inner_text()
    assert '模拟可报名活动' in page.locator('[data-store-link-metric="available"]').inner_text()
    assert '模拟已参与活动' in page.locator('[data-store-link-metric="enrolled"]').inner_text()
    assert page.locator('.store-link-row-select').is_checked()
    assert page.evaluate("document.activeElement.classList.contains('store-link-row-select')")
    output=Path('artifacts/store-link-metrics-20261012');output.mkdir(parents=True,exist_ok=True)
    page.locator('[data-store-link-metric="enrolled"]').scroll_into_view_if_needed()
    page.screenshot(path=str(output/f'metrics-{width}.png'),full_page=True)


def test_stale_metrics_cannot_replace_new_filter_results(console_page):
    page=console_page
    page.evaluate('''async () => {
        document.querySelectorAll('.tab-page').forEach(el=>el.classList.remove('active'));
        document.getElementById('tab-store-links').classList.add('active');
        storeLinkLoadRequestId=200;
        renderStoreLinkRows([{id:1,item_id:'MLM1',sold_quantity:2}]);
        const oldFetch=window.fetch;
        let resolve;
        window.fetch=()=>new Promise(r=>resolve=r);
        const task=StoreLinkMetrics.load(storeLinkRows,200);
        storeLinkLoadRequestId=201;
        renderStoreLinkRows([{id:2,item_id:'MLM2',sold_quantity:7}]);
        resolve({ok:true,json:async()=>({status:'success',data:[{id:1,sold_quantity:999}]})});
        await task;
        window.fetch=oldFetch;
    }''')
    assert page.locator('[data-store-link-metric="sales"]').inner_text()=='7'
