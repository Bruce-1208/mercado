"""Offline SKU matrix controls and counts on all four catalog surfaces."""
import json
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_supplier_matrix_picture_order_price_save_and_counts(console_page, width):
    page = console_page
    page.set_viewport_size({'width':width,'height':1000})
    errors=[]
    page.on('pageerror',lambda e: errors.append(str(e)))
    page.route('https://cbu01.alicdn.com/**',lambda r:r.fulfill(content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="#dce8ff"/><circle cx="50" cy="50" r="28" fill="#567dda"/></svg>'))
    page.evaluate("""() => {
      document.querySelectorAll('.tab-page').forEach(e=>e.classList.remove('active'));
      document.getElementById('tab-1688-products').classList.add('active');
      const variations = ['红色','蓝色'].map((color,i)=>({sku_id:String(i+1),seller_sku:'CUP-'+i,
        attribute_combinations:[{id:'COLOR',name:'颜色',value_name:color},{id:'SIZE',name:'尺寸',value_name:'M'}],
        price:i ? 18:12.5, available_quantity:i ? 20:0, weight_g:150,
        image_url:'https://cbu01.alicdn.com/'+i+'.jpg',images:['https://cbu01.alicdn.com/'+i+'.jpg','https://cbu01.alicdn.com/detail.jpg']}));
      window.skuTestRow={id:81,source_type:'ai_original',can_operate:true,title:'彩色陶瓷杯 · SKU 管理示例',
        source_item_id:'168812345678',main_image_url:'https://cbu01.alicdn.com/0.jpg',
        original_1688:{title:'彩色陶瓷杯 · SKU 管理示例',images:['https://cbu01.alicdn.com/0.jpg','https://cbu01.alicdn.com/1.jpg','https://cbu01.alicdn.com/detail.jpg'],variations}};
      products1688Rows=[skuTestRow]; products1688RenderRows();
    }""")
    assert page.locator('#products-1688-body .zs-sku-count').inner_text()=='2 个 SKU'
    assert '168812345678' in page.locator('#products-1688-body').inner_text()
    assert page.locator('#products-1688-body').get_by_role('button', name='修改资料').count() == 0
    assert page.locator('#products-1688-body').get_by_role('button', name='编辑刊登内容').count() == 0
    page.locator('#products-1688-body').get_by_role('button', name='查看详情').click()
    assert page.locator('#products-1688-detail-dialog').is_visible()
    assert page.locator('#products-1688-edit-dialog').count() == 0
    page.locator('[data-1688-sku="0"][data-field="seller_sku"]').fill('CUP-RED-NEW')
    page.locator('[data-1688-sku="0"][data-field="available_quantity"]').fill('7')
    page.locator('[data-1688-sku="0"][data-field="weight_g"]').fill('175')
    assert page.locator('#p1688-sku-net-0').inner_text()=='3'
    page.locator('[data-1688-sku="0"][data-field="price"]').fill('25')
    assert page.locator('#p1688-sku-net-0').inner_text()=='5'
    secondary = page.locator('[data-sku-picture-key="1688-0"] .zs-sku-secondary')
    assert secondary.locator('summary').inner_text() == '副图 · 2 张展开'
    assert not secondary.evaluate('el => el.open')
    assert not secondary.locator('img').first.is_visible()
    secondary.locator('summary').focus()
    page.keyboard.press('Enter')
    assert secondary.locator('img').first.is_visible()
    main_button=page.locator('[data-sku-scope="1688"][data-sku-row="0"][data-sku-action="main"][data-sku-position="1"]')
    main_button.focus(); page.keyboard.press('Enter')
    assert page.evaluate('products1688CollectSkus()[0].image_url').endswith('/detail.jpg')
    assert secondary.evaluate('el => el.open')
    assert page.evaluate('document.activeElement.closest("[data-sku-picture-key]").dataset.skuPictureKey') == '1688-0'
    assert page.evaluate('products1688CollectSkus()[1].image_url').endswith('/1.jpg')
    assert page.locator('[data-1688-sku="0"][data-field="price"]').input_value()=='25'
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.wait_for_function("Array.from(document.querySelectorAll('#products-1688-sku-editor .zs-sku-selected > .is-main img')).every(img => img.complete && img.naturalWidth > 0)")
    output=ROOT/'artifacts/sku-matrix-20261014';output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/f'supplier-{width}.png'), animations='disabled')
    captured=[]
    def save(route):
        captured.append(route.request.post_data_json)
        route.fulfill(content_type='application/json',body=json.dumps({'status':'success','data':{'id':81}}))
    page.route('**/api/1688-products/81',save)
    page.locator('#products-1688-edit-save').click()
    page.wait_for_function('!products1688EditSaving')
    assert captured[0]['variations'][0]['images'][0].endswith('/detail.jpg')
    assert captured[0]['variations'][0]['seller_sku']=='CUP-RED-NEW'
    assert captured[0]['variations'][0]['available_quantity']==7
    assert captured[0]['variations'][0]['weight_g']==175
    assert captured[0]['variations'][1]['seller_sku']=='CUP-1'
    assert captured[0]['variations'][1]['available_quantity']==20
    assert captured[0]['variations'][0]['price']==25
    assert captured[0]['variations'][0]['net_proceeds_usd']==5
    assert page.evaluate('skuTestRow.original_1688.variations[0].seller_sku')=='CUP-0'
    assert not page.locator('#products-1688-detail-dialog').is_visible()
    assert not errors


@pytest.mark.parametrize('width', [1440, 1024, 390])
@pytest.mark.parametrize('mode', ['collection','products'])
def test_mercado_sku_badge_filter_and_picture_selection(console_page,mode,width):
    page=console_page
    page.set_viewport_size({'width':width,'height':1000})
    page.route('https://img.test/**', lambda route: route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="lightblue"/></svg>'))
    page.evaluate("""mode=>{
      document.querySelectorAll('.tab-page').forEach(e=>e.classList.remove('active'));
      document.getElementById('tab-mercado-products').classList.add('active');
      document.getElementById('tab-mercado-products').appendChild(document.getElementById('mercado-list-module'));
      const source={pictures:[{id:'a',source:'https://img.test/red.jpg'},{id:'b',source:'https://img.test/blue.jpg'}, ...Array.from({length:25},(_,i)=>({id:'detail'+i,source:`https://img.test/detail${i}.jpg`}))],
        variations:[{id:'red',seller_sku:'RED',price:10,net_proceeds_usd:3,available_quantity:0,picture_ids:['a'],attribute_combinations:[{id:'COLOR',value_name:'Red'}]},
          {id:'blue',seller_sku:'BLUE',price:20,net_proceeds_usd:4,available_quantity:2,picture_ids:['b'],attribute_combinations:[{id:'COLOR',value_name:'Blue'}]}]};
      window.skuMarketRow={id:82,source_type:'collected',title:'SKU 图片编辑',source_item_id:'MLM123456',can_operate:true,
        source_snapshot_json:mode==='products'?{source}:undefined,source_json:mode==='collection'?JSON.stringify(source):undefined};
      mercadoListMode=mode;mercadoListRows=[skuMarketRow];renderMercadoListRows(mercadoListRows);
    }""",mode)
    assert page.locator('#mercado-list-body .zs-sku-count').inner_text()=='2 个 SKU'
    page.evaluate('openMercadoProductEditor(82)')
    page.locator('[data-sku-scope="mercado"][data-sku-row="0"][data-sku-action="main"]').first.wait_for(state='attached')
    page.evaluate("scrollMercadoEditorSection('variants')")
    secondary = page.locator('[data-sku-picture-key="mercado-0"] .zs-sku-secondary')
    assert secondary.locator('summary').inner_text() == '副图 · 26 张展开'
    assert not secondary.locator('img').first.is_visible()
    secondary.locator('summary').focus(); page.keyboard.press('Space')
    assert secondary.evaluate('el => el.open')
    assert secondary.locator('img').count() == 26
    assert secondary.locator('a[href$="detail24.jpg"]').count() == 1
    secondary.locator('[data-sku-action="main"]').first.click()
    assert page.evaluate('collectMercadoProductVariations()[0].image_url').endswith('/blue.jpg')
    assert page.locator('[data-sku-picture-key="mercado-0"] .is-main img').count() == 1
    assert secondary.locator('a[href$="red.jpg"]').count() == 1
    assert secondary.evaluate('el => el.open')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output=ROOT/'artifacts/sku-secondary-20261014'; output.mkdir(parents=True,exist_ok=True)
    page.locator('[data-sku-picture-key="mercado-0"] .is-main').scroll_into_view_if_needed()
    assert secondary.locator('.zs-sku-secondary-grid').evaluate('el => el.scrollWidth <= el.clientWidth')
    page.screenshot(path=str(output/f'{mode}-{width}.png'),animations='disabled')
    secondary.locator('summary').focus(); page.keyboard.press('Enter')
    assert not secondary.locator('img').first.is_visible()
    assert page.evaluate('collectMercadoProductVariations()[0].available_quantity')==0
    assert page.evaluate('collectMercadoProductVariations()[0].net_proceeds_usd')==3
    page.keyboard.press('Escape')
    assert not page.locator('#mercado-product-edit-dialog').is_visible()
    assert page.evaluate('document.getElementById("mercado-sku-min").closest(".zs-advanced-filters") !== null')
    query=page.evaluate("""()=>{document.getElementById('mercado-sku-min').value='2';document.getElementById('mercado-sku-max').value='9';return ZeshunSku.filterQuery('mercado').toString()}""")
    assert query=='sku_count_min=2&sku_count_max=9'


def test_original_badge_and_server_filter_scope(console_page):
    page=console_page
    result=page.evaluate("""()=>{
      const row={original_1688:{variations:[{},{},{}]},ai_original:{variations:[{}]}};
      document.getElementById('ai-original-sku-min').value='3';
      return {badge:ZeshunSku.badge(row),query:ZeshunSku.filterQuery('ai-original').toString(),
        advanced:!!document.getElementById('ai-original-sku-min').closest('.zs-advanced-filters')};
    }""")
    assert '3 个 SKU' in result['badge']
    assert result['query']=='sku_count_min=3' and result['advanced']


@pytest.mark.parametrize('width', [1440, 390])
def test_supplier_detail_read_only_product_keeps_sku_controls_disabled(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("""() => {
      document.querySelectorAll('.tab-page').forEach(e=>e.classList.remove('active'));
      document.getElementById('tab-1688-products').classList.add('active');
      products1688Rows=[{id:83,title:'其他成员的测试产品',source_item_id:'701168800083',
        can_operate:false,source_purchase_price_cny:20,
        original_1688:{variations:[{sku_id:'locked',seller_sku:'LOCKED',price:20,
          available_quantity:0,images:['https://img.test/locked.jpg']}]}}];
      products1688RenderRows();
    }""")
    trigger = page.locator('#products-1688-body').get_by_role('button', name='查看详情')
    trigger.focus()
    page.keyboard.press('Enter')
    dialog = page.locator('#products-1688-detail-dialog')
    assert dialog.is_visible()
    assert page.locator('#products-1688-edit-price').is_disabled()
    assert page.locator('#products-1688-edit-save').is_disabled()
    controls = dialog.locator('#products-1688-sku-editor input, #products-1688-sku-editor button')
    assert controls.count() >= 4
    assert controls.evaluate_all('(els) => els.every(el => el.disabled)')
    assert '仅可查看' in page.locator('#products-1688-edit-message').inner_text()
    before = page.evaluate('JSON.stringify(products1688Rows)')
    page.evaluate('products1688SaveEdit()')
    assert page.evaluate('JSON.stringify(products1688Rows)') == before
    page.keyboard.press('Escape')
    assert not dialog.is_visible()
    assert trigger.evaluate('el => el === document.activeElement')


def test_supplier_single_sku_can_be_edited_in_detail_without_spurious_save(console_page):
    page = console_page
    page.evaluate("""() => {
      document.querySelectorAll('.tab-page').forEach(e=>e.classList.remove('active'));
      document.getElementById('tab-1688-products').classList.add('active');
      products1688Rows=[{id:84,title:'单 SKU 测试产品',source_item_id:'701168800084',
        can_operate:true,source_purchase_price_cny:20,source_weight_g:160,
        original_1688:{seller_sku:'SINGLE',price:20,available_quantity:0}}];
      products1688RenderRows();
    }""")
    captured=[]
    def save(route):
        captured.append(route.request.post_data_json)
        route.fulfill(content_type='application/json', body=json.dumps({'status':'success','data':{'id':84}}))
    page.route('**/api/1688-products/84', save)
    page.locator('#products-1688-body').get_by_role('button', name='查看详情').click()
    sku = page.locator('[data-1688-sku="0"][data-field="seller_sku"]')
    assert sku.input_value() == 'SINGLE'
    assert page.locator('[data-1688-sku="0"][data-field="available_quantity"]').input_value() == '0'
    page.locator('#products-1688-edit-save').click()
    assert not captured
    assert '没有需要保存的修改' in page.locator('#products-1688-edit-message').inner_text()
    sku.fill('SINGLE-UPDATED')
    page.locator('#products-1688-edit-save').click()
    page.wait_for_function('!products1688EditSaving')
    assert list(captured[0]) == ['variations']
    assert captured[0]['variations'][0]['seller_sku'] == 'SINGLE-UPDATED'
    assert captured[0]['variations'][0]['available_quantity'] == 0


def test_secondary_gallery_preserves_missing_main_and_explicit_removal(console_page):
    result = console_page.evaluate("""() => {
      const gallery = ['https://img.test/main.jpg', 'https://img.test/detail.jpg', 'https://img.test/detail.jpg'];
      const row = {original_1688:{images:Array.from({length:30}, (_,i)=>({url:`https://img.test/${i}.jpg`}))}};
      return {all:ZeshunSku.withSecondary([gallery[0]],gallery),
        missing:ZeshunSku.withSecondary([],gallery),
        edited:ZeshunSku.withSecondary([gallery[0]],gallery,true), supplier:products1688Images(row).length};
    }""")
    assert result['all'] == ['https://img.test/main.jpg', 'https://img.test/detail.jpg']
    assert result['missing'] == []
    assert result['edited'] == ['https://img.test/main.jpg']
    assert result['supplier'] == 30


def test_confirming_missing_sku_main_includes_gallery_and_allows_removal(console_page):
    page = console_page
    page.evaluate("""() => {
      document.querySelectorAll('.tab-page').forEach(e=>e.classList.remove('active'));
      document.getElementById('tab-1688-products').classList.add('active');
      products1688Rows=[{id:85,title:'待确认主图',can_operate:true,original_1688:{
        images:['https://img.test/main.jpg','https://img.test/detail.jpg'],
        variations:[{sku_id:'missing',price:20,available_quantity:4}]}}];
      products1688RenderRows(); products1688OpenDetail(85);
    }""")
    host = page.locator('[data-sku-picture-key="1688-0"]')
    assert host.locator('.zs-sku-missing').is_visible()
    host.locator('.zs-sku-chooser summary').click()
    host.locator('[data-sku-action="add"]').first.click()
    assert host.locator('.is-main a').get_attribute('href') == 'https://img.test/main.jpg'
    assert host.locator('.zs-sku-secondary summary').inner_text() == '副图 · 1 张展开'
    assert not host.locator('.zs-sku-secondary').evaluate('el => el.open')
    host.locator('.zs-sku-secondary summary').click()
    host.locator('.zs-sku-secondary [data-sku-action="remove"]').click()
    assert host.locator('.zs-sku-empty').inner_text() == '暂无副图'
    assert page.evaluate('products1688CollectSkus()[0].images') == ['https://img.test/main.jpg']
    assert page.evaluate('products1688CollectSkus()[0].available_quantity') == 4
    assert host.locator('.zs-sku-chooser [data-sku-url$="detail.jpg"]').is_enabled()
    page.keyboard.press('Escape')
