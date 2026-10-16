"""The original-product drawer shows captured photos and each SKU's own cover."""
from io import BytesIO

import pytest
from PIL import Image
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_full_source_gallery_and_sku_white_images_in_detail_and_editor(console_page, width):
    page = console_page
    import json
    page.route('**/api/store-links/category-paths', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'status':'success','data':{'CBT123':[{'id':'CBT1','name':'家居与装饰'},{'id':'CBT123','name':'厨房用品与水杯'}]}})))
    page.set_viewport_size({'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    image = BytesIO()
    Image.new('RGB', (100, 100), '#c5dcef').save(image, format='PNG')
    def picture(route): route.fulfill(content_type='image/png', body=image.getvalue())
    page.route('**/api/ai-original-products/source-image?*', picture)
    page.route('**/api/ai-original-products/images/*', picture)
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        const urls = Array.from({length:26}, (_, i) => `https://cbu01.alicdn.com/img/ibank/gallery-${i}.jpg`);
        const sourceSkus = ['red', 'blue'].map(color => ({seller_sku:`SKU-${color}`, image_url:`https://cbu01.alicdn.com/img/ibank/${color}.jpg`,
            attribute_combinations:[{id:'COLOR',name:'颜色',value_name:color}], price:12, stock:8}));
        const skus = sourceSkus.map(sku => ({...sku, source_image_url:sku.image_url,
            image_url:`/api/ai-original-products/images/1688-${sku.seller_sku}-ai-white.jpg`,
            white_image_url:`/api/ai-original-products/images/1688-${sku.seller_sku}-ai-white.jpg`,
            image_generation_method:'local_background_removal'}));
        aiOriginalRows = [{id:41, category_id:'CBT123', main_image_url:'/api/ai-original-products/images/1688-parent-ai-white.jpg',
            ai_status:'completed', original_1688:{title:'多色水杯', main_image_url:urls[0], images:[...urls, {url:urls[0]}], variations:sourceSkus},
            ai_original:{status:'completed',generated_by:'测试业务员 <img>',variations:skus}}];
        renderAiOriginalProducts();
    }""")
    attribution = page.locator('#ai-original-grid .ai-original-generator')
    attribution.scroll_into_view_if_needed()
    page.wait_for_function("document.querySelector('#ai-original-grid .ai-original-generator').innerText.length > 0")
    assert attribution.inner_text() == '生成者：测试业务员 <img>'
    assert attribution.locator('img').count() == 0
    checkbox = page.locator('#ai-original-grid input[type=checkbox]').first
    checkbox.focus()
    page.keyboard.press('Space')
    assert checkbox.is_checked()
    page.keyboard.press('Space')
    assert not checkbox.is_checked()
    card_output = ROOT / 'artifacts/ai-original-unified-20261014'
    card_output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(card_output / f'generator-{width}.png'))
    assert page.locator('#ai-original-grid button').filter(has_text='查看详情').count() == 0
    page.wait_for_function("document.querySelector('#ai-original-grid .ai-original-category-path').textContent.includes('厨房用品与水杯')")
    page.locator('#ai-original-grid .ai-original-category-path').scroll_into_view_if_needed()
    page.locator('#ai-original-grid .ai-original-card').first.screenshot(path=str(card_output / f'list-{width}.png'))
    page.locator('#ai-original-grid button').filter(has_text='编辑分类').focus()
    page.keyboard.press('Enter')
    editor = page.locator('#ai-original-editor-dialog')
    page.wait_for_function("document.getElementById('ai-original-editor-dialog').getAnimations().every(a => a.playState === 'finished')")
    page.screenshot(path=str(card_output / f'category-{width}.png'))
    assert editor.locator('.ai-original-editor-source-gallery').is_visible()
    gallery = editor.locator('.ai-original-detail-gallery').first
    assert gallery.locator('a').count() == 31
    assert gallery.locator('a[href$="gallery-25.jpg"]').count() == 1
    assert gallery.locator('a[href$="red.jpg"]').count() == 1
    assert gallery.locator('a[href$="blue.jpg"]').count() == 1
    for sku in editor.locator('#ai-original-editor-variations tr').all():
        assert sku.locator('.zs-sku-selected .is-main a[href$="-ai-white.jpg"]').count() == 1
        assert sku.locator('.zs-sku-selected .zs-sku-photo').count() == 31
        assert sku.locator('.zs-sku-selected a[href$="red.jpg"], .zs-sku-selected a[href$="blue.jpg"]').count() == 2
    assert editor.locator('.ai-original-variant-state.ok').count() == 2
    assert editor.locator('.zs-sku-selected .is-main a[href$="-ai-white.jpg"]').count() == 2
    assert '家居与装饰 › 厨房用品与水杯' in editor.locator('#ai-original-editor-category-path').inner_text()
    editor.get_by_role('button', name='变体与图片', exact=True).focus()
    page.keyboard.press('Enter')
    assert editor.locator('#ai-original-section-skus').is_visible()
    secondary = editor.locator('.zs-sku-secondary').first
    assert secondary.locator('summary').inner_text() == '副图 · 30 张展开'
    assert not secondary.locator('img').first.is_visible()
    secondary.locator('summary').focus(); page.keyboard.press('Enter')
    assert secondary.locator('img').first.is_visible()
    assert secondary.locator('a[href$="gallery-25.jpg"]').count() == 1
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = ROOT / 'artifacts/ai-original-unified-20261014'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'editor-{width}.png'))
    assert editor.locator('.zs-sku-chooser').first.locator('button[data-sku-url$="red.jpg"]').count() == 1
    secondary.locator('[data-sku-action="remove"]').first.click()
    assert secondary.evaluate('el => el.open')
    assert editor.locator('#ai-original-editor-variations tr').first.locator('.zs-sku-selected .zs-sku-photo').count() == 30
    assert page.evaluate('aiOriginalSkuWhiteImageReady(collectAiOriginalEditorVariations()[0])') is True
    editor.locator('[data-variant-index="0"][data-variant-field="image"]').fill('https://cbu01.alicdn.com/img/ibank/new.jpg')
    assert page.evaluate('aiOriginalSkuWhiteImageReady(collectAiOriginalEditorVariations()[0])') is False
    page.keyboard.press('Escape')
    assert not editor.is_visible()
    assert not errors


def test_legacy_sku_without_white_cover_is_not_publishable(console_page):
    page = console_page
    result = page.evaluate("""() => {
        const row = {id:1, ai_status:'completed',review_status:'approved',category_id:'CBT123',
            title_es:'Cup',title_pt:'Copo',description_es:'A cup',description_pt:'Um copo',
            weight_g:100,net_proceeds_usd:10,package_length_cm:10,package_width_cm:10,package_height_cm:10,
            main_image_url:'/api/ai-original-products/images/1688-parent-ai-white.jpg',
            original_1688:{variations:[{image_url:'https://cbu01.alicdn.com/img/ibank/red.jpg'}]},
            ai_original:{status:'completed',image_generation_method:'local_background_removal',
                attributes:[{id:'MATERIAL',value_name:'Steel'}],
                variations:[{image_url:'https://cbu01.alicdn.com/img/ibank/red.jpg',attribute_combinations:[{id:'COLOR',value_name:'Red'}]}]}};
        aiOriginalRows=[row]; aiOriginalSelected=new Set([1]);
        return {status:aiOriginalProductStatus(row), ready:aiOriginalPublishableSelection().length,
            message:aiOriginalRecoveryMessage(row), checks:aiOriginalListingChecks(row)};
    }""")
    assert result['status'] == 'failed' and result['ready'] == 0
    assert 'SKU 原图已齐' in result['message']
    assert next(check for check in result['checks'] if check['label'] == 'SKU 白底主图')['ok'] is False


def test_missing_captured_image_explains_recollection(console_page):
    result = console_page.evaluate("""() => {
      const row={ai_status:'completed',original_1688:{variations:[{sku_id:'11',image_url:''}]},
        ai_original:{status:'completed',variations:[{sku_id:'11',image_url:''}]}};
      return {missing:aiOriginalMissingSkuSources(row), message:aiOriginalRecoveryMessage(row),
        notices:aiOriginalMissingCheckMessages(row, [{label:'SKU 采集原图缺失',ok:false}])};
    }""")
    assert result['missing'] == [1]
    assert '重新采集' in result['message']
    assert '生成失败' not in result['notices'][0]


@pytest.mark.parametrize('width', [1440, 390])
def test_parent_photo_does_not_hide_missing_variant_photos(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    result = page.evaluate("""() => {
      const original = {images:[{secure_url:'https://cbu01.alicdn.com/first.jpg'}],
        variations:[{sku_id:'red'}, {sku_id:'blue'}]};
      const row = {ai_status:'failed',original_1688:original,
        ai_original:{status:'failed',failed_step:'sku_images',variations:original.variations,
          input_blockers:[{field:'sku_images',message:'2 个 SKU 缺少采集原图'}],
          error:'SKU 1 缺少采集主图'}};
      const missing = aiOriginalMissingSkuSources(row);
      const message = aiOriginalRecoveryMessage(row);
      original.images = [];
      return {missing, message, withoutCover:aiOriginalMissingSkuSources(row)};
    }""")
    assert result['missing'] == [1, 2]
    assert '缺少采集原图' in result['message']
    assert result['withoutCover'] == [1, 2]


@pytest.mark.parametrize('width', [1440, 390])
def test_partial_draft_keeps_every_sku_and_recollection_reports_real_result(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("""() => {
      document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
      document.getElementById('tab-ai-original-products').classList.add('active');
      aiOriginalRows=[{id:42,source_type:'ai_original',can_operate:true,
        original_1688:{title:'测试补采',images:[],variations:[{sku_id:'11',label:'红色'}, {sku_id:'22',label:'蓝色'}]},
        ai_original:{status:'failed',variations:[{sku_id:'22',image_url:'https://cbu01.alicdn.com/blue.jpg'}]}}];
      renderAiOriginalProducts(); openAiOriginalDetail(42);
      window.recollectCalls=[];
      window.addEventListener('message', event => {
        if(event.data?.type !== 'ZESHUN_RECOLLECT') return;
        recollectCalls.push(event.data);
        window.postMessage({type:'ZESHUN_RECOLLECT_RESULT',requestId:event.data.requestId,accepted:true},location.origin);
        setTimeout(() => window.postMessage({type:'ZESHUN_RECOLLECT_RESULT', requestId:event.data.requestId,
          result:{ok:false,error:'1688 需要登录，请登录后重试'}},location.origin),100);
      });
    }""")
    drawer=page.locator('#ai-original-editor-dialog')
    assert drawer.locator('#ai-original-editor-variations tr').count()==2
    assert '白底主图待生成' in drawer.inner_text()
    button=drawer.get_by_role('button',name='一键补采缺失数据')
    button.focus(); page.keyboard.press('Enter')
    page.wait_for_function("document.querySelector('#ai-original-editor-overview [role=status]').textContent.includes('需要登录')")
    assert button.is_enabled()
    assert page.evaluate('recollectCalls.length')==1
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.evaluate('aiOriginalRows[0].can_operate=false; openAiOriginalDetail(42)')
    assert drawer.get_by_role('button',name='一键补采缺失数据').is_disabled()


@pytest.mark.parametrize('width', [1440, 390])
def test_single_sku_card_uses_sku_cover(console_page, width):
    import json
    from erp.mercadolibre_collection_store import _mirror_zying_snapshot_fields
    cover = '/api/ai-original-products/images/sku1-ai-white.jpg'
    row = _mirror_zying_snapshot_fields({'id': 941, 'source_type': 'ai_original',
        'main_image_url': '/api/ai-original-products/images/old-ai-white.jpg',
        'source_snapshot_json': json.dumps({'original_1688': {'title': '单变体测试', 'variations': [
            {'seller_sku': 'sku1', 'image_url': 'https://example.test/sku1.jpg'}]},
            'ai_original': {'status': 'completed', 'variations': [{'seller_sku': 'sku1',
                'image_url': cover, 'white_image_url': cover, 'image_generation_method': 'local_background_removal'}]}})})
    page = console_page
    picture = BytesIO()
    Image.new('RGB', (100, 100), '#c5dcef').save(picture, format='PNG')
    page.route('**/api/ai-original-products/images/*', lambda route: route.fulfill(content_type='image/png', body=picture.getvalue()))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate('''row => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        aiOriginalRows = [row]; renderAiOriginalProducts();
    }''', row)
    assert page.locator('#ai-original-grid img[src*="sku1-ai-white.jpg"]').count() >= 1
    assert page.locator('#ai-original-grid img[src*="old-ai-white.jpg"]').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
