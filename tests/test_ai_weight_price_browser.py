"""Offline browser regressions; only synthetic HTML and Flask's test client are used."""
import base64
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from flask import Flask

from erp.ai_weight_price.browser import Browser, CircuitOpen, NoExactMatch
from erp.ai_weight_price.config import validate
from erp.ai_weight_price.service import Service
from erp.ai_weight_price.store import Store
from erp.ai_weight_price.web import create_blueprint
from erp.ai_weight_price.supplier_adapter import DOM_SNAPSHOT, SupplierAdaptationError, verified_selectors
from erp import mercadolibre_playwright_collector


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
    # No external requests are allowed, even if a fixture accidentally references one.
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


def test_zying_origin_scripts_ignore_us_icon_outside_plugin_shadow_root(page):
    page.set_content('<img src="/assets/US.svg"><div id="zying-host"></div>')
    page.evaluate('''() => {
      const root = document.querySelector('#zying-host').attachShadow({mode: 'open'});
      root.innerHTML = '<div class="zying-meli-detail-metric-line">重量：509g</div><img src="/assets/CN.svg">';
      root.querySelector('.zying-meli-detail-metric-line').__reactFiber$fixture = {
        memoizedProps: {data: {weight: 509}}, return: null
      };
    }''')

    lines = page.evaluate(mercadolibre_playwright_collector.SHADOW_PLUGIN_TEXT_SCRIPT)
    payload = page.evaluate(mercadolibre_playwright_collector.PLUGIN_REACT_METRICS_SCRIPT)

    assert '__ZYING_SELF_SHIP_ORIGIN__:US' not in lines
    assert '__ZYING_SELF_SHIP_ORIGIN__:CN' in lines
    assert payload['data']['self_ship_origin'] == 'CN'
    assert payload['data']['weight_g'] == 509


SUPPLIER_HTML = '''<meta charset="utf-8"><main><h1>不锈钢杯</h1><img src="https://img.example/main.jpg" width="120" height="120">
<div data-member-id="seller1">杯具工厂</div><section>
<article data-sku-id="blue"><span>蓝色500ml一只</span><b>￥12.50</b><p>含包装450克</p></article>
<article data-sku-id="red"><span>红色500ml一只</span><b>￥15.50</b><p>含包装460克</p></article>
</section></main>'''


def test_image_payload_retries_temporary_cdn_http_error(monkeypatch):
    """A transient Alibaba CDN 420 must not immediately skip the ERP item."""
    from erp.ai_weight_price.browser import Browser

    picture = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    class Stop:
        def __init__(self):
            self.delays = []

        def is_set(self):
            return False

        def wait(self, delay):
            self.delays.append(delay)
            return False

    class Response:
        def __init__(self, status_code, body=b""):
            self.status_code = status_code
            self.body = body
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(
                    f"{self.status_code} Client Error", response=self
                )

        def iter_content(self, chunk_size):
            return [self.body]

    responses = iter([Response(420), Response(200, picture)])
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr("erp.ai_weight_price.browser.requests.get", get)
    stop = Stop()
    adapter = Browser(validate({}), stop, lambda *args: None)
    payload = adapter.image_payload({
        "erp_goods_id": "cdn-retry",
        "main_image_url": "https://cbu01.alicdn.com/img/retry.jpg",
    })

    assert payload["mimeType"] == "image/png"
    assert payload["buffer"] == picture
    assert len(calls) == 2
    assert stop.delays == [0.5]
    assert calls[0][1]["headers"]["Referer"] == "https://www.1688.com/"


def test_empty_image_search_keeps_browser_steps_in_background_without_screenshots_or_dashboard(page, monkeypatch, tmp_path):
    import base64
    picture = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=")
    page.route("https://www.1688.com/", lambda route: route.fulfill(body='''<meta charset="utf-8"><body>
        <h1>以图搜货（离线测试页面）</h1><input type="file" hidden><div id="result">等待上传主图</div>
        <script>document.querySelector('input').onchange=()=>document.getElementById('result').textContent='未找到相关商品';</script></body>''', content_type="text/html"))
    page.goto("https://www.1688.com/")
    service = Service(tmp_path)
    service.store.add({"erp_goods_id": "visual-1", "title": "离线测试商品 · 蓝色水杯", "main_image_url": "https://img.example/main.png"})
    service.store.set_state("run", {"run_id": "visual-test", "mode": "pipeline", "outcome": "completed"})
    adapter = Browser(validate({}), threading.Event(), service.store.log)
    adapter.context = page.context
    adapter.record_visual = service.record_visual
    monkeypatch.setattr(adapter, "image_payload", lambda task: {"name": "product-main.png", "mimeType": "image/png", "buffer": picture})
    focused = []
    original_focus = page.bring_to_front
    monkeypatch.setattr(page, "bring_to_front", lambda: (focused.append(page.url), original_focus())[1])
    with pytest.raises(NoExactMatch, match="空结果"):
        adapter.image_search(page, service.store.get("visual-1"))
    events = service.store.get("visual-1")["visual_history"]
    assert [e["step"] for e in events] == ["supplier_home", "uploading", "uploaded", "search_empty"]
    assert focused == []
    assert all(not e.get("screenshot_url") for e in events)
    assert page.locator('input').evaluate('e=>e.files[0].name') == 'product-main.png'
    service.store.skip("visual-1", "1688返回空搜索结果")
    service.record_visual("visual-1", "skipped", "未完全匹配，已记录并跳过，继续下一件")
    service.circuit(CircuitOpen("1688需要登录"))
    app = Flask(__name__); app.register_blueprint(create_blueprint(service)); client = app.test_client()
    assert client.get('/api/ai-weight-price/visuals/config.json').status_code == 404
    def respond(route):
        url = urlsplit(route.request.url)
        response = client.get(url.path + ('?' + url.query if url.query else ''), base_url='http://127.0.0.1:5018')
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
    page.route('http://127.0.0.1:5018/**', respond)
    page.route('https://img.example/main.png', lambda route: route.fulfill(body=picture, content_type='image/png'))
    page.set_viewport_size({"width": 1300, "height": 1080})
    page.goto('http://127.0.0.1:5018/ai-weight-price')
    expect = pytest.importorskip('playwright.sync_api').expect
    expect(page.locator('#n-skipped')).to_have_text('1')
    expect(page.locator('#visual-progress')).to_have_count(0)
    expect(page.get_by_text('主图搜货 · 可视化过程')).to_have_count(0)
    expect(page.locator('#circuit')).to_be_visible()
    expect(page.locator('#resume-switch')).to_be_visible()


def test_1688_login_and_human_review_are_resumable_pauses_and_request_attention(page, monkeypatch):
    adapter = Browser(validate({}), threading.Event(), lambda *args: None)
    adapter.owned.append(page)
    focused = []
    original_focus = page.bring_to_front
    monkeypatch.setattr(page, "bring_to_front", lambda: (focused.append(page.url), original_focus())[1])
    page.route('https://login.taobao.com/**', lambda route: route.fulfill(
        body='<body>1688 登录</body>', content_type='text/html'))
    page.goto('https://login.taobao.com/member/login.jhtml')
    with pytest.raises(CircuitOpen, match='1688需要登录'):
        adapter.check(page)
    assert page not in adapter.owned
    assert not page.is_closed()
    assert focused == ['https://login.taobao.com/member/login.jhtml']
    adapter.owned.append(page)
    page.route('https://www.1688.com/**', lambda route: route.fulfill(
        body='<meta charset="utf-8"><body>请按住滑块完成人机验证</body>', content_type='text/html'))
    page.goto('https://www.1688.com/')
    with pytest.raises(CircuitOpen, match='人机审核'):
        adapter.check(page)
    assert page not in adapter.owned
    assert not page.is_closed()
    assert focused[-1] == 'https://www.1688.com/'
    assert len(focused) == 2


def test_image_search_foreign_redirect_without_review_evidence_is_not_a_manual_pause(page, monkeypatch):
    import base64
    page.route('https://www.1688.com/', lambda route: route.fulfill(body='''<body>
      <input type="file" hidden><script>document.querySelector('input').onchange=()=>
      window.open('https://redirect.example/transient');</script></body>''', content_type='text/html'))
    page.context.route('https://redirect.example/**', lambda route: route.fulfill(
        body='<body>临时跳转页面</body>', content_type='text/html'))
    page.goto('https://www.1688.com/')
    adapter = Browser(validate({}), threading.Event(), lambda *args: None)
    adapter.context = page.context
    monkeypatch.setattr(adapter, 'image_payload', lambda task: {
        'name': 'main.png', 'mimeType': 'image/png',
        'buffer': base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=')})
    with pytest.raises(NoExactMatch, match='搜索超时') as error:
        adapter.image_search(page, {'erp_goods_id': '1'}, timeout=.2)
    assert not isinstance(error.value, CircuitOpen)


def observed_supplier_answer(snapshot):
    nodes = snapshot["nodes"]
    node = lambda predicate: next(n["n"] for n in nodes if predicate(n))
    return {"certain": True, "title": node(lambda n: n["text"] == "不锈钢杯"),
            "image": node(lambda n: n["tag"] == "img"),
            "merchant": {"node": node(lambda n: "data-member-id" in n["attrs"]), "attribute": "data-member-id"},
            "sku_attribute": "data-sku-id", "skus": [
                {"row": node(lambda n: n["attrs"].get("data-sku-id") == "blue"),
                 "label": node(lambda n: n["text"] == "蓝色500ml一只")},
                {"row": node(lambda n: n["attrs"].get("data-sku-id") == "red"),
                 "label": node(lambda n: n["text"] == "红色500ml一只")} ]}


def test_supplier_auto_adaptation_uses_real_nodes_and_records_evidence(page, tmp_path):
    page.route("https://detail.1688.com/offer/1.html", lambda route: route.fulfill(body=SUPPLIER_HTML, content_type="text/html"))
    page.goto("https://detail.1688.com/offer/1.html")
    service = Service(tmp_path)
    service.store.add({"erp_goods_id": "g1", "title": "杯"})
    config = validate({})
    adapter = Browser(config, threading.Event(), service.store.log)
    adapter.adapt_supplier = observed_supplier_answer
    adapter.record_supplier_adaptation = service.record_supplier_adaptation
    adapter.ensure_supplier_detail(page, "g1")
    assert adapter.value(page, "supplier_title", required=True) == "不锈钢杯"
    rows = page.locator(adapter.s["sku_rows"])
    assert rows.count() == 2
    assert [adapter.value(row, "sku_label", required=True) for row in rows.all()] == ["蓝色500ml一只", "红色500ml一只"]
    event = Store(tmp_path).get("g1")["supplier_adaptations"][-1]
    assert event["ok"] and event["evidence"]["merchant_id"] == "seller1"
    assert [sku["id"] for sku in event["evidence"]["skus"]] == ["blue", "red"]
    assert any("自动适配通过" in log["message"] for log in service.store.logs())
    assert config["selectors"]["supplier_title"] == ""  # no positional paths saved globally
    # A second page must be observed again, even if it happens to have the same layout.
    adapter.adapt_supplier = lambda _: {"certain": False}
    with pytest.raises(SupplierAdaptationError, match="无法确认"):
        adapter.ensure_supplier_detail(page, "g1")
    assert len(service.store.get("g1")["supplier_adaptations"]) == 2
    assert service.store.state("supplier_adaptation")["ok"] is False
    assert adapter.s["supplier_title"] == ""


@pytest.mark.parametrize("problem", ["unknown_node", "outside_label", "duplicate_sku", "invented_attribute", "uncertain", "changed_page", "hidden_node"])
def test_supplier_auto_adaptation_rejects_unverifiable_results(page, problem):
    page.set_content(SUPPLIER_HTML)
    snapshot = page.evaluate(DOM_SNAPSHOT)
    answer = observed_supplier_answer(snapshot)
    if problem == "unknown_node":
        answer["title"] = 99999
    elif problem == "outside_label":
        answer["skus"][0]["label"] = answer["title"]
    elif problem == "duplicate_sku":
        answer["skus"][1] = answer["skus"][0]
    elif problem == "invented_attribute":
        answer["merchant"]["attribute"] = "data-secret-token"
    elif problem == "uncertain":
        answer["certain"] = False
    elif problem == "changed_page":
        page.locator("h1").evaluate("e=>e.textContent='另一商品'")
    elif problem == "hidden_node":
        page.locator("h1").evaluate("e=>e.hidden=true")
    with pytest.raises(SupplierAdaptationError):
        verified_selectors(page, snapshot, answer)


def test_supplier_snapshot_excludes_credentials_hidden_text_and_scripts(page):
    page.set_content(SUPPLIER_HTML + '''<input value="private-input"><textarea>private-textarea</textarea>
        <header>private-header</header><div hidden>private-hidden</div>
        <script>window.privateToken='private-script'</script><div data-token="private-data">普通说明</div>''')
    import json
    serialized = json.dumps(page.evaluate(DOM_SNAPSHOT), ensure_ascii=False)
    assert "普通说明" in serialized
    assert "private-" not in serialized


def test_risk_check_ignores_only_child_frame_detached_during_inspection(monkeypatch):
    from types import SimpleNamespace
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    main = SimpleNamespace(is_detached=lambda: False)
    detached = [False]
    child = SimpleNamespace(is_detached=lambda: detached[0])
    page = SimpleNamespace(frames=[main, child], main_frame=main)
    def check(frame):
        if frame is child:
            detached[0] = True
            raise RuntimeError('Frame was detached')
    monkeypatch.setattr(adapter, 'check_frame', check)
    adapter.check(page)
    monkeypatch.setattr(adapter, 'check_frame', lambda frame: (_ for _ in ()).throw(RuntimeError('main failed')))
    with pytest.raises(RuntimeError, match='main failed'):
        adapter.check(page)


@pytest.mark.parametrize("label", ["搜索", "搜 索", "搜\u00a0索", '<span role="img" aria-label="search"></span><span>搜 索</span>'])
def test_category_search_handles_ant_spacing_icons_and_nearby_filter(page, label):
    page.set_content(f'<button>搜索</button><section><div id="category"></div><button id="wanted">{label}</button></section>')
    adapter = Browser(validate({}), threading.Event(), lambda *args, **kwargs: None)
    assert adapter.category_search(page, page.locator("#category"), timeout=0).get_attribute("id") == "wanted"


def test_category_search_ignores_hidden_buttons_and_rejects_ambiguity(page):
    logs = []
    adapter = Browser(validate({}), threading.Event(), lambda message, **kwargs: logs.append(message))
    page.set_content('<section><div id="category"></div><button hidden>搜索</button><button id="wanted">搜索</button></section>')
    assert adapter.category_search(page, page.locator("#category"), timeout=0).get_attribute("id") == "wanted"
    page.set_content('<section><div id="category"></div><button>搜索</button><button>搜 索</button></section>')
    with pytest.raises(ValueError, match="可见匹配 2"):
        adapter.category_search(page, page.locator("#category"), timeout=0)
    assert '"visible_matches": 2' in logs[-1]


def test_category_search_accepts_explicit_selector_and_waits_until_enabled(page):
    page.set_content('<div id="category"></div><button id="query" disabled>查询商品</button>')
    adapter = Browser(validate({"selectors": {"erp_search": "#query"}}), threading.Event(), lambda *args, **kwargs: None)
    page.evaluate("setTimeout(()=>document.getElementById('query').disabled=false, 100)")
    assert adapter.category_search(page, page.locator("#category")).get_attribute("id") == "query"


def test_current_page_accepts_missing_pagination_only_for_an_unambiguous_single_page(page):
    adapter = Browser(validate({}), threading.Event(), lambda *args, **kwargs: None)
    page.set_content('<main>唯一一页商品</main>')
    assert adapter.current_page(page) == 1
    page.set_content('''<ul class="ant-pagination"><li class="ant-pagination-item">1</li>
        <li class="ant-pagination-item">2</li><li class="ant-pagination-next"><button>下一页</button></li></ul>''')
    with pytest.raises(ValueError, match="无法唯一确认"):
        adapter.current_page(page)


def test_next_page_retries_one_dropped_click_without_failing_the_run(page):
    logs = []
    adapter = Browser(
        validate({}), threading.Event(),
        lambda message, *args, **kwargs: logs.append((message, kwargs.get("level"))),
    )
    page.set_content('''
      <ul class="ant-pagination">
        <li id="active" class="ant-pagination-item ant-pagination-item-active">1</li>
        <li class="ant-pagination-next"><button id="next">next</button></li>
      </ul>
      <script>
        let clicks=0;
        next.onclick=()=>{ clicks+=1; if(clicks===2) active.textContent='2'; };
      </script>
    ''')

    assert adapter.next_page(page, 1, timeout=0) is True
    assert page.evaluate("clicks") == 2
    assert any("正在重试" in message for message, _level in logs)


def test_product_rows_wait_for_delayed_react_refresh(page):
    logs = []
    adapter = Browser(
        validate({}), threading.Event(),
        lambda message, *args, **kwargs: logs.append((message, kwargs.get("level"))),
    )
    page.set_content('''
      <div class="product-item">old product</div>
      <script>setTimeout(()=>document.querySelector('.product-item').textContent='new product', 80)</script>
    ''')
    old = adapter._rows_fingerprint(["old product"])

    rows, fingerprint = adapter.wait_for_product_rows(page, 2, {old}, timeout=1)

    assert rows.all_inner_texts() == ["new product"]
    assert fingerprint != old
    assert any("延迟刷新" in message for message, _level in logs)


def test_product_rows_timeout_pauses_instead_of_failing_run(page):
    adapter = Browser(validate({}), threading.Event(), lambda *args, **kwargs: None)
    page.set_content('<div class="product-item">unchanged product</div>')
    old = adapter._rows_fingerprint(["unchanged product"])

    with pytest.raises(CircuitOpen, match="已暂停任务并保留进度"):
        adapter.wait_for_product_rows(page, 2, {old}, timeout=0)


@pytest.mark.parametrize("duplicate_hidden", [False, True])
def test_image_search_uploads_task_image_and_waits_for_new_results(page, monkeypatch, duplicate_hidden):
    import base64
    page.route("https://www.1688.com/", lambda route: route.fulfill(body='''<body>
        <input type="file" hidden id="picture"><a href="https://detail.1688.com/offer/old.html">旧推荐</a>
        <script>document.getElementById('picture').onchange=()=>setTimeout(()=>{
          document.querySelector('a').href='https://detail.1688.com/offer/new.html';},150);</script></body>''', content_type="text/html"))
    page.goto("https://www.1688.com/")
    if duplicate_hidden:
        page.evaluate("document.body.insertAdjacentHTML('afterbegin', '<div hidden><input type=file id=inactive-upload></div>')")
    adapter = Browser(validate({}), threading.Event(), lambda *args, **kwargs: None)
    adapter.context = page.context
    received = []
    def payload(task):
        received.append(task["main_image_url"])
        return {"name": "main.png", "mimeType": "image/png", "buffer": base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=")}
    monkeypatch.setattr(adapter, "image_payload", payload)
    result = adapter.image_search(page, {"erp_goods_id": "g1", "main_image_url": "https://img.example/actual.png"})
    assert received == ["https://img.example/actual.png"]
    assert result.locator("a").get_attribute("href").endswith("new.html")
    assert page.locator("#picture").evaluate("el => el.files[0].name") == "main.png"
    if duplicate_hidden:
        assert page.locator("#inactive-upload").evaluate("el => el.files.length") == 0


def test_image_search_keeps_verified_upload_when_page_adds_another_input(page, monkeypatch):
    import base64
    page.route("https://www.1688.com/", lambda route: route.fulfill(body='''<input type="file" hidden id="picture"><div id="result"></div>
      <script>picture.onchange=()=>result.innerHTML='<a href="https://detail.1688.com/offer/55.html">结果</a>'</script>''', content_type="text/html"))
    page.goto("https://www.1688.com/")
    adapter = Browser(validate({}), threading.Event(), lambda *args, **kwargs: None)
    adapter.context = page.context
    monkeypatch.setattr(adapter, "image_payload", lambda task: {
        "name": "main.png", "mimeType": "image/png",
        "buffer": base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=")})
    original_visual = adapter.visual
    def visual(task, step, message, target=None):
        if step == "uploading":
            page.evaluate("document.body.insertAdjacentHTML('beforeend','<input type=file id=late-upload>')")
        original_visual(task, step, message, target)
    monkeypatch.setattr(adapter, "visual", visual)

    result = adapter.image_search(page, {"erp_goods_id": "g1"})

    assert result is page
    assert page.locator("#picture").evaluate("element => element.files.length") == 1
    assert page.locator("#late-upload").evaluate("element => element.files.length") == 0


def test_image_result_page_waits_for_real_cards_instead_of_early_offer_links(page, monkeypatch):
    import base64
    page.route('https://www.1688.com/', lambda route: route.fulfill(
        body='''<input type="file" hidden><script>
        document.querySelector('input').onchange=()=>location.href='https://air.1688.com/kapp/1688-search/pc-image-search/?imageId=test';
        </script>''', content_type='text/html'))
    page.route('https://air.1688.com/**', lambda route: route.fulfill(
        body='''<meta charset="utf-8"><a href="https://detail.1688.com/offer/early.html">页面壳推荐</a>
        <script>setTimeout(()=>document.body.insertAdjacentHTML('beforeend',
        '<div>找到以下货源</div><article><img width="120" height="120" src="https://img.example/real.jpg"><p>真实货源 ￥22</p></article>'),120)</script>''',
        content_type='text/html'))
    page.goto('https://www.1688.com/')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    adapter.context = page.context
    monkeypatch.setattr(adapter, 'image_payload', lambda task: {
        'name': 'main.png', 'mimeType': 'image/png',
        'buffer': base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=')})

    result = adapter.image_search(page, {'erp_goods_id': '1'})

    assert '/pc-image-search/' in result.url
    assert [card['main_image_url'] for card in adapter.result_image_cards(result)] == [
        'https://img.example/real.jpg',
    ]


def test_image_search_rejects_ambiguous_uploads_before_upload(page, monkeypatch):
    page.set_content('<input type="file" hidden><input type="file" hidden>')
    adapter = Browser(validate({}), threading.Event(), lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="实际 2 个"):
        adapter.image_search(page, {"erp_goods_id": "g1"})


def test_image_search_clicks_uploaded_preview_submit_and_reads_only_first_n_images(page, monkeypatch):
    import base64
    page.route('https://www.1688.com/', lambda route: route.fulfill(body='''<meta charset="utf-8"><body>
      <input type="file" hidden><button hidden id="submit">搜索图片</button><div id="results"></div>
      <script>document.querySelector('input').onchange=()=>setTimeout(()=>document.querySelector('#submit').hidden=false,50);
      document.querySelector('#submit').onclick=()=>{window.submitted=true;document.querySelector('#submit').hidden=true;
      document.querySelector('#results').innerHTML=Array.from({length:4},(_,i)=>`<a href="https://detail.1688.com/offer/${i+1}.html"><img width="100" height="100" src="https://img.example/${i+1}.jpg" alt="候选${i+1}"></a>`).join('');};</script></body>''', content_type='text/html'))
    page.goto('https://www.1688.com/')
    adapter = Browser(validate({'max_candidates': 2}), threading.Event(), lambda *a: None)
    adapter.context = page.context
    monkeypatch.setattr(adapter, 'page', lambda *a: page)
    monkeypatch.setattr(adapter, 'delay', lambda *a: None)
    monkeypatch.setattr(adapter, 'image_payload', lambda task: {'name': 'main.png', 'mimeType': 'image/png', 'buffer': base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=')})
    cards = adapter.search_images({'erp_goods_id': '1'})
    assert page.evaluate('window.submitted') is True
    assert [c['url'] for c in cards] == ['https://detail.1688.com/offer/1.html', 'https://detail.1688.com/offer/2.html']
    assert cards[1]['main_image_url'] == 'https://img.example/2.jpg'


def test_image_search_clicks_generic_submit_inside_upload_dialog(page, monkeypatch):
    import base64
    page.route('https://www.1688.com/', lambda route: route.fulfill(body='''<meta charset="utf-8"><button>提交</button><div role="dialog">
      <input type="file" hidden><button hidden id="submit">提 交</button></div><div id="results"></div>
      <script>document.querySelector('input').onchange=()=>setTimeout(()=>document.querySelector('#submit').hidden=false,40);
      document.querySelector('#submit').onclick=()=>{window.submitted=true;document.querySelector('#results').innerHTML='<a href="https://detail.1688.com/offer/9.html">新结果</a>';};</script>''',
      content_type='text/html'))
    page.goto('https://www.1688.com/')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    adapter.context = page.context
    monkeypatch.setattr(adapter, 'image_payload', lambda task: {
        'name': 'main.png', 'mimeType': 'image/png',
        'buffer': base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=')})
    result = adapter.image_search(page, {'erp_goods_id': '1'})
    assert result is page
    assert page.evaluate('window.submitted') is True


def test_image_search_clicks_search_in_same_component_as_image_upload(page, monkeypatch):
    import base64
    page.route('https://www.1688.com/', lambda route: route.fulfill(body='''<meta charset="utf-8">
      <section id="image-search"><input type="file" accept=".jpg,.jpeg,.png,.webp" hidden>
      <button id="ordinary-search" class="searchBtn--fixture">搜索</button>
      <div><div><button id="image-submit" class="action--fixture actionPrimary--fixture">搜索</button></div></div></section><div id="results"></div>
      <script>document.querySelector('#ordinary-search').onclick=()=>window.wrongButton=true;
      document.querySelector('#image-submit').onclick=()=>{window.submitted=true;
      document.querySelector('#results').innerHTML='<a href="https://detail.1688.com/offer/19.html">新结果</a>';};</script>''',
      content_type='text/html'))
    page.goto('https://www.1688.com/')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    adapter.context = page.context
    monkeypatch.setattr(adapter, 'image_payload', lambda task: {
        'name': 'main.png', 'mimeType': 'image/png',
        'buffer': base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=')})
    result = adapter.image_search(page, {'erp_goods_id': '1'})
    assert result is page
    assert page.evaluate('window.submitted') is True
    assert page.evaluate('Boolean(window.wrongButton)') is False


def test_current_1688_detail_reads_only_official_sku_prices_and_weights(page):
    page.set_content('''<div id="productTitle" data-module="od_title"><div class="title-content"><h1>测试收纳袋</h1></div></div>
      <div id="shopNavigation"><a class="shop-company-name" href="https://shop123.1688.com">测试工厂</a></div>
      <div id="gallery"><img class="preview-img" src="https://img.example/O1CNother_!!1.jpg">
        <img class="preview-img" src="https://img.example/O1CNtarget_!!1.jpg"></div>
      <div id="skuSelection" data-module="od_sku_selection">SKU</div>
      <div id="productPackInfo" data-module="od_product_pack_info">包装信息</div>
      <div id="productAttributes">材质 尼龙</div>''')
    page.evaluate('''() => {
      document.querySelector('#skuSelection').__reactFiber$fixture = {memoizedProps: {dataManager: {params: {skuItems: [
        {skuId: 22, specAttrs: '黑色&gt;大号', discountPrice: '2.50', price: '3.00'},
        {skuId: 11, specAttrs: '黄色&gt;小号', discountPrice: '1.25', price: '2.00'}
      ]}}}, return: null};
      document.querySelector('#productPackInfo').__reactFiber$fixture = {memoizedProps: {packInfoData: {skuInfo: [
        {skuId: 11, weight: 40}, {skuId: 22, weight: 55}
      ]}}, return: null};
    }''')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    offer = adapter.current_supplier_offer(page, {'main_image_url': 'https://img.example/O1CNtarget_!!9.webp'}, timeout=.2)
    assert offer['title'] == '测试收纳袋'
    assert offer['main_image_url'].endswith('O1CNtarget_!!1.jpg')
    assert offer['merchant_id'] == 'shop123'
    assert offer['skus'] == [
        {'id': '11', 'label': '黄色 / 小号', 'price': '1.25', 'raw_price': '¥1.25',
         'raw_surcharge': '', 'raw_weight': '40g', 'raw_text': '黄色 / 小号；页面单价 ¥1.25；包装重量 40g'},
        {'id': '22', 'label': '黑色 / 大号', 'price': '2.50', 'raw_price': '¥2.50',
         'raw_surcharge': '', 'raw_weight': '55g', 'raw_text': '黑色 / 大号；页面单价 ¥2.50；包装重量 55g'},
    ]


def test_current_1688_detail_reads_weight_embedded_in_sku_label_when_pack_info_is_empty(page):
    page.set_content('''<div id="productTitle" data-module="od_title"><div class="title-content"><h1>测试冲浪板</h1></div></div>
      <div id="shopNavigation"><a class="shop-company-name" href="https://shop123.1688.com">测试工厂</a></div>
      <div id="gallery"><img class="preview-img" src="https://img.example/O1CNtarget_!!1.jpg"></div>
      <div id="skuSelection" data-module="od_sku_selection">SKU</div>
      <div id="productPackInfo" data-module="od_product_pack_info">包装信息</div>''')
    page.evaluate('''() => {
      document.querySelector('#skuSelection').__reactFiber$fixture = {memoizedProps: {dataManager: {params: {skuItems: [
        {skuId: 22, specAttrs: '菠萝冲浪板84*56cm(0.45kg / 看产品介绍', discountPrice: '17.85'},
        {skuId: 11, specAttrs: '粉色小海豚72*44cm（140g / 看产品介绍', discountPrice: '4.50'}
      ]}}}, return: null};
    }''')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    offer = adapter.current_supplier_offer(page, {'main_image_url': 'https://img.example/O1CNtarget_!!9.webp'}, timeout=.2)
    assert [sku['raw_weight'] for sku in offer['skus']] == ['140g', '0.45kg']
    assert offer['skus'][1]['raw_text'].endswith('包装重量 0.45kg')


def test_current_1688_detail_applies_one_explicit_common_package_weight_to_all_skus(page):
    page.set_content('''<div id="productTitle" data-module="od_title"><div class="title-content"><h1>测试睡衣</h1></div></div>
      <div id="shopNavigation"><a class="shop-company-name" href="https://shop123.1688.com">测试工厂</a></div>
      <div id="gallery"><img class="preview-img" src="https://img.example/O1CNtarget_!!1.jpg"></div>
      <div id="skuSelection" data-module="od_sku_selection">SKU</div>
      <div id="productPackInfo" data-module="od_product_pack_info">包装信息</div>''')
    page.evaluate('''() => {
      document.querySelector('#skuSelection').__reactFiber$fixture = {memoizedProps: {dataManager: {params: {skuItems: [
        {skuId: 22, specAttrs: '红色&gt;M', discountPrice: '8.00'},
        {skuId: 11, specAttrs: '黑色&gt;S', discountPrice: '7.50'}
      ]}}}, return: null};
      document.querySelector('#productPackInfo').__reactFiber$fixture = {memoizedProps: {packInfoData: {skuInfo: [
        {volume: 0, length: 0, width: 0, weight: 300, height: 0}
      ]}}, return: null};
    }''')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    offer = adapter.current_supplier_offer(page, {'main_image_url': 'https://img.example/O1CNtarget_!!9.webp'}, timeout=.2)
    assert [sku['raw_weight'] for sku in offer['skus']] == ['300g', '300g']


def test_clickable_1688_cards_without_offer_hrefs_exclude_preview_and_open_real_detail(page, monkeypatch):
    page.route('https://air.1688.com/**', lambda route: route.fulfill(body='''<meta charset="utf-8"><body>
      <img src="https://img.example/preview.jpg" width="120" height="120"><div>找到以下货源</div>
      <div style="display:flex"><article><img src="https://img.example/one.jpg" width="120" height="120" onclick="window.open('https://detail.1688.com/offer/123.html')"><p>蓝色杯 ￥22</p></article>
      <article><img src="https://img.example/two.jpg" width="120" height="120"><p>红色杯 ￥25</p></article></div></body>''', content_type='text/html'))
    page.context.route('https://detail.1688.com/offer/123.html', lambda route: route.fulfill(body=SUPPLIER_HTML, content_type='text/html'))
    page.goto('https://air.1688.com/kapp/1688-search/pc-image-search/?imageId=fixture')
    adapter = Browser(validate({'selectors': {'sku_price': 'b'}}), threading.Event(), lambda *a: None)
    adapter.context = page.context
    adapter.search_results['1'] = page
    adapter.adapt_supplier = observed_supplier_answer
    monkeypatch.setattr(adapter, 'delay', lambda *a: None)
    cards = adapter.result_image_cards(page)
    assert [c['main_image_url'] for c in cards] == ['https://img.example/one.jpg', 'https://img.example/two.jpg']
    offer = adapter.read_offer({'erp_goods_id': '1'}, cards[0])
    assert offer['url'] == 'https://detail.1688.com/offer/123.html'
    assert offer['skus'][0]['price'] == '12.50'
    assert len(page.context.pages) == 0  # both temporary 1688 view tabs are closed after reading


def test_partial_erp_write_preserves_weight_and_status_after_reload(page, monkeypatch):
    html = '''<meta charset="utf-8"><div class="curd-detail-wrap"><div class="crud-detail-header"><div class="h1">产品编号：101</div></div>
      <input id="netproceed" value="9.5"><input id="weight" value="430">
      <label><input type="radio" name="stat" value="3000" checked>待审核</label>
      <label><input type="radio" name="stat" value="8000">屏蔽</label><label><input type="radio" name="stat" value="9000">风险</label>
      <button id="save">保存</button><p id="saved" hidden>保存成功</p></div><script>
      const old=JSON.parse(localStorage.getItem('saved')||'null');if(old){document.querySelector('#netproceed').value=old.net;document.querySelector('#weight').value=old.weight;document.querySelector(`[value="${old.status}"]`).checked=true;}
      document.querySelector('#weight').oninput=()=>localStorage.setItem('weightEdited','yes');
      document.querySelector('#save').onclick=()=>{localStorage.setItem('saved',JSON.stringify({net:document.querySelector('#netproceed').value,weight:document.querySelector('#weight').value,status:document.querySelector('input[name=stat]:checked').value}));document.querySelector('#saved').hidden=false;};</script>'''
    page.route('https://meli.zying.net/**', lambda route: route.fulfill(body=html, content_type='text/html'))
    page.goto('https://meli.zying.net/#/product/101')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    monkeypatch.setattr(adapter, 'page', lambda *a: page)
    changes = {'net_income_usd': '4'}
    before = []
    actual = adapter.write_patch({'erp_goods_id': '101', 'erp_edit_url': page.url}, changes, before.append)
    assert before == [{'weight_g': '430', 'net_income_usd': '9.5', 'review_status': '待审核'}]
    assert actual == {'weight_g': '430', 'net_income_usd': '4', 'review_status': '待审核'}
    assert page.evaluate("localStorage.getItem('weightEdited')") is None


def test_locate_erp_detail_prefers_stable_id_when_title_and_image_repeat(page, monkeypatch):
    page.set_content('''<div class="product-item"><span class="product-id">790301169</span><span class="product-title">重复标题</span><img class="product-pic" src="https://img.example/same.jpg"></div>
      <div class="product-item"><span class="product-id">817846219</span><span class="product-title">重复标题</span><img class="product-pic" src="https://img.example/same.jpg"></div>
      <div class="curd-detail-wrap"><div class="crud-detail-header"><div class="h1">产品编号：999</div></div></div>''')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    current = {'id': '999'}
    monkeypatch.setattr(adapter, 'apply_category', lambda *a: None)
    monkeypatch.setattr(adapter, 'first_page', lambda *a: None)

    def value(root, key, attribute=None, required=False):
        if key == 'erp_title':
            return root.locator('.product-title').inner_text()
        if key == 'erp_image':
            return root.locator('img.product-pic').get_attribute('src') or ''
        if key == 'erp_id':
            return root.locator('.product-id').inner_text()
        if key == 'erp_edit_id':
            return current['id']
        raise AssertionError(key)

    monkeypatch.setattr(adapter, 'value', value)
    monkeypatch.setattr(adapter, 'erp_goods_id', lambda _page, row, _record: row.locator('.product-id').inner_text())

    def unique(root, key):
        class Click:
            def click(self):
                current['id'] = root.locator('.product-id').inner_text()
        return Click()

    monkeypatch.setattr(adapter, 'unique', unique)
    adapter.locate_erp_detail(page, {
        'erp_goods_id': '817846219', 'title': '重复标题',
        'main_image_url': 'https://img.example/same.jpg', 'source_page': 1,
        'source_category': '',
    })
    assert current['id'] == '817846219'


def test_locate_erp_detail_uses_source_index_when_duplicate_cards_have_no_ids(page, monkeypatch):
    page.set_content('''<li class="ant-pagination-item-active">1</li>
      <div class="product-item"><div class="product-title" data-id="790301169">重复标题</div>
        <img class="product-pic" src="https://img.example/same.jpg"></div>
      <div class="product-item"><div class="product-title" data-id="817846219">重复标题</div>
        <img class="product-pic" src="https://img.example/same.jpg"></div>
      <div class="curd-detail-wrap"><div class="crud-detail-header"><div class="h1">产品编号：999</div></div>
        <textarea placeholder="请输入内容"></textarea><img class="ant-image-img" src="https://img.example/stale.jpg"></div>
      <script>
        window.clicked=[];
        document.querySelectorAll('.product-title').forEach(node => node.onclick=()=>{
          window.clicked.push(node.dataset.id);
          document.querySelector('.h1').textContent='产品编号：'+node.dataset.id;
          document.querySelector('textarea').value='重复标题';
          document.querySelector('.ant-image-img').src='https://img.example/same.jpg';
        });
      </script>''')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    monkeypatch.setattr(adapter, 'apply_category', lambda *a: None)
    monkeypatch.setattr(adapter, 'first_page', lambda *a: None)

    adapter.locate_erp_detail(page, {
        'erp_goods_id': '817846219', 'title': '重复标题',
        'main_image_url': 'https://img.example/same.jpg', 'source_page': 1,
        'source_index': 2, 'source_category': '',
    })

    assert page.evaluate('window.clicked') == ['817846219']
    assert page.locator('.crud-detail-header .h1').inner_text() == '产品编号：817846219'


def test_erp_write_patch_ignores_hidden_save_clones(page, monkeypatch):
    html = '''<meta charset="utf-8"><div class="curd-detail-wrap"><div class="crud-detail-header"><div class="h1">产品编号：101</div></div>
      <input id="netproceed" value="9.5"><input id="weight" value="430">
      <label><input type="radio" name="stat" value="3000" checked>待审核</label>
      <button id="hidden-save" hidden>保存</button><button id="save">保存</button><p id="saved" hidden>保存成功</p><script>
      const old=JSON.parse(localStorage.getItem('save-hidden')||'null');if(old)document.querySelector('#netproceed').value=old.net;
      document.querySelector('#save').onclick=()=>{localStorage.setItem('save-hidden',JSON.stringify({net:document.querySelector('#netproceed').value}));document.querySelector('#saved').hidden=false;};
    </script></div>'''
    page.route('https://meli.zying.net/**', lambda route: route.fulfill(body=html, content_type='text/html'))
    page.goto('https://meli.zying.net/#/product/101')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    monkeypatch.setattr(adapter, 'page', lambda *a: page)
    actual = adapter.write_patch({'erp_goods_id': '101', 'erp_edit_url': page.url},
                                 {'net_income_usd': '4'}, lambda _old: None)
    assert actual == {'weight_g': '430', 'net_income_usd': '4', 'review_status': '待审核'}


def test_erp_write_patch_confirms_by_reload_when_save_has_no_success_toast(page, monkeypatch):
    html = '''<meta charset="utf-8"><div class="curd-detail-wrap"><div class="crud-detail-header"><div class="h1">产品编号：101</div></div>
      <input id="netproceed" value="9.5"><input id="weight" value="430">
      <label><input type="radio" name="stat" value="3000" checked>待审核</label>
      <button id="save">更新</button><script>
      const old=JSON.parse(localStorage.getItem('saved-no-toast')||'null');
      if(old)document.querySelector('#netproceed').value=old.net;
      document.querySelector('#save').onclick=()=>localStorage.setItem('saved-no-toast',JSON.stringify({net:document.querySelector('#netproceed').value}));</script></div>'''
    page.route('https://meli.zying.net/**', lambda route: route.fulfill(body=html, content_type='text/html'))
    page.goto('https://meli.zying.net/#/product/101')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    monkeypatch.setattr(adapter, 'page', lambda *a: page)

    actual = adapter.write_patch({'erp_goods_id': '101', 'erp_edit_url': page.url},
                                 {'net_income_usd': '4'}, lambda _old: None)

    assert actual == {'weight_g': '430', 'net_income_usd': '4', 'review_status': '待审核'}


def test_erp_write_patch_selects_primary_when_save_buttons_are_duplicated(page, monkeypatch):
    html = '''<meta charset="utf-8"><div class="curd-detail-wrap"><div class="crud-detail-header"><div class="h1">产品编号：101</div></div>
      <input id="netproceed" value="9.5"><input id="weight" value="430">
      <label><input type="radio" name="stat" value="3000" checked>待审核</label>
      <button id="top-save">保存</button><button id="main-save" class="primary" style="margin-top:1px">保存</button><p id="saved" hidden>保存成功</p><script>
      const old=JSON.parse(localStorage.getItem('save-duplicate')||'null');if(old)document.querySelector('#netproceed').value=old.net;
      document.querySelectorAll('button').forEach(button => button.onclick=()=>{localStorage.setItem('save-duplicate',JSON.stringify({net:document.querySelector('#netproceed').value}));document.querySelector('#saved').hidden=false;});
    </script></div>'''
    page.route('https://meli.zying.net/**', lambda route: route.fulfill(body=html, content_type='text/html'))
    page.goto('https://meli.zying.net/#/product/101')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    monkeypatch.setattr(adapter, 'page', lambda *a: page)
    actual = adapter.write_patch({'erp_goods_id': '101', 'erp_edit_url': page.url},
                                 {'net_income_usd': '4'}, lambda _old: None)
    assert actual == {'weight_g': '430', 'net_income_usd': '4', 'review_status': '待审核'}


def test_erp_write_patch_rejects_unsupported_review_status():
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    with pytest.raises(ValueError, match='审核状态只能回填为通过或价格异常'):
        adapter.write_patch(
            {'erp_goods_id': '101'},
            {'review_status': '风险'},
            lambda _old: None,
        )


def test_erp_write_patch_saves_and_verifies_approved_status(page, monkeypatch):
    html = '''<meta charset="utf-8"><div class="curd-detail-wrap"><div class="crud-detail-header"><div class="h1">产品编号：101</div></div>
      <input id="netproceed" value="9.5"><input id="weight" value="430">
      <label><input type="radio" name="stat" value="1000">通过</label>
      <label><input type="radio" name="stat" value="3000" checked>待审核</label>
      <label><input type="radio" name="stat" value="4000">价格异常</label>
      <button id="save">保存</button><p id="saved" hidden>保存成功</p></div><script>
      const old=JSON.parse(localStorage.getItem('saved-status')||'null');
      if(old){document.querySelector('#netproceed').value=old.net;document.querySelector(`[value="${old.status}"]`).checked=true;}
      document.querySelector('#save').onclick=()=>{localStorage.setItem('saved-status',JSON.stringify({net:document.querySelector('#netproceed').value,status:document.querySelector('input[name=stat]:checked').value}));document.querySelector('#saved').hidden=false;};</script>'''
    page.route('https://meli.zying.net/**', lambda route: route.fulfill(body=html, content_type='text/html'))
    page.goto('https://meli.zying.net/#/product/101')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)
    monkeypatch.setattr(adapter, 'page', lambda *a: page)

    actual = adapter.write_patch(
        {'erp_goods_id': '101', 'erp_edit_url': page.url},
        {'net_income_usd': '4', 'review_status': '通过'},
        lambda _old: None,
    )

    assert actual == {'weight_g': '430', 'net_income_usd': '4', 'review_status': '通过'}


def test_erp_save_button_accepts_portal_footer_update_action(page):
    page.set_content('''<div class="curd-detail-wrap"><input id="weight"></div>
      <footer><button id="portal-update" class="ant-btn-primary">更新</button></footer>''')
    adapter = Browser(validate({}), threading.Event(), lambda *a: None)

    button = adapter.erp_save_button(page, page.locator('.curd-detail-wrap'))

    assert button.get_attribute('id') == 'portal-update'


def test_image_search_timeout_keeps_evidence_and_rejects_stale_home_recommendations(page, monkeypatch, tmp_path):
    import base64
    page.route("https://www.1688.com/", lambda route: route.fulfill(body='''<meta charset="utf-8"><body>
        <h1>以图搜货（离线测试页面）</h1><input type="file" hidden>
        <a href="https://detail.1688.com/offer/123.html">首页旧推荐</a></body>''', content_type="text/html"))
    page.goto("https://www.1688.com/")
    service = Service(tmp_path)
    service.store.add({"erp_goods_id": "timeout-1", "title": "离线测试商品"})
    adapter = Browser(validate({}), threading.Event(), service.store.log)
    adapter.context = page.context
    adapter.record_visual = service.record_visual
    monkeypatch.setattr(adapter, "image_payload", lambda task: {"name": "main.png", "mimeType": "image/png", "buffer": base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII=")})
    with pytest.raises(NoExactMatch, match="搜索超时.*无法确认完全匹配"):
        adapter.image_search(page, service.store.get("timeout-1"), timeout=.1)
    events = service.store.get("timeout-1")["visual_history"]
    assert events[-1]["step"] == "search_timeout"
    assert not events[-1].get("screenshot_url")
    assert not any(event["step"] == "search_results" for event in events)


def test_console_shows_worker_error_and_logs_on_task_page(page, tmp_path, monkeypatch):
    service = Service(tmp_path)
    service.config.save(validate({"supplier_auto_adapt": False}))
    service.store.set_state("login", {"confirmed": True})
    service.store.set_state("run_selection", {"category": "", "start_page": 1, "end_page": 2})
    service.store.set_state("run", {"mode": "collect", "message": "已停止"})
    reason = "未找到唯一的智赢分类搜索按钮"
    service.store.set_state("run_error", reason)
    service.store.log(reason, level="ERROR")
    monkeypatch.setattr(service, "require_login", lambda *args: None)
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(service))
    client = app.test_client()
    def respond(route):
        request = route.request
        url = urlsplit(request.url)
        response = client.open(url.path + ("?" + url.query if url.query else ""), method=request.method,
                               data=request.post_data, headers=request.headers, base_url="http://127.0.0.1:5018")
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
    page.route("http://127.0.0.1:5018/**", respond)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:5018/ai-weight-price")
    expect = pytest.importorskip("playwright.sync_api").expect
    expect(page.locator("#run-summary")).to_have_text("采集失败")
    expect(page.locator("#run-message")).to_have_text(reason)
    expect(page.locator("#latest-event")).to_contain_text(reason)
    expect(page.locator("#live-console")).to_be_visible()
    expect(page.locator("#live-log-list")).to_contain_text(reason)
    expect(page.locator("#view-logs")).to_be_hidden()
    page.locator("#process-range").click()
    expect(page.locator("#run-summary")).to_have_text("最近操作失败")
    expect(page.locator("#run-message")).to_contain_text("缺少DOM字段")
    expect(page.locator("#latest-event")).to_contain_text("缺少DOM字段")
    page.locator("#run-progress button").click()
    expect(page.locator("#view-logs")).to_be_visible()
    expect(page.locator("#logs")).to_contain_text("缺少DOM字段")
    page.get_by_role("button", name="参数设置", exact=True).click()
    page.locator("#sku-price-mode").select_option("base_plus_surcharge")
    page.locator("#usd-cny-rate").fill("7.2")
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.locator("#toast")).to_have_text("配置已保存，下次启动生效")
    assert service.config.load()["sku_price_mode"] == "base_plus_surcharge"
    assert service.config.load()["usd_cny_rate"] == "7.2"
    assert not errors


def test_console_only_shows_1688_link_after_configured_match_threshold(page, tmp_path):
    service = Service(tmp_path)
    service.config.save(validate({"match_threshold": .95}))
    for key, title, score, approved in (
        ("high", "高分商品", .96, True),
        ("low", "低分商品", .90, False),
    ):
        service.store.add({"erp_goods_id": key, "title": title})
        service.store.update(
            key,
            best_match_url=f"https://detail.1688.com/offer/{1 if key == 'high' else 2}.html",
            best_match_confidence=score,
            image_match_confidence=score,
            best_match_approved=approved,
        )

    app = Flask(__name__)
    app.register_blueprint(create_blueprint(service))
    client = app.test_client()

    def respond(route):
        request = route.request
        url = urlsplit(request.url)
        response = client.open(
            url.path + ("?" + url.query if url.query else ""),
            method=request.method,
            data=request.post_data,
            headers=request.headers,
            base_url="http://127.0.0.1:5018",
        )
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

    page.route("http://127.0.0.1:5018/**", respond)
    page.goto("http://127.0.0.1:5018/ai-weight-price")
    page.locator("#task-scope").select_option("all")
    expect = pytest.importorskip("playwright.sync_api").expect
    high = page.locator("#rows tr", has_text="高分商品")
    low = page.locator("#rows tr", has_text="低分商品")
    expect(high.locator("a.supplier-link")).to_have_attribute(
        "href", "https://detail.1688.com/offer/1.html"
    )
    expect(low.locator("a.supplier-link")).to_have_count(0)


def product_detail_fixture(page):
    page.set_content('''
      <li class="ant-pagination-item-active">1</li>
      <div class="product-item"><div class="product-title" onclick="showProduct(101, 'blue cup')">blue cup</div>
        <img class="product-pic" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="></div>
      <div class="product-item"><div class="product-title" onclick="showProduct(102, 'red cup')">red cup</div>
        <img class="product-pic" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="></div>
      <div class="curd-detail-wrap"><div class="crud-detail-header"><div class="h1"></div></div>
        <textarea placeholder="请输入内容"></textarea></div>
      <script>
        window.clicked=0;
        function showProduct(id,title){window.clicked++;document.querySelector('.h1').textContent='产品编号：'+id;
          document.querySelector('textarea').value=title;}
      </script>''')
    return {"title": "blue cup", "main_image_url": ""}


def test_missing_card_id_reads_verified_detail_and_collects_each_product(page, tmp_path, monkeypatch):
    from erp.ai_weight_price.config import selection_key
    product_detail_fixture(page)
    config = validate({})
    config["run_selection"] = {"category": "", "start_page": 1, "end_page": 1}
    store = Store(tmp_path)
    adapter = Browser(config, threading.Event(), store.log)
    monkeypatch.setattr(adapter, "page", lambda *args: page)
    monkeypatch.setattr(adapter, "release", lambda *args: None)
    monkeypatch.setattr(adapter, "apply_category", lambda *args: "全部分类")
    assert adapter.collect(store) == 1
    assert page.evaluate("window.clicked") == 2
    assert store.get("101")["title"] == "blue cup"
    assert store.get("102")["title"] == "red cup"
    assert store.list(scope=selection_key(config["run_selection"], config))["total"] == 2
    assert store.state("collection")["complete"]
    assert any("已从智赢详情读取产品编号：102" in event["message"] for event in store.logs())


def test_collect_starts_at_configured_item_on_first_selected_page(page, tmp_path, monkeypatch):
    from erp.ai_weight_price.config import selection_key
    page.set_content('''<li class="ant-pagination-item-active">1</li>
      <div class="product-item"><span class="product-id">101</span><div class="product-title">first</div><img class="product-pic" src="https://img.example/1.jpg"></div>
      <div class="product-item"><span class="product-id">102</span><div class="product-title">second</div><img class="product-pic" src="https://img.example/2.jpg"></div>
      <div class="product-item"><span class="product-id">103</span><div class="product-title">third</div><img class="product-pic" src="https://img.example/3.jpg"></div>''')
    config = validate({})
    config["run_selection"] = {"category": "", "start_page": 1, "end_page": 1, "start_item": 2}
    store = Store(tmp_path)
    adapter = Browser(config, threading.Event(), store.log)
    monkeypatch.setattr(adapter, "page", lambda *args: page)
    monkeypatch.setattr(adapter, "release", lambda *args: None)
    monkeypatch.setattr(adapter, "apply_category", lambda *args: "全部分类")
    assert adapter.collect(store) == 1
    scope = selection_key(config["run_selection"], config)
    rows = store.list(scope=scope)["rows"]
    assert [row["erp_goods_id"] for row in rows] == ["102", "103"]
    assert [row["source_index"] for row in rows] == [2, 3]


@pytest.mark.parametrize("selector", ["", ".product-id"])
def test_empty_or_default_card_id_uses_detail_fallback(page, selector):
    record = product_detail_fixture(page)
    adapter = Browser(validate({"selectors": {"erp_id": selector}}), threading.Event(), lambda *args: None)
    assert adapter.erp_goods_id(page, page.locator(".product-item").first, record) == "101"


@pytest.mark.parametrize("scenario", ["stale", "wrong_product", "non_erp_id", "ambiguous"])
def test_detail_fallback_never_accepts_wrong_or_stale_id(page, scenario):
    record = product_detail_fixture(page)
    if scenario == "stale":
        page.evaluate("showProduct(101, 'blue cup'); window.showProduct=()=>{}")
    elif scenario == "wrong_product":
        page.evaluate("window.showProduct=()=>{document.querySelector('.h1').textContent='202';document.querySelector('textarea').value='different product'}")
    elif scenario == "non_erp_id":
        page.evaluate("window.showProduct=()=>{document.querySelector('.h1').textContent='MLM123456';document.querySelector('textarea').value='blue cup'}")
    else:
        page.evaluate("document.body.append(document.querySelector('.curd-detail-wrap').cloneNode(true))")
    adapter = Browser(validate({}), threading.Event(), lambda *args, **kwargs: None)
    with pytest.raises(ValueError):
        adapter.erp_goods_id(page, page.locator(".product-item").first, record, timeout=.3)


def closable_product_detail_fixture(page):
    record = product_detail_fixture(page)
    page.evaluate('''() => {
      const detail = document.querySelector('.curd-detail-wrap');
      detail.insertAdjacentHTML('afterbegin', '<button class="crud-detail-close">关闭</button>');
      detail.querySelector('button').onclick = () => {detail.hidden = true;};
      const show = window.showProduct;
      window.showProduct = (id, title) => {detail.hidden = false; show(id, title);};
    }''')
    return record


def test_detail_fallback_reopens_already_selected_product_with_same_id(page):
    record = closable_product_detail_fixture(page)
    page.evaluate("showProduct(101, 'blue cup');window.clicked=0")
    adapter = Browser(validate({}), threading.Event(), lambda *args, **kwargs: None)

    assert adapter.erp_goods_id(page, page.locator('.product-item').first, record) == '101'
    assert page.evaluate('window.clicked') == 1


def test_detail_fallback_recovers_after_first_click_does_not_load(page):
    record = closable_product_detail_fixture(page)
    page.evaluate('''() => {
      const show = window.showProduct;
      window.attempts = 0;
      window.showProduct = (id, title) => {if (++window.attempts === 2) show(id, title);};
    }''')
    events = []
    adapter = Browser(validate({}), threading.Event(), lambda message, **kw: events.append(message))

    assert adapter.erp_goods_id(page, page.locator('.product-item').first, record, timeout=.35) == '101'
    assert page.evaluate('window.attempts') == 2
    assert any('重试一次' in event for event in events)
    assert any('"root_count": 0' in event for event in events)


def test_detail_fallback_does_not_accept_old_fields_during_header_transition(page):
    record = closable_product_detail_fixture(page)
    page.evaluate('''() => {
      window.showProduct = () => {
        document.querySelector('.curd-detail-wrap').hidden = false;
        document.querySelector('.h1').textContent = '202';
        document.querySelector('textarea').value = 'blue cup';
        setTimeout(() => {document.querySelector('textarea').value = 'different product';}, 50);
      };
    }''')
    events = []
    adapter = Browser(validate({}), threading.Event(), lambda message, **kw: events.append(message))

    with pytest.raises(ValueError, match='重试后仍无法核对'):
        adapter.erp_goods_id(page, page.locator('.product-item').first, record, timeout=.35)
    assert sum('智赢详情编号读取超时' in event for event in events) == 2
    assert any('"id": "202"' in event and '"title_match": false' in event for event in events)


def test_detail_fallback_rejects_card_replaced_during_retry(page):
    record = product_detail_fixture(page)
    page.evaluate('''() => {
      window.showProduct = () => {
        window.clicked++;
        document.querySelector('.product-title').textContent = 'replacement product';
      };
    }''')
    adapter = Browser(validate({}), threading.Event(), lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match='列表商品.*发生变化'):
        adapter.erp_goods_id(page, page.locator('.product-item').first, record, timeout=.3)
    assert page.evaluate('window.clicked') == 1


def test_detail_fallback_does_not_retry_after_stop(page):
    from erp.ai_weight_price.browser import Stopped
    record = product_detail_fixture(page)
    page.evaluate('() => {window.showProduct = () => {window.clicked++;};}')
    stop = threading.Event()
    def log(message, **kwargs):
        if '读取超时' in message:
            stop.set()
    adapter = Browser(validate({}), stop, log)
    with pytest.raises(Stopped):
        adapter.erp_goods_id(page, page.locator('.product-item').first, record, timeout=.3)
    assert page.evaluate('window.clicked') == 1


def test_explicit_card_id_is_used_and_bad_custom_selector_is_not_ignored(page):
    record = product_detail_fixture(page)
    page.evaluate("document.querySelector('.product-item').insertAdjacentHTML('beforeend','<span class=custom-id>商品ID：201</span>')")
    adapter = Browser(validate({"selectors": {"erp_id": ".custom-id"}}), threading.Event(), lambda *args: None)
    assert adapter.erp_goods_id(page, page.locator(".product-item").first, record) == "201"
    assert page.evaluate("window.clicked") == 0
    with pytest.raises(ValueError, match="实际 0"):
        adapter.erp_goods_id(page, page.locator(".product-item").last, record)
    page.evaluate("document.querySelector('.product-item').insertAdjacentHTML('beforeend','<span class=custom-id>202</span>')")
    with pytest.raises(ValueError, match="实际 2"):
        adapter.erp_goods_id(page, page.locator(".product-item").first, record)
    assert page.evaluate("window.clicked") == 0


@pytest.mark.parametrize("wrong_saved_value", [False, True])
def test_write_fills_integer_dollar_net_proceeds_and_verifies_after_reload(page, monkeypatch, wrong_saved_value):
    html = '''<div class="curd-detail-wrap"><div id="goods">101</div><div id="sku">blue cup</div>
      <input id="cost" value="99"><input id="netproceed" value="5"><input id="weight" value="400">
      <button id="save">保存</button><span id="saved" hidden>已保存</span></div>
      <script>
        const previous=JSON.parse(sessionStorage.getItem('saved')||'null');
        if(previous){document.querySelector('#netproceed').value=previous.net;document.querySelector('#weight').value=previous.weight;}
        document.querySelector('#save').onclick=()=>{sessionStorage.setItem('saved',JSON.stringify({
          net:WRONG?'777':document.querySelector('#netproceed').value,weight:document.querySelector('#weight').value}));
          document.querySelector('#saved').hidden=false;};
      </script>'''.replace("WRONG", "true" if wrong_saved_value else "false")
    page.route("https://meli.zying.net/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    page.goto("https://meli.zying.net/#/product/101")
    config = validate({"selectors": {"erp_edit_id": "#goods", "erp_edit_sku": "#sku", "erp_cost_input": "#cost",
                                     "erp_weight_input": "#weight", "erp_save": "#save", "erp_saved": "#saved"}})
    adapter = Browser(config, threading.Event(), lambda *args: None)
    monkeypatch.setattr(adapter, "page", lambda *args: page)
    monkeypatch.setattr(adapter, "release", lambda *args: None)
    task = {"erp_edit_url": page.url, "erp_goods_id": "101", "erp_sku": "blue cup", "cost_price": "22", "net_income_usd": "4", "weight_g": "450"}
    before = []
    if wrong_saved_value:
        with pytest.raises(ValueError, match="美元净收益"):
            adapter.write(task, before.append)
    else:
        adapter.write(task, before.append)
        assert page.locator("#netproceed").input_value() == "4"
    assert before == [{"net_income_usd": "5", "weight_g": "400"}]
    assert page.locator("#cost").input_value() == "99"
    assert page.locator("#weight").input_value() == "450"
