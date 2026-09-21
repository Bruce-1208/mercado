import json
import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
template = (ROOT / 'bit/templates/index.html').read_text(encoding='utf-8')
start = template.index('<div class="tab-page ai-original-page"')
end = template.index('<div class="tab-page mercado-workbench"', start)
styles = '\n'.join(re.findall(r'<style[^>]*>(.*?)</style>', template, re.S))
links = ''.join(f'<link rel="stylesheet" href="/static/{name}">' for name in ['ai-original-products.css', 'ai-original-workbench.css'])
scripts = ''.join(f'<script defer src="/static/{name}"></script>' for name in ['ai-original-products.js', 'ai-original-workbench.js'])
html = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>{styles}</style>{links}
<style>body{{display:block!important;background:#f4f6fa!important;padding:24px!important;margin:0!important;min-width:0!important}}#tab-ai-original-products{{display:block!important;max-width:1380px;margin:auto}}@media(max-width:760px){{body{{padding:10px!important}}}}</style>{scripts}</head><body>{template[start:end]}</body></html>'''
prepared = {'title_es': 'Organizador de escritorio de bambú con dos cajones', 'title_pt': 'Organizador de mesa de bambu com duas gavetas', 'description_es': 'Organizador de bambú. Dos cajones para guardar artículos de oficina.', 'description_pt': 'Organizador de bambu. Duas gavetas para materiais de escritório.', 'attributes': [{'id':'MATERIAL','name':'Material','value_name':'Bambú'}, {'id':'COLOR','name':'Color','value_name':'Natural'}], 'main_image_url':'https://workbench.test/sample-ai-white.jpg','image_generation_method':'ai_image_edit'}
rows = [dict(id=101, title='竹制桌面收纳盒 双抽屉办公文具整理架', source_item_id='1688123456789', ai_status='completed', review_status='approved', weight_g=650, weight_basis='ai_original_manual', net_proceeds_usd=18.5, category_id='CBT1234', main_image_url=prepared['main_image_url'], ai_original=prepared, original_1688={'title':'竹制桌面收纳盒 双抽屉办公文具整理架','source_1688_item_id':'123456789','price':28.8,'main_image_url':'https://workbench.test/sample.jpg','source_url':'https://detail.1688.com/offer/123456789.html'}, **{k:v for k,v in prepared.items() if k in ('title_es','title_pt','description_es','description_pt')}), dict(id=102, title='便携折叠桌面支架', source_item_id='1688987654321', ai_status='pending', review_status='unreviewed', ai_original={}, original_1688={'title':'便携折叠桌面支架','source_1688_item_id':'987654321','price':9.6}, main_image_url='https://workbench.test/sample.jpg')]
with sync_playwright() as pw:
    paths = [os.environ.get('CONSOLE_TEST_BROWSER',''), pw.chromium.executable_path, 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe']
    executable = next(p for p in paths if p and Path(p).is_file())
    browser = pw.chromium.launch(executable_path=executable, headless=True)
    page = browser.new_page(viewport={'width':1440,'height':1100})
    errors=[]
    page.on('pageerror', lambda err: errors.append(str(err)))
    def route(request):
        url=request.request.url
        if '/static/' in url:
            request.fulfill(path=str(ROOT/'bit/static'/url.rsplit('/',1)[-1]))
        elif url.endswith('.jpg'):
            request.fulfill(status=404, body='')
        elif '/categories/' in url:
            request.fulfill(json={'status':'success','data':{'attributes':[]}})
        else:
            request.fulfill(content_type='text/html', body=html)
    page.route('**/*', route)
    page.goto('https://workbench.test/')
    page.evaluate('(rows) => { aiOriginalRows=rows; renderAiOriginalProducts(); }', rows)
    page.wait_for_function("document.querySelectorAll('.ai-original-card[data-decorated]').length===2")
    assert page.locator('#ai-original-count-ready').inner_text()=='1'
    assert page.locator('#ai-original-count-pending').inner_text()=='1'
    assert page.locator('.ai-original-settings-disclosure').get_attribute('open') is None
    assert page.locator('#ai-original-process').is_visible()
    page.locator('.ai-original-card input[type=checkbox]').first.check()
    assert page.locator('#ai-original-publish').is_enabled()
    assert page.locator('#ai-original-select-all').evaluate('(e)=>e.indeterminate')
    page.locator('#ai-original-select-all').check()
    assert page.locator('#ai-original-publish').is_disabled()
    page.evaluate('renderAiOriginalTaskStatus({running:true,status:"running"})')
    page.locator('.ai-original-card input[type=checkbox]').last.uncheck()
    assert page.locator('#ai-original-process').is_disabled()
    page.evaluate('renderAiOriginalTaskStatus({running:false,status:"idle"})')
    for width in (1440,1024,390):
        page.set_viewport_size({'width':width,'height':1100})
        page.screenshot(path=str(ROOT/f'.data/ai-original-{width}.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Overflow at {width}'
    page.set_viewport_size({'width':1440,'height':1100})
    page.locator('.ai-original-edit-button').first.click()
    assert page.locator('#ai-original-editor-dialog').is_visible()
    assert page.locator('#ai-original-editor-title-es').input_value()==prepared['title_es']
    assert not errors, errors
    print('PASS: desktop/tablet/mobile layout, draft checks, select-all, publish gating, running-state lock, editor integration; no browser errors.')
    browser.close()
