import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 390])
def test_check_backgrounds_and_single_repair_preserve_selection(console_page, width):
    page=console_page
    page.set_viewport_size({'width':width,'height':1000})
    requests=[]
    def process(route):
        requests.append(route.request.post_data_json)
        route.fulfill(json={'status':'success','data':{'status':'idle','running':False}})
    page.route('**/api/ai-original-products/process',process)
    page.evaluate('''() => {
      document.querySelectorAll('.tab-page').forEach(el=>el.classList.remove('active'));
      document.getElementById('tab-ai-original-products').classList.add('active');
      const ready={id:1,ai_status:'completed',review_status:'approved',category_id:'CBT123',
        title_es:'Cup',title_pt:'Copo',description_es:'Cup description',description_pt:'Copo description',
        weight_g:100,net_proceeds_usd:10,package_length_cm:10,package_width_cm:10,package_height_cm:10,
        main_image_url:'/test-ai-white.jpg',original_1688:{title:'模拟检查商品'},
        ai_original:{status:'completed',category_id:'CBT123',image_generation_method:'local_background_removal',attributes:[{id:'MATERIAL',value_name:'Steel'}],validation:{errors:[],warnings:[]}}};
      aiOriginalRows=[ready,{...ready,id:2,net_proceeds_usd:null},
        {...ready,id:3,net_proceeds_usd:null,category_id:''},
        {...ready,id:4,ai_original:{...ready.ai_original,missing_required_attributes:[{id:'MODEL'}]}}];
      aiOriginalSelected=new Set([1,2]);
      renderAiOriginalProducts(); decorateAiOriginalWorkbench();
    }''')
    assert page.evaluate('aiOriginalRows.map(row=>aiOriginalProductStatus(row))')==['approved','incomplete','failed','failed']
    cards=page.locator('#ai-original-grid .ai-original-card')
    assert '11/11' in cards.nth(0).locator('.ai-original-check-panel summary').text_content()
    assert '10/11' in cards.nth(1).locator('.ai-original-check-panel summary').text_content()
    assert 'needs-input' in cards.nth(1).locator('.ai-original-check-panel').get_attribute('class')
    assert cards.nth(2).locator('.ai-original-check-panel').evaluate("el=>getComputedStyle(el).backgroundColor")=='rgb(254, 242, 242)'
    assert cards.nth(3).locator('.ai-original-check-panel').get_attribute('class').find('has-failure')>=0
    cards.nth(1).get_by_role('button',name='修复此商品').click()
    assert not requests
    assert '待补充' in page.locator('#ai-original-task-status').inner_text()
    repair=cards.nth(2).get_by_role('button',name='修复此商品')
    repair.focus();page.keyboard.press('Enter')
    page.wait_for_function("document.querySelector('#ai-original-task-status').textContent.includes('AI 原创任务')")
    assert len(requests)==1 and requests[0]['product_item_ids']==[3]
    assert page.evaluate('[...aiOriginalSelected]')==[1,2]
    cards.nth(2).locator('.ai-original-check-panel summary').click()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output=ROOT/'artifacts/ai-original-single-repair-20261013';output.mkdir(exist_ok=True)
    page.screenshot(path=str(output/f'checks-{width}.png'))


def test_missing_sku_source_must_finish_recollection_before_ai(console_page):
    page=console_page
    posted=[]
    page.route('**/api/ai-original-products/process',lambda route: (posted.append(route.request.post_data_json),route.fulfill(json={'status':'success','data':{'status':'idle'}})))
    page.evaluate('''() => {
      aiOriginalRows=[{id:9,ai_status:'failed',original_1688:{variations:[{sku_id:'11'}]},
        ai_original:{status:'failed',variations:[{sku_id:'11'}]}},
        {id:77,ai_status:'pending',original_1688:{title:'保留勾选的模拟商品'}}];
      aiOriginalSelected=new Set([77]);
      window.recollectCount=0;
      recollectAiOriginalProduct=async id => {recollectCount++; return false;};
    }''')
    page.evaluate('repairAiOriginalProduct(9)')
    assert not posted
    assert page.evaluate('recollectCount')==1
    page.evaluate('''() => {
      recollectAiOriginalProduct=async id => {
        recollectCount++;
        aiOriginalRows[0].original_1688.variations[0].image_url='https://cbu01.alicdn.com/sku.jpg';
        return true;
      };
    }''')
    page.evaluate('repairAiOriginalProduct(9)')
    assert len(posted)==1 and posted[0]['product_item_ids']==[9]
    assert page.evaluate('recollectCount')==2
    assert page.evaluate('[...aiOriginalSelected]')==[77]
