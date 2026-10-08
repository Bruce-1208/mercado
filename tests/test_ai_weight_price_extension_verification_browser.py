"""Offline DOM checks for the extension's verification observer."""
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
def page(browser):
    context = browser.new_context()
    context.route('**/*', lambda route: route.abort())
    page = context.new_page()
    page.add_script_tag(path=str(EXTENSION / '1688-verification.js'))
    yield page
    context.close()


SLIDER = '''<div class="nc-container"><div class="nc_scale" style="width:300px;height:40px;background:#ddd">
  <span class="btn_slide" style="display:block;width:40px;height:40px">&gt;</span>
  <span>请按住滑块完成滑动验证</span></div></div>'''


def test_visible_simple_slider_has_viewport_coordinates(page):
    page.set_content(SLIDER)
    snapshot = page.evaluate('zeshun1688VerificationSnapshot()')
    assert snapshot['blocked'] is True
    assert snapshot['slider']['endX'] > snapshot['slider']['startX']
    assert snapshot['slider']['y'] > 0


def test_hidden_frame_challenge_and_product_slider_text_are_ignored(page):
    page.set_content('<h1>工业滑块组件</h1><div style="display:none">' + SLIDER + '</div>'
                     '<iframe style="display:none" srcdoc="请按住滑块完成验证"></iframe>')
    assert page.evaluate('zeshun1688VerificationSnapshot().blocked') is False


def test_verification_disappearance_and_success_widget_are_distinct_from_data_approval(page):
    page.set_content(SLIDER)
    page.locator('.nc_scale').evaluate('node => {node.innerHTML = "验证通过";}')
    snapshot = page.evaluate('zeshun1688VerificationSnapshot()')
    assert snapshot['blocked'] is False
    assert snapshot['slider'] is None
    # There is deliberately no approved/verified field: actual target readback is separate.
    assert 'verified' not in snapshot


def test_multiple_or_puzzle_controls_do_not_produce_a_drag_target(page):
    page.set_content(SLIDER + SLIDER)
    assert page.evaluate('zeshun1688VerificationSnapshot().slider') is None
    page.set_content(SLIDER.replace('<span class="btn_slide"', '<canvas></canvas><span class="btn_slide"'))
    assert page.evaluate('zeshun1688VerificationSnapshot().blocked') is True
    assert page.evaluate('zeshun1688VerificationSnapshot().slider') is None


def test_same_origin_frame_slider_coordinates_include_embedding_offset(page):
    page.set_content('<iframe style="width:400px;height:160px;margin-left:100px" srcdoc=""></iframe>')
    frame = page.frames[1]
    frame.set_content(SLIDER)
    snapshot = page.evaluate('zeshun1688VerificationSnapshot()')
    assert snapshot['blocked'] is True
    assert snapshot['slider']['startX'] > 100


def test_cancelled_search_cannot_be_revived_by_a_new_search(page):
    page.set_content('<div id="fixture"></div>')
    page.evaluate('''() => {
      window.chrome = {runtime: {onMessage: {addListener: fn => window.receiver = fn},
        sendMessage: async () => ({ok: true})}};
    }''')
    page.add_script_tag(path=str(EXTENSION / 'content-1688.js'))
    result = page.evaluate('''async () => {
      const message = {type: 'AI_WEIGHT_PRICE_SEARCH', data_url:'data:image/png;base64,dGVzdA==', timeout_ms:3000};
      const old = new Promise(resolve => receiver(message, null, resolve));
      await new Promise(resolve => setTimeout(resolve, 150));
      receiver({type:'AI_WEIGHT_PRICE_CANCEL_SUPPLIER_ACTION'}, null, () => {});
      document.querySelector('#fixture').innerHTML = `<section id="image-search" style="width:500px;height:180px">
        <input type="file" accept="image/*" hidden><button id="image-submit">搜索图片</button></section>
        <div id="search-result" class="search-result"></div>`;
      window.uploads = 0;
      document.querySelector('input').onchange = () => uploads++;
      document.querySelector('#image-submit').onclick = () => {
        document.querySelector('#search-result').innerHTML = '<a href="https://detail.1688.com/offer/910001.html"><img width="100" height="100" src="https://img.test/1.jpg"></a>';
      };
      const fresh = new Promise(resolve => receiver(message, null, resolve));
      return {old: await old, fresh: await fresh, uploads};
    }''')
    assert result['old']['ok'] is False
    assert '暂停' in result['old']['error']
    assert result['fresh']['ok'] is True
    assert result['uploads'] == 1
