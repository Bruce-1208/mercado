"""Offline filter controls; procurement tabs and real sync actions are mocked."""
from pathlib import Path
import pytest

EXTENSION = Path(__file__).resolve().parents[1] / 'browser_extension/zeshun_collector'


@pytest.fixture(scope='module')
def browser():
    api = pytest.importorskip('playwright.sync_api')
    with api.sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).is_file():
            pytest.skip('Chromium is required')
        instance = pw.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def popup(browser):
    page = browser.new_page(viewport={'width': 390, 'height': 900})
    page.route('**/*', lambda route: route.fulfill(path=str(EXTENSION / route.request.url.rsplit('/', 1)[-1]))
               if route.request.url.startswith('https://extension.test/') else route.abort())
    page.add_init_script('''
      window.messages=[];window.mockTask={phase:'idle',running:false};
      window.filterOptions={salespeople:['小王','小李'],groups:['一组','二组'],fixed_salesperson:''};
      window.chrome={tabs:{query:async()=>[],sendMessage:(_id,_msg,cb)=>cb({ok:true})},
        runtime:{openOptionsPage:()=>{},sendMessage:(msg,cb)=>{
          messages.push(msg);
          if(msg.type==='GET_STATE') return cb({ok:true,authenticated:true,user:{username:'test'}});
          if(msg.type==='GET_PURCHASE_TRACKING_OPTIONS') return cb({ok:true,...filterOptions});
          if(msg.type==='GET_PURCHASE_TRACKING_REQUEST') return cb({ok:true,...mockTask});
          if(msg.type==='OPEN_PURCHASE_TRACKING_LOGIN') {
            mockTask={...msg,request_id:'request-one',running:true,phase:'waiting_login',total:1,message:'等待登录'};
            return cb({ok:true,...mockTask});
          }
          if(msg.type==='RESET_PURCHASE_TRACKING_DATES') {mockTask={phase:'error',running:false};return cb({ok:true,...mockTask});}
          cb({ok:true,state:{running:false},categories:[]});
        }}};
    ''')
    page.goto('https://extension.test/popup.html')
    page.locator('#purchase-mode').click()
    page.wait_for_function("document.querySelector('#purchase-open-login').disabled===false")
    yield page
    page.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_all_filters_are_submitted_and_locked_for_task(popup, width):
    popup.set_viewport_size({'width': width, 'height': 1000})
    popup.locator('#purchase-date-start').fill('2026-10-01')
    popup.locator('#purchase-date-end').fill('2026-10-03')
    popup.locator('#purchase-order-status').select_option('@找货')
    popup.locator('#purchase-platform').select_option('pdd')
    popup.locator('#purchase-salesperson').select_option('小王')
    popup.locator('#purchase-group-name').select_option('一组')
    popup.locator('#purchase-salesperson').focus()
    assert popup.evaluate('document.activeElement.id') == 'purchase-salesperson'
    popup.locator('#purchase-open-login').click()
    popup.wait_for_function("document.querySelector('#purchase-date-start').disabled")
    msg = popup.evaluate("messages.find(m=>m.type==='OPEN_PURCHASE_TRACKING_LOGIN')")
    assert {k: msg[k] for k in ['date_start', 'date_end', 'order_status', 'platform', 'salesperson', 'group_name']} == {
        'date_start': '2026-10-01', 'date_end': '2026-10-03', 'order_status': '@找货', 'platform': 'pdd',
        'salesperson': '小王', 'group_name': '一组'}
    for name in ['date-start', 'date-end', 'order-status', 'platform', 'salesperson', 'group-name']:
        assert popup.locator('#purchase-' + name).is_disabled()
    assert popup.locator('#purchase-confirm-login').is_enabled()
    assert '2026-10-01 至 2026-10-03' in popup.locator('#purchase-task-meta').inner_text()
    assert '小王' in popup.locator('#purchase-task-meta').inner_text()
    assert '一组' in popup.locator('#purchase-task-meta').inner_text()
    assert popup.evaluate('document.documentElement.scrollWidth <= innerWidth')
    artifact = Path(__file__).resolve().parents[1] / 'artifacts/purchase-tracking-filters-20261012'
    artifact.mkdir(parents=True, exist_ok=True)
    popup.screenshot(path=str(artifact / f'popup-{width}.png'), full_page=True)
    popup.locator('#purchase-reset-dates').click()
    popup.wait_for_function("document.querySelector('#purchase-date-start').disabled===false")
    assert popup.locator('#purchase-salesperson').is_enabled()
    assert popup.locator('#purchase-group-name').is_enabled()


def test_restored_task_retains_saved_filters_even_if_options_change(popup):
    popup.evaluate('''() => { mockTask={request_id:'saved',phase:'waiting_login',running:true,total:3,
      date_start:'2026-09-01',date_end:'2026-09-30',order_status:'已发-在途中',platform:'1688',
      salesperson:'旧业务员',group_name:'旧店铺组'}; return loadPurchaseTrackingStatus(); }''')
    assert popup.locator('#purchase-date-start').input_value() == '2026-09-01'
    assert popup.locator('#purchase-salesperson').input_value() == '旧业务员'
    assert popup.locator('#purchase-group-name').input_value() == '旧店铺组'
    assert popup.locator('#purchase-order-status').input_value() == '已发-在途中'


def test_member_filter_is_fixed_to_own_salesperson(popup):
    popup.evaluate("() => {filterOptions={salespeople:['小王'],groups:['一组'],fixed_salesperson:'小王'};return loadPurchaseTrackingOptions();}")
    assert popup.locator('#purchase-salesperson').is_disabled()
    assert popup.locator('#purchase-salesperson').input_value() == '小王'
    assert popup.locator('#purchase-group-name').is_enabled()
