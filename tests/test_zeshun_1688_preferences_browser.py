"""Offline DOM checks for the 1688 page preferences panel."""
from pathlib import Path
from test_1688_product_management_browser import products_browser


def test_page_reminder_category_restore_save_and_clear(products_browser):
    page = products_browser.new_page()
    page.set_content('<html><body>1688</body></html>')
    page.evaluate("""() => {
      window.loggedIn = false;
      window.saved = {product: 3, '1688': 7};
      window.chrome = {
        runtime: {sendMessage: async message => message.type === 'GET_STATE'
          ? {authenticated: loggedIn}
          : {ok: true, categories: [{id: 7, name: '家居'}, {id: 8, name: '玩具'}]}},
        storage: {local: {
          get: async () => ({zeshunCollectionCategoryOptions: saved}),
          set: async values => {saved = values.zeshunCollectionCategoryOptions;}
        }}
      };
    }""")
    source = Path('browser_extension/zeshun_collector/content-1688.js').read_text()
    start = source.index('  async function sendRuntimeMessage(')
    end = source.index('  async function collectFromPage(', start)
    page.add_script_tag(content='let preferencesFocusListener = null;\n' + source[start:end])
    page.evaluate('mountCollectionPreferences()')
    panel = page.locator('.zeshun-1688-preferences')
    assert '未登录' in panel.inner_text()
    assert panel.locator('select').is_disabled()
    page.evaluate('loggedIn = true')
    panel.locator('button').click()
    page.wait_for_function("document.querySelector('.zeshun-1688-preferences select').value === '7'")
    panel.locator('select').select_option('8')
    assert page.evaluate("saved['1688']") == 8
    assert page.evaluate('saved.product') == 3
    panel.locator('select').select_option('')
    assert page.evaluate("saved['1688']") is None
    page.close()


def test_search_cards_collect_once_without_navigation(products_browser):
    page = products_browser.new_page()
    url = 'https://s.1688.com/selloffer/offer_search.htm?keywords=test'
    page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
      <div class="search-offer-wrapper"><a href="https://detail.1688.com/offer/123456.html"><img><span class="title-text">旧版商品</span></a></div>
      <div class="offer-list-row-offer"><a href="https://detail.1688.com/offer/234567.html"><img></a></div>
      <div class="newCard"><a href="https://detail.1688.com/offer/345678.html"><img alt="新版商品"></a></div>
      <div data-offer-id="456789"><img></div>
      <a href="https://detail.1688.com/offer/567890.html"><img></a>
      <a href="https://shop.1688.com">店铺链接</a>
    '''))
    page.goto(url)
    page.evaluate('''() => {
      window.calls = [];
      window.chrome = {
        runtime: {onMessage: {addListener() {}}, sendMessage: async message => {
          calls.push(message);
          if (message.type === 'GET_STATE') return {authenticated:false};
          return {ok:true};
        }},
        storage: {local: {get:async () => ({})}}
      };
    }''')
    page.add_script_tag(content=Path('browser_extension/zeshun_collector/content-1688.js').read_text())
    buttons = page.locator('.zeshun-card-collect')
    assert buttons.count() == 5
    buttons.nth(1).click()
    page.wait_for_function("calls.some(m => m.type === 'COLLECT_1688_URL')")
    calls = page.evaluate("calls.filter(m => m.type === 'COLLECT_1688_URL')")
    assert len(calls) == 1
    assert calls[0]['product']['source_item_id'] == '234567'
    assert page.url == url
    page.evaluate('''() => {
      const card = document.createElement('div');
      card.className = 'offer-item';
      card.innerHTML = '<a href="https://detail.1688.com/offer/678901.html"><img></a>';
      document.body.appendChild(card);
    }''')
    page.wait_for_function("document.querySelectorAll('.zeshun-card-collect').length === 6")
    assert page.locator('.search-offer-wrapper .zeshun-card-collect').count() == 1
    page.close()


def test_collection_preserves_title_destination_over_other_links(products_browser):
    page = products_browser.new_page()
    page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
      <div class="search-offer-wrapper" data-offer-id="999999" data-renderkey="offer_888888">
        <a href="https://shop.1688.com">店铺</a>
        <a href="https://detail.1688.com/offer/777777.html"><img></a>
        <a href="//detail.1688.com/offer/123456.html?sk=keep-me&amp;from=search"><span class="title-text">可打开的标题</span></a>
      </div>
    '''))
    page.goto('https://s.1688.com/selloffer/offer_search.htm')
    page.evaluate('''() => {
      window.calls = [];
      window.chrome = {
        runtime: {onMessage: {addListener() {}}, sendMessage: async message => {
          calls.push(message);
          return message.type === 'GET_STATE' ? {authenticated:false} : {ok:true};
        }}, storage: {local: {get:async () => ({})}}
      };
    }''')
    page.add_script_tag(content=Path('browser_extension/zeshun_collector/content-1688.js').read_text())
    page.locator('.zeshun-card-collect').click()
    page.wait_for_function("calls.some(m => m.type === 'COLLECT_1688_URL')")
    product = page.evaluate("calls.find(m => m.type === 'COLLECT_1688_URL').product")
    assert product['source_item_id'] == '123456'
    assert product['source_url'] == 'https://detail.1688.com/offer/123456.html?sk=keep-me&from=search'
    assert product['final_url'] == product['source_url']
    assert page.url == 'https://s.1688.com/selloffer/offer_search.htm'
    page.close()
