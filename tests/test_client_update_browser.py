from pathlib import Path
from test_zeshun_popup_browser import browser, EXTENSION


def test_plugin_update_notice(browser):
    page = browser.new_page(viewport={'width': 420, 'height': 950})
    page.route('https://extension.test/**', lambda route: route.fulfill(path=str(EXTENSION / route.request.url.rsplit('/', 1)[-1])))
    page.add_init_script('''
      window.latestVersion = '1.8.71';
      window.chrome = {
        tabs: {query:async () => []},
        runtime: {getManifest:() => ({version:'1.8.9'}), openOptionsPage:() => {}, sendMessage:(message, callback) => {
          if(message.type === 'CHECK_EXTENSION_UPDATE') return callback({ok:true, version:window.latestVersion, downloadUrl:'https://example.test/api/browser-extension/download'});
          callback({ok:true, authenticated:false});
        }}
      };
    ''')
    page.goto('https://extension.test/popup.html')
    page.locator('#update-notice').wait_for(state='visible')
    assert '1.8.9 → v1.8.71' in page.locator('#update-notice').inner_text()
    assert page.locator('#update-download').get_attribute('href').endswith('/api/browser-extension/download')
    folder = Path('artifacts/client-update-notice')
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / 'plugin-update.png'))
    page.evaluate("window.latestVersion = '1.8.8'; checkExtensionUpdate()")
    assert page.locator('#update-notice').is_hidden()
    page.evaluate("window.latestVersion = '1.8.9'; checkExtensionUpdate()")
    assert page.locator('#update-notice').is_hidden()
    page.close()
