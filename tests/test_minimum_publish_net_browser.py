"""Verify both publication panels send the optional floor, without live writes."""
import json
import pytest

from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('panel', ['products', 'original'])
def test_floor_input_validation_and_publish_payload(console_page, width, panel):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.set_default_timeout(5000)
    errors, requests = [], []
    page.on('pageerror', lambda exc: errors.append(str(exc)))
    page.route('**/api/mercado-products/publish', lambda route: (
        requests.append(route.request.post_data_json),
        route.fulfill(content_type='application/json', body=json.dumps({
            'status': 'success', 'data': {'running': False, 'status': 'completed', 'results': []}}))))
    confirmations = []
    page.on('dialog', lambda dialog: (confirmations.append(dialog.message), dialog.accept()))
    page.evaluate('''panel => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        const original = panel === 'original';
        document.getElementById(original ? 'tab-ai-original-products' : 'tab-mercado-products').classList.add('active');
        const ready = {id:11, source_type:'collected', review_status:'approved', ai_status:'completed',
          category_id:'CBT123', title_es:'Producto', title_pt:'Produto', description_es:'Descripcion',
          description_pt:'Descricao', main_image_url:'/test-ai-white.jpg', weight_g:100,
          net_proceeds_usd:10, package_length_cm:10, package_width_cm:10, package_height_cm:10, weight_basis:'ai_original_manual', original_1688:{title:'测试产品'},
          ai_original:{status:'completed', image_generation_method:'local_background_removal',
                       attributes:[{id:'MATERIAL', value_name:'Plastic'}]}};
        if (original) {
            document.getElementById('ai-original-platform').value='mercadolibre';
            aiOriginalRows=[ready]; aiOriginalSelected=new Set([11]);
            document.getElementById('ai-original-stores').innerHTML='<option value="7" selected>测试店铺</option>';
            renderAiOriginalProducts(); updateAiOriginalSelection();
            loadAiOriginalPublishStatus=()=>{};
        } else {
            mercadoListMode='products'; mercadoListRows=[ready];
            selectedMercadoCollectionIds=new Set([11]);
            renderMercadoPublishStores([{id:7,site_id:'CBT',display_name:'测试店铺'}]);
            document.getElementById('mercado-publish-store').options[0].selected=true;
            renderMercadoPublishPicker(document.getElementById('mercado-publish-store'));
            document.getElementById('mercado-product-actions').style.display='flex';
            document.getElementById('mercado-list-module').classList.remove('collection-mode');
            document.getElementById('mercado-list-module').classList.add('product-mode');
            updateMercadoSelection();
            document.getElementById('mercado-product-actions').closest('.tab-page').classList.add('active');
            document.getElementById('mercado-publish-workspace').open=true;
            loadMercadoList=async()=>{};
        }
    }''', panel)
    floor = page.locator('#ai-original-minimum-net' if panel == 'original' else '#mercado-publish-minimum-net')
    button = page.locator('#ai-original-publish' if panel == 'original' else '#mercado-publish-selected')
    floor.fill('-1')
    (page.locator('#mercado-publish-review-next') if panel == 'products' else button).click()
    assert requests == []
    floor.fill('0.001')
    (page.locator('#mercado-publish-review-next') if panel == 'products' else button).click()
    assert requests == []
    floor.fill('25.50')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = ROOT / 'artifacts/minimum-publish-net-20261010'
    output.mkdir(parents=True, exist_ok=True)
    floor.scroll_into_view_if_needed()
    page.screenshot(path=str(output / f'{panel}-{width}.png'))
    if panel == 'products':
        page.locator('#mercado-publish-review-next').click()
        assert '25.50' in page.locator('#mercado-publish-review').inner_text()
    button.click()
    page.wait_for_function('!aiOriginalSubmitting && !mercadoPublishSubmitting')
    assert requests[0]['minimum_net_proceeds_usd'] == 25.5
    assert requests[0]['net_proceeds_ratio'] == (200 if panel == 'original' else 100)
    assert '25.50' in confirmations[-1]
    assert not errors
