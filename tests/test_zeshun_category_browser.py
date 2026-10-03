"""Exercise destination categories in the real popup DOM with offline Chrome mocks."""

from pathlib import Path

import pytest


EXTENSION = Path(__file__).resolve().parents[1] / "browser_extension/zeshun_collector"


@pytest.fixture
def popup():
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).is_file():
            pytest.skip("Chromium is required")
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 420, "height": 950})
        page.route("**/*", lambda route: route.fulfill(
            path=str(EXTENSION / route.request.url.rsplit("/", 1)[-1])))
        page.add_init_script("""
          window.messages = [];
          window.saved = {zeshunCollectionCategoryOptions:{product:7, zying:9}};
          window.categories = [{id:7,name:'家居用品'},{id:9,name:'汽配产品'}];
          window.categoryError = '';
          window.loggedIn = true;
          window.platform = 'mercado';
          window.batchState = {running:false};
          const tabs = [
            {id:10,active:true,url:'https://articulo.mercadolibre.com.mx/MLM-1234567'},
            {id:11,url:'https://listado.mercadolibre.com.mx/test'},
            {id:12,url:'https://meli.zying.net/#/product'}
          ];
          window.chrome = {
            storage:{local:{
              get:async key => ({[key]:structuredClone(saved[key])}),
              set:async values => Object.assign(saved, structuredClone(values))
            }},
            tabs:{
              query:async options => options.active ? [tabs[0]] : tabs,
              sendMessage:(id,message,callback) => callback(
                message.type === 'EXTRACT_PRODUCT'
                  ? {ok:true,product:{source_item_id:'MLM1234567',title:'测试商品',source_platform:platform}}
                  : {ok:true,detail:true,platform,zying:{logged_in:true,message:'已登录'}}
              )
            },
            runtime:{openOptionsPage:()=>{},sendMessage:(message,callback)=>{
              messages.push(structuredClone(message));
              const source = {ok:true,credential:'offline-mock',
                categories:[{category_id:'123',category_name:'智赢来源测试分类'}],developers:[]};
              switch(message.type) {
                case 'GET_STATE':return callback({authenticated:loggedIn,user:{username:'界面测试'},queueLength:0});
                case 'GET_ZESHUN_CATEGORIES':return callback(categoryError
                  ? {ok:false,error:categoryError} : {ok:true,categories:structuredClone(categories)});
                case 'GET_MERCADO_DATA_STATUS':return callback({ok:true,ready:true,message:'已配置'});
                case 'READ_ZYING_CONTEXT':case 'GET_ZYING_OPTIONS':return callback(source);
                case 'GET_ZYING_STATUS':return callback({ok:true,status:'idle',running:false});
                case 'GET_PRODUCT_BATCH_STATUS':return callback({ok:true,state:structuredClone(batchState)});
                case 'START_PRODUCT_BATCH':
                  batchState = {running:true,params:message.params};
                  return callback({ok:true,state:structuredClone(batchState)});
                case 'START_ZYING_COLLECTION':return callback({ok:true});
                case 'SUBMIT_PRODUCT':return callback({ok:true,taskId:88});
                default:return callback({ok:true});
              }
            }}
          };
        """)
        page.goto("https://extension.test/popup.html")
        page.wait_for_function("document.querySelector('#product-management-category').value === '7' && !document.querySelector('#product-management-category').disabled")
        yield page
        browser.close()


def test_independent_destination_categories_and_batch_submission(popup, tmp_path):
    assert popup.locator("#product-management-category").input_value() == "7"
    assert popup.locator("#zying-management-category").input_value() == "9"
    popup.screenshot(path=str(tmp_path / "mercado.png"), full_page=True)
    popup.locator("#product-tab").select_option("11")
    popup.locator("#product-batch-start").click()
    popup.wait_for_function("messages.some(m => m.type === 'START_PRODUCT_BATCH')")
    assert popup.evaluate("messages.find(m => m.type === 'START_PRODUCT_BATCH').params.management_category_id") == 7
    assert popup.locator("#product-management-category").is_disabled()


def test_zying_source_filter_and_destination_are_independent(popup, tmp_path):
    popup.locator("#zying-mode").click()
    popup.locator("#zying-refresh").click()
    popup.locator("#zying-category").select_option("123")
    popup.wait_for_function("!document.querySelector('#zying-start').disabled")
    popup.screenshot(path=str(tmp_path / "zying.png"), full_page=True)
    popup.locator("#zying-start").click()
    popup.wait_for_function("messages.some(m => m.type === 'START_ZYING_COLLECTION')")
    params = popup.evaluate("messages.find(m => m.type === 'START_ZYING_COLLECTION').params")
    assert params["category"] == "123"
    assert params["management_category_id"] == 9


def test_current_items_use_independent_destinations(popup):
    popup.locator("#collect").click()
    popup.wait_for_function("messages.some(m => m.type === 'SUBMIT_PRODUCT')")
    assert popup.evaluate("messages.find(m => m.type === 'SUBMIT_PRODUCT').product.management_category_id") == 7
    popup.evaluate("platform = '1688'; messages.length = 0; initialize()")
    popup.wait_for_function("document.querySelector('#collect').textContent.includes('AI原创') && !document.querySelector('#collect').disabled")
    popup.locator("#collect").click()
    popup.wait_for_function("messages.some(m => m.type === 'SUBMIT_PRODUCT')")
    assert popup.evaluate("messages.find(m => m.type === 'SUBMIT_PRODUCT').product.management_category_id") is None


def test_deleted_destination_blocks_zying_then_can_retry_without_category(popup):
    popup.locator("#zying-mode").click()
    popup.locator("#zying-refresh").click()
    popup.wait_for_function("!document.querySelector('#zying-start').disabled")
    popup.evaluate("categories = [{id:7,name:'家居用品'}]")
    popup.locator("#zying-refresh-management-categories").click()
    popup.wait_for_function("document.querySelector('#zying-management-category-status').textContent.includes('不可用')")
    popup.locator("#zying-start").click()
    assert "所选泽顺产品分类不可用" in popup.locator("#result").inner_text()
    assert not popup.evaluate("messages.some(m => m.type === 'START_ZYING_COLLECTION')")
    popup.locator("#zying-management-category").select_option("")
    popup.locator("#zying-start").click()
    popup.wait_for_function("messages.some(m => m.type === 'START_ZYING_COLLECTION')")
    assert popup.evaluate("messages.find(m => m.type === 'START_ZYING_COLLECTION').params.management_category_id") is None


def test_failed_refresh_preserves_selection_and_logout_disables_it(popup):
    popup.evaluate("categoryError = '模拟连接失败'")
    popup.locator("#product-refresh-management-categories").click()
    popup.wait_for_function("document.querySelector('#product-management-category-status').textContent.includes('模拟连接失败')")
    assert popup.locator("#product-management-category").input_value() == "7"
    popup.evaluate("loggedIn = false; refreshState()")
    popup.wait_for_function("document.querySelector('#product-management-category').disabled")
    assert popup.locator("#zying-management-category").is_disabled()
