"""Product list reviews use the same checks without real marketplace writes."""
import json
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_product_list_review_actions_and_visible_failure(console_page, width):
    page = console_page
    page.set_viewport_size({'width':width,'height':1000})
    errors, requests = [], []
    page.on('pageerror',lambda error:errors.append(str(error)))
    def publish(route):
        requests.append(route.request.post_data_json)
        route.fulfill(status=400,content_type='application/json',body=json.dumps({
            'status':'error','message':'复核未通过','data':{'review_issues':[{
                'product_item_id':11,'title':'模拟面具 <img>','issues':['SKU 白底主图未完成']}]}}))
    page.route('**/api/mercado-products/publish',publish)
    page.on('dialog',lambda dialog:dialog.accept())
    page.evaluate('''() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-mercado-products').classList.add('active');
        document.getElementById('mercado-list-products-host').append(document.getElementById('mercado-list-module'));
        mercadoListMode='products';
        document.getElementById('mercado-list-module').classList.remove('collection-mode');
        document.getElementById('mercado-list-module').classList.add('product-mode');
        document.getElementById('mercado-product-actions').style.display='flex';
        renderMercadoPublishStores([{id:7,site_id:'CBT',display_name:'模拟店铺'}]);
        document.getElementById('mercado-publish-store').options[0].selected=true;
        renderMercadoPublishPicker(document.getElementById('mercado-publish-store'));
        const rows=[11,12,13].map(id=>({id,source_type:'collected',title:'模拟面具 '+id,
            source_item_id:'MLM'+id,review_status:'approved',weight_g:100,net_proceeds_usd:10,
            can_operate:id!==13,last_publish_status:'failed',last_publish_error:'模拟平台问题：图片不合规'}));
        renderMercadoListRows(rows);
        loadMercadoList=async()=>{};
    }''')
    body = page.locator('#mercado-list-body')
    assert page.locator('#mercado-review-publish-selected').is_disabled()
    for identifier in [11,12]:
        checkbox=page.locator(f'.mercado-item-select[value="{identifier}"]')
        checkbox.focus();page.keyboard.press('Space')
    page.locator('#mercado-review-publish-selected').click()
    page.wait_for_function('!mercadoPublishSubmitting')
    assert requests[0]['product_item_ids']==[11,12]
    assert requests[0]['review_before_publish'] is True
    assert requests[0]['token_ids']==[7]
    panel=page.locator('#mercado-product-review-issues')
    assert 'SKU 白底主图未完成' in panel.inner_text()
    assert panel.locator('img').count()==0
    page.locator('#mercado-review-publish-page').click()
    page.wait_for_function('!mercadoPublishSubmitting')
    assert requests[1]['product_item_ids']==[11,12]
    row=page.locator('[data-product-id="11"]')
    assert '模拟平台问题：图片不合规' in row.inner_text()
    row.get_by_role('button',name='一键复核上架').click()
    page.wait_for_function('!mercadoPublishSubmitting')
    assert requests[2]['product_item_ids']==[11]
    assert page.locator('[data-product-id="13"] .mercado-product-review-one').is_disabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output=ROOT/'artifacts/publish-review-20261014';output.mkdir(parents=True,exist_ok=True)
    panel.scroll_into_view_if_needed()
    page.screenshot(path=str(output/f'products-{width}.png'))
    assert not errors


def test_product_review_requires_targets_and_announces_completed_issues(console_page):
    page=console_page
    requests=[]
    page.route('**/api/mercado-products/publish',lambda route:(requests.append(route.request.post_data_json),route.abort()))
    page.evaluate('''() => {
        mercadoListMode='products';
        mercadoListRows=[{id:11,title:'模拟商品',source_type:'collected',review_status:'approved'}];
        document.getElementById('mercado-publish-store').innerHTML='';
        selectedMercadoCollectionIds=new Set([11]);
        reviewMercadoProducts();
    }''')
    assert not requests
    assert '请先配置上架账号和目标站点' in page.locator('#mercado-list-message').text_content()
    assert page.locator('#mercado-publish-workspace').get_attribute('open') is not None
    page.evaluate('''() => renderMercadoPublishStatus({running:false,status:'completed',failed_count:1,
        results:[{status:'pending_review'}]})''')
    assert '1 件失败、1 件待复核' in page.locator('#mercado-list-message').text_content()
    page.evaluate('''() => {
        mercadoPublishRunning=true;
        updateMercadoSelection();
        reviewMercadoProducts([11]);
    }''')
    assert page.locator('#mercado-review-publish-selected').is_disabled()
    assert page.locator('#mercado-review-publish-page').is_disabled()
    assert not requests
