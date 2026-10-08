"""Role visibility and narrow-panel layout, without starting collection jobs."""
import json
from pathlib import Path

import pytest
from test_zeshun_popup_browser import browser, EXTENSION


@pytest.mark.parametrize('role', ['super_admin', 'enterprise_admin', 'operator', None])
@pytest.mark.parametrize('width', [320, 420, 600])
def test_role_visibility_and_layout(browser, role, width):
    page = browser.new_page(viewport={'width': width, 'height': 900})
    page.route('https://extension.test/**', lambda route: route.fulfill(
        path=str(EXTENSION / route.request.url.rsplit('/', 1)[-1])))
    page.add_init_script('window.testRole = ' + json.dumps(role) + ';' + """
      window.messages = [];
      window.chrome = {
        tabs: {query:async () => []},
        runtime: {openOptionsPage:() => {}, sendMessage:(message, callback) => {
          messages.push(message);
          if(message.type === 'GET_STATE') return callback({authenticated:!!window.testRole,
            user:{username:'测试账号',role_key:window.testRole}});
          callback({ok:true, categories:[], run:null});
        }}
      };
    """)
    page.goto('https://extension.test/popup.html')
    page.wait_for_function("!document.querySelector('#auth-status').textContent.includes('正在检查')")
    assert page.locator('#yandex-mode').is_visible() == (role == 'super_admin')
    assert page.locator('#yandex-support').is_visible() == (role == 'super_admin')
    page.evaluate("showMode('yandex')")
    assert page.locator('#yandex-panel').is_visible() == (role == 'super_admin')
    if role != 'super_admin':
        assert not page.evaluate("messages.some(m => m.type === 'GET_YANDEX_SEARCH_STATUS')")
    else:
        page.evaluate("window.testRole = 'operator'; refreshState()")
        assert page.locator('#yandex-panel').is_hidden()
        assert page.locator('#product-panel').is_visible()
    for mode in ['product', 'zying', 'zying-infringement', 'purchase']:
        page.evaluate('(mode) => showMode(mode)', mode)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.evaluate("showMode('product')")
    output = Path(__file__).resolve().parents[1] / 'artifacts/zeshun-ui-1.8.45'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'{role or "logged-out"}-{width}.png'), full_page=True)
    page.close()


def test_focus_recovers_account_display_and_old_login_error(browser):
    page = browser.new_page()
    page.route('https://extension.test/**', lambda route: route.fulfill(
        path=str(EXTENSION / route.request.url.rsplit('/', 1)[-1])))
    page.add_init_script("""
      window.loggedIn = false;
      window.chrome = {tabs:{query:async()=>[]}, runtime:{sendMessage:(message,callback)=> {
        if(message.type==='GET_STATE') return callback({authenticated:loggedIn,user:loggedIn?{username:'collector',role_key:'operator'}:null});
        callback({ok:true,categories:[]});
      }}};
    """)
    page.goto('https://extension.test/popup.html')
    page.wait_for_function("document.querySelector('#auth-status').classList.contains('logged-out')")
    page.evaluate("showResult('请先登录泽顺控制台账号', 'error'); window.loggedIn=true; window.dispatchEvent(new Event('focus'))")
    page.wait_for_function("document.querySelector('#auth-status').classList.contains('logged-in')")
    assert page.locator('#result').is_hidden()
    assert 'collector' in page.locator('#auth-status').inner_text()
    assert page.locator('#yandex-mode').is_hidden()
    page.close()
