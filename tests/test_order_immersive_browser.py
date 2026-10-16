"""Offline mobile visibility and reversible workspace/row-only immersion."""
from pathlib import Path
import pytest
from test_console_ui_browser import console_browser, console_page


def seed(page):
    page.evaluate('''() => {
      document.querySelectorAll('.tab-page').forEach(e=>e.classList.toggle('active',e.id==='tab-orders'));
      renderOrderRows(Array.from({length:24},(_,i)=>({id:String(2000018902300000+i),pack_id:String(2000018902300000+i),
        country:'墨西哥',status:'已发-在途中',shop_name:'示例店铺',source:'美客多',amount:20,fee:3,
        actual_freight_usd:4,quoted_freight_usd:4,balance:13,balance_cny:91,income:140,cost:32,profit:59,
        purchase_order:'5127514670081027336',purchase_tracking:'YT12345678900',
        order_remark:i===1?'请核对尺码，优先发货':'',
        sku_items:[{title:'女士轻便休闲运动鞋',seller_sku:'SKU-A100',quantity:1}]})));
    }''')


@pytest.mark.parametrize('width,height',[(390,844),(375,667),(1440,900),(1024,768)])
def test_mobile_and_immersive_controls(console_page,width,height):
    p=console_page;p.set_viewport_size({'width':width,'height':height});p.reload();seed(p)
    p.wait_for_timeout(100)
    if width<761:
        assert p.locator('.topbar').is_hidden()
        assert p.locator('.sidebar').is_hidden()
        assert p.locator('#order-filter-region').is_hidden()
        assert p.locator('.order-table-wrap').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
        count=p.locator('#order-table-body tr').evaluate_all('(rows)=>rows.filter(e=>{const b=e.getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight-60}).length')
        assert count>=6,count
    p.locator('#order-list-focus-toggle').click()
    p.locator('#order-search-input').fill('保留筛选')
    p.locator('#order-rows-only-toggle').click()
    assert p.locator('#order-filter-region').is_hidden()
    assert p.locator('.topbar').is_hidden()
    assert p.locator('#tab-orders > .tab-heading').is_hidden()
    assert p.locator('.order-pagination').is_hidden()
    assert p.locator('#order-rows-restore').is_visible()
    assert p.evaluate('document.documentElement.scrollWidth<=innerWidth')
    out=Path(__file__).resolve().parents[1]/'artifacts/order-immersive-20261013';out.mkdir(exist_ok=True)
    p.screenshot(path=str(out/f'rows-{width}x{height}.png'))
    p.locator('#order-table-body tr').first.locator('.order-product strong').click()
    assert p.locator('#order-detail-dialog').is_visible()
    p.keyboard.press('Escape')
    assert p.locator('#order-rows-restore').is_visible()
    p.locator('#order-rows-restore').click()
    assert p.locator('#tab-orders > .tab-heading').is_visible()
    assert p.locator('#order-rows-only-toggle').evaluate('(e)=>e===document.activeElement')
    p.locator('#order-list-focus-toggle').click()
    assert p.locator('#order-search-input').input_value()=='保留筛选'
    p.locator('#order-rows-only-toggle').click()
    p.locator('#order-table-body .order-row-checkbox').first.check()
    assert p.locator('#order-bulk-bar').is_visible()
    assert p.locator('#order-rows-restore').is_hidden()
    p.locator('#workbench-immersive-exit').click()
    assert p.locator('.topbar').is_visible()
    # All modules share the single topbar entry and a reliable keyboard exit.
    p.evaluate("document.querySelectorAll('.tab-page').forEach(e=>e.classList.toggle('active',e.id==='tab-inventory'))")
    assert p.locator('[data-immersive-toggle]').count()==1
    assert p.locator('.tab-page [data-immersive-toggle]').count()==0
    p.locator('.topbar [data-immersive-toggle]').click()
    assert p.locator('.topbar').is_hidden()
    p.keyboard.press('Escape')
    assert p.locator('.topbar').is_visible()
    assert p.locator('.topbar [data-immersive-toggle]').evaluate('(e)=>e===document.activeElement')
