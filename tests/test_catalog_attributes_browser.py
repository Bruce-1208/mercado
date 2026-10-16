"""Real template, mocked category rules and saves; no external business actions."""
import json
from urllib.parse import parse_qs,urlsplit
import pytest
from test_console_ui_browser import ROOT,console_browser,console_page  # noqa: F401

FIELDS=[{'attribute_id':'BRAND','name':'品牌','choices':[],'is_choice':False},
        {'attribute_id':'MODEL','name':'型号','choices':[],'is_choice':False},
        {'attribute_id':'COLOR','name':'颜色','choices':[{'id':'red','name':'Red'},{'id':'blue','name':'Blue'}],'is_choice':True}]

def report(number,missing):
    return {'id':number,'category_id':'CBT123','status':'missing' if missing else 'complete',
      'message':f'缺少 {len(missing)} 项属性' if missing else '必填及已有属性齐全','fields':FIELDS,
      'missing':[{'attribute_id':f['attribute_id'],'name':f['name'],'scope':'商品','sku_index':None,'is_choice':f['is_choice']} for f in FIELDS if f['attribute_id'] in missing]}

@pytest.mark.parametrize('width',[1440,1024,390])
@pytest.mark.parametrize('kind',['supplier','original','collection','products'])
def test_missing_badges_batch_generic_then_enum_setting(console_page,width,kind):
    page=console_page;page.set_viewport_size({'width':width,'height':1000})
    missing=['BRAND','MODEL','COLOR']; saves=[];errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    def check(route):
        ids=parse_qs(urlsplit(route.request.url).query)['ids'][0].split(',')
        route.fulfill(content_type='application/json',body=json.dumps({'status':'success','data':{'results':[report(int(i),missing) for i in ids]}}))
    def save(route):
        body=route.request.post_data_json;saves.append(body)
        if body['mode']=='generic':missing[:]=['COLOR']
        else:missing[:]=[]
        route.fulfill(content_type='application/json',body=json.dumps({'status':'success','data':{'changed':2,'results':[report(i,missing) for i in body['ids']]}}))
    page.route('**/api/catalog/attributes/check?*',check)
    page.route('**/api/catalog/attributes',save)
    page.evaluate("""kind=>{
      window.testAttributeRows=[1,2].map(id=>({id,product_number:'ZS20261015'+id,category_id:'CBT123',title:'属性待补全示例 '+id,source_item_id:'123456'+id,
        source_type:kind==='supplier'||kind==='original'?'ai_original':'collected',can_operate:true,
        original_1688:{title:'属性待补全示例 '+id,category_id:'CBT123'},ai_original:{status:'pending'},
        source_json:JSON.stringify({category_id:'CBT123',attributes:[]}),source_snapshot_json:{source:{category_id:'CBT123',attributes:[]}}}));
      document.querySelectorAll('.tab-page').forEach(e=>e.classList.remove('active'));
      if(kind==='supplier') {
        document.getElementById('tab-1688-products').classList.add('active');
        products1688Rows=testAttributeRows;products1688RenderRows();products1688Selected=new Set([1,2]);products1688UpdateSelection();
        load1688Products=async()=>products1688RenderRows();
      } else if(kind==='original') {
        document.getElementById('tab-ai-original-products').classList.add('active');
        aiOriginalRows=testAttributeRows;aiOriginalSelected=new Set([1,2]);renderAiOriginalProducts();
        loadAiOriginalProducts=async()=>renderAiOriginalProducts();
      } else {
        document.getElementById('tab-mercado-products').classList.add('active');
        document.getElementById('tab-mercado-products').appendChild(document.getElementById('mercado-list-module'));
        mercadoListMode=kind;mercadoListRows=testAttributeRows;selectedMercadoCollectionIds=new Set([1,2]);renderMercadoListRows(testAttributeRows);
        loadMercadoList=async()=>renderMercadoListRows(testAttributeRows);
      }
    }""",kind)
    if kind in {'supplier', 'original'}:
        section = '#tab-1688-products' if kind == 'supplier' else '#tab-ai-original-products'
        assert page.locator(section + ' [data-catalog-attributes]').count() == 0
        assert page.locator(section).get_by_role('button', name='批量设置属性', exact=True).count() == 0
        assert page.locator(section).get_by_role('button', name='一键补全属性', exact=True).count() == 0
        if kind == 'original':
            assert page.locator('.ai-original-image-pair .ai-original-product-number').count() == 2
            assert page.locator('.ai-original-check-panel').count() == 2
            assert '1688 ·' not in page.locator('.ai-original-card').first.text_content()
            assert '采购价' not in page.locator('.ai-original-card').first.text_content()
            assert 'ZS202610151' in page.locator('.ai-original-product-number').first.text_content()
            page.locator('.ai-original-card').first.scroll_into_view_if_needed()
            if width <= 1200:
                assert page.locator('.ai-original-fields').first.bounding_box()['width'] > page.locator('.ai-original-card').first.bounding_box()['width'] * .8
            out=ROOT/'artifacts/ai-original-wide-cards-20261015';out.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(out/f'original-{width}.png'),animations='disabled')
            bulk = page.locator('#ai-original-bulk-menu')
            assert bulk.get_attribute('open') is None
            bulk.locator('summary').focus();page.keyboard.press('Enter')
            assert page.locator('#ai-original-bulk-length').is_visible()
            assert page.locator('#ai-original-bulk-net-amount').is_visible()
            assert page.locator('#ai-original-review-status').is_visible()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(out/f'bulk-{width}.png'),animations='disabled')
            bulk.locator('summary').focus();page.keyboard.press('Enter')
            assert bulk.get_attribute('open') is None

        assert not saves
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        return
    badge=page.locator(f'[data-catalog-attributes="{kind}"][data-catalog-id="1"]').first
    page.wait_for_function("kind=>document.querySelector(`[data-catalog-attributes=\"${kind}\"]`)?.dataset.status==='missing'",arg=kind)
    badge.scroll_into_view_if_needed()
    page.wait_for_function("kind=>document.querySelector(`[data-catalog-attributes=\"${kind}\"]`)?.innerText.includes('MODEL')",arg=kind)
    assert 'MODEL' in badge.inner_text() and 'BRAND' in badge.inner_text()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    out=ROOT/'artifacts/catalog-attributes-20261014';out.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(out/f'{kind}-missing-{width}.png'),animations='disabled')
    if kind in {'collection','products'}:
        page.locator('#mercado-list-module .zs-bulk-menu > summary').click()
    button=page.locator('.tab-page.active').get_by_role('button',name='一键补全属性',exact=True)
    button.focus();page.keyboard.press('Enter')
    page.wait_for_function("document.getElementById('catalog-attributes-message').textContent.includes('已修改 2 件')")
    assert saves==[{'kind':kind,'ids':[1,2],'mode':'generic'}]
    dialog=page.locator('#catalog-attributes-dialog')
    assert '需手动选择' in dialog.inner_text()
    assert 'MODEL' not in page.locator('#catalog-attributes-results').inner_text()
    assert 'COLOR' in page.locator('#catalog-attributes-results').inner_text()
    page.locator('#catalog-attributes-field').select_option('2')
    assert page.locator('#catalog-attributes-value').evaluate('e=>e.tagName')=='SELECT'
    assert page.locator('#catalog-attributes-value option').all_text_contents()==['请选择有效选项','Red','Blue']
    page.locator('#catalog-attributes-value').select_option('blue')
    page.locator('#catalog-attributes-save').focus();page.keyboard.press('Enter')
    page.wait_for_function("document.querySelectorAll('#catalog-attributes-results [data-status=complete]').length===2")
    assert saves[-1]['value']=='blue' and saves[-1]['only_missing'] is True
    assert saves[-1]['category_id']=='CBT123'
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert dialog.evaluate('e=>e.scrollWidth<=e.clientWidth')
    out=ROOT/'artifacts/catalog-attributes-20261014';out.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(out/f'{kind}-{width}.png'),animations='disabled')
    page.keyboard.press('Escape');assert not dialog.is_visible()
    assert button.evaluate('e=>e===document.activeElement')
    assert not errors


def test_publish_record_start_sort_is_stable_and_keeps_input(console_page):
    page=console_page
    result=page.evaluate("""()=>{
      const rows=[{id:99,status:'published',started_at:'2026-10-01 12:00:00'},
        {id:1,status:'failed',started_at:'2026-10-14 12:00:00'},
        {id:3,status:'pending',created_at:'2026-10-14 13:00:00'},
        {id:2,status:'failed',started_at:'2026-10-14 12:00:00'}];
      renderMercadoPublishRecords({rows,total:4,counts:{all:4}});
      return {ordered:mercadoPublishRecordRows.map(r=>r.id),original:rows.map(r=>r.id)};
    }""")
    assert result=={'ordered':[3,2,1,99],'original':[99,1,3,2]}
