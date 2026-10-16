"""Validate visible rows, filter preservation and batch access using offline data."""
import json
from pathlib import Path
import pytest
from test_console_ui_browser import console_browser, console_page

@pytest.mark.parametrize('width,height', [(1366,768),(1366,650),(1280,720),(1440,900),(1024,768),(390,844)])
def test_laptop_visible_rows_and_controls(console_page,width,height):
    page=console_page
    page.set_viewport_size({'width':width,'height':height})
    page.reload()
    page.evaluate('''() => {
        document.querySelectorAll('.tab-page').forEach(el=>el.classList.remove('active'));
        document.getElementById('tab-orders').classList.add('active');
        renderOrderRows(Array.from({length:24},(_,i)=>({id:String(2000018902300000+i),pack_id:String(2000018902300000+i),
          country:'墨西哥',status:'已发-在途中',shop_name:'墨西哥旗舰店',source:'美客多',salesperson:'业务员',
          amount:20,fee:3,actual_freight_usd:4,quoted_freight_usd:4,balance:13,balance_cny:91,income:140,cost:32,profit:59,
          purchase_order:'5127514670081027336',purchase_tracking:'YT12345678900',platform_tracking_number:'CNMX12345678900',
          ordered_at:'2026-10-13 12:30:00',sku_items:[{title:'女士轻便休闲运动鞋',seller_sku:'SKU-A100',quantity:1}]})));
        setOrderDensity('compact',false);
    }''')
    toggle=page.locator('#order-list-focus-toggle')
    assert toggle.get_attribute('aria-expanded')=='false'
    page.wait_for_timeout(100)
    if width>=761:
        metrics=page.locator('.order-table-wrap').evaluate('''e=>{const box=e.getBoundingClientRect(); const top=e.querySelector('thead').getBoundingClientRect().bottom;
          const rows=[...e.querySelectorAll('tbody tr')].map(r=>r.getBoundingClientRect());return {visible:rows.filter(r=>r.top>=top&&r.bottom<=Math.min(box.bottom,innerHeight)).length,rowHeight:rows[0].height,tableHeight:box.height};}''')
        assert metrics['visible']>=8, metrics
        output=Path(__file__).resolve().parents[1]/'artifacts/order-laptop-20261013';output.mkdir(exist_ok=True)
        page.screenshot(path=str(output/f'{width}x{height}.png'))
        (output/f'{width}x{height}.json').write_text(json.dumps(metrics))
    toggle.click()
    page.locator('#order-search-input').fill('保留筛选')
    toggle.click()
    assert page.locator('#order-filter-region').is_hidden()
    toggle.focus();page.keyboard.press('Enter')
    assert page.locator('#order-search-input').input_value()=='保留筛选'
    assert toggle.evaluate('(el)=>el===document.activeElement')
    toggle.click()
    page.locator('#order-table-body .order-row-checkbox').first.check()
    assert page.locator('#order-bulk-bar').is_visible()
    assert toggle.get_attribute('aria-expanded')=='true'
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
