"""Failed SKU mapping must not resize tiles or conceal bilingual titles."""
import pytest

from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_fixed_cards_bilingual_titles_and_visible_failure(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.evaluate('''() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        document.getElementById('ai-original-platform').value = 'mercadolibre';
        const ready = {id:1, source_item_id:'1688625324015225', ai_status:'completed',
          review_status:'approved', category_id:'CBT123',
          title_es:'Disfraz con accesorios para niños y adultos',
          title_pt:'Fantasia com acessórios para crianças e adultos',
          description_es:'Descripcion', description_pt:'Descricao',
          weight_g:100, net_proceeds_usd:10, package_length_cm:10,
          package_width_cm:10, package_height_cm:10,
          main_image_url:'/test-ai-white.jpg',
          ai_original:{status:'completed', image_generation_method:'local_background_removal',
            main_image_url:'/test-ai-white.jpg', attributes:[{id:'MATERIAL',value_name:'Plastic'}]},
          original_1688:{title:'测试产品'}};
        aiOriginalRows = [ready, {...ready, id:2, ai_status:'failed',
          ai_error:'自动映射已尝试 3 次：龟头戴与悟空头戴映射后无法区分。<img src=x onerror=alert(1)>' + '规格映射详情。'.repeat(150),
          original_1688:{title:'很长的来源标题'.repeat(30)},
          ai_original:{...ready.ai_original, workflow_status:'needs_variations',
            preparation:{image_ready:true, copy_ready:true, category_ready:true}}},
          {...ready, id:3, ai_status:'pending', title_es:'', title_pt:'',
            ai_original:{}, original_1688:{title:'待生成产品'}}];
        renderAiOriginalProducts(); decorateAiOriginalWorkbench();
    }''')
    cards = page.locator('#ai-original-grid > .ai-original-card')
    sizes = []
    for index in range(3):
        card = cards.nth(index)
        card.scroll_into_view_if_needed()
        box = card.bounding_box()
        sizes.append((box['width'], box['height']))
        assert card.locator('.ai-original-copy > p[lang="es"]').is_visible()
        assert card.locator('.ai-original-copy > p[lang="pt"]').is_visible()
        assert card.locator('.ai-original-copy-details').get_attribute('open') is None
    assert all(abs(w - sizes[0][0]) < 1 and h == sizes[0][1] == 660 for w, h in sizes)
    failed = cards.nth(1)
    failed.scroll_into_view_if_needed()
    issues = failed.locator('.ai-original-issues')
    assert issues.is_visible()
    assert issues.get_attribute('open') is not None
    assert '自动映射已尝试' in issues.inner_text()
    assert issues.locator('img').count() == 0
    assert failed.locator('.ai-original-copy > p[lang="es"]').is_visible()
    assert failed.locator('.ai-original-source > p.bad, .ai-original-source > p.pending').count() == 0
    badge = failed.locator('.ai-original-badge')
    tooltip = page.locator('#ai-original-card-tooltip')
    badge.hover()
    if not tooltip.is_visible():
        page.screenshot(path=str(ROOT / 'artifacts' / f'fixed-card-debug-{width}.png'))
    assert tooltip.is_visible(), badge.evaluate('(el) => ({bounds:el.getBoundingClientRect().toJSON(), notice:el.dataset.cardNotice, hovered:el.matches(":hover"), described:el.getAttribute("aria-describedby")})')
    assert '龟头戴与悟空头戴' in tooltip.inner_text()
    assert '已保存白底主图、双语文案、类目' in tooltip.inner_text()
    assert tooltip.locator('img').count() == 0
    bounds = tooltip.bounding_box()
    assert 0 <= bounds['x'] and bounds['x'] + bounds['width'] <= width
    assert failed.bounding_box()['height'] == 660
    output = ROOT / 'artifacts' / 'ai-original-fixed-cards'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'hover-{width}.png'))
    page.mouse.move(0, 0)
    assert not tooltip.is_visible()
    failed.scroll_into_view_if_needed()
    page.screenshot(path=str(output / f'visible-issues-{width}.png'))
    badge.focus()
    assert tooltip.is_visible()
    badge.press('Escape')
    assert not tooltip.is_visible()
    failed.locator('.ai-original-copy-details > summary').click()
    failed.locator('.ai-original-check-panel > summary').click()
    assert failed.bounding_box()['height'] == 660
    failed.locator('[id^="ai-original-net-"]').fill('15')
    assert failed.locator('.ai-original-manual-checks').inner_text().find('已填写待保存') >= 0
    assert page.evaluate('aiOriginalRows[1].ai_status') == 'failed'
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors


@pytest.mark.parametrize('width', [1440, 390])
def test_editor_highlights_missing_invalid_fields_and_clears_after_correction(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        document.getElementById('ai-original-platform').value = 'mercadolibre';
        aiOriginalRows = [{id:1, ai_status:'failed', original_1688:{title:'测试待修正商品'},
          ai_original:{status:'failed', failed_step:'variations', title_es:'X'.repeat(61),
            attributes:[{id:'GENDER', value_name:'Invalid option'}]}}];
        openAiOriginalEditor(1);
        aiOriginalEditorState.schema = [
          {id:'MATERIAL', name:'材质', required:true},
          {id:'GENDER', name:'性别', required:true, values:[{id:'girls',name:'Girls'}]}];
        renderAiOriginalCategoryFields();
    }""")
    dialog = page.locator('#ai-original-editor-dialog')
    assert dialog.is_visible()
    for suffix in ['category-id', 'title-es', 'title-pt', 'description-es', 'attr-MATERIAL', 'attr-GENDER']:
        control = dialog.locator(f'#ai-original-editor-{suffix}')
        assert control.get_attribute('aria-invalid') == 'true'
        assert control.evaluate('el => getComputedStyle(el).borderTopWidth') == '2px'
    assert dialog.locator('.ai-original-section-issue').inner_text().find('变体 / SKU') >= 0
    dialog.locator('#ai-original-editor-title-es').fill('Disfraz')
    assert dialog.locator('#ai-original-editor-title-es').get_attribute('aria-invalid') is None
    dialog.locator('#ai-original-editor-attr-MATERIAL').fill('Plastic')
    assert dialog.locator('#ai-original-editor-attr-MATERIAL').get_attribute('aria-invalid') is None
    dialog.locator('#ai-original-editor-attr-GENDER').select_option('Girls')
    assert dialog.locator('#ai-original-editor-attr-GENDER').get_attribute('aria-invalid') is None
    output = ROOT / 'artifacts' / 'ai-original-fixed-cards'
    output.mkdir(parents=True, exist_ok=True)
    dialog.locator('#ai-original-editor-category-id').scroll_into_view_if_needed()
    page.screenshot(path=str(output / f'editor-issues-{width}.png'))
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
