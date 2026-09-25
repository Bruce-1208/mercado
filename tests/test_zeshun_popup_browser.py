"""Exercise extension UI with mocked Chrome messages; never start business jobs."""
import json
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
    assert message["params"] == {
        "selection": {
            "category": "", "start_product_id": "",
            "product_developer_id": "", "product_developer_name": "",
        },
        "max_items": 10,
    }
    popup.evaluate("pendingAction({ok:false,error:'测试启动失败'}); window.holdAction=false")
    popup.wait_for_function("!document.querySelector('#weight-price-start').disabled")
    assert "测试启动失败" in popup.locator("#result").inner_text()


def test_yandex_collection_starts_and_reopens_results(popup):
    popup.evaluate("""() => {
      window.yandexMockRun = null;
      const original = chrome.runtime.sendMessage;
      chrome.runtime.sendMessage = (message, callback) => {
        if (message.type === 'GET_YANDEX_SEARCH_STATUS') {
          messages.push(message);
          callback({ok:true, run:window.yandexMockRun});
          return;
        }
        if (message.type === 'START_YANDEX_SEARCH') {
          messages.push(message);
          window.yandexMockRun = {id:42, keyword:message.keyword, requested_count:message.count,
            found_count:2, scanned_count:5, status:'running', message:'正在抓取国外商品'};
          callback({ok:true, run_id:42, status:'queued'});
          return;
        }
        if (message.type === 'OPEN_YANDEX_SEARCH_RESULTS') {
          messages.push(message);
          callback({ok:true, run_id:42});
          return;
        }
        original(message, callback);
      };
    }""")
    popup.locator("#yandex-mode").click()
    popup.locator("#yandex-keyword").fill("行车记录仪")
    popup.locator("#yandex-count").fill("7")
    popup.locator("#yandex-start").click()
    popup.wait_for_function("messages.some(message => message.type === 'START_YANDEX_SEARCH')")
    assert popup.evaluate("messages.find(message => message.type === 'START_YANDEX_SEARCH')") == {
        "type": "START_YANDEX_SEARCH", "keyword": "行车记录仪", "count": 7,
    }
    popup.wait_for_function("document.querySelector('#yandex-summary').textContent.includes('已找到 2 / 7')")
    assert popup.locator("#yandex-start").is_disabled()
    popup.evaluate("""() => {
      window.yandexMockRun = {...window.yandexMockRun, status:'completed',
        found_count:6, scanned_count:18, message:'采集完成'};
      loadYandexStatus();
    }""")
    popup.wait_for_function("document.querySelector('#yandex-summary').textContent.includes('已找到 6 / 7')")
    assert popup.locator("#yandex-start").is_enabled()
    popup.locator("#yandex-open-results").click()
    popup.wait_for_function("messages.some(message => message.type === 'OPEN_YANDEX_SEARCH_RESULTS')")


def test_resume_requires_acknowledgement_and_keeps_range(popup):
    popup.evaluate("testState.circuit={reason:'请完成1688登录',at:1}; renderWeightPriceStatus(structuredClone(testState))")
    assert popup.locator("#weight-price-start").is_disabled()
    assert popup.locator("#weight-price-start").inner_text() == "继续核重核价"
    popup.locator("#weight-price-acknowledged").check()
    assert popup.locator("#weight-price-start").is_enabled()
    assert popup.locator("#weight-price-start-product-id").is_disabled()
    popup.locator("#weight-price-start").click()
    message = popup.evaluate("messages.find(m => m.type === 'CONTINUE_AI_WEIGHT_PRICE')")
    assert message["params"] == {"acknowledged": True}
    popup.evaluate("testState.circuit={reason:'新的验证',at:2}; renderWeightPriceStatus(structuredClone(testState))")
    assert not popup.locator("#weight-price-acknowledged").is_checked()


def test_start_hint_explains_login_permission_and_cursor(popup):
    popup.evaluate("testState.login.confirmed=false; renderWeightPriceStatus(testState)")
    assert "确认已登录" in popup.locator("#weight-price-start-hint").inner_text()
    popup.evaluate("testState.login.confirmed=true; testState.can_execute=false; renderWeightPriceStatus(testState)")
    assert "执行权限" in popup.locator("#weight-price-start-hint").inner_text()
    popup.evaluate("testState.can_execute=true; renderWeightPriceStatus(testState)")
    popup.locator("#weight-price-start-product-id").fill("abc")
    assert popup.locator("#weight-price-start").is_disabled()
    assert "起始产品编号" in popup.locator("#weight-price-start-hint").inner_text()
    popup.locator("#weight-price-start-product-id").fill("848332340")
    assert popup.locator("#weight-price-start").is_enabled()


def test_weight_price_can_run_in_test_mode_when_erp_writeback_is_disabled(popup):
    popup.evaluate("testState.client_config={writeback_enabled:false}; renderWeightPriceStatus(structuredClone(testState))")
    assert popup.locator("#weight-price-start").is_enabled()
    assert "已就绪" in popup.locator("#weight-price-start-hint").inner_text()


def test_supplier_login_keeps_state_without_reloading_status(popup):
    popup.evaluate("""() => {
      testState.circuit={reason:'请完成1688登录',at:1};
      renderWeightPriceStatus(structuredClone(testState));
      messages.length = 0;
    }""")
    popup.locator("#weight-price-open-supplier").click()
    popup.wait_for_function("document.querySelector('#result').textContent.includes('1688登录')")
    assert popup.evaluate("messages.map(message => message.type)") == ["OPEN_AI_WEIGHT_PRICE_SUPPLIER"]
    assert popup.locator("#weight-price-resume").is_visible()


def test_weight_price_shows_current_category_and_execution_terminal(popup):
    popup.evaluate("""renderWeightPriceStatus({
      ok:true, can_execute:true, running:true, login:{confirmed:true},
      execution_terminal:'OPS-PC-01',
      run:{current_task_id:'848332340',message:'正在核验'},
      current_product:{erp_goods_id:'848332340',title:'不锈钢水杯',
                       zying_category_name:'家居 / 厨房用品'}
    })""")

    meta = popup.locator("#weight-price-current-meta")
    assert meta.inner_text() == "当前产品 848332340 · 智赢分类 家居 / 厨房用品 · 执行终端 OPS-PC-01"
    assert meta.get_attribute("title") == "不锈钢水杯"


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


def test_weight_price_categories_use_zying_fields(popup):
    popup.evaluate("""renderWeightPriceCategories([
      {category_id:'202170568',category_name:'家居/家电类 [202170568]'},
      {category_id:'202170569',category_name:'202170569',category_leaf_name:'玩具'},
      {category_id:'202170570',category_name:'202170570'}
    ])""")
    options = popup.locator("#weight-price-category option")
    assert options.all_text_contents() == ["全部分类", "家居/家电类", "玩具", "分类名称待同步，请刷新智赢产品页"]
    assert options.nth(1).get_attribute("value") == "202170568"
    assert options.nth(2).get_attribute("value") == "202170569"
    assert options.nth(3).evaluate("option => option.disabled") is True


def test_weight_price_can_select_product_developer(popup):
    popup.evaluate("""renderWeightPriceStatus({
      ok:true,can_execute:true,running:false,login:{confirmed:true},categories:[],
      developers:[{id:'17',name:'产品开发甲'},{id:'18',name:'产品开发乙'}]
    })""")
    assert popup.locator("#weight-price-developer option").all_text_contents() == [
        "全部产品开发", "产品开发甲", "产品开发乙"
    ]
    popup.locator("#weight-price-developer").select_option("18")
    popup.locator("#weight-price-start").click()
    message = popup.evaluate("messages.findLast(m => m.type === 'START_AI_WEIGHT_PRICE')")
    assert message["params"]["selection"]["product_developer_id"] == "18"
    assert message["params"]["selection"]["product_developer_name"] == "产品开发乙"


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


def test_zying_modes_reuse_logged_in_tab_and_forward_page_developers(popup):
    popup.evaluate("""() => {
      messages.length = 0;
      chrome.tabs.query = async () => [{
        id: 88, active: true, lastAccessed: 100,
        url: 'https://meli.zying.net/#/product'
      }];
      chrome.runtime.sendMessage = (message, callback) => {
        messages.push(message);
        if (message.type === 'GET_STATE') {
          return callback({authenticated:true,user:{username:'test'}});
        }
        if (message.type === 'GET_ZYING_STATUS' || message.type === 'GET_ZYING_INFRINGEMENT_STATUS') {
          return callback({ok:true,running:false});
        }
        if (message.type === 'READ_ZYING_CONTEXT') {
          return callback({ok:true,credential:'page-token',categories:[
            {category_id:'101',category_name:'家居'}
          ],developers:[{id:'17',name:'产品开发甲'}]});
        }
        if (message.type === 'GET_ZYING_OPTIONS') {
          return callback({ok:true,categories:message.context.categories,
                           developers:message.context.developers});
        }
        callback({ok:true});
      };
    }""")
    popup.locator("#zying-mode").click()
    popup.locator("#zying-refresh").click()
    popup.wait_for_function("document.querySelector('#zying-developer').options.length === 2")
    assert popup.evaluate("messages.some(message => message.type === 'OPEN_ZYING_LOGIN')") is False
    context = popup.evaluate("messages.find(message => message.type === 'GET_ZYING_OPTIONS').context")
    assert context["developers"] == [{"id": "17", "name": "产品开发甲"}]
    assert popup.locator("#zying-developer").input_value() == ""
    assert popup.locator("#zying-developer option").all_text_contents() == [
        "全部产品开发", "产品开发甲"
    ]

    popup.evaluate("messages.length = 0")
    popup.locator("#zying-infringement-mode").click()
    popup.locator("#zying-infringement-refresh").click()
    popup.wait_for_function(
        "document.querySelector('#zying-infringement-developer').options.length === 2"
    )
    assert popup.evaluate("messages.some(message => message.type === 'OPEN_ZYING_LOGIN')") is False


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


def test_yandex_background_uses_bridge_and_opens_the_matching_run(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event = {addListener: () => {}};
      const storage = {get: (_, cb) => cb({}), set: (_, cb) => cb && cb(), remove: (_, cb) => cb && cb()};
      window.createdTabs = [];
      window.chrome = {
        storage: {sync: storage, session: storage, local: storage},
        runtime: {onInstalled: event, onStartup: event, onMessage: event},
        alarms: {onAlarm: event}, contextMenus: {onClicked: event},
        tabs: {create: async options => { createdTabs.push(options); return options; }}
      };
      window.importScripts = () => {};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""async () => {
      let saved = {};
      const requests = [];
      storageGet = async () => saved;
      storageSet = async (_, data) => { saved = {...saved, ...data}; };
      settings = async () => ({consoleUrl: 'https://console.example.test/'});
      apiRequest = async (path, options) => {
        requests.push({path, method: options.method, body: options.body || null});
        return path.endsWith('/status') || path.endsWith('/42')
          ? {run: {id: 42, status: 'completed', found_count: 6}}
          : {run_id: 42, status: 'queued'};
      };
      const started = await startYandexSearch(' 行车记录仪 ', 7);
      const progress = await getYandexSearchStatus();
      await openYandexSearchResults();
      return {started, progress, requests, createdTabs};
    }""")
    assert result["started"]["run_id"] == 42
    assert result["progress"]["run"]["found_count"] == 6
    assert result["requests"] == [
        {"path": "/api/browser-extension/yandex/search", "method": "POST", "body": '{"keyword":"行车记录仪","count":7}'},
        {"path": "/api/browser-extension/yandex/search/42", "method": "GET", "body": None},
    ]
    assert result["createdTabs"] == [{"url": "https://console.example.test/yandex-console/?run_id=42", "active": True}]
    page.close()


def test_ai_weight_price_tab_wait_polls_after_missed_complete_event_and_stays_background(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const listeners = new Set();
      const event = {addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn)};
      const storage = {get: (_,cb) => cb({}), set: (_,cb) => cb && cb(), remove: (_,cb) => cb && cb()};
      window.getCalls = 0;
      window.createdTabs = [];
      window.chrome = {
        storage:{sync:storage,session:storage,local:storage},
        runtime:{onInstalled:{addListener:()=>{}},onStartup:{addListener:()=>{}},onMessage:{addListener:()=>{}}},
        alarms:{onAlarm:{addListener:()=>{}}},contextMenus:{onClicked:{addListener:()=>{}}},
        tabs:{
          onUpdated:event,
          get:async id => ({id,status:(++getCalls >= 2 ? 'complete' : 'loading')}),
          query:async () => [],
          create:async options => { createdTabs.push(options); return {id:99,...options}; },
          update:async (id, options) => ({id,...options})
        }
      };
      window.importScripts=()=>{};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("aiWeightPriceWaitTab(7, 1200).then(tab => ({id:tab.id,calls:getCalls}))")
    assert result["id"] == 7
    assert result["calls"] >= 2
    tab_id = page.evaluate("aiWeightPriceTab('1688.com','https://www.1688.com/')")
    assert tab_id == 99
    assert page.evaluate("createdTabs[0]") == {"url": "https://www.1688.com/", "active": False}
    page.close()


def test_ai_weight_price_writeback_requires_real_save_then_supports_persisted_readback(browser):
    page = browser.new_page()
    page.set_content("""
      <div class="curd-detail-wrap">
        <div class="crud-detail-header"><span class="h1">商品编号 1001</span></div>
        <input id="weight" value="500">
        <input id="netproceed" value="9">
        <label><input type="radio" name="stat" value="待审核" checked>待审核</label>
        <label><input type="radio" name="stat" value="通过">通过</label>
        <button id="save" type="button">保存</button>
      </div>
    """)
    page.evaluate("""() => {
      window.chrome={runtime:{onMessage:{addListener:fn=>window.receiveZying=fn}}};
      document.querySelector('#save').addEventListener('click', () => {
        window.persisted={
          weight:document.querySelector('#weight').value,
          income:document.querySelector('#netproceed').value,
          status:document.querySelector('input[name=stat]:checked').value
        };
      });
    }""")
    page.add_script_tag(path=str(EXTENSION / "content-zying.js"))
    message = {
        "type": "AI_WEIGHT_PRICE_WRITEBACK",
        "erp_goods_id": "1001",
        "task": {"erp_goods_id": "1001", "title": "测试商品"},
        "selectors": {
            "erp_detail": ".curd-detail-wrap",
            "erp_edit_id": ".crud-detail-header .h1",
            "erp_weight": "#weight",
            "erp_net_income": "#netproceed",
        },
        "changes": {"weight_g": "540", "net_income_usd": "8", "review_status": "通过"},
    }
    result = page.evaluate(
        "message => new Promise(resolve => receiveZying(message, {}, resolve))", message
    )
    assert result["ok"] is True
    assert result["submitted"] is True
    assert result["before"]["review_status"] == "待审核"
    assert page.evaluate("persisted") == {"weight": "540", "income": "8", "status": "通过"}

    verified = page.evaluate(
        "message => new Promise(resolve => receiveZying({...message,type:'AI_WEIGHT_PRICE_VERIFY_WRITEBACK'}, {}, resolve))",
        message,
    )
    assert verified["ok"] is True
    assert verified["persisted"] is True
    assert verified["after"]["erp_goods_id"] == "1001"
    assert verified["after"]["weight_g"] == 540
    assert verified["after"]["net_income_usd"] == 8
    assert verified["after"]["review_status"] == "通过"

    page.evaluate("""() => {
      document.querySelector('#save').remove();
      document.querySelector('input[value="待审核"]').click();
    }""")
    failed = page.evaluate(
        "message => new Promise(resolve => receiveZying(message, {}, resolve))", message
    )
    assert failed["ok"] is False
    assert "保存按钮必须唯一且可用" in failed["error"]
    page.close()


def test_ai_weight_price_product_extraction_filters_selected_developer(browser):
    page = browser.new_page()
    page.set_content("""
      <div class="product-item"><span class="product-id">1001</span>
        <span class="product-title">甲的商品</span><span>产品开发甲</span>
        <img class="product-pic" src="https://img.test/1.jpg"></div>
      <div class="product-item"><span class="product-id">1002</span>
        <span class="product-title">乙的商品</span><span>产品开发乙</span>
        <img class="product-pic" src="https://img.test/2.jpg"></div>
    """)
    page.evaluate("window.chrome={runtime:{onMessage:{addListener:()=>{}}}}")
    page.add_script_tag(path=str(EXTENSION / "content-zying.js"))
    products = page.evaluate("""() => aiWeightPriceExtractProducts({
      selectors:{erp_rows:'.product-item',erp_id:'.product-id',
                 erp_title:'.product-title',erp_image:'.product-pic'},
      selection:{product_developer_id:'18',product_developer_name:'产品开发乙'},
      max_items:10
    })""")
    assert len(products) == 1
    assert products[0]["erp_goods_id"] == "1002"
    assert products[0]["source_index"] == 2
    assert products[0]["product_developer_id"] == "18"
    assert products[0]["product_developer_name"] == "产品开发乙"
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


def test_mercado_collection_reads_sibling_origin_icon_inside_zying_shadow_root(browser):
    page = browser.new_page()
    page.set_content(
        "<h1 class='ui-pdp-title'>测试商品</h1>"
        "<img src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=='>"
    )
    page.evaluate("""() => {
      const host = document.createElement('div');
      const root = host.attachShadow({mode: 'open'});
      root.innerHTML = '<div class="zying-meli-detail-metric-line">重量：509g</div>' +
        '<img src="/assets/CN.svg">';
      document.body.append(host);
    }""")
    page.add_script_tag(path=str(EXTENSION / "collector-core.js"))
    product = page.evaluate("""() => ZeshunCollectorCore.extractProduct(
      document, 'https://articulo.mercadolibre.com.mx/MLM-12345-test'
    )""")
    assert product["plugin_snapshot"]["self_ship_origin"] == "CN"
    page.close()


def test_batch_collection_reads_zying_511_closed_shadow_root(browser):
    page = browser.new_page()
    detail_url = "https://articulo.mercadolibre.com.mx/MLM-12345-closed-shadow"
    page.route(detail_url, lambda route: route.fulfill(body="""
      <h1 class="ui-pdp-title">封闭浮层测试商品</h1>
      <meta property="og:image" content="https://img.test/main.jpg">
      <div id="zying-host"></div>
    """, content_type="text/html"))
    page.goto(detail_url)
    page.evaluate("""() => {
      const closedRoots = new WeakMap();
      const host = document.querySelector('#zying-host');
      const root = host.attachShadow({mode: 'closed'});
      closedRoots.set(host, root);
      root.innerHTML = '<div id="zyCardWrap">' +
        '<div class="zying-meli-detail-metric-line">销量 88 半托管</div>' +
        '<div class="zying-meli-detail-metric-line">重量 509 g 尺寸 11 x 10 x 17 cm</div>' +
        '<img src="/assets/CN.svg"></div>';
      window.chrome = {
        dom: {openOrClosedShadowRoot: node => closedRoots.get(node) || node.shadowRoot || null},
        runtime: {
          onMessage: {addListener: listener => { window.receive = listener; }},
          sendMessage: (_message, callback) => callback && callback({ok: true})
        }
      };
    }""")
    page.add_script_tag(path=str(EXTENSION / "collector-core.js"))
    page.add_script_tag(path=str(EXTENSION / "content.js"))

    result = page.evaluate("""new Promise(resolve => receive(
      {type: 'EXTRACT_BATCH_PRODUCT'}, {}, resolve
    ))""")

    assert result["ok"] is True, result
    snapshot = result["product"]["plugin_snapshot"]
    assert snapshot["sales"] == 88
    assert snapshot["fulfillment_type"] == "semi_managed"
    assert snapshot["fulfillment_eligible"] is True
    assert snapshot["self_ship_origin"] == "CN"
    assert result["product"]["weight_g"] == 509
    page.close()


def test_real_chromium_extension_api_reads_closed_shadow_root(browser, tmp_path):
    extension_path = str(EXTENSION.resolve())
    candidates = [
        os.environ.get("CONSOLE_TEST_BROWSER", ""),
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        browser.browser_type.executable_path,
    ]
    executable = next((p for p in candidates if p and Path(p).is_file()), None)
    if not executable:
        pytest.skip("Chromium is required")
    context = browser.browser_type.launch_persistent_context(
        str(tmp_path / "extension-profile"),
        executable_path=executable,
        headless=True,
        args=[
            f"--disable-extensions-except={extension_path}",
            f"--load-extension={extension_path}",
        ],
    )
    try:
        detail_url = "https://articulo.mercadolibre.com.mx/MLM-54321-real-closed-shadow"
        page = context.new_page()
        page.route(detail_url, lambda route: route.fulfill(body="""
          <meta charset="utf-8">
          <h1 class="ui-pdp-title">真实扩展封闭浮层测试</h1>
          <meta property="og:image" content="https://img.test/main.jpg">
          <div id="zying-host"></div>
          <script>
            const root = document.querySelector('#zying-host').attachShadow({mode: 'closed'});
            root.innerHTML = '<div id="zyCardWrap">' +
              '<div class="zying-meli-detail-metric-line">销量 126 半托管</div>' +
              '<div class="zying-meli-detail-metric-line">重量 618 g 尺寸 20 x 15 x 8 cm</div>' +
              '<img src="/assets/CN.svg"></div>';
          </script>
        """, content_type="text/html"))
        page.goto(detail_url)
        page.wait_for_function("document.querySelector('#zeshun-plugin-launcher')")
        workers = context.service_workers
        worker = workers[0] if workers else context.wait_for_event("serviceworker")
        result = worker.evaluate("""async url => {
          const tabs = await chrome.tabs.query({url});
          if (!tabs.length) return {ok: false, error: 'test tab not found'};
          return chrome.tabs.sendMessage(tabs[0].id, {type: 'EXTRACT_BATCH_PRODUCT'});
        }""", detail_url)
    finally:
        context.close()

    assert result["ok"] is True, result
    snapshot = result["product"]["plugin_snapshot"]
    assert snapshot["sales"] == 126, snapshot
    assert snapshot["fulfillment_type"] == "semi_managed"
    assert snapshot["self_ship_origin"] == "CN"
    assert result["product"]["weight_g"] == 618


def test_real_extension_decodes_zying_protected_svg_metrics_across_isolated_worlds(browser, tmp_path):
    extension_path = str(EXTENSION.resolve())
    fixture = tmp_path / "zying-fixture"
    fixture.mkdir()
    (fixture / "manifest.json").write_text(json.dumps({
        "manifest_version": 3,
        "name": "ZYing protected metric fixture",
        "version": "1.0.0",
        "content_scripts": [{
            "matches": ["https://articulo.mercadolibre.com.mx/*"],
            "js": ["fixture.js"],
            "run_at": "document_idle",
        }],
    }), encoding="utf-8")
    (fixture / "fixture.js").write_text(r"""
      (() => {
        const escapeXml = value => String(value).replace(/[&<>"']/g, character => ({
          '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;'
        })[character]);
        const visual = value => {
          const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="180" height="20">` +
            `<text x="0" y="15">${escapeXml(value)}</text></svg>`;
          const url = URL.createObjectURL(new Blob([svg], {type: 'image/svg+xml'}));
          const span = document.createElement('span');
          span.className = 'fixture-protected-visual';
          span.style.cssText = `display:inline-block;width:180px;height:20px;background-image:url("${url}")`;
          return span;
        };
        const line = (...values) => {
          const row = document.createElement('div');
          row.className = 'zying-meli-detail-metric-line';
          values.forEach(value => row.append(visual(value)));
          return row;
        };
        const host = document.createElement('div');
        host.id = 'zying-protected-fixture-host';
        document.body.append(host);
        const root = host.attachShadow({mode: 'closed'});
        const card = document.createElement('div');
        card.id = 'zyCardWrap';
        card.className = 'zying-meli-detail-wrap';
        card.append(line('销量：', '88'));
        card.append(line('重量：', '509g', '| $4.20'));
        card.append(line('尺寸：', '11 × 10 × 17 cm', '计抛 0.31kg'));
        card.append(line('净收益：', '$8.60'));
        card.append(line('半托管'));
        const flag = document.createElement('img');
        flag.src = '/assets/CN.svg';
        card.append(flag);
        root.append(card);
      })();
    """, encoding="utf-8")
    candidates = [
        os.environ.get("CONSOLE_TEST_BROWSER", ""),
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        browser.browser_type.executable_path,
    ]
    executable = next((p for p in candidates if p and Path(p).is_file()), None)
    if not executable:
        pytest.skip("Chromium is required")
    fixture_path = str(fixture.resolve())
    context = browser.browser_type.launch_persistent_context(
        str(tmp_path / "protected-svg-profile"),
        executable_path=executable,
        headless=True,
        args=[
            f"--disable-extensions-except={extension_path},{fixture_path}",
            f"--load-extension={extension_path},{fixture_path}",
        ],
    )
    try:
        detail_url = "https://articulo.mercadolibre.com.mx/MLM-98765-protected-svg"
        page = context.new_page()
        page.route(detail_url, lambda route: route.fulfill(body="""
          <meta charset="utf-8">
          <h1 class="ui-pdp-title">智赢加密指标测试</h1>
          <meta property="og:image" content="https://img.test/main.jpg">
        """, content_type="text/html"))
        page.goto(detail_url)
        page.wait_for_function("document.querySelector('#zeshun-plugin-launcher')")
        page.wait_for_function("document.querySelector('#zying-protected-fixture-host')")
        workers = context.service_workers
        worker = workers[0] if workers else context.wait_for_event("serviceworker")
        result = worker.evaluate("""async url => {
          const tabs = await chrome.tabs.query({url});
          return chrome.tabs.sendMessage(tabs[0].id, {type: 'EXTRACT_BATCH_PRODUCT'});
        }""", detail_url)
    finally:
        context.close()

    assert result["ok"] is True, result
    product = result["product"]
    assert product["weight_g"] == 509
    assert [product["package_length_cm"], product["package_width_cm"],
            product["package_height_cm"]] == [11, 10, 17]
    assert product["net_proceeds_usd"] == 8.6
    assert product["scrape_status"] == "ok"
    assert product["plugin_snapshot"]["read_method"] == "browser_extension_protected_svg"


def test_1688_extension_collection_reuses_weight_price_package_facts(browser):
    page = browser.new_page()
    page.route("https://detail.1688.com/**", lambda route: route.fulfill(body="""
      <meta charset="utf-8"><div id="productTitle"><h1>测试收纳袋</h1></div>
      <div id="gallery"><img src="https://cbu01.alicdn.com/test.jpg"></div>
      <div id="skuSelection" data-module="od_sku_selection"></div>
      <div id="productPackInfo" data-module="od_product_pack_info"></div>
    """, content_type="text/html"))
    page.add_init_script("""window.chrome={runtime:{
      onMessage:{addListener:fn=>window.__zeshunMessageHandler=fn},
      sendMessage:()=>Promise.resolve({ok:true,data:zeshunRead1688PageData()})
    }};""")
    page.goto("https://detail.1688.com/offer/123456789.html")
    page.evaluate("""() => {
      document.querySelector('#skuSelection').__reactFiber$fixture={memoizedProps:{dataManager:{params:{skuItems:[
        {skuId:22,specAttrs:'黑色&gt;大号',discountPrice:'2.50'},
        {skuId:11,specAttrs:'黄色&gt;小号',discountPrice:'1.25'}
      ]}}},return:null};
      document.querySelector('#productPackInfo').__reactFiber$fixture={memoizedProps:{packInfoData:{skuInfo:[
        {skuId:11,weight:40,length:11,width:10,height:17},
        {skuId:22,weight:55,length:11,width:10,height:17}
      ]}},return:null};
    }""")
    page.add_script_tag(path=str(EXTENSION / "1688-page.js"))
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    page.wait_for_timeout(100)
    extracted = page.evaluate("""() => new Promise(resolve =>
      window.__zeshunMessageHandler({type:'EXTRACT_PRODUCT'}, null, resolve))""")
    detail = page.evaluate("""() => new Promise(resolve =>
      window.__zeshunMessageHandler({type:'AI_WEIGHT_PRICE_READ_DETAIL'}, null, resolve))""")
    page.close()

    assert extracted["ok"] is True
    assert extracted["product"]["weight_g"] == 47.5
    assert [v["weight_g"] for v in extracted["product"]["variations"]] == [40, 55]
    assert [extracted["product"][key] for key in (
        "package_length_cm", "package_width_cm", "package_height_cm")] == [11, 10, 17]
    assert extracted["product"]["scrape_status"] == "ok"
    assert detail["ok"] is True
    assert detail["detail"]["weight_g"] == 47.5
    assert [detail["detail"][key] for key in (
        "package_length_cm", "package_width_cm", "package_height_cm")] == [11, 10, 17]


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


def test_zying_collection_opens_web_login_and_validates_cursor_limit(popup):
    popup.locator("#zying-mode").click()
    popup.wait_for_function("messages.some(m => m.type === 'OPEN_ZYING_LOGIN')")
    assert "智赢网页版登录页面" in popup.locator("#zying-page-status").inner_text()
    popup.evaluate("""() => {
      zyingContext={credential:'test',categories:[]};
      authenticated=true; zyingRunning=false; zyingStartButton.disabled=false;
    }""")
    popup.locator("#zying-start-product-id").fill("not-an-id")
    popup.locator("#zying-max-items").fill("3")
    popup.locator("#zying-start").click()
    assert "起始产品编号必须是正整数" in popup.locator("#result").inner_text()
    assert not popup.evaluate("messages.some(m => m.type === 'START_ZYING_COLLECTION')")


def test_background_open_zying_login_navigates_existing_tab_to_login_route(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event = {addListener: () => {}};
      const updates = [];
      const created = [];
      const storageArea = {
        get: (_, callback) => callback({}),
        set: (_, callback) => callback && callback(),
        remove: (_, callback) => callback && callback()
      };
      window.updates = updates;
      window.created = created;
      window.chrome = {
        storage: {sync: storageArea, session: storageArea, local: storageArea},
        runtime: {onInstalled:event, onStartup:event, onMessage:{addListener: fn => window.receive = fn}},
        alarms: {onAlarm:event},
        contextMenus: {onClicked:event},
        tabs: {
          query: async () => [{id: 7, windowId: 3, url: 'https://meli.zying.net/'}],
          update: async (...args) => { updates.push(args); return {id: 7}; },
          create: async options => { created.push(options); return {id: 8}; }
        },
        windows: {update: async (...args) => { updates.push(args); }}
      };
      window.importScripts = () => {};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("new Promise(resolve => receive({type:'OPEN_ZYING_LOGIN'}, {}, resolve))")
    assert result == {"ok": True, "tab_id": 7, "existing": True}
    assert page.evaluate("updates[0]") == [7, {"url": "https://meli.zying.net/#/login", "active": True}]
    assert page.evaluate("created") == []
    page.close()


def test_ai_weight_price_open_login_returns_before_slow_status_request(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event = {addListener: () => {}};
      const storageArea = {
        get: (_, callback) => callback({}),
        set: (_, callback) => callback && callback(),
        remove: (_, callback) => callback && callback()
      };
      window.updates=[]; window.auditStarted='';
      window.chrome={
        storage:{sync:storageArea,session:storageArea,local:storageArea},
        runtime:{onInstalled:event,onStartup:event,onMessage:{addListener:fn=>window.receive=fn}},
        alarms:{onAlarm:event},contextMenus:{onClicked:event},
        tabs:{
          query:async()=>[{id:7,windowId:3,url:'https://meli.zying.net/#/product'}],
          update:async(...args)=>{updates.push(args);return {id:7};}
        },
        windows:{update:async(...args)=>{updates.push(args);}}
      };
      window.importScripts=()=>{};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""async () => {
      aiWeightPriceAction=action=>{
        auditStarted=action;
        return new Promise(()=>{});
      };
      const response=await Promise.race([
        new Promise(resolve=>receive({type:'OPEN_AI_WEIGHT_PRICE_LOGIN'},{},resolve)),
        new Promise(resolve=>setTimeout(()=>resolve({timeout:true}),200))
      ]);
      return {response,auditStarted,updates};
    }""")

    assert result["response"] == {"ok": True, "tab_id": 7, "existing": True}
    assert result["auditStarted"] == "login/open"
    assert result["updates"][0] == [7, {"url": "https://meli.zying.net/#/login", "active": True}]
    page.close()


def test_zying_infringement_starts_with_product_cursor_and_pending_only_copy(popup):
    popup.locator("#zying-infringement-mode").click()
    popup.evaluate("""() => {
      zyingContext = {credential:'page-token', categories:[]};
      authenticated = true;
      document.querySelector('#zying-infringement-start').disabled = false;
    }""")
    popup.locator("#zying-infringement-start-product-id").fill("848332340")
    popup.locator("#zying-infringement-max-items").fill("4")
    popup.locator("#zying-infringement-start").click()
    popup.wait_for_function("messages.some(m => m.type === 'START_ZYING_INFRINGEMENT')")
    message = popup.evaluate("messages.find(m => m.type === 'START_ZYING_INFRINGEMENT')")
    assert message["params"]["start_product_id"] == "848332340"
    assert message["params"]["max_items"] == 4
    assert "待审核列表" in popup.locator("#zying-infringement-panel").inner_text()
    assert "每 20 个" in popup.locator("#zying-infringement-panel").inner_text()


def test_zying_developers_refresh_live_selector_without_webpack(popup):
    popup.add_script_tag(path=str(EXTENSION / "zying-page.js"))
    rows = popup.evaluate("""async () => {
      const field = document.createElement('div');
      field.className = 'ant-form-item';
      field.innerHTML = '<label>产品开发</label><div class="ant-select"></div>';
      document.body.append(field);
      const select = field.querySelector('.ant-select');
      select.__reactFiber$test = {memoizedProps:{options:[{value:17,label:'开发甲'}]}};
      window.__zeshunZyingDevelopersCache = {expiresAt:Date.now()+60000,rows:[{id:'99',name:'旧人员'}]};
      const first = await zeshunReadZyingProductDevelopers();
      select.__reactFiber$test.memoizedProps.options = [{value:18,label:'开发乙'}];
      return [first, await zeshunReadZyingProductDevelopers()];
    }""")
    assert rows == [[{"id": "17", "name": "开发甲"}], [{"id": "18", "name": "开发乙"}]]


def test_zying_developers_read_current_vite_localforage_cache(browser):
    page = browser.new_page()
    page.route("https://meli.zying.net/**", lambda route: route.fulfill(
        body="<meta charset='utf-8'><main></main>", content_type="text/html"
    ))
    page.goto("https://meli.zying.net/#/product")
    page.add_script_tag(path=str(EXTENSION / "zying-page.js"))
    rows = page.evaluate("""async () => {
      await new Promise((resolve, reject) => {
        const request = indexedDB.open('localforage', 1);
        request.onupgradeneeded = () => request.result.createObjectStore('keyvaluepairs');
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const db = request.result;
          const put = db.transaction('keyvaluepairs', 'readwrite').objectStore('keyvaluepairs').put({
            time: Date.now(), data: {list: [{id:17,name:'产品开发甲'}, {id:18,name:'产品开发乙'}]}
          }, 'logins');
          put.onerror = () => reject(put.error);
          put.onsuccess = () => { db.close(); resolve(); };
        };
      });
      return await zeshunReadZyingProductDevelopers();
    }""")
    assert rows == [
        {"id": "17", "name": "产品开发甲"},
        {"id": "18", "name": "产品开发乙"},
    ]
    page.close()


def test_ai_weight_price_image_uses_server_proxy_after_browser_fetch_failure(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{}};
      const storage={get:(_,cb)=>cb({}),set:(_,cb)=>cb&&cb(),remove:(_,cb)=>cb&&cb()};
      window.chrome={storage:{sync:storage,session:storage,local:storage},
        runtime:{onInstalled:event,onStartup:event,onMessage:event},alarms:{onAlarm:event},
        contextMenus:{onClicked:event}};
      window.importScripts=()=>{};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""async () => {
      window.fetch=async()=>{ throw new TypeError('Failed to fetch'); };
      window.proxyCall=null;
      aiWeightPriceAction=async(action,payload)=>{
        proxyCall={action,payload};
        return {data_url:'data:image/jpeg;base64,dGVzdA=='};
      };
      return {dataUrl:await aiWeightPriceImageDataUrl('https://oss.hzzying.com/item.jpg'),proxyCall};
    }""")
    assert result == {
        "dataUrl": "data:image/jpeg;base64,dGVzdA==",
        "proxyCall": {
            "action": "image",
            "payload": {"url": "https://oss.hzzying.com/item.jpg"},
        },
    }
    page.close()


def test_ai_weight_price_reads_exactly_one_zying_product_per_step(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{}};
      const storage={get:(_,cb)=>cb({}),set:(_,cb)=>cb&&cb(),remove:(_,cb)=>cb&&cb()};
      window.chrome={storage:{sync:storage,session:storage,local:storage},
        runtime:{onInstalled:event,onStartup:event,onMessage:event},alarms:{onAlarm:event},
        contextMenus:{onClicked:event}};
      window.importScripts=()=>{};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""async () => {
      aiWeightPriceTab=async()=>7;
      aiWeightPriceWaitTab=async()=>{};
      window.extractRequests=[];
      aiWeightPriceSendContentMessage=async(_tabId,message)=>{
        if(message.type==='AI_WEIGHT_PRICE_SELECT_PAGE') return {ok:true};
        extractRequests.push({start_index:message.start_index,max_items:message.max_items});
        const products=[
          {erp_goods_id:'1001',title:'第一件',main_image_url:'https://img.test/1.jpg',source_index:1},
          {erp_goods_id:'1002',title:'第二件',main_image_url:'https://img.test/2.jpg',source_index:2}
        ];
        return {ok:true,products:products.slice(message.start_index,message.start_index+1)};
      };
      const first=await aiWeightPriceReadNextProduct({}, {}, {});
      const second=await aiWeightPriceReadNextProduct({}, {}, {...first.cursor,seen_ids:['1001']});
      return {first,second,extractRequests};
    }""")

    assert result["first"]["product"]["erp_goods_id"] == "1001"
    assert result["second"]["product"]["erp_goods_id"] == "1002"
    assert all(request["max_items"] == 1 for request in result["extractRequests"])
    assert result["extractRequests"] == [
        {"start_index": 0, "max_items": 1},
        {"start_index": 0, "max_items": 1},
        {"start_index": 1, "max_items": 1},
    ]
    page.close()


def test_ai_weight_price_applies_selected_zying_filters_before_first_read(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{}};
      const storage={get:(_,cb)=>cb({}),set:(_,cb)=>cb&&cb(),remove:(_,cb)=>cb&&cb()};
      window.filterScripts=[];
      window.chrome={storage:{sync:storage,session:storage,local:storage},
        runtime:{onInstalled:event,onStartup:event,onMessage:event},alarms:{onAlarm:event},
        contextMenus:{onClicked:event},scripting:{executeScript:async args=>{
          filterScripts.push({world:args.world,args:args.args});
          return [{result:{ok:true,category:{selected:true},developer:{selected:true}}}];
        }}};
      window.zeshunApplyZyingProductFilters=()=>({ok:true});
      window.importScripts=()=>{};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""async () => {
      aiWeightPriceTab=async()=>7;
      aiWeightPriceWaitTab=async()=>{};
      aiWeightPriceSendContentMessage=async(_tabId,message)=>{
        if(message.type==='AI_WEIGHT_PRICE_SELECT_PAGE') return {ok:true};
        return {ok:true,products:[{erp_goods_id:'1001',title:'筛选后的商品',
          main_image_url:'https://img.test/1.jpg',source_index:1}]};
      };
      const read=await aiWeightPriceReadNextProduct({},
        {category:'leaf',product_developer_id:'17',product_developer_name:'产品开发甲'},{});
      return {read,filterScripts};
    }""")
    assert result["read"]["product"]["erp_goods_id"] == "1001"
    assert result["filterScripts"] == [{
        "world": "MAIN",
        "args": [{"category": "leaf", "product_developer_id": "17", "product_developer_name": "产品开发甲"}],
    }]
    page.close()


def test_ai_weight_price_reinjects_missing_1688_receiver(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{}};
      const storage={get:(_,cb)=>cb({}),set:(_,cb)=>cb&&cb(),remove:(_,cb)=>cb&&cb()};
      window.chrome={storage:{sync:storage,session:storage,local:storage},
        runtime:{onInstalled:event,onStartup:event,onMessage:event},alarms:{onAlarm:event},
        contextMenus:{onClicked:event},scripting:{executeScript:async args=>{window.injected=args;}}};
      window.importScripts=()=>{};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""async () => {
      let calls=0;
      sendTabMessage=async()=>{
        calls+=1;
        if(calls===1) throw new Error('Could not establish connection. Receiving end does not exist.');
        return {ok:true,candidates:[{url:'https://detail.1688.com/offer/1.html'}]};
      };
      const response=await aiWeightPriceSendContentMessage(7,{type:'AI_WEIGHT_PRICE_SEARCH'},['content-1688.js']);
      return {response,calls,injected};
    }""")

    assert result["response"]["ok"] is True
    assert result["calls"] == 2
    assert result["injected"] == {"target": {"tabId": 7}, "files": ["content-1688.js"]}
    page.close()


def test_zying_product_extractor_honors_single_item_cursor(browser):
    page = browser.new_page()
    page.set_content("""
      <div class="product-item" data-product-id="1001"><span class="product-title">第一件</span><img src="https://img.test/1.jpg"></div>
      <div class="product-item" data-product-id="1002"><span class="product-title">第二件</span><img src="https://img.test/2.jpg"></div>
    """)
    page.evaluate("window.chrome={runtime:{onMessage:{addListener:()=>{}}}}")
    page.add_script_tag(path=str(EXTENSION / "content-zying.js"))
    products = page.evaluate("aiWeightPriceExtractProducts({start_index:1,max_items:1})")
    assert [product["erp_goods_id"] for product in products] == ["1002"]
    page.close()


def test_purchase_tracking_listener_ignores_weight_price_messages(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      window.listeners=[];
      window.chrome={runtime:{onMessage:{addListener:listener=>listeners.push(listener)}}};
    }""")
    page.add_script_tag(path=str(EXTENSION / "purchase-tracking-content.js"))
    result = page.evaluate("""() => {
      let response='not-called';
      const returned=listeners[0]({type:'AI_WEIGHT_PRICE_SEARCH'},null,value=>response=value);
      return {returned,response};
    }""")
    assert result == {"returned": False, "response": "not-called"}
    page.close()


def test_1688_image_search_repeats_twenty_upload_submit_result_cycles(browser):
    page = browser.new_page()
    page.route("https://www.1688.com/**", lambda route: route.fulfill(body="""
      <meta charset="utf-8"><style>#image-search{width:600px;height:180px} #image-submit{width:90px;height:36px}</style>
      <section id="image-search">
        <input id="picture" type="file" accept=".jpg,.jpeg,.png,.webp" hidden>
        <button id="ordinary-search">搜索</button>
        <button id="image-submit" class="action--fixture actionPrimary--fixture">搜索</button>
      </section>
      <div id="search-result" class="search-result"></div>
      <script>
        window.searchCount=0; window.wrongClicks=0;
        ordinarySearch=document.querySelector('#ordinary-search');
        ordinarySearch.onclick=()=>window.wrongClicks++;
        picture=document.querySelector('#picture');
        picture.onchange=()=>document.querySelector('#search-result').innerHTML='';
        imageSubmit=document.querySelector('#image-submit');
        imageSubmit.onclick=()=>{
          const id=++window.searchCount;
          document.querySelector('#search-result').innerHTML=`<article class="offer-card"><a href="https://detail.1688.com/offer/${900000+id}.html"><img width="100" height="100" src="https://img.example/${id}.jpg" alt="候选${id}"><span class="title-text">候选${id}</span></a><span class="price">￥${id}</span></article>`;
        };
      </script>
    """, content_type="text/html"))
    page.add_init_script("""window.chrome={runtime:{onMessage:{addListener:listener=>window.read1688=listener},sendMessage:async()=>({ok:true,data:{}})}}""")
    page.goto("https://www.1688.com/")
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    results = page.evaluate("""async () => {
      const dataUrl='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=';
      const rows=[];
      for(let index=1;index<=20;index++){
        const response=await new Promise(resolve=>read1688({type:'AI_WEIGHT_PRICE_SEARCH',data_url:dataUrl,timeout_ms:3000},null,resolve));
        rows.push({ok:response.ok,submitted:response.submitted,url:response.candidates?.[0]?.url});
      }
      return {rows,searchCount,wrongClicks,uploaded:picture.files.length};
    }""")

    assert results["searchCount"] == 20
    assert results["wrongClicks"] == 0
    assert results["uploaded"] == 1
    assert len(results["rows"]) == 20
    assert all(row["ok"] and row["submitted"] for row in results["rows"])
    assert [row["url"] for row in results["rows"]] == [
        f"https://detail.1688.com/offer/{900000 + index}.html" for index in range(1, 21)
    ]
    page.close()


def test_1688_image_search_clicks_portal_submit_button_after_upload(browser):
    page = browser.new_page()
    page.route("https://www.1688.com/**", lambda route: route.fulfill(body="""
      <meta charset="utf-8"><style>
        #home-search{position:absolute;left:20px;top:20px;width:700px;height:90px}
        #portal{position:fixed;left:400px;top:180px;width:320px;height:240px;background:#fff}
        #preview{width:120px;height:120px}.dialog-action{width:90px;height:36px;background:#f60}
      </style>
      <section id="home-search"><input id="picture" type="file" accept="image/*" hidden></section>
      <button id="keyword-search" class="searchBtn--homepage">搜索</button>
      <div id="portal" hidden><img id="preview"><div class="dialog-action">搜索</div></div>
      <div id="results" class="search-result"></div>
      <script>
        window.searchCount=0; window.wrongClicks=0;
        document.querySelector('#keyword-search').onclick=()=>window.wrongClicks++;
        document.querySelector('#picture').onchange=event=>{
          document.querySelector('#results').innerHTML='';
          document.querySelector('#preview').src=URL.createObjectURL(event.target.files[0]);
          document.querySelector('#portal').hidden=false;
        };
        document.querySelector('.dialog-action').onclick=()=>{
          const id=++window.searchCount;
          document.querySelector('#results').innerHTML=`<article class="search-result-card">
            <a href="https://detail.1688.com/offer/${920000+id}.html"><img src="https://img.example/${id}.jpg">
            <span class="title-text">弹层搜图候选${id}</span></a><span class="price">￥${id}</span></article>`;
        };
      </script>
    """, content_type="text/html"))
    page.add_init_script("""window.chrome={runtime:{onMessage:{addListener:listener=>window.read1688=listener},sendMessage:async()=>({ok:true,data:{}})}}""")
    page.goto("https://www.1688.com/")
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    results = page.evaluate("""async () => {
      const dataUrl='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=';
      const rows=[];
      for(let index=1;index<=20;index++){
        rows.push(await new Promise(resolve=>read1688({type:'AI_WEIGHT_PRICE_SEARCH',data_url:dataUrl,timeout_ms:3000},null,resolve)));
      }
      return {rows,searchCount,wrongClicks,uploaded:picture.files.length};
    }""")

    assert results["searchCount"] == 20
    assert results["wrongClicks"] == 0
    assert results["uploaded"] == 1
    assert all(row["ok"] and row["submitted"] and row["ready"] for row in results["rows"])
    assert [row["candidates"][0]["url"] for row in results["rows"]] == [
        f"https://detail.1688.com/offer/{920000 + index}.html" for index in range(1, 21)
    ]
    assert all(row["submit_label"] == "搜索" for row in results["rows"])
    page.close()


@pytest.mark.parametrize("input_state", ["cleared", "replaced", "removed"])
def test_1688_uploaded_count_popup_submits_remote_preview(browser, input_state):
    page = browser.new_page()
    page.route("https://www.1688.com/**", lambda route: route.fulfill(body="""
      <meta charset="utf-8">
      <section><input type="search"><button id="keyword">搜索</button><span>以图搜款</span></section>
      <input id="upload" type="file" accept="image/*" hidden>
      <div id="panel" hidden><header>以图搜款 <span>已上传1张图</span></header>
        <div><img src="https://img.test/uploaded.jpg" width="100" height="100"></div>
        <button id="submit"><span>搜索</span></button></div>
      <div class="search-result" id="results"></div>
      <script>
        window.searches=0; window.wrongClicks=0;
        keyword.onclick=()=>wrongClicks++;
        window.attachUpload=()=>{
          const input=document.querySelector('#upload');
          input.onchange=()=>{
            results.innerHTML=''; panel.hidden=false;
            if(window.inputState==='cleared') input.value='';
            if(window.inputState==='replaced') input.replaceWith(input.cloneNode());
            if(window.inputState==='removed') input.remove();
          };
        };
        attachUpload();
        submit.onclick=()=>{
          const id=++searches; panel.hidden=true;
          results.innerHTML=`<a href="https://detail.1688.com/offer/${930000+id}.html"><img src="https://img.test/a.jpg" alt="本次搜图结果"></a>`;
          if(!document.querySelector('#upload')){
            const input=document.createElement('input'); input.id='upload';input.type='file';input.accept='image/*';input.hidden=true;document.body.append(input);
          }
          attachUpload();
        };
      </script>
    """, content_type="text/html"))
    page.add_init_script("window.chrome={runtime:{onMessage:{addListener:fn=>window.listener=fn},sendMessage:async()=>({ok:true})}}")
    page.goto("https://www.1688.com/")
    page.evaluate("state=>window.inputState=state", input_state)
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    result = page.evaluate("""async () => {
      const rows=[];
      for(let i=0;i<20;i++){
        const response=await new Promise(resolve=>listener({type:'AI_WEIGHT_PRICE_SEARCH',
          data_url:'data:image/png;base64,dGVzdA==',timeout_ms:3000},null,resolve));
        rows.push(response); if(!response.ok) break;
      }
      return {rows,searches,wrongClicks};
    }""")
    assert result["searches"] == 20, result
    assert result["wrongClicks"] == 0
    assert all(row["ok"] and row["ready"] for row in result["rows"])
    assert [row["candidates"][0]["url"] for row in result["rows"]] == [
        f"https://detail.1688.com/offer/{930000+i}.html" for i in range(1, 21)
    ]
    page.close()


def test_1688_image_search_clicks_non_button_image_action_after_upload(browser):
    page = browser.new_page()
    page.route("https://www.1688.com/**", lambda route: route.fulfill(body="""
      <meta charset="utf-8"><style>#image-search{width:600px;height:180px} #image-submit{display:block;width:90px;height:36px}</style>
      <section id="image-search">
        <input id="picture" type="file" accept="image/*" hidden>
        <button id="ordinary-search">搜索</button>
        <div id="image-submit" data-spm-click="搜索图片">搜索图片</div>
      </section>
      <div id="search-result" class="search-result"></div>
      <script>
        window.searchCount=0; window.wrongClicks=0;
        document.querySelector('#ordinary-search').onclick=()=>window.wrongClicks++;
        document.querySelector('#picture').onchange=()=>{};
        document.querySelector('#image-submit').onclick=()=>{
          const id=++window.searchCount;
          document.querySelector('#search-result').innerHTML=`<article class="offer-card"><a href="https://detail.1688.com/offer/${910000+id}.html"><img width="100" height="100" src="https://img.example/${id}.jpg"><span class="title-text">候选${id}</span></a><span class="price">￥${id}</span></article>`;
        };
      </script>
    """, content_type="text/html"))
    page.add_init_script("""window.chrome={runtime:{onMessage:{addListener:listener=>window.read1688=listener},sendMessage:async()=>({ok:true,data:{}})}}""")
    page.goto("https://www.1688.com/")
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    result = page.evaluate("""async () => {
      const dataUrl='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=';
      return await new Promise(resolve=>read1688({type:'AI_WEIGHT_PRICE_SEARCH',data_url:dataUrl,timeout_ms:3000},null,resolve));
    }""")
    assert result["ok"] is True
    assert result["submitted"] is True
    assert result["submit_label"] == "搜索图片"
    assert page.evaluate("searchCount") == 1
    assert page.evaluate("wrongClicks") == 0
    assert page.locator("#search-result a").count() == 1
    assert page.locator("#search-result a").get_attribute("href").endswith("910001.html")
    page.close()


def test_zying_product_collection_forwards_selected_category_and_developer(popup):
    popup.evaluate("""() => {
      messages.length = 0;
      chrome.tabs.query = async () => [{id:88, active:true, lastAccessed:100,
        url:'https://meli.zying.net/#/product'}];
      chrome.runtime.sendMessage = (message, callback) => {
        messages.push(message);
        if (message.type === 'GET_STATE') return callback({authenticated:true,user:{username:'test'}});
        if (message.type === 'GET_ZYING_STATUS') return callback({ok:true,running:false});
        if (message.type === 'READ_ZYING_CONTEXT') return callback({ok:true,credential:'page-token',
          categories:[{category_id:'202170568',category_name:'家居/家电类'}],
          developers:[{id:'17',name:'产品开发甲'},{id:'18',name:'产品开发乙'}]});
        if (message.type === 'GET_ZYING_OPTIONS') return callback({ok:true,
          categories:message.context.categories,developers:message.context.developers});
        if (message.type === 'START_ZYING_COLLECTION') return callback({ok:true,running:true});
        return callback({ok:true,running:false});
      };
    }""")
    popup.locator("#zying-mode").click()
    popup.locator("#zying-refresh").click()
    popup.wait_for_function("document.querySelector('#zying-developer').options.length === 3")
    popup.locator("#zying-category").select_option("202170568")
    popup.locator("#zying-developer").select_option("18")
    popup.locator("#zying-start").click()
    popup.wait_for_function("messages.some(message => message.type === 'START_ZYING_COLLECTION')")
    message = popup.evaluate("messages.find(message => message.type === 'START_ZYING_COLLECTION')")
    assert message["params"]["category"] == "202170568"
    assert message["params"]["category_name"] == "家居/家电类"
    assert message["params"]["product_developer_id"] == "18"
    assert message["params"]["product_developer_name"] == "产品开发乙"


def test_background_image_search_follows_new_1688_result_tab(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{},removeListener:()=>{}};
      const storage={get:(_,cb)=>cb({}),set:(_,cb)=>cb&&cb(),remove:(_,cb)=>cb&&cb()};
      window.chrome={storage:{sync:storage,session:storage,local:storage},
        runtime:{onInstalled:event,onStartup:event,onMessage:event},alarms:{onAlarm:event},
        contextMenus:{onClicked:event},scripting:{executeScript:async()=>{}},
        tabs:{query:async()=>window.tabs}};
      window.importScripts=()=>{};
      window.tabs=[{id:7,url:'https://www.1688.com/',status:'complete'}];
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""async () => {
      sendTabMessage=async(tabId,message)=>{
        if(message.type==='AI_WEIGHT_PRICE_SEARCH'){
          tabs=[...tabs,{id:8,url:'https://air.1688.com/kapp/1688-search/pc-image-search/?imageId=test',status:'complete'}];
          throw new Error('The message port closed before a response was received.');
        }
        if(message.type==='AI_WEIGHT_PRICE_SEARCH_RESULTS'&&tabId===8){
          return {ok:true,ready:true,candidates:[{url:'https://detail.1688.com/offer/99.html',main_image_url:'https://img.test/99.jpg'}]};
        }
        return {ok:true,ready:false,candidates:[]};
      };
      return await aiWeightPriceSearchSupplier(7,{data_url:'data:image/png;base64,dGVzdA=='},1500);
    }""")
    assert result == [{
        "url": "https://detail.1688.com/offer/99.html",
        "main_image_url": "https://img.test/99.jpg",
        "result_tab_id": 8,
    }]
    page.close()


def test_background_image_search_does_not_reload_new_result_tab_while_injecting_listener(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{},removeListener:()=>{}};
      const storage={get:(_,cb)=>cb({}),set:(_,cb)=>cb&&cb(),remove:(_,cb)=>cb&&cb()};
      window.reloads=[];
      window.injections=[];
      window.chrome={storage:{sync:storage,session:storage,local:storage},
        runtime:{onInstalled:event,onStartup:event,onMessage:event},alarms:{onAlarm:event},
        contextMenus:{onClicked:event},
        scripting:{executeScript:async args=>{
              if(args.func) return [{result:args.target.tabId===7?'1.8.22':''}];
          injections.push(args);
        }},
        tabs:{query:async()=>window.tabs,reload:async tabId=>reloads.push(tabId)}};
      window.importScripts=()=>{};
      window.tabs=[{id:7,url:'https://www.1688.com/',status:'complete'}];
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""async () => {
      sendTabMessage=async(tabId,message)=>{
        if(message.type==='AI_WEIGHT_PRICE_SEARCH'){
          tabs=[...tabs,{id:8,url:'https://air.1688.com/kapp/1688-search/pc-image-search/?imageId=test',status:'complete'}];
          throw new Error('The message port closed before a response was received.');
        }
        if(message.type==='AI_WEIGHT_PRICE_SEARCH_RESULTS'&&tabId===8){
          if(!injections.some(item=>item.target.tabId===8))
            throw new Error('Could not establish connection. Receiving end does not exist.');
          return {ok:true,ready:true,candidates:[{url:'https://detail.1688.com/offer/100.html'}]};
        }
        return {ok:true,ready:false,candidates:[]};
      };
      const candidates=await aiWeightPriceSearchSupplier(7,{data_url:'data:image/png;base64,dGVzdA=='},1500);
      return {candidates,reloads,injections};
    }""")
    assert result["candidates"] == [{
        "url": "https://detail.1688.com/offer/100.html",
        "result_tab_id": 8,
    }]
    assert result["reloads"] == []
    assert any(
        injection.get("target") == {"tabId": 8, "frameIds": [0]}
        and injection.get("files") == ["content-1688.js"]
        for injection in result["injections"]
    )
    page.close()


def test_image_search_recovers_document_replacement_without_all_frame_injection(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{},removeListener:()=>{}};
      const storage={get:(_,cb)=>cb({}),set:(_,cb)=>cb&&cb(),remove:(_,cb)=>cb&&cb()};
      window.injections=[]; window.reloads=[];
      window.chrome={storage:{sync:storage,session:storage,local:storage},
        runtime:{onInstalled:event,onStartup:event,onMessage:event},alarms:{onAlarm:event},
        contextMenus:{onClicked:event},
        scripting:{executeScript:async args=>{
          if(args.func) return [{result:''}];
          if(args.target.allFrames) throw new Error('Cannot access contents of child frame');
          injections.push(args.target);
        }},tabs:{reload:async id=>reloads.push(id)}};
      window.importScripts=()=>{};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""async () => {
      let calls=0, uploads=0;
      sendTabMessage=async()=>{
        if(++calls<=3) throw new Error('Could not establish connection. Receiving end does not exist.');
        uploads++;
        return {ok:true,submitted:true,ready:true,candidates:[{url:'https://detail.1688.com/offer/99.html'}]};
      };
      const response=await aiWeightPriceSendImageMessage(7,{type:'AI_WEIGHT_PRICE_SEARCH'},['content-1688.js'],2000);
      return {response,calls,uploads,injections,reloads};
    }""")
    assert result["response"]["ready"] is True
    assert result["calls"] == 4
    assert result["uploads"] == 1
    assert result["injections"] == [{"tabId": 7, "frameIds": [0]}] * 3
    assert result["reloads"] == []
    page.close()


def test_1688_reinjection_restores_receiver_when_version_marker_survives(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      window.receivers=new Set();
      window.chrome={runtime:{onMessage:{
        addListener:fn=>receivers.add(fn), removeListener:fn=>receivers.delete(fn),
        hasListener:fn=>receivers.has(fn)
      },sendMessage:async()=>({ok:true})}};
    }""")
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    assert page.evaluate("receivers.size") == 1
    page.evaluate("receivers.clear()")
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    assert page.evaluate("receivers.size") == 1
    result = page.evaluate("""() => {
      let response; [...receivers][0]({type:'AI_WEIGHT_PRICE_SEARCH_RESULTS'},null,value=>response=value);
      return response;
    }""")
    assert result["ok"] is True
    page.close()


def test_image_search_missing_receiver_obeys_deadline(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{}};
      const storage={get:(_,cb)=>cb({}),set:(_,cb)=>cb&&cb(),remove:(_,cb)=>cb&&cb()};
      window.chrome={storage:{sync:storage,session:storage,local:storage},
        runtime:{onInstalled:event,onStartup:event,onMessage:event},alarms:{onAlarm:event},
        contextMenus:{onClicked:event},scripting:{executeScript:async()=>[]}};
      window.importScripts=()=>{};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""async () => {
      let calls=0; const start=Date.now();
      sendTabMessage=async()=>{calls++;throw new Error('Receiving end does not exist');};
      try {await aiWeightPriceSendFrameMessage(7,0,{},['content-1688.js'],450);}
      catch(error){return {error:error.message,calls,elapsed:Date.now()-start};}
    }""")
    assert "1688页面连接未就绪" in result["error"]
    assert result["calls"] >= 2
    assert 450 <= result["elapsed"] < 2000
    page.close()


def test_1688_lazy_image_search_opener_uses_live_homepage_label(browser):
    page = browser.new_page()
    page.route("https://www.1688.com/**", lambda route: route.fulfill(body="""
      <meta charset="utf-8"><div id="widget"></div><div class="search-result" id="results"></div>
      <script>
        window.opens=0; window.searches=0;
        setTimeout(()=>{
          widget.innerHTML='<div id="opener"><span>以图搜款</span></div>';
          document.querySelector('#opener').onclick=()=>{
            opens++;
            widget.innerHTML='<input type="file" accept="image/*"><button id="submit">搜索图片</button>';
            submit.onclick=()=>{
              if(!document.querySelector('input').files.length) throw new Error('no image');
              searches++;
              results.innerHTML='<a href="https://detail.1688.com/offer/990001.html"><img src="https://img.test/a.jpg" alt="已搜索"></a>';
            };
          };
        },700);
      </script>
    """, content_type="text/html"))
    page.add_init_script("window.chrome={runtime:{onMessage:{addListener:fn=>window.listener=fn},sendMessage:async()=>({ok:true})}}")
    page.goto("https://www.1688.com/")
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    result = page.evaluate("""async () => {
      const response=await new Promise(resolve=>listener({type:'AI_WEIGHT_PRICE_SEARCH',
        data_url:'data:image/png;base64,dGVzdA==',timeout_ms:3000},null,resolve));
      return {response,opens,searches};
    }""")
    assert result["response"]["ok"] is True
    assert result["response"]["ready"] is True
    assert result["opens"] == result["searches"] == 1
    page.close()


@pytest.mark.parametrize("configured", [True, False])
@pytest.mark.parametrize("delayed", [True, False])
def test_1688_opens_widget_before_using_preexisting_hidden_input(browser, configured, delayed):
    page = browser.new_page()
    page.route("https://www.1688.com/**", lambda route: route.fulfill(body="""
      <meta charset="utf-8">
      <button id="opener">以图搜款</button>
      <div id="panel" style="display:none"><input type="file" accept="image/*" hidden></div>
      <div class="search-result" id="results"></div>
      <script>
        window.opens=0; window.uploads=0;
        document.querySelector('#opener').onclick=()=>{
          opens++;
          setTimeout(() => {
          panel.style.display='block';
          // Opening mounts the live control; the initial input is a placeholder.
          panel.innerHTML='<input type="file" accept="image/*" hidden><button id="submit">搜索图片</button>';
          panel.querySelector('input').onchange=()=>{uploads++};
          submit.onclick=()=>{
            if(!uploads) return;
            results.innerHTML='<a href="https://detail.1688.com/offer/990002.html"><img alt="当前结果"></a>';
          };
          }, window.delayMount ? 500 : 0);
        };
      </script>
    """, content_type="text/html"))
    page.add_init_script("window.chrome={runtime:{onMessage:{addListener:fn=>window.listener=fn},sendMessage:async()=>({ok:true})}}")
    page.goto("https://www.1688.com/")
    page.evaluate("value => window.delayMount = value", delayed)
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    result = page.evaluate("""async configured => {
      const response=await new Promise(resolve=>listener({type:'AI_WEIGHT_PRICE_SEARCH',
        selectors:configured?{image_search_open:'#opener'}:{},
        data_url:'data:image/png;base64,dGVzdA==',timeout_ms:3000},null,resolve));
      return {response,opens,uploads};
    }""", configured)
    assert result["response"]["ok"] is True, result
    assert result["response"]["ready"] is True
    assert result["opens"] == result["uploads"] == 1
    page.close()


def test_1688_rendered_image_results_keep_click_path(browser):
    page = browser.new_page()
    page.route("https://air.1688.com/**", lambda route: route.fulfill(body="""
      <meta charset="utf-8"><div>找到以下货源</div>
      <section><article><img width="120" height="120" src="https://img.test/one.jpg"><p>同款货源 ￥22</p></article></section>
    """, content_type="text/html"))
    page.add_init_script("window.chrome={runtime:{onMessage:{addListener:listener=>window.read1688=listener},sendMessage:async()=>({ok:true,data:{}})}}")
    page.goto("https://air.1688.com/kapp/1688-search/pc-image-search/?imageId=test")
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    result = page.evaluate("""() => new Promise(resolve=>read1688({type:'AI_WEIGHT_PRICE_SEARCH_RESULTS'},null,resolve))""")
    assert result["ok"] is True
    assert result["ready"] is True
    assert result["candidates"][0]["main_image_url"] == "https://img.test/one.jpg"
    assert result["candidates"][0]["result_image_path"].endswith("img:nth-of-type(1)")
    page.close()


def test_zying_filter_bridge_applies_selected_category_and_developer(browser):
    page = browser.new_page()
    page.set_content("""
      <form class='ant-form-item' id='category-form'><label>商品分类</label>
        <div class='ant-cascader' id='category'></div><button type='button' id='category-search'>搜索</button>
      </form>
      <form class='ant-form-item' id='developer-form'><label>产品开发</label>
        <select id='developer'><option value=''>全部产品开发</option><option value='17'>产品开发甲</option></select>
        <button type='button' id='developer-search'>搜索</button>
      </form>
      <script>
        window.filterCalls=[]; window.searchClicks=[];
        category.addEventListener('click',()=>{});
        document.querySelector('#category-search').onclick=()=>searchClicks.push('category');
        document.querySelector('#developer-search').onclick=()=>searchClicks.push('developer');
        category.__reactFiber$fixture={memoizedProps:{options:[{value:'root',label:'家居',children:[{value:'leaf',label:'收纳'}]}],
          onChange:(values,options)=>filterCalls.push({kind:'category',values,options:options.map(item=>item.value)})}};
        developer.__reactFiber$fixture={memoizedProps:{options:[{value:'17',label:'产品开发甲'}],
          onChange:(value,option)=>filterCalls.push({kind:'developer',value,option:option.value})}};
      </script>
    """)
    page.add_script_tag(path=str(EXTENSION / "zying-page.js"))
    result = page.evaluate("""() => zeshunApplyZyingProductFilters({
      category:'leaf', product_developer_id:'17', product_developer_name:'产品开发甲'
    })""")
    assert result["ok"] is True
    assert result["category"]["selected"] is True
    assert result["developer"]["selected"] is True
    assert page.evaluate("filterCalls") == [
        {"kind": "category", "values": ["root", "leaf"], "options": ["root", "leaf"]},
    ]
    assert page.locator("#developer").input_value() == "17"
    assert page.evaluate("searchClicks") == ["category", "developer"]
    page.close()


def test_zying_product_tab_preferred_over_active_login(popup):
    result = popup.evaluate("""async () => {
      chrome.tabs.query = async () => [
        {id:1, active:true, url:'https://meli.zying.net/#/login'},
        {id:2, active:false, url:'https://meli.zying.net/#/product'}
      ];
      return (await findZyingPageTab()).id;
    }""")
    assert result == 2


def test_zying_cards_without_ids_read_fresh_matching_detail(browser):
    page = browser.new_page()
    page.set_content("""
      <div class="product-item"><span class="product-title">商品甲</span>
        <a href="https://detail.1688.com/offer/999999.html">供应商</a>
        <img class="product-pic" src="https://img.test/1.jpg"></div>
      <div class="product-item"><span class="product-title">商品乙</span>
        <img class="product-pic" src="https://img.test/2.jpg"></div>
      <span class="product-id">8888</span>
    """)
    page.evaluate("""() => {
      window.chrome={runtime:{onMessage:{addListener:()=>{}}}};
      for (const [index, node] of [...document.querySelectorAll('.product-title')].entries()) {
        node.onclick = () => {
          const panel = document.createElement('div'); panel.className='curd-detail-wrap';
          panel.innerHTML = '<button class="crud-detail-close">关闭</button><div class="crud-detail-header"><span class="h1">'+(1001+index)+'</span></div><textarea placeholder="请输入内容">'+node.textContent+'</textarea>';
          panel.querySelector('button').onclick=()=>panel.remove();
          document.body.append(panel);
        };
      }
    }""")
    page.add_script_tag(path=str(EXTENSION / "content-zying.js"))
    result = page.evaluate("""() => aiWeightPriceExtractProducts({selectors:{erp_id:'.product-id'},max_items:2})""")
    assert [row["erp_goods_id"] for row in result] == ["1001", "1002"]
    assert [row["title"] for row in result] == ["商品甲", "商品乙"]
    page.close()


def test_zying_products_wait_for_delayed_spa_cards(browser):
    page = browser.new_page()
    page.set_content('<main></main>')
    page.evaluate("window.chrome={runtime:{onMessage:{addListener:()=>{}}}}")
    page.add_script_tag(path=str(EXTENSION / "content-zying.js"))
    result = page.evaluate("""async () => {
      setTimeout(() => document.querySelector('main').innerHTML='<div class="product-item" data-product-id="1001"><span class="product-title">延迟加载商品</span><img src="https://img.test/1.jpg"></div>',300);
      return await aiWeightPriceExtractProducts();
    }""")
    assert result[0]["erp_goods_id"] == "1001"
    page.close()


def test_shared_zying_read_waits_for_categories_before_developers(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{},removeListener:()=>{}};
      window.importScripts=()=>{};
      window.chrome={runtime:{onInstalled:event,onStartup:event,onMessage:event},
        alarms:{onAlarm:event},contextMenus:{onClicked:event}};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""async () => {
      window.reads=[];
      aiWeightPriceTab=async()=>7;
      aiWeightPriceWaitTab=async()=>{};
      readZyingContext=async(id,options)=>{
        reads.push(options.includeDevelopers);
        return {credential:'test',categories:reads.length >= 2 ? [{category_id:'17'}] : [],
          developers:options.includeDevelopers ? [{id:'18',name:'开发甲'}] : []};
      };
      return await aiWeightPriceReadZyingTab({requireCategories:true,includeDevelopers:true});
    }""")
    assert result["context"]["developers"] == [{"id": "18", "name": "开发甲"}]
    assert page.evaluate("reads") == [False, False, True]
    page.close()


def test_zying_mismatched_detail_is_rejected(browser):
    page = browser.new_page()
    page.set_content('<div class="product-item"><span class="product-title">正确商品</span><img src="https://img.test/1.jpg"></div>')
    page.evaluate("window.chrome={runtime:{onMessage:{addListener:()=>{}}}}")
    page.add_script_tag(path=str(EXTENSION / "content-zying.js"))
    error = page.evaluate("""async () => {
      const wait = aiWeightPriceWaitFor;
      aiWeightPriceWaitFor = (check, message) => wait(check,message,500);
      document.querySelector('.product-title').onclick = () => {
        const panel=document.createElement('div');panel.className='curd-detail-wrap';
        panel.innerHTML='<div class="crud-detail-header"><span class="h1">9999</span></div><textarea placeholder="请输入内容">别的商品</textarea>';
        document.body.append(panel);
      };
      try { await aiWeightPriceExtractProducts(); return ''; }
      catch(error) { return error.message; }
    }""")
    assert "无法确认商品" in error
    page.close()


def test_live_developers_win_over_older_server_cache(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{}};
      window.importScripts=()=>{};
      window.chrome={runtime:{onInstalled:event,onStartup:event,onMessage:event},
        alarms:{onAlarm:event},contextMenus:{onClicked:event}};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""async () => {
      apiRequest=async(path,options)=>{
        window.sent=JSON.parse(options.body);
        return {categories:[],developers:[{id:'17',name:'旧人员'}]};
      };
      return await loadZyingOptions({credential:'test',categories:[],developers:[{id:'18',name:'新人员'}]});
    }""")
    assert result["developers"] == [{"id": "18", "name": "新人员"}]
    assert page.evaluate("sent.refresh") is True
    page.close()


@pytest.fixture
def supplier_page(browser):
    page = browser.new_page()
    page.route("https://detail.1688.com/**", lambda route: route.fulfill(body="""
      <meta charset="utf-8"><h1>错误的贸易有限公司</h1>
      <div class="membership-price">1年会员</div><p>8年经验，100件起批，推荐商品500g</p>
      <img src="https://img.alicdn.com/icon.svg">
      <div id="productTitle"><div class="title-content"><h1>真正的商品标题</h1></div></div>
      <div id="gallery"><img class="preview-img" src="https://cbu01.alicdn.com/product.jpg"></div>
      <div id="skuSelection" data-module="od_sku_selection"></div>
      <div id="productPackInfo" data-module="od_product_pack_info"></div>
      <img src="https://cbu01.alicdn.com/recommendation.jpg">
      <script>window.recommendation='https://cbu01.alicdn.com/unrelated.jpg';</script>
    """, content_type="text/html"))
    page.goto("https://detail.1688.com/offer/907173727091.html")
    page.add_script_tag(path=str(EXTENSION / "1688-page.js"))
    page.evaluate("""() => {
      window.chrome={runtime:{onMessage:{addListener:fn=>window.readSupplier=fn},
        sendMessage:async message=>({ok:true,data:zeshunRead1688PageData()})}};
      document.querySelector('#skuSelection').__reactFiber$test={memoizedProps:{skuItems:[
        {skuId:'11',specAttrs:'颜色:红色&gt;尺寸:小号',discountPrice:'',price:'12.50',stock:0},
        {skuId:'22',specAttrs:'颜色:蓝色&gt;尺寸:大号',price:'18.00',stock:null}
      ]}};
    }""")
    page.add_script_tag(path=str(EXTENSION / "content-1688.js"))
    yield page
    page.close()


def supplier_extract(page):
    return page.evaluate("""() => new Promise(resolve => readSupplier({type:'EXTRACT_PRODUCT'},null,resolve))""")


def test_1688_ignores_company_icons_recommendations_and_unlabelled_numbers(supplier_page):
    result = supplier_extract(supplier_page)
    assert result["ok"] is True
    product = result["product"]
    assert product["title"] == "真正的商品标题"
    assert product["price"] == 12.5
    assert product["images"] == ["https://cbu01.alicdn.com/product.jpg"]
    assert product["main_image_url"] == product["images"][0]
    assert product["weight_g"] is None
    assert product["package_length_cm"] is None
    assert len(product["variations"]) == 2
    assert product["variations"][0]["available_quantity"] == 0
    assert product["variations"][1]["available_quantity"] is None
    assert product["variations"][0]["attribute_combinations"] == [
        {"name": "颜色", "value_name": "红色"}, {"name": "尺寸", "value_name": "小号"}
    ]


def test_1688_package_units_and_per_sku_identity(supplier_page):
    supplier_page.evaluate("""() => {
      document.querySelector('#productPackInfo').__reactFiber$test={memoizedProps:{skuInfo:[
        {skuId:'22',weight:0.6,weightUnit:'kg',length:300,width:200,height:100,sizeUnit:'mm'},
        {skuId:'11',weightKg:0.25,lengthMm:100,widthMm:80,heightMm:40}
      ]}};
    }""")
    product = supplier_extract(supplier_page)["product"]
    small, large = product["variations"]
    assert small["weight_g"] == 250
    assert large["weight_g"] == 600
    assert [small[key] for key in ("package_length_cm", "package_width_cm", "package_height_cm")] == [10, 8, 4]
    assert [large[key] for key in ("package_length_cm", "package_width_cm", "package_height_cm")] == [30, 20, 10]
    assert product["weight_g"] == 425
    assert product["package_length_cm"] is None


def test_1688_main_reader_does_not_take_ancestor_recommendation_skus(supplier_page):
    result = supplier_page.evaluate("""() => {
      document.querySelector('#skuSelection').__reactFiber$test.return={memoizedProps:{recommendations:[
        {skuId:'999',specAttrs:'不相关推荐',price:1}
      ]}};
      return zeshunRead1688PageData();
    }""")
    assert len(result["sku_sets"]) == 1
    assert [row["skuId"] for row in result["sku_sets"][0]] == ["11", "22"]


def test_1688_isolated_content_world_uses_main_world_bridge(supplier_page):
    data = supplier_page.evaluate("zeshunRead1688PageData()")
    session = supplier_page.context.new_cdp_session(supplier_page)
    frame_id = session.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
    context_id = session.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "extension-test"})["executionContextId"]
    def evaluate(expression):
        result = session.send("Runtime.evaluate", {"expression": expression, "contextId": context_id,
            "awaitPromise": True, "returnByValue": True})
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")
    assert evaluate("Object.keys(document.querySelector('#skuSelection')).some(k=>k.startsWith('__reactFiber$'))") is False
    evaluate("window.chrome={runtime:{onMessage:{addListener:fn=>window.readSupplier=fn},sendMessage:async()=>({ok:true,data:" + json.dumps(data) + "})}}")
    evaluate((EXTENSION / "content-1688.js").read_text(encoding="utf-8"))
    result = evaluate("new Promise(resolve=>readSupplier({type:'EXTRACT_PRODUCT'},null,resolve))")
    assert result["ok"] is True
    assert len(result["product"]["variations"]) == 2
    assert result["product"]["price"] == 12.5
    session.detach()


def test_1688_delayed_skus_are_waited_for(supplier_page):
    supplier_page.evaluate("""() => {
      const node=document.querySelector('#skuSelection'); const fiber=node.__reactFiber$test;
      delete node.__reactFiber$test;
      setTimeout(()=>node.__reactFiber$test=fiber,500);
    }""")
    assert len(supplier_extract(supplier_page)["product"]["variations"]) == 2


def test_1688_page_identity_mismatch_does_not_return_product(supplier_page):
    supplier_page.evaluate("chrome.runtime.sendMessage=async()=>({ok:true,data:{offer_id:'999'}})")
    result = supplier_extract(supplier_page)
    assert result["ok"] is False
    assert "product" not in result


def test_1688_list_does_not_restore_known_wrong_stale_metrics(browser):
    page = browser.new_page()
    page.add_script_tag(path=str(Path(__file__).resolve().parents[1] / "bit/static/1688-products.js"))
    result = page.evaluate("""() => {
      const row={weight_g:8,price:1,package_length_cm:5,package_width_cm:6,package_height_cm:7,
        original_1688:{weight_g:null,price:null,package_length_cm:null,package_width_cm:null,package_height_cm:null}};
      return [products1688Weight(row), products1688Dimensions(row),products1688Money(products1688SourceValue(row,'price'))];
    }""")
    assert result == ["—", "—", "—"]
    page.close()


def test_1688_legacy_sku_table_uses_named_columns(supplier_page):
    supplier_page.evaluate("""() => {
      delete document.querySelector('#skuSelection').__reactFiber$test;
      document.querySelector('#skuSelection').innerHTML='<table class="sku-table"><thead><tr><th>颜色</th><th>价格</th><th>库存</th></tr></thead><tbody><tr data-sku-id="11"><td>红色</td><td>12.5</td><td>0</td></tr></tbody></table>';
    }""")
    product = supplier_extract(supplier_page)["product"]
    assert product["price"] == 12.5
    assert product["variations"][0]["attribute_combinations"] == [{"name": "颜色", "value_name": "红色"}]
    assert product["variations"][0]["available_quantity"] == 0


def test_1688_variant_detail_displays_actual_weight_and_package(browser):
    page = browser.new_page()
    page.add_script_tag(path=str(Path(__file__).resolve().parents[1] / "bit/static/1688-products.js"))
    markup = page.evaluate("""products1688VariationRows([{sku_id:'11',label:'红色',price:12.5,weight_g:250,
      package_length_cm:10,package_width_cm:8,package_height_cm:4}])""")
    assert "250 g" in markup
    assert "10 × 8 × 4 cm" in markup
    page.close()


def test_1688_background_reads_only_sender_detail_tab_in_main_world(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{}}; window.importScripts=()=>{};
      window.zeshunRead1688PageData=()=>{};
      window.executions=[];
      window.chrome={windows:{onRemoved:event},runtime:{onInstalled:event,onStartup:event,onMessage:{addListener:fn=>window.workerHandler=fn}},
        alarms:{onAlarm:event},contextMenus:{onClicked:event},scripting:{executeScript:async args=>{
          executions.push({world:args.world,tabId:args.target.tabId});
          return [{result:{offer_id:'907173727091',sku_sets:[],pack_sets:[]}}];
        }}};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    result = page.evaluate("""() => new Promise(resolve=>workerHandler({type:'READ_1688_PAGE_DATA'},
      {tab:{id:7,url:'https://detail.1688.com/offer/907173727091.html'}},resolve))""")
    assert result["data"]["offer_id"] == "907173727091"
    assert page.evaluate("executions") == [{"world": "MAIN", "tabId": 7}]
    rejected = page.evaluate("""() => new Promise(resolve=>workerHandler({type:'READ_1688_PAGE_DATA'},
      {tab:{id:8,url:'https://example.com/'}},resolve))""")
    assert rejected["ok"] is False
    assert len(page.evaluate("executions")) == 1
    page.close()


def test_1688_list_redirect_cannot_submit_another_product(browser):
    page = browser.new_page()
    page.evaluate("""() => {
      const event={addListener:()=>{}}; window.importScripts=()=>{};
      window.chrome={runtime:{onInstalled:event,onStartup:event,onMessage:event},alarms:{onAlarm:event},
        contextMenus:{onClicked:event},tabs:{create:async()=>({id:7}),update:async()=>{},remove:async()=>{}}};
    }""")
    page.add_script_tag(path=str(EXTENSION / "background.js"))
    error = page.evaluate("""async () => {
      aiWeightPriceWaitTab=async()=>{};extractFromTab=async()=>({source_item_id:'99999'});
      submitProduct=async()=>{throw new Error('不应提交');};
      try {await collect1688ListProduct({source_item_id:'907173727091',source_url:'https://detail.1688.com/offer/907173727091.html'});return '';}
      catch(error) {return error.message;}
    }""")
    assert "跳转到了其他商品" in error
    page.close()
