"""Publishing navigation uses mock products and never submits a live task."""
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_publish_steps_and_invalidated_review(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.evaluate('''async () => {
        await loadAiOriginalStores();
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        aiOriginalRows = [{id:1, ai_status:'completed', review_status:'approved',
          category_id:'CBT123', title_es:'Producto', title_pt:'Produto',
          description_es:'Descripcion', description_pt:'Descricao', weight_g:100,
          weight_basis:'ai_original_manual', net_proceeds_usd:10,
          package_length_cm:10, package_width_cm:10, package_height_cm:10,
          main_image_url:'/test-ai-white.jpg', ai_original:{status:'completed',
          category_id:'CBT123', image_generation_method:'local_background_removal',
          validation:{errors:[]}, attributes:[{id:'MATERIAL',value_name:'Metal'}]}}];
        aiOriginalSelected.clear(); renderAiOriginalProducts();
        aiOriginalStoreRows = [{id:1,display_name:'模拟店铺', site_settings:[]}];
        renderAiOriginalStoreFilters();
    }''')
    panel = page.locator('.ai-original-publish-panel')
    assert panel.is_hidden()
    assert page.locator('#ai-original-publish-next').is_disabled()
    page.evaluate('toggleAllAiOriginalProducts(true)')
    page.locator('#ai-original-publish-next').click()
    assert page.locator('.ai-original-publish-layout').is_visible()
    assert page.locator('#ai-original-publish').is_hidden()
    page.locator('#ai-original-publish-review-next').click()
    assert '请选择对应店铺' in page.locator('#ai-original-publish-status').inner_text()
    page.locator('#ai-original-stores').select_option('1')
    page.locator('#ai-original-publish-review-next').focus()
    page.keyboard.press('Enter')
    review = page.locator('#ai-original-publish-review')
    assert review.is_visible()
    assert '可上架 1 件' in review.inner_text()
    assert '模拟店铺' in review.inner_text()
    assert page.locator('#ai-original-publish').is_visible()
    assert review.evaluate('(node) => document.activeElement === node')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = ROOT / 'artifacts' / 'ai-original-publish-workflow'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'review-{width}.png'))
    page.locator('#ai-original-publish-back').click()
    page.locator('#ai-original-quantity').fill('25')
    page.locator('#ai-original-publish-review-next').click()
    assert '25' in review.inner_text()
    page.evaluate('aiOriginalSelected.clear(); updateAiOriginalSelection()')
    assert panel.is_hidden()
    page.evaluate('aiOriginalSelected.add(1); updateAiOriginalSelection()')
    assert review.is_hidden()
    assert not page.locator('#ai-original-publish-workspace').evaluate('(node) => node.open')
    assert not errors
