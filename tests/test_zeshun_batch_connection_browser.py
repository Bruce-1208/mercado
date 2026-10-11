"""Collector recovery in Chromium with local page/data fixtures only."""
from test_zeshun_data_card_browser import EXTENSION, browser, mercado_page


def test_reinjection_installs_one_listener_and_keeps_list_readable(mercado_page):
    page = mercado_page(content=False)
    page.evaluate("""() => {
      window.listeners = [];
      chrome.runtime.onMessage.addListener = listener => listeners.push(listener);
    }""")
    for _ in range(2):
        for filename in ["collector-core.js", "mercado-data.js", "content.js"]:
            page.add_script_tag(path=str(EXTENSION / filename))
    assert page.evaluate("listeners.length") == 1
    result = page.evaluate("""() => new Promise(resolve =>
      listeners[0]({type:'READ_PRODUCT_LIST'}, {}, resolve))""")
    assert result["ok"], result
    assert len(result["items"]) > 0
    assert page.locator(".zeshun-card-collect").count() == 1


def test_popup_shows_each_lane_stage_and_elapsed_time(browser):
    page = browser.new_page()
    try:
        page.set_content(''.join(f'<div id="product-batch-{name}"></div>'
                                for name in ["status", "summary", "current", "progress"]))
        source = (EXTENSION / "popup.js").read_text()
        render = source[source.index("function renderProductBatch("):
                        source.index("async function checkProductDataStatus(")]
        page.add_script_tag(content="""
          let productBatchState;
          const productBatchFields = {concurrency:{value:'3'}};
          function syncProductBatchControls() {}
        """ + render)
        page.evaluate("""() => renderProductBatch({running:true, active_items:[
          {lane:1,stage:'读取详情',started_at:Date.now()-12000,
            url:'https://www.mercadolibre.com.mx/item/up/MLMU5473360186'},
          {lane:2,stage:'上传商品',started_at:Date.now()-3000,
            url:'https://articulo.mercadolibre.com.mx/MLM-123456'}]})""")
        summary = page.locator("#product-batch-summary").inner_text()
        assert "通道 1 · 读取详情 · 12 秒 · MLMU5473360186" in summary
        assert "通道 2 · 上传商品 · 3 秒 · MLM-123456" in summary
    finally:
        page.close()
