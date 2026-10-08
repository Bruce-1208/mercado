"""Bounded ordinary slider input and independent recovery checks, fully offline."""
import os
import threading
from pathlib import Path

import pytest

from erp.ai_weight_price.browser import Browser, CircuitOpen, Stopped
from erp.ai_weight_price.config import validate


@pytest.fixture(scope="module")
def chromium():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        executable = os.environ.get("AWP_TEST_BROWSER_EXECUTABLE") or pw.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("Install a Playwright browser or set AWP_TEST_BROWSER_EXECUTABLE")
        browser = pw.chromium.launch(executable_path=executable, headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(chromium):
    context = chromium.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    page.route("https://www.1688.com/", lambda route: route.fulfill(
        body="<body></body>", content_type="text/html"))
    page.goto("https://www.1688.com/")
    try:
        yield page
    finally:
        context.close()


@pytest.fixture
def adapter(page, monkeypatch):
    monkeypatch.setattr("erp.ai_weight_price.browser.SLIDER_VERIFY_TIMEOUT", .4)
    monkeypatch.setattr(Browser, "_unresolved_slider_attempts", set())
    logs = []
    adapter = Browser(validate({}), threading.Event(), lambda message, *args, **kwargs: logs.append(message))
    adapter.owned.append(page)
    adapter.test_logs = logs
    return adapter


UPLOAD = '<section id="image-search"><button>以图搜货</button><input type="file" hidden></section>'


def slider_html(on_release="", style=""):
    return """<meta charset="utf-8"><style>
      .nc_scale {position:relative;width:300px;height:40px;background:#ddd;}
      .btn_slide {position:absolute;left:0;top:0;width:40px;height:40px;background:#999;}
      .nc-lang-cnt {display:block;margin-left:45px;padding-top:10px;}
      </style><div id="nc_1_wrapper" style="STYLE"><div id="nc_1_scale" class="nc_scale">
      <span id="nc_1_n1z" class="btn_slide"></span><span class="nc-lang-cnt">请按住滑块完成人机验证</span>
      </div></div><script>
      window.dragDowns=0;window.dragUps=0;window.dragMoves=0;
      document.querySelector('.btn_slide').onmousedown=()=>{
        window.dragDowns++;document.onmousemove=()=>window.dragMoves++;
        document.onmouseup=()=>{window.dragUps++;document.onmousemove=null;RELEASE};
      };</script>""".replace("STYLE", style).replace("RELEASE", on_release)


def test_drag_continues_only_after_risk_clears_and_upload_surface_is_readable(page, adapter, monkeypatch):
    page.set_content(slider_html("document.body.innerHTML='" + UPLOAD + "'"))
    focused = []
    monkeypatch.setattr(page, "bring_to_front", lambda: focused.append(True))

    adapter.check(page)

    assert page.evaluate("window.dragDowns") == 1
    assert page.evaluate("window.dragMoves") == 12
    assert page.evaluate("window.dragUps") == 1
    assert page.locator("input[type=file]").count() == 1
    assert page in adapter.owned
    assert not focused
    assert any("已恢复可读取" in message for message in adapter.test_logs)


def test_unchanged_challenge_pauses_keeps_tab_and_never_drags_again(page, adapter):
    page.set_content(slider_html() + UPLOAD)

    with pytest.raises(CircuitOpen, match="验证未解除"):
        adapter.check(page)
    with pytest.raises(CircuitOpen):
        adapter.check(page)

    assert page.evaluate("window.dragDowns") == 1
    assert page.evaluate("window.dragUps") == 1
    assert page not in adapter.owned
    adapter.__exit__()
    assert not page.is_closed()
    assert not any("已恢复可读取" in message for message in adapter.test_logs)


def test_removed_challenge_without_target_surface_is_unconfirmed(page, adapter):
    page.set_content(slider_html("document.body.innerHTML='<main>加载中</main>'"))

    with pytest.raises(CircuitOpen, match="尚不可读取"):
        adapter.check(page)

    assert page.evaluate("window.dragDowns") == 1
    assert page not in adapter.owned
    assert not any("已恢复可读取" in message for message in adapter.test_logs)


def test_success_label_in_retained_widget_does_not_trigger_another_attempt(page, adapter):
    page.set_content(slider_html(
        "document.querySelector('.nc-lang-cnt').textContent='验证通过';"
        "document.body.insertAdjacentHTML('beforeend','" + UPLOAD + "')"))

    adapter.check(page)
    adapter.check(page)

    assert page.evaluate("window.dragDowns") == 1
    assert page.locator("#nc_1_wrapper").is_visible()
    assert page in adapter.owned


def test_a_new_challenge_on_same_page_requires_manual_takeover(page, adapter):
    html = slider_html("document.body.innerHTML='" + UPLOAD + "'")
    page.set_content(html)
    adapter.check(page)
    page.set_content(slider_html())

    with pytest.raises(CircuitOpen):
        adapter.check(page)

    assert page.evaluate("window.dragDowns") == 0
    assert page not in adapter.owned


def test_adapter_recreation_cannot_retry_unresolved_batch_even_on_a_new_page(page, adapter):
    adapter.config["run_id"] = "same-resumable-batch"
    page.set_content(slider_html())
    with pytest.raises(CircuitOpen):
        adapter.check(page)
    assert page.evaluate("window.dragDowns") == 1

    resumed = Browser(adapter.config, threading.Event(), lambda *args: None)
    new_page = page.context.new_page()
    new_page.route("https://www.1688.com/", lambda route: route.fulfill(
        body=slider_html(), content_type="text/html"))
    new_page.goto("https://www.1688.com/")
    resumed.owned.append(new_page)
    with pytest.raises(CircuitOpen):
        resumed.check(new_page)
    assert new_page.evaluate("window.dragDowns") == 0
    assert new_page not in resumed.owned

    # Risk disappearance without a readable surface still cannot clear the
    # retry guard; manual completion plus actual upload readiness can.
    new_page.set_content('<main>加载中</main>')
    with pytest.raises(CircuitOpen, match="尚不可读取"):
        resumed.check(new_page)
    assert resumed._unresolved_slider_attempts
    new_page.set_content(UPLOAD)
    resumed.check(new_page)
    assert not resumed._unresolved_slider_attempts
    new_page.set_content(slider_html("document.body.innerHTML='" + UPLOAD + "'"))
    resumed.check(new_page)
    assert new_page.evaluate("window.dragDowns") == 1


@pytest.mark.parametrize("style", ["display:none", "visibility:hidden", "opacity:0"])
def test_hidden_widgets_are_ignored_without_drag(page, adapter, style):
    page.set_content(slider_html(style=style) + UPLOAD)

    adapter.check(page)

    assert page.evaluate("window.dragDowns") == 0
    assert page in adapter.owned


@pytest.mark.parametrize("unsupported", [
    '<p>请完成安全验证</p><input type="range">',
    '<canvas width="200" height="80"></canvas>',
    '<div class="puzzle" style="width:150px;height:30px">拼图</div>',
])
def test_non_slider_and_puzzle_challenges_pause_without_drag(page, adapter, unsupported):
    html = slider_html().replace('<div id="nc_1_scale"', unsupported + '<div id="nc_1_scale"')
    if "type=\"range\"" in unsupported:
        html = '<p>请完成安全验证</p><input type="range"><script>window.dragDowns=0;</script>'
    page.set_content(html)

    with pytest.raises(CircuitOpen):
        adapter.check(page)

    assert page.evaluate("window.dragDowns") == 0
    assert page not in adapter.owned


def test_multiple_visible_sliders_are_ambiguous_and_never_dragged(page, adapter):
    page.set_content(slider_html() + slider_html().replace("nc_1_", "nc_2_"))

    with pytest.raises(CircuitOpen):
        adapter.check(page)

    assert page.evaluate("window.dragDowns") == 0


def test_visible_iframe_uses_main_page_mouse_coordinates_and_requires_parent_readiness(page, adapter):
    page.set_content('<iframe style="width:400px;height:180px;margin:40px;border:0"></iframe>' + UPLOAD)
    frame = page.frames[1]
    frame.set_content(slider_html("frameElement.style.display='none'"))

    adapter.check(page)

    assert frame.evaluate("window.dragDowns") == 1
    assert frame.evaluate("window.dragUps") == 1
    assert not page.locator("iframe").is_visible()
    assert page in adapter.owned


def test_hidden_iframe_ancestor_never_receives_a_drag(page, adapter):
    page.set_content('<div style="opacity:0"><iframe style="width:400px;height:180px"></iframe></div>' + UPLOAD)
    frame = page.frames[1]
    frame.set_content(slider_html())

    adapter.check(page)

    assert frame.evaluate("window.dragDowns") == 0


@pytest.mark.parametrize("has_prices", [True, False])
def test_detail_requires_complete_official_sku_data_after_challenge_disappears(page, adapter, has_prices):
    release = "document.querySelector('#nc_1_wrapper').remove();"
    if has_prices:
        release += """document.querySelector('#skuSelection').__reactFiber$fixture={memoizedProps:{
          skuItems:[{skuId:11,specAttrs:'小号',price:'2.5'}]},return:null};"""
    detail = """<div id="productTitle" data-module="od_title"><div class="title-content"><h1>测试商品</h1></div></div>
      <div id="shopNavigation"><a class="shop-company-name" href="https://seller1.1688.com">测试商家</a></div>
      <div id="gallery"><img class="preview-img" width="100" height="100" src="https://img.example/main.jpg"></div>
      <div id="skuSelection" data-module="od_sku_selection">SKU</div>"""
    page.route("https://detail.1688.com/offer/1.html", lambda route: route.fulfill(
        body=slider_html(release) + detail, content_type="text/html"))
    page.goto("https://detail.1688.com/offer/1.html")

    if has_prices:
        adapter.check(page)
        assert page in adapter.owned
    else:
        with pytest.raises(CircuitOpen, match="尚不可读取"):
            adapter.check(page)
        assert page not in adapter.owned
    assert page.evaluate("window.dragDowns") == 1


@pytest.mark.parametrize("result", ["cards", "empty", "unrelated"])
def test_image_search_requires_real_results_or_explicit_empty_response(page, adapter, result):
    results = {
        "cards": '<p>找到以下货源</p><article><img width="100" height="100" src="https://img.example/offer.jpg"><b>¥2.50</b></article>',
        "empty": '<p>未找到相关商品</p>',
        "unrelated": '<a href="https://detail.1688.com/offer/2.html">主页推荐</a>',
    }[result]
    page.route("https://www.1688.com/1688-search/pc-image-search/", lambda route: route.fulfill(
        body=slider_html("document.querySelector('#nc_1_wrapper').remove()") + results,
        content_type="text/html"))
    page.goto("https://www.1688.com/1688-search/pc-image-search/")

    if result == "unrelated":
        with pytest.raises(CircuitOpen, match="尚不可读取"):
            adapter.check(page)
        assert page not in adapter.owned
    else:
        adapter.check(page)
        assert page in adapter.owned
    assert page.evaluate("window.dragDowns") == 1


def test_verification_reappearing_between_samples_is_not_success(page, adapter):
    release = """document.querySelector('#nc_1_wrapper').style.display='none';
      setTimeout(()=>document.querySelector('#nc_1_wrapper').style.display='',70);"""
    page.set_content(slider_html(release) + UPLOAD)

    with pytest.raises(CircuitOpen):
        adapter.check(page)

    assert not any("已恢复可读取" in message for message in adapter.test_logs)
    assert page.evaluate("window.dragDowns") == 1


def test_stop_during_drag_releases_mouse_and_retains_recovery_tab(page, adapter, monkeypatch):
    page.set_content(slider_html())
    move = page.mouse.move
    calls = []

    def stop_after_move(x, y, **kwargs):
        calls.append((x, y))
        move(x, y, **kwargs)
        if len(calls) == 3:
            adapter.stop.set()

    monkeypatch.setattr(page.mouse, "move", stop_after_move)

    with pytest.raises(Stopped):
        adapter.check(page)

    assert page.evaluate("window.dragDowns") == 1
    assert page.evaluate("window.dragUps") == 1
    assert len(calls) == 3
    assert page not in adapter.owned
    adapter.__exit__()
    assert not page.is_closed()


def test_stop_during_confirmation_pauses_without_waiting_for_timeout(page, adapter, monkeypatch):
    page.set_content(slider_html("document.querySelector('#nc_1_wrapper').remove()") + UPLOAD)

    def stop_while_confirming(_):
        adapter.stop.set()
        return False

    monkeypatch.setattr(adapter, "_supplier_page_readable", stop_while_confirming)
    with pytest.raises(Stopped):
        adapter.check(page)

    assert page.evaluate("window.dragUps") == 1
    assert page not in adapter.owned
    assert not any("已恢复可读取" in message for message in adapter.test_logs)


def test_partial_mouse_input_error_releases_button_and_pauses(page, adapter, monkeypatch):
    page.set_content(slider_html())
    down = page.mouse.down

    def down_then_error(**kwargs):
        down(**kwargs)
        raise RuntimeError("browser input became unavailable")

    monkeypatch.setattr(page.mouse, "down", down_then_error)
    with pytest.raises(CircuitOpen, match="无法确认"):
        adapter.check(page)

    assert page.evaluate("window.dragDowns") == 1
    assert page.evaluate("window.dragUps") == 1
    assert page not in adapter.owned
    assert adapter._unresolved_slider_attempts


def test_manual_recovery_read_error_keeps_circuit_open_instead_of_skipping(page, adapter, monkeypatch):
    adapter.config["run_id"] = "unconfirmed-recovery"
    page.set_content(slider_html())
    with pytest.raises(CircuitOpen):
        adapter.check(page)
    page.set_content(UPLOAD)
    resumed = Browser(adapter.config, threading.Event(), lambda *args: None)
    resumed.owned.append(page)

    def unavailable(_):
        raise RuntimeError("Execution context was destroyed")

    monkeypatch.setattr(resumed, "_supplier_page_readable", unavailable)
    with pytest.raises(CircuitOpen, match="尚无法确认"):
        resumed.check(page)

    assert page not in resumed.owned
    assert not page.is_closed()
    assert resumed._unresolved_slider_attempts


def test_stop_before_inspection_does_not_attempt_input(page, adapter):
    page.set_content(slider_html())
    adapter.stop.set()

    with pytest.raises(Stopped):
        adapter.check(page)

    assert page.evaluate("window.dragDowns") == 0
