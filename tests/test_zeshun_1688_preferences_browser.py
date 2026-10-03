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
    start = source.index('  async function mountCollectionPreferences()')
    end = source.index('  async function collectFromPage(', start)
    page.add_script_tag(content=source[start:end])
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
