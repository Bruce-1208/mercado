"""Missing SKU sources produce one actionable notice, not downstream failures."""
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 390])
def test_missing_sources_are_deduplicated_and_pending_steps_are_explicit(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate('''() => {
      document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
      document.getElementById('tab-ai-original-products').classList.add('active');
      document.getElementById('ai-original-platform').value = 'mercadolibre';
      const variants = ['S','M','L'].map((label,i) => ({sku_id:String(i+1),label,image_url:'',
        attribute_combinations:[{id:'SIZE',value_name:label}]}));
      const error = 'SKU 1 缺少采集主图，请补充该 SKU 的图片链接后重新生成';
      aiOriginalRows = [{id:1,ai_status:'failed',ai_error:error,category_id:'CBT123',
        title_es:'Disfraz infantil con guantes',title_pt:'Fantasia infantil com luvas',
        description_es:'Disfraz infantil',description_pt:'Fantasia infantil',
        weight_g:400,package_length_cm:30,package_width_cm:20,package_height_cm:5,
        original_1688:{title:'模拟服装',variations:variants},
        ai_original:{status:'failed',failed_step:'sku_images',error,category_id:'CBT123',
          preparation:{required_ready:true,variations_ready:true},variations:variants,
          attributes:[{id:'MATERIAL',value_name:'Polyester'}],
          input_blockers:[{field:'sku_images',message:'3 个 SKU 缺少采集原图，请重新采集或补充图片链接'}]}}];
      renderAiOriginalProducts(); decorateAiOriginalWorkbench();
    }''')
    panel = page.locator('.ai-original-issues')
    panel.scroll_into_view_if_needed()
    text = panel.text_content()
    assert text.count('缺少采集原图') == 1
    assert 'SKU 1 缺少采集主图' not in text
    assert '平台语言规则尚未校验' in text
    assert '商品白底主图待 SKU 图片处理完成后生成' in text
    assert '平台语言规则生成失败' not in text
    assert '净收益待补齐' in text
    assert panel.locator('li').count() == 4
    panel.locator('summary').focus()
    assert panel.locator('summary').evaluate('(el) => document.activeElement === el')
    page.keyboard.press('Enter')
    assert panel.get_attribute('open') is None
    page.keyboard.press('Enter')
    page.evaluate('toggleAllAiOriginalProducts(true)')
    assert page.evaluate('aiOriginalSelected.size') == 1
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    directory = ROOT / 'artifacts/sku-image-repair-1041447098398'
    directory.mkdir(exist_ok=True)
    page.screenshot(path=str(directory / f'diagnostics-{width}.png'))
    # Once photos are supplied, stale source errors must not ask for recollection.
    result = page.evaluate('''() => {
      const row = aiOriginalRows[0];
      row.original_1688.variations.forEach(v => v.image_url='https://example.com/source.jpg');
      return aiOriginalConsolidateIssues(row, [row.ai_error]);
    }''')
    assert len(result) == 1 and 'SKU 原图已齐' in result[0]
