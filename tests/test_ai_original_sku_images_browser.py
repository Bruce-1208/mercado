"""The original-product drawer shows captured photos and each SKU's own cover."""
from io import BytesIO

import pytest
from PIL import Image
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 390])
def test_full_source_gallery_and_sku_white_images_in_detail_and_editor(console_page, width):
    page = console_page
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
            ai_original:{status:'completed',variations:skus}}];
        renderAiOriginalProducts();
    }""")
    page.locator('#ai-original-grid button').filter(has_text='查看详情').click()
    drawer = page.locator('#product-detail-drawer')
    gallery = drawer.locator('.ai-original-detail-gallery')
    assert gallery.locator('a').count() == 28
    assert gallery.locator('a[href$="gallery-25.jpg"]').count() == 1
    assert gallery.locator('a[href$="red.jpg"]').count() == 1
    assert gallery.locator('a[href$="blue.jpg"]').count() == 1
    assert drawer.locator('.ai-original-sku-image-list > div').count() == 2
    assert drawer.locator('.ai-original-sku-image-list').inner_text().count('白底主图已就绪') == 2
    for color in ['red', 'blue']:
        assert drawer.locator(f'.ai-original-sku-image-list a[href$="SKU-{color}-ai-white.jpg"]').count() == 1
    drawer.locator('.ai-original-sku-image-list').scroll_into_view_if_needed()
    page.wait_for_function("Array.from(document.querySelectorAll('.ai-original-sku-image-list img')).every(img => img.complete && img.naturalWidth > 0)")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = ROOT / 'artifacts/ai-original-sku-images-20261010'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'detail-{width}.png'))
    page.keyboard.press('Escape')
    page.evaluate('openAiOriginalEditor(41)')
    editor = page.locator('#ai-original-editor-dialog')
    assert editor.locator('.ai-original-variant-thumbnails a[href$="-ai-white.jpg"]').count() == 2
    assert editor.locator('.ai-original-variant-thumbnails a[href$="red.jpg"]').count() == 1
    editor.locator('[data-variant-index="0"][data-variant-field="image"]').fill('https://cbu01.alicdn.com/img/ibank/new.jpg')
    assert page.evaluate('aiOriginalSkuWhiteImageReady(collectAiOriginalEditorVariations()[0])') is False
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
    assert result['status'] == 'incomplete' and result['ready'] == 0
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
