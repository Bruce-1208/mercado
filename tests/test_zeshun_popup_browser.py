"""Exercise extension UI with mocked Chrome messages; never start business jobs."""
import os
from pathlib import Path

import pytest

EXTENSION = Path(__file__).resolve().parents[1] / "browser_extension/zeshun_collector"


@pytest.fixture(scope="module")
def browser():
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as pw:
        candidates = [os.environ.get("CONSOLE_TEST_BROWSER", ""), pw.chromium.executable_path,
                      "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"]
        executable = next((p for p in candidates if p and Path(p).is_file()), None)
        if not executable:
            pytest.skip("Chromium is required")
        instance = pw.chromium.launch(executable_path=executable, headless=True)
        yield instance
        instance.close()


@pytest.fixture
def popup(browser):
    page = browser.new_page(viewport={"width": 420, "height": 950})
    page.route("https://extension.test/**", lambda route: route.fulfill(
        path=str(EXTENSION / route.request.url.rsplit("/", 1)[-1])))
    page.add_init_script("""
      window.testState = {ok:true, can_execute:true, running:false, login:{confirmed:true}, categories:[]};
      window.messages = [];
      window.chrome = {
        tabs: {query:async () => []},
        runtime: {openOptionsPage:() => {}, sendMessage:(message, callback) => {
          messages.push(message);
          if(message.type === 'GET_STATE') return callback({authenticated:true, user:{username:'test'}});
          if(message.type === 'GET_AI_WEIGHT_PRICE_STATUS') return callback(structuredClone(testState));
          if(window.holdAction) { window.pendingAction = callback; return; }
          callback(structuredClone(testState));
        }}
      };
    """)
    page.goto("https://extension.test/popup.html")
    page.locator("#weight-price-mode").click()
    page.wait_for_function("document.querySelector('#weight-price-start').disabled === false")
    yield page
    page.close()


def test_start_and_busy_controls(popup):
    assert popup.locator("#weight-price-open-login").inner_text() == "打开智赢登录页面"
    popup.evaluate("window.holdAction = true")
    popup.locator("#weight-price-start").click()
    assert popup.locator("#weight-price-start").is_disabled()
    assert popup.locator("#weight-price-open-login").is_disabled()
    message = popup.evaluate("messages.find(m => m.type === 'START_AI_WEIGHT_PRICE')")
    assert message["params"] == {"selection": {"category": "", "start_page": 1,
                                                "end_page": 1, "start_item": 1}, "max_items": 10}
    popup.evaluate("pendingAction({ok:false,error:'测试启动失败'}); window.holdAction=false")
    popup.wait_for_function("!document.querySelector('#weight-price-start').disabled")
    assert "测试启动失败" in popup.locator("#result").inner_text()


def test_resume_requires_acknowledgement_and_keeps_range(popup):
    popup.evaluate("testState.circuit={reason:'请完成1688登录',at:1}; renderWeightPriceStatus(structuredClone(testState))")
    assert popup.locator("#weight-price-start").is_disabled()
    assert popup.locator("#weight-price-start").inner_text() == "继续核重核价"
    popup.locator("#weight-price-acknowledged").check()
    assert popup.locator("#weight-price-start").is_enabled()
    assert popup.locator("#weight-price-start-page").is_disabled()
    popup.locator("#weight-price-start").click()
    message = popup.evaluate("messages.find(m => m.type === 'CONTINUE_AI_WEIGHT_PRICE')")
    assert message["params"] == {"acknowledged": True}
    popup.evaluate("testState.circuit={reason:'新的验证',at:2}; renderWeightPriceStatus(structuredClone(testState))")
    assert not popup.locator("#weight-price-acknowledged").is_checked()


def test_start_hint_explains_login_permission_and_range(popup):
    popup.evaluate("testState.login.confirmed=false; renderWeightPriceStatus(testState)")
    assert "确认已登录" in popup.locator("#weight-price-start-hint").inner_text()
    popup.evaluate("testState.login.confirmed=true; testState.can_execute=false; renderWeightPriceStatus(testState)")
    assert "执行权限" in popup.locator("#weight-price-start-hint").inner_text()
    popup.evaluate("testState.can_execute=true; renderWeightPriceStatus(testState)")
    popup.locator("#weight-price-end-page").fill("0")
    assert popup.locator("#weight-price-start").is_disabled()
    assert "结束页" in popup.locator("#weight-price-start-hint").inner_text()
    popup.locator("#weight-price-end-page").fill("2")
    assert popup.locator("#weight-price-start").is_enabled()


def test_categories_use_names_and_keep_ids_in_values(popup):
    popup.evaluate("""renderZyingOptions({categories:[
      {category_id:'202170568',category_name:'家居/家电类 [202170568]'},
      {category_id:'202170569',category_name:'202170569',category_leaf_name:'玩具'},
      {category_id:'202170570',category_name:'202170570'}
    ]})""")
    options = popup.locator("#zying-category option")
    assert options.all_text_contents() == ["全部分类", "家居/家电类", "玩具", "分类名称待同步，请刷新智赢产品页"]
    assert options.nth(1).get_attribute("value") == "202170568"
    assert options.nth(3).evaluate("option => option.disabled") is True


def test_page_extractor_handles_custom_fields_react_labels_and_empty_inner_options(popup):
    popup.add_script_tag(path=str(EXTENSION / "zying-page.js"))
    rows = popup.evaluate("""() => {
      const element=document.createElement('div'); element.className='ant-cascader';
      element.__reactFiber$test={memoizedProps:{options:[]},return:{memoizedProps:{
        fieldNames:{value:'id',label:'name',children:'nodes'}, options:[
          {id:'100',name:{props:{children:'家居'}},nodes:[
            {id:'101',name:{props:{children:['家电类',' [101]']}}},
            {id:'102',name:'102',category_name:'玩具'},
            {id:'103',name:'103'}
          ]}
        ]
      }}};
      document.body.append(element);
      return zeshunReadZyingPageContext().categories;
    }""")
    assert rows == [
        {"category_id": "100", "category_name": "家居", "category_leaf_name": "家居"},
        {"category_id": "101", "category_name": "家居/家电类", "category_leaf_name": "家电类"},
        {"category_id": "102", "category_name": "家居/玩具", "category_leaf_name": "玩具"},
    ]


def test_page_extractor_reads_developers_from_loaded_zying_bridge(popup):
    popup.add_script_tag(path=str(EXTENSION / "zying-page.js"))
    rows = popup.evaluate("""async () => {
      window.webpackChunkzying = [];
      window.__zeshunZyingRequire = () => ({Z: {
        getFreeStyleApi: async command => ({data: {logins: [
          {id: 17, name: '产品开发甲'},
          {id: 17, name: '重复项'},
          {id: 18, name: '产品开发乙'}
        ]}})
      }});
      return await zeshunReadZyingProductDevelopers();
    }""")
    assert rows == [
        {"id": "17", "name": "产品开发甲"},
        {"id": "18", "name": "产品开发乙"},
    ]


def test_auth_state_does_not_wait_for_purchase_network(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{}};
      const storage={get:(_,cb)=>cb({browserExtensionAuth:{token:'test',user:{username:'test'}}})};
      window.chrome={storage:{sync:storage,session:storage,local:storage},
        runtime:{onInstalled:event,onStartup:event,onMessage:event},
        alarms:{onAlarm:event},contextMenus:{onClicked:event}};
      window.importScripts=()=>{};
      window.fetch=()=>{throw new Error('GET_STATE must not use network');};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    assert page.evaluate("state().then(result => result.authenticated)") is True
    page.close()


def test_product_batch_inputs_search_and_start(popup):
    popup.evaluate("""() => {
      chrome.tabs.query=async () => [
        {id:7,title:'墨西哥搜索',url:'https://listado.mercadolibre.com.mx/toy',active:true},
        {id:8,title:'巴西搜索',url:'https://lista.mercadolivre.com.br/toy'},
        {id:9,title:'无关页面',url:'https://example.com'}
      ];
      window.batch={running:false};
      chrome.runtime.sendMessage=(message,cb) => {
        messages.push(message);
        if(message.type==='CHECK_PRODUCT_ZYING') return cb({ok:true,found:true,logged_in:true,message:'智赢插件已检测并处于登录状态'});
        if(message.type==='START_PRODUCT_BATCH') batch={running:true,message:'正在采集第 2 页'};
        if(message.type==='STOP_PRODUCT_BATCH') batch={running:false,message:'已停止采集'};
        cb({ok:true,state:batch,tab_id:8});
      };
    }""")
    popup.locator("#product-mode").click()
    popup.locator("#product-refresh-tabs").click()
    assert popup.locator("#product-tab option").count() == 3
    popup.locator("#product-country").select_option("MLB")
    popup.locator("#product-keyword").fill("brinquedo infantil")
    popup.locator("#product-search").click()
    popup.wait_for_function("document.querySelector('#product-tab').value === '8'")
    search = popup.evaluate("messages.find(m => m.type === 'OPEN_PRODUCT_SEARCH')")
    assert search == {"type": "OPEN_PRODUCT_SEARCH", "country": "MLB", "keyword": "brinquedo infantil"}
    popup.locator("#product-max-items").fill("17")
    popup.locator("#product-concurrency").fill("11")
    popup.locator("#product-batch-start").click()
    assert "并发数" in popup.locator("#result").inner_text()
    assert not popup.evaluate("messages.some(m => m.type === 'START_PRODUCT_BATCH')")
    popup.locator("#product-concurrency").fill("4")
    popup.locator("#product-min-sales").fill("20")
    popup.locator("#product-max-sales").fill("200")
    popup.locator("#product-batch-start").click()
    message = popup.evaluate("messages.find(m => m.type === 'START_PRODUCT_BATCH')")
    assert message == {"type": "START_PRODUCT_BATCH", "tab_id": 8, "params": {
        "max_items": 17, "concurrency": 4, "min_sales": 20, "max_sales": 200}}
    assert popup.locator("#product-batch-start").is_disabled()
    assert popup.locator("#product-concurrency").is_disabled()
    popup.locator("#product-batch-stop").click()
    assert popup.locator("#product-batch-start").is_enabled()
    assert popup.locator("#product-batch-status").inner_text() == "已停止采集"


def test_product_list_reads_actual_pagination_international_and_shipping_profile(browser):
    page = browser.new_page()
    page.route("https://listado.mercadolibre.com.mx/**", lambda route: route.fulfill(body="""
      <ul><li class="ui-search-layout__item"><div class="poly-card">
        <a class="poly-component__title" href="https://articulo.mercadolibre.com.mx/MLM-12345-toy">Toy</a>
        <img src="https://flags.test/CN.svg"><span>Internacional China</span>
      </div></li></ul>
      <ul class="andes-pagination">
        <li><a href="https://listado.mercadolibre.com.mx/toy">1</a></li>
        <li class="andes-pagination__button--current">2</li>
        <li class="andes-pagination__button--next"><a href="https://listado.mercadolibre.com.mx/toy_Desde_101">Next</a></li>
      </ul>""", content_type="text/html"))
    page.route("https://flags.test/**", lambda route: route.abort())
    page.goto("https://listado.mercadolibre.com.mx/toy_Desde_51")
    page.evaluate("window.chrome={runtime:{onMessage:{addListener:fn=>window.receive=fn}}}")
    page.add_script_tag(path=str(EXTENSION / "collector-core.js"))
    page.add_script_tag(path=str(EXTENSION / "content.js"))
    data = page.evaluate("new Promise(resolve=>receive({type:'READ_PRODUCT_LIST'}, {}, resolve))")
    assert data["ok"] is True
    assert data["page"] == 2
    assert data["first_url"] == "https://listado.mercadolibre.com.mx/toy"
    assert data["next_url"].endswith("_Desde_101")
    assert data["international_selected"] is False
    assert all(item["isChinaOrigin"] and item["eligible"] and item["key"] == "MLM12345" for item in data["items"])
    page.evaluate("document.body.innerHTML='<h1 class=ui-pdp-title>Detail</h1>'")
    data = page.evaluate("new Promise(resolve=>receive({type:'READ_PRODUCT_LIST'}, {}, resolve))")
    assert data["ok"] is False
    assert "详情页" in data["error"]
    page.close()


def test_mercado_collection_requires_logged_in_zying_plugin(browser):
    page = browser.new_page()
    page.set_content("<div id='host'></div>")
    page.evaluate("""() => {
      const host=document.querySelector('#host'); const root=host.attachShadow({mode:'open'});
      root.innerHTML='<div class="zying-meli-detail-metric-line">请先登录智赢账号</div>';
    }""")
    page.add_script_tag(path=str(EXTENSION / "collector-core.js"))
    assert page.evaluate("ZeshunCollectorCore.pluginLoginStatus(document)") == {
        "found": True, "logged_in": False,
        "message": "智赢插件尚未登录，请先登录后刷新当前美客多页面"}
    page.evaluate("document.querySelector('#host').shadowRoot.firstElementChild.textContent='重量 520 g'")
    assert page.evaluate("ZeshunCollectorCore.pluginLoginStatus(document).logged_in") is True
    page.close()


def test_zying_sales_and_fulfillment_are_the_final_collection_filter(browser):
    page = browser.new_page()
    page.set_content("<div id='host'></div>")
    page.evaluate("""() => {
      const host=document.querySelector('#host'); const root=host.attachShadow({mode:'open'});
      root.innerHTML='<div id="zyCardWrap"><div class="zying-meli-detail-metric-line">销量 1.2万 半托管</div></div>';
    }""")
    page.add_script_tag(path=str(EXTENSION / "collector-core.js"))
    assert page.evaluate("ZeshunCollectorCore.parsePluginSales('销量 1.2万')") == 12000
    assert page.evaluate("ZeshunCollectorCore.parsePluginProductInfo('销量 88 半托管', '')") == {
        "sales": 88, "fulfillment_type": "semi_managed",
        "fulfillment_label": "半托管", "fulfillment_eligible": True}
    assert page.evaluate("ZeshunCollectorCore.parsePluginProductInfo('销量 88 本土仓', 'CN').fulfillment_eligible") is False
    assert page.evaluate("ZeshunCollectorCore.parsePluginProductInfo('销量 88 全托管', 'CN').fulfillment_eligible") is False
    page.close()


def test_zying_collection_opens_web_login_and_requires_page_range(popup):
    popup.locator("#zying-mode").click()
    popup.wait_for_function("messages.some(m => m.type === 'OPEN_ZYING_LOGIN')")
    assert "智赢网页版登录页面" in popup.locator("#zying-page-status").inner_text()
    popup.evaluate("""() => {
      zyingContext={credential:'test',categories:[]};
      authenticated=true; zyingRunning=false; zyingStartButton.disabled=false;
    }""")
    popup.locator("#zying-start-page").fill("")
    popup.locator("#zying-end-page").fill("3")
    popup.locator("#zying-start").click()
    assert "必须指定有效的起始页和结束页" in popup.locator("#result").inner_text()
    assert not popup.evaluate("messages.some(m => m.type === 'START_ZYING_COLLECTION')")
