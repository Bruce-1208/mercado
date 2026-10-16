import json
import pytest
from test_console_ui_browser import ROOT, console_browser, console_page


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_list_editor_package_sync_and_chinese_labels(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    row = {'id': 1, 'category_id': 'CBT123', 'ai_status': 'pending', 'review_status': 'unreviewed', 'weight_g': 200,
           'net_proceeds_usd': 8, 'package_length_cm': 10, 'package_width_cm': 9, 'package_height_cm': 8,
           'original_1688': {'title': '离线测试产品'}, 'ai_original': {'status': 'completed', 'attributes': [
               {'id': 'PACKAGE_LENGTH', 'value_name': '999 cm'}]}}
    bodies = []
    def save(route):
        body = route.request.post_data_json
        bodies.append(body)
        row.update(body)
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': row}))
    page.route('**/api/ai-original-products/1', save)
    page.route('**/api/ai-original-products/1/listing', save)
    page.route('**/api/ai-original-products?*', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'status':'success','data':{'rows':[row]}})))
    page.route('**/api/ai-original-products/categories/CBT123/attributes*', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'status':'success','data':{'required':[
        {'id':'PACKAGE_LENGTH','name':'Package length','required':True},
        {'id':'PACKAGE_WEIGHT','name':'Package weight','required':True},
        {'id':'CUSTOM','name':'Water resistance','name_zh':'防水性','values':[{'id':'1','name':'Yes','name_zh':'是'}]}], 'optional':[]}})))
    page.evaluate('''row => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        aiOriginalRows=[row]; renderAiOriginalProducts(); decorateAiOriginalWorkbench();
    }''', row)
    page.locator('#ai-original-length-1').fill('25')
    page.locator('#ai-original-width-1').fill('15')
    page.locator('#ai-original-height-1').fill('5')
    page.locator('#ai-original-net-1').fill('12')
    page.locator('#ai-original-weight-1').fill('350')
    page.get_by_role('button', name='保存参数', exact=True).click()
    page.wait_for_function('aiOriginalRows[0].package_length_cm == 25')
    assert bodies[0]['review_status'] == 'unreviewed'
    page.locator('#ai-original-grid .ai-original-edit-button').click()
    editor = page.locator('#ai-original-editor-dialog')
    field = editor.locator('#ai-original-editor-attr-PACKAGE_LENGTH')
    field.wait_for()
    page.wait_for_function("document.getElementById('ai-original-editor-dialog').getAnimations().every(a => a.playState === 'finished')")
    assert field.input_value() == '25'
    assert editor.locator('#ai-original-editor-weight').input_value() == '350'
    assert editor.locator('#ai-original-editor-net').input_value() == '12'
    assert '包装长度' in field.locator('..').inner_text()
    assert '防水性' in editor.locator('#ai-original-editor-attr-CUSTOM').locator('..').inner_text()
    assert editor.locator('#ai-original-editor-attr-CUSTOM option').last.inner_text() == 'Yes · 是'
    field.fill('30')
    assert editor.locator('#ai-original-editor-length').input_value() == '30'
    editor.locator('#ai-original-editor-weight').fill('420')
    assert editor.locator('#ai-original-editor-attr-PACKAGE_WEIGHT').input_value() == '420'
    editor.get_by_role('button', name='类目字段', exact=True).focus()
    page.keyboard.press('Enter')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = ROOT / 'artifacts/ai-original-editor-sync'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'fields-{width}.png'))
    editor.get_by_role('button', name='保存并同步', exact=True).click()
    page.wait_for_function('!document.getElementById("ai-original-editor-dialog").open')
    assert bodies[-1]['package_length_cm'] == '30'
    assert bodies[-1]['weight_g'] == '420'
    assert next(a for a in bodies[-1]['attributes'] if a['id'] == 'PACKAGE_LENGTH')['value_name'] == '30 cm'
    assert not errors
