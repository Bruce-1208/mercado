import sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests'))
from test_console_ui_browser import console_browser, console_page

@pytest.mark.parametrize('width',[1440,1024,390])
@pytest.mark.parametrize('mode',['1688','collection','products','ai'])
def test_tiles(console_page,width,mode):
    page=console_page
    page.set_viewport_size({'width':width,'height':1000})
    selector=page.evaluate('''mode => {
      document.querySelectorAll('.tab-page').forEach(el=>el.classList.remove('active'));
      const rows=Array.from({length:4},(_,i)=>({id:i+1,title:'桌面金属收纳盒 · 多功能文具整理',source_item_id:'16880000'+i,
        ai_status:'completed',review_status:'approved',price:18.5,weight_g:250,net_proceeds_usd:12.5,
        package_length_cm:12,package_width_cm:8,package_height_cm:20,sale_price_usd:24,
        title_es:'Organizador de escritorio',title_pt:'Organizador',description_es:'Metal',description_pt:'Metal',
        category_id:'CBT123', ai_original:{status:'completed',attributes:[]},
        original_1688:{title:'桌面金属收纳盒',price:18.5}}));
      if(mode==='ai') {
        document.getElementById('tab-ai-original-products').classList.add('active');
        aiOriginalRows=rows; renderAiOriginalProducts(); decorateAiOriginalWorkbench();
        return '#ai-original-grid > .ai-original-card';
      }
      if(mode==='1688') {
        document.querySelector('.products-1688-page').classList.add('active');
        products1688Rows=rows; products1688RenderRows(); return '#products-1688-body > tr';
      }
      const module=document.getElementById('mercado-list-module');
      module.closest('.tab-page').classList.add('active');
      module.classList.toggle('product-mode',mode==='products');
      module.classList.toggle('collection-mode',mode==='collection');
      mercadoListMode=mode; renderMercadoListRows(rows);
      return mode==='products'?'#mercado-list-body .market-product-card':'#mercado-list-body > tr';
    }''',mode)
    cards=page.locator(selector)
    assert cards.count()==4
    cards.first.scroll_into_view_if_needed()
    sizes=cards.evaluate_all('''els=>els.map(el=>{const b=el.getBoundingClientRect();return {x:b.x,y:b.y,w:b.width,overflow:el.scrollWidth-el.clientWidth}})''')
    assert max(s['overflow'] for s in sizes)<=1,sizes
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),sizes
    assert (sizes[1]['x']>sizes[0]['x']) == (width>=1024),sizes
    if mode == 'ai':
        draft = cards.first.locator('.ai-original-copy-details')
        assert draft.get_attribute('open') is None
        draft.locator(':scope > summary').click()
        assert draft.get_attribute('open') is not None
        draft.locator(':scope > summary').click()
    cards.first.screenshot(path=str(Path(__file__).parent/f'compact-card-{mode}-{width}.png'))
    page.screenshot(path=str(Path(__file__).parent/f'{mode}-{width}.png'),full_page=True)
