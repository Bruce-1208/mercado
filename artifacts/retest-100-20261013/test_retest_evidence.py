import json
from pathlib import Path
import pytest
from test_console_ui_browser import console_browser, console_page
OUT=Path('/Users/a11/mercado/artifacts/retest-100-20261013')

def test_mobile_navigation_settled_and_scrollable(console_page):
    page=console_page
    page.set_viewport_size({'width':390,'height':844})
    page.reload()
    page.wait_for_timeout(350)
    toggle=page.locator('#sidebar-toggle')
    toggle.click();page.keyboard.press('Escape')
    page.wait_for_timeout(350)
    metrics=page.evaluate("""() => {
      const nav=document.querySelector('.sidebar');const r=nav.getBoundingClientRect();
      return {sidebarRight:r.right,visibility:getComputedStyle(nav).visibility,navInert:document.getElementById('console-navigation').inert,
        width:document.documentElement.scrollWidth,viewport:innerWidth,bodyOverflow:getComputedStyle(document.body).overflow,
        scrollers:[...document.querySelectorAll('main,.content,.page,.order-table-wrap')].map(e=>({cls:e.className,overflow:getComputedStyle(e).overflowY,height:e.clientHeight,scroll:e.scrollHeight}))};
    }""")
    (OUT/'mobile-navigation.json').write_text(json.dumps(metrics,indent=2))
    page.screenshot(path=str(OUT/'screenshots/mobile-settled.png'),full_page=True)
    assert metrics['sidebarRight'] <= 0
    assert metrics['navInert']
    assert metrics['width'] <= metrics['viewport']
    assert toggle.evaluate('(e)=>e===document.activeElement')
