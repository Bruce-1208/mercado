"""Single-shop appeal UI contracts; all browser and business calls are mocked."""
from pathlib import Path

import pytest

EXTENSION = Path(__file__).resolve().parents[1] / "browser_extension/zeshun_collector"


@pytest.fixture(scope="module")
def browser():
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).is_file():
            pytest.skip("Chromium is required")
        instance = pw.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def popup(browser):
    page = browser.new_page(viewport={"width": 420, "height": 950})
    page.route("https://extension.test/**", lambda route: route.fulfill(
        path=str(EXTENSION / route.request.url.rsplit("/", 1)[-1])))
    page.add_init_script("""
      window.messages = [];
      window.mockIdentity = {seller_id:'100',root_seller_id:'100',shop_name:'当前店铺',
        sites:[{site_id:'MLM',site_code:'MX',site_name:'墨西哥',seller_id:'101'},
               {site_id:'MLB',site_code:'BR',site_name:'巴西',seller_id:'102'}]};
      window.mockAppeal = null;
      window.mockTabs = [{id:7,active:true,lastAccessed:100,windowType:'normal',
        title:'当前店铺后台',url:'https://global-selling.mercadolibre.com/help'}];
      window.chrome = {
        tabs:{query:async query => mockTabs.filter(tab => !query.windowType || tab.windowType === query.windowType),
          sendMessage:(_id,_message,callback) => callback({ok:true,detail:false})},
        runtime:{openOptionsPage:() => {},sendMessage:(message,callback) => {
          messages.push(message);
          if(message.type === 'GET_STATE') return callback({ok:true,authenticated:true,user:{username:'test'}});
          if(message.type === 'GET_ZESHUN_CATEGORIES') return callback({ok:true,categories:[]});
          if(message.type === 'GET_APPEAL_CONTEXT') return callback({ok:true,context:{identity:mockIdentity}});
          if(message.type === 'GET_APPEAL_STATUS') return callback({ok:true,state:mockAppeal});
          if(message.type === 'START_APPEAL') {
            if(window.holdStart) {window.pendingStart=callback;return;}
            mockAppeal={running:true,status:'running',shop:mockIdentity,message:'正在申诉',
              selection:message.params,
              completed_groups:1,sent_groups:1,failed_groups:0,logs:['已发送首组']};
            return callback({ok:true,state:mockAppeal});
          }
          if(message.type === 'STOP_APPEAL') {
            mockAppeal={...mockAppeal,running:false,status:'stopped',message:'已停止'};
            return callback({ok:true,state:mockAppeal});
          }
          callback({ok:true,state:{running:false},ready:true,categories:[]});
        }}
      };
    """)
    page.goto("https://extension.test/popup.html")
    page.locator("#appeal-mode").click()
    page.wait_for_function("!document.querySelector('#appeal-start').disabled")
    yield page
    page.close()


def test_current_shop_binding_and_agent_parameters(popup):
    assert "当前店铺" in popup.locator("#appeal-shop").inner_text()
    assert popup.locator("#product-panel").is_hidden()
    popup.locator('#appeal-sites input[value="MLB"]').check()
    popup.locator('#appeal-forms input[value="侵权"]').check()
    popup.locator("#appeal-copy-mode").select_option("AI话术模式")
    assert popup.locator("#appeal-copy-hint").is_visible()
    popup.locator("#appeal-loop-count").select_option("1")
    popup.locator("#appeal-message").fill("自定义话术")
    popup.locator("#appeal-start").click()
    popup.wait_for_function("messages.some(m => m.type === 'START_APPEAL')")
    message = popup.evaluate("messages.find(m => m.type === 'START_APPEAL')")
    assert message == {"type": "START_APPEAL", "tabId": 7, "params": {
        "sites": ["MLM", "MLB"], "forms": ["延误", "侵权"],
        "mode": "AI客服", "appeal_copy_mode": "AI话术模式", "loop_count": "1", "message": "自定义话术",
    }}
    assert popup.locator("#appeal-start").is_disabled()
    assert popup.locator("#appeal-refresh").is_disabled()
    assert popup.locator("#appeal-tab").is_disabled()
    assert popup.locator("#appeal-stop").is_enabled()
    assert "已发送 1" in popup.locator("#appeal-summary").inner_text()
    assert "已发送首组" in popup.locator("#appeal-log").inner_text()


def test_busy_start_blocks_repeat_and_human_mode_uses_normal_copy(popup):
    popup.locator("#appeal-copy-mode").select_option("AI话术模式")
    popup.locator("#appeal-customer-mode").select_option("人工客服")
    assert popup.locator("#appeal-copy-mode").input_value() == "普通模式"
    assert popup.locator('#appeal-copy-mode option[value="AI话术模式"]').get_attribute("disabled") is not None
    popup.evaluate("window.holdStart=true")
    popup.locator("#appeal-start").click()
    assert popup.locator("#appeal-start").is_disabled()
    assert popup.locator("#appeal-refresh").is_disabled()
    popup.evaluate("pendingStart({ok:false,error:'当前账号已变化，请重新读取店铺'});holdStart=false")
    popup.wait_for_function("!document.querySelector('#appeal-start').disabled")
    assert "当前账号已变化" in popup.locator("#result").inner_text()


def test_reopening_mode_reads_progress_without_starting_again(popup):
    popup.locator("#appeal-start").click()
    popup.locator("#product-mode").click()
    assert popup.locator("#appeal-panel").is_hidden()
    popup.locator("#appeal-mode").click()
    assert popup.locator("#appeal-stop").is_enabled()
    assert popup.evaluate("messages.filter(m=>m.type==='START_APPEAL').length") == 1
    popup.locator("#appeal-stop").click()
    popup.wait_for_function("document.querySelector('#appeal-status').textContent === '已停止'")
    assert popup.locator("#appeal-start").is_enabled()


def test_cannot_start_without_selected_site_or_form(popup):
    popup.locator('#appeal-sites input[value="MLM"]').uncheck()
    assert popup.locator("#appeal-start").is_disabled()
    popup.locator('#appeal-sites input[value="MLB"]').check()
    popup.locator('#appeal-forms input[value="延误"]').uncheck()
    assert popup.locator("#appeal-start").is_disabled()
    popup.locator('#appeal-forms input[value="投诉"]').check()
    assert popup.locator("#appeal-start").is_enabled()


@pytest.mark.parametrize("address", [
    {"url": "https://global-selling.mercadolibre.com"},
    {"url": "https://global-selling.mercadolibre.com/sales/summary?site=MLM#orders"},
    {"url": "about:blank", "pendingUrl": "https://global-selling.mercadolibre.com/help"},
    {"url": "https://sellers.mercadolibre.com"},
    {"url": "https://sellers.mercadolibre.com/orders/omni/list?filters=TAB_TODAY"},
    {"url": "about:blank", "pendingUrl": "https://sellers.mercadolibre.com/orders/omni/list?filters=TAB_TODAY"},
])
def test_domain_prefix_finds_shop_in_popup_window(popup, address):
    popup.evaluate("address => {mockTabs=[{id:9,active:true,windowType:'popup',title:'独立店铺窗口',...address}];}", address)
    popup.locator("#appeal-refresh").click()
    popup.wait_for_function("document.querySelector('#appeal-tab').value === '9' && !document.querySelector('#appeal-start').disabled")
    assert "当前店铺" in popup.locator("#appeal-shop").inner_text()
    assert popup.evaluate("messages.filter(m=>m.type==='GET_APPEAL_CONTEXT').length") == 0


def test_domain_prefix_does_not_match_other_hosts_or_redirect_parameters(popup):
    popup.evaluate("""() => {mockTabs = [
      {id:8,url:'https://global-selling.mercadolibre.com.example.test/help'},
      {id:9,url:'https://example.test/?next=https://global-selling.mercadolibre.com/help'},
      {id:10,url:'https://sellers.mercadolibre.com.example.test/orders/omni/list'},
      {id:11,url:'https://example.test/?next=https://sellers.mercadolibre.com/orders/omni/list'}
    ];}""")
    count = popup.evaluate("messages.filter(m=>m.type==='GET_APPEAL_CONTEXT').length")
    popup.locator("#appeal-refresh").click()
    popup.wait_for_function("document.querySelector('#appeal-tab').options.length === 1")
    assert popup.locator("#appeal-start").is_enabled()
    assert popup.locator("#appeal-tab").input_value() == ""
    assert popup.evaluate("messages.filter(m=>m.type==='GET_APPEAL_CONTEXT').length") == count


def test_start_does_not_require_reading_shop_identity(popup):
    assert popup.evaluate("messages.filter(m=>m.type==='GET_APPEAL_CONTEXT').length") == 0
    assert popup.locator('#appeal-sites input').count() == 6
    popup.locator('#appeal-start').click()
    message = popup.evaluate("messages.find(m=>m.type==='START_APPEAL')")
    assert 'expected_seller_id' not in message['params']


def test_refresh_preserves_selected_sites(popup):
    popup.locator('#appeal-sites input[value="MLB"]').check()
    popup.locator('#appeal-refresh').click()
    assert popup.locator('#appeal-sites input[value="MLB"]').is_checked()


def test_no_backend_tab_starts_directly_without_login_preflight(popup):
    popup.evaluate("mockTabs=[]")
    popup.locator("#appeal-refresh").click()
    popup.wait_for_function("document.querySelector('#appeal-tab').options.length === 1")
    popup.locator("#appeal-start").click()
    message = popup.evaluate("messages.find(m=>m.type==='START_APPEAL')")
    assert message["tabId"] == 0
    assert popup.evaluate("messages.filter(m=>m.type==='GET_APPEAL_CONTEXT').length") == 0


def test_logged_out_task_reminder_survives_reopening(popup):
    popup.evaluate("""mockAppeal={running:false,status:'failed',
      error:'当前美客多店铺未登录，申诉任务已停止。请登录后台后重新启动',
      message:'当前美客多店铺未登录，申诉任务已停止。请登录后台后重新启动'}""")
    popup.locator("#product-mode").click()
    popup.locator("#appeal-mode").click()
    assert "未登录，申诉任务已停止" in popup.locator("#appeal-status").inner_text()
    assert popup.locator("#appeal-start").is_enabled()
    assert popup.locator("#appeal-stop").is_disabled()
