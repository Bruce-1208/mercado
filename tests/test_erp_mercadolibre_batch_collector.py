import asyncio

import pytest

from erp import mercadolibre_batch_collector as collector
from erp import mercadolibre_playwright_collector as playwright_collector
from erp.mercadolibre_batch_collector import (
    build_marketplace_search_url,
    canonical_marketplace_item_url,
    extract_listing_item_id,
    marketplace_url_has_cross_border_filter,
    merge_listing_candidates,
    normalize_collection_scope,
    normalize_collection_workers,
    parse_detail_html,
    parse_listing_html,
    parse_plugin_metrics,
    validate_collection_request,
)


def test_parse_zying_plugin_dimensions_and_volumetric_weight():
    result = parse_plugin_metrics("重量 250g 尺寸 20 x 20 x 5 cm 计抛333g")

    assert result["package_length_cm"] == 20
    assert result["package_width_cm"] == 20
    assert result["package_height_cm"] == 5
    assert result["weight_g"] == 250
    assert result["volumetric_weight_kg"] == pytest.approx(0.3333)
    assert result["weight_basis"] == "plugin_actual"


def test_collected_spanish_attribute_names_use_mercado_ids():
    rows = collector._attribute_rows(
        [
            {"name": "Marca", "value": "Generic"},
            {"name": "Género", "value": "Mujer"},
            {"name": "Composición", "value": "Poliéster"},
            {"name": "Es un kit de fábrica", "value": "No"},
        ]
    )

    assert [row["id"] for row in rows] == [
        "BRAND",
        "GENDER",
        "COMPOSITION",
        "IS_FACTORY_KIT",
    ]


def test_parse_zying_plugin_converts_kg_to_grams():
    result = parse_plugin_metrics("商品 毛重 0.8 kg 尺寸 35×20×10 体积重 1.4 kg")

    assert result["weight_g"] == 800
    assert result["volumetric_weight_kg"] == pytest.approx(1.1667)
    assert result["plugin_volumetric_display"] == "1.4 kg"
    assert result["dimensions_display"] == "35 × 20 × 10 cm"


def test_volumetric_weight_is_not_written_into_actual_weight():
    result = parse_plugin_metrics("尺寸 30 x 20 x 10 cm 计抛 1 kg")

    assert result["weight_g"] is None
    assert result["volumetric_weight_kg"] == 1


def test_merge_listing_candidates_normalizes_and_deduplicates_ids():
    rows = merge_listing_candidates(
        [],
        [
            {
                "href": "https://articulo.mercadolibre.com.mx/MLM-3016972321",
                "title": "Producto uno",
                "main_image_url": "//http2.mlstatic.com/one.jpg",
            },
            {
                "href": "https://articulo.mercadolibre.com.mx/MLM-3016972321?variation=1",
                "title": "Duplicado",
            },
            {"href": "https://example.test/not-a-product", "title": "Inválido"},
        ],
        20,
    )

    assert len(rows) == 1
    assert rows[0]["source_item_id"] == "MLM3016972321"
    assert rows[0]["main_image_url"].startswith("https://")
    assert extract_listing_item_id(
        "https://www.mercadolibre.com.mx/producto/p/MLM2069788918?"
        "pdp_filters=item_id%3AMLM3016972321"
    ) == "MLM3016972321"
    assert extract_listing_item_id(
        "https://www.mercadolibre.com.mx/producto/p/MLM2069788918?"
        "pdp_filters=SHIPPING_ORIGIN%3A10215069#position=3&wid=MLM5546998214"
    ) == "MLM5546998214"


def test_collection_request_requires_mercado_host_and_bounded_count():
    assert validate_collection_request(
        "https://listado.mercadolibre.com.mx/bolsas", 25
    )[1] == 25
    with pytest.raises(ValueError, match="Mercado Libre"):
        validate_collection_request("https://example.test/products", 10)
    with pytest.raises(ValueError, match="1-500"):
        validate_collection_request("https://listado.mercadolibre.com.mx/bolsas", 501)

    assert normalize_collection_workers(10) == 10
    with pytest.raises(ValueError, match="1-10"):
        normalize_collection_workers(11)


def test_keyword_builds_country_frontend_search_urls():
    assert playwright_collector.DEFAULT_SETUP_URL == "https://www.mercadolibre.com/"
    assert build_marketplace_search_url("bolsas para mujer", "MLM") == (
        "https://listado.mercadolibre.com.mx/bolsas-para-mujer"
    )
    assert build_marketplace_search_url("bolsa feminina", "MLB") == (
        "https://lista.mercadolivre.com.br/bolsa-feminina"
    )
    assert build_marketplace_search_url("disfraces", "MLU") == (
        "https://listado.mercadolibre.com.uy/disfraces"
    )
    assert build_marketplace_search_url(
        "cosplay", "MLM", "cross_border"
    ) == (
        "https://listado.mercadolibre.com.mx/"
        "cosplay_NoIndex_True_SHIPPING*ORIGIN_10215069"
    )
    assert normalize_collection_scope("cross_border") == "cross_border"
    with pytest.raises(ValueError, match="采集国家"):
        build_marketplace_search_url("bolsas", "MPE")
    with pytest.raises(ValueError, match="采集专区"):
        normalize_collection_scope("official_store")
    assert canonical_marketplace_item_url("MLM3016972321") == (
        "https://articulo.mercadolibre.com.mx/MLM-3016972321"
    )
    assert canonical_marketplace_item_url("MLB5113933391") == (
        "https://produto.mercadolivre.com.br/MLB-5113933391"
    )
    assert marketplace_url_has_cross_border_filter(
        "https://listado.mercadolibre.com.mx/"
        "cosplay_NoIndex_True_SHIPPING*ORIGIN_10215069"
    )


def test_cross_border_scope_only_keeps_international_cards():
    rows = merge_listing_candidates(
        [],
        [
            {
                "href": "https://articulo.mercadolibre.com.mx/MLM-3016972321",
                "title": "Producto internacional",
                "is_cross_border": True,
                "shipping_origin_country": "CN",
            },
            {
                "href": "https://articulo.mercadolibre.com.mx/MLM-3016972322",
                "title": "Producto internacional USA",
                "is_cross_border": True,
                "shipping_origin_country": "US",
            },
            {
                "href": "https://articulo.mercadolibre.com.mx/MLM-3016972323",
                "title": "Producto local",
                "is_cross_border": False,
            },
        ],
        20,
        collection_scope="cross_border",
    )

    assert [row["source_item_id"] for row in rows] == ["MLM3016972321"]
    assert rows[0]["is_cross_border"] is True
    assert rows[0]["shipping_origin_country"] == "CN"
    assert rows[0]["listing_url"].endswith("/MLM-3016972321")


def test_all_scope_skips_us_cards_but_keeps_every_other_origin():
    rows = merge_listing_candidates(
        [],
        [
            {
                "href": "https://articulo.mercadolibre.com.mx/MLM-3016972331",
                "title": "Producto USA",
                "shipping_origin_country": "US",
            },
            {
                "href": "https://articulo.mercadolibre.com.mx/MLM-3016972332",
                "title": "Producto sin país confirmado",
                "shipping_origin_country": "",
            },
            {
                "href": "https://articulo.mercadolibre.com.mx/MLM-3016972333",
                "title": "Producto China",
                "shipping_origin_country": "CN",
            },
        ],
        20,
        collection_scope="all",
    )

    assert [row["source_item_id"] for row in rows] == [
        "MLM3016972332",
        "MLM3016972333",
    ]


def test_listing_html_detects_us_flag_asset_without_origin_text():
    listing = parse_listing_html(
        """
        <li class="ui-search-layout__item">
          <a class="ui-search-link" href="/MLM-3016972341">USA flag only</a>
          <h2>USA flag only</h2>
          <img src="https://http2.mlstatic.com/flags/US.svg?v=1" />
        </li>
        """,
        "https://listado.mercadolibre.com.mx/bolsas",
    )

    assert listing["rows"][0]["shipping_origin_country"] == "US"


def test_listing_html_marks_international_seller_cards():
    listing = parse_listing_html(
        """
        <html><body><ol>
          <li class="ui-search-layout__item">
            <a class="ui-search-link" href="/MLM-3016972321">Bolsa</a>
            <h2>Bolsa</h2><span>China Internacional China</span>
          </li>
          <li class="ui-search-layout__item">
            <a class="ui-search-link" href="/MLM-3016972322">USA</a>
            <h2>USA</h2><span>Internacional Estados Unidos</span>
          </li>
          <li class="ui-search-layout__item">
            <a class="ui-search-link" href="/MLM-3016972323">Local</a>
            <h2>Local</h2><span>Llega mañana</span>
          </li>
        </ol></body></html>
        """,
        "https://listado.mercadolibre.com.mx/bolsas",
    )

    assert [row["is_cross_border"] for row in listing["rows"]] == [True, True, False]
    assert [row["shipping_origin_country"] for row in listing["rows"]] == ["CN", "US", ""]


def test_playwright_listing_pages_keep_images_needed_by_lazy_result_grid():
    should_block = playwright_collector._should_block_resource

    assert not should_block(
        "image",
        "https://http2.mlstatic.com/result.webp",
        "https://listado.mercadolibre.com.mx/intercoms",
        allow_images=True,
    )
    assert should_block(
        "image",
        "https://http2.mlstatic.com/detail.webp",
        "https://articulo.mercadolibre.com.mx/MLM-123",
        allow_images=False,
    )
    assert should_block(
        "font", "https://example.test/font.woff2", "", allow_images=True
    )
    assert not should_block(
        "image",
        "https://example.test/captcha/picture.png",
        "https://example.test/account-verification",
        allow_images=False,
    )

    class FakePage:
        def __init__(self):
            self.route_calls = 0

        def set_default_timeout(self, _timeout):
            return None

        async def route(self, *_args):
            self.route_calls += 1

    page = FakePage()

    class FakeContext:
        async def new_page(self):
            return page

    runtime = type("Runtime", (), {"context": FakeContext(), "pages": []})()
    result = asyncio.run(
        playwright_collector._new_page(runtime, optimize_resources=False)
    )

    assert result is page
    assert page.route_calls == 0


def test_playwright_waits_for_bitbrowser_cdp_listener(monkeypatch):
    calls = []
    browser = type("Browser", (), {"contexts": [object()]})()

    class Chromium:
        async def connect_over_cdp(self, url, timeout):
            calls.append((url, timeout))
            if len(calls) < 3:
                raise RuntimeError("connect ECONNREFUSED 127.0.0.1:60228")
            return browser

    playwright = type("Playwright", (), {"chromium": Chromium()})()
    monkeypatch.setenv("MERCADO_BITBROWSER_CDP_READY_TIMEOUT_SECONDS", "5")

    result = asyncio.run(
        playwright_collector._connect_bitbrowser_over_cdp(
            playwright, "http://127.0.0.1:60228"
        )
    )

    assert result is browser
    assert len(calls) == 3
    assert all(url == "http://127.0.0.1:60228" for url, _ in calls)


def test_playwright_keyword_flow_searches_selected_frontend_and_enters_international():
    events = []

    class Navigation:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Locator:
        def __init__(self, page, kind):
            self.page = page
            self.kind = kind

        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            return None

        async def fill(self, value):
            assert self.kind == "search"
            self.page.searched_keyword = value

        async def press(self, key, **_kwargs):
            assert self.kind == "search"
            assert key == "Enter"
            self.page.url = "https://listado.mercadolibre.com.mx/cosplay"

        async def click(self, **_kwargs):
            assert self.kind == "international"
            self.page.url = (
                "https://listado.mercadolibre.com.mx/"
                "cosplay_NoIndex_True_SHIPPING*ORIGIN_10215069"
            )

    class Page:
        def __init__(self):
            self.url = "about:blank"
            self.visited = []
            self.searched_keyword = ""

        async def goto(self, url, **_kwargs):
            self.visited.append(url)
            self.url = url

        def locator(self, selector):
            return Locator(
                self,
                "search" if 'name="as_word"' in selector else "international",
            )

        def expect_navigation(self, **_kwargs):
            return Navigation()

        async def wait_for_load_state(self, *_args, **_kwargs):
            return None

    page = Page()
    result = asyncio.run(
        playwright_collector._run_keyword_search_flow(
            page,
            (
                "https://listado.mercadolibre.com.mx/"
                "cosplay_NoIndex_True_SHIPPING*ORIGIN_10215069"
            ),
            "  cosplay  ",
            collection_scope="cross_border",
            on_page=events.append,
            stop_event=None,
        )
    )

    assert page.visited == [
        (
            "https://listado.mercadolibre.com.mx/"
            "cosplay_NoIndex_True_SHIPPING*ORIGIN_10215069"
        ),
        "https://www.mercadolibre.com.mx/",
        (
            "https://listado.mercadolibre.com.mx/"
            "cosplay_NoIndex_True_SHIPPING*ORIGIN_10215069"
        ),
    ]
    assert page.searched_keyword == "cosplay"
    assert marketplace_url_has_cross_border_filter(result)
    assert [event["stage"] for event in events] == [
        "keyword_search",
        "keyword_search_fallback",
        "keyword_search_complete",
        "cross_border_filter",
        "cross_border_filter_complete",
    ]


def test_playwright_keyword_flow_uses_direct_search_url_when_results_load():
    events = []
    source_url = "https://listado.mercadolibre.com.mx/cosplay-sexy"

    class Page:
        def __init__(self):
            self.url = "about:blank"
            self.visited = []

        async def goto(self, url, **_kwargs):
            self.visited.append(url)
            self.url = url

        async def wait_for_selector(self, *_args, **_kwargs):
            return None

        def is_closed(self):
            return False

        async def evaluate(self, script, *_args):
            if script == playwright_collector.LISTING_DOM_SCRIPT:
                return {"rows": [{"href": "https://example.test/MLM-123456789"}]}
            return None

    page = Page()
    result = asyncio.run(
        playwright_collector._run_keyword_search_flow(
            page,
            source_url,
            "cosplay sexy",
            collection_scope="all",
            on_page=events.append,
            stop_event=None,
        )
    )

    assert result == source_url
    assert page.visited == [source_url]
    assert events[-1]["stage"] == "keyword_search_complete"
    assert "直接进入" in events[-1]["message"]


def test_playwright_keyword_flow_waits_for_initial_buyer_verification(monkeypatch):
    events = []
    source_url = "https://listado.mercadolibre.com.mx/cosplay-sexy"

    class Page:
        def __init__(self):
            self.url = "about:blank"
            self.listing_reads = 0
            self.brought_to_front = False

        async def goto(self, _url, **_kwargs):
            self.url = "https://www.mercadolibre.com.mx/gz/account-verification"

        def is_closed(self):
            return False

        async def evaluate(self, script, *_args):
            if script != playwright_collector.LISTING_DOM_SCRIPT:
                return None
            self.listing_reads += 1
            if self.listing_reads == 1:
                return {"rows": [], "body": "Por seguridad, completa este paso"}
            self.url = source_url
            return {"rows": [{"href": "https://example.test/MLM-123456789"}]}

        async def bring_to_front(self):
            self.brought_to_front = True

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(playwright_collector.asyncio, "sleep", no_sleep)
    page = Page()
    result = asyncio.run(
        playwright_collector._run_keyword_search_flow(
            page,
            source_url,
            "cosplay sexy",
            collection_scope="all",
            on_page=events.append,
            stop_event=None,
        )
    )

    assert result == source_url
    assert page.brought_to_front is True
    assert [event["stage"] for event in events] == [
        "keyword_search",
        "waiting_verification",
        "verification_resolved",
        "keyword_search_complete",
    ]


def test_playwright_keyword_flow_stops_when_international_facet_is_missing():
    class Navigation:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Locator:
        def __init__(self, page, search):
            self.page = page
            self.search = search

        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            if not self.search:
                raise TimeoutError("facet missing")

        async def fill(self, value):
            self.page.keyword = value

        async def press(self, _key, **_kwargs):
            self.page.url = "https://listado.mercadolibre.com.mx/cosplay"

    class Page:
        url = "about:blank"
        keyword = ""

        async def goto(self, url, **_kwargs):
            self.url = url

        def locator(self, selector):
            return Locator(self, 'name="as_word"' in selector)

        def expect_navigation(self, **_kwargs):
            return Navigation()

        async def wait_for_load_state(self, *_args, **_kwargs):
            return None

    page = Page()
    with pytest.raises(RuntimeError, match="左侧没有 Internacional.*已停止"):
        asyncio.run(
            playwright_collector._run_keyword_search_flow(
                page,
                "https://listado.mercadolibre.com.mx/cosplay",
                "cosplay",
                collection_scope="cross_border",
                on_page=None,
                stop_event=None,
            )
        )
    assert page.keyword == "cosplay"
    assert page.url == "https://listado.mercadolibre.com.mx/cosplay"


def test_listing_dom_scripts_prefer_original_price_over_current_price():
    for script in (
        playwright_collector.LISTING_DOM_SCRIPT,
        collector._LISTING_PAGE_SCRIPT,
    ):
        assert ".andes-money-amount--previous" in script
        assert "const collectedPrice = originalPrice || currentPrice" in script
        assert "assetIsUsFlag" in script


def test_listing_and_detail_html_prefer_original_price():
    listing = parse_listing_html(
        """
        <li class="ui-search-layout__item">
          <a class="ui-search-link" href="/MLM-3016972321">Producto</a>
          <h2>Producto</h2>
          <s class="andes-money-amount andes-money-amount--previous">
            <span class="andes-money-amount__fraction">1,299</span>
            <span class="andes-money-amount__cents">90</span>
          </s>
          <div class="ui-search-price__second-line">
            <span class="andes-money-amount">
              <span class="andes-money-amount__fraction">999</span>
            </span>
          </div>
        </li>
        """,
        "https://listado.mercadolibre.com.mx/cosplay",
    )
    assert listing["rows"][0]["price"] == "1,299.90"

    detail = parse_detail_html(
        """
        <html><head><meta itemprop="price" content="999"></head><body>
          <h1 class="ui-pdp-title">Producto</h1>
          <s class="andes-money-amount andes-money-amount--previous">
            <span class="andes-money-amount__fraction">1,299</span>
            <span class="andes-money-amount__cents">90</span>
          </s>
        </body></html>
        """,
        "https://articulo.mercadolibre.com.mx/MLM-3016972321",
    )
    assert detail["price"] == "1,299.90"


def test_plugin_reader_scans_loaded_react_wrappers_not_only_stale_metric_lines():
    script = playwright_collector.PLUGIN_REACT_METRICS_SCRIPT

    assert "const reactNodes = []" in script
    assert "root.querySelectorAll('*')" in script
    assert "captureValue(directProps" in script


def test_detail_reader_attaches_before_navigation_context_is_created():
    import inspect

    source = inspect.getsource(playwright_collector._collect_detail)
    assert source.index("_PluginMetricReader.open(page)") < source.index(
        "await _goto(page, primary_url)"
    )
    assert "await _goto(page, fallback_url)" in source


def test_product_detail_wait_does_not_accept_generic_error_page_heading():
    class Page:
        def __init__(self):
            self.selector = ""

        async def wait_for_selector(self, selector, **_kwargs):
            self.selector = selector
            raise RuntimeError("not a product page")

    page = Page()
    ready = asyncio.run(
        playwright_collector._wait_for_product_detail(page, timeout=1)
    )

    assert ready is False
    assert "h1.ui-pdp-title" in page.selector
    assert ", h1" not in page.selector


def test_plugin_reader_waits_past_protected_placeholder_for_loaded_api_data(
    monkeypatch,
):
    class Reader:
        def __init__(self):
            self.calls = 0

        async def read(self):
            self.calls += 1
            if self.calls == 1:
                return {}
            return {
                "found": True,
                "metrics": {},
                "data": {
                    "weight_g": 196,
                    "size_cm": [12, 10, 6],
                    "volume_weight_g": 120,
                },
            }

    async def protected_lines(_page):
        return ["xutxutxut35s97l97l97kdzt"]

    async def no_sleep(_seconds):
        return None

    reader = Reader()
    monkeypatch.setattr(playwright_collector, "_plugin_dom_lines", protected_lines)
    monkeypatch.setattr(playwright_collector.asyncio, "sleep", no_sleep)

    metrics, _lines = asyncio.run(
        playwright_collector._wait_for_plugin_metrics(
            object(), 1, None, react_reader=reader
        )
    )

    assert reader.calls == 5
    assert metrics["weight_g"] == 196
    assert metrics["package_length_cm"] == 12


def test_default_and_legacy_edge_labels_use_playwright(monkeypatch):
    calls = []

    def fake_collect(source_url, requested_count, **kwargs):
        calls.append((source_url, requested_count, kwargs["max_workers"]))
        return {"browser_mode": "playwright", "rows": []}

    monkeypatch.setattr(
        playwright_collector, "collect_marketplace_listing_playwright", fake_collect
    )
    result = collector.collect_marketplace_listing(
        "https://listado.mercadolibre.com.mx/bolsas",
        2,
        browser_mode="edge_ui",
        max_workers=3,
    )

    assert collector.DEFAULT_BROWSER_MODE == "bitbrowser"
    assert result["browser_mode"] == "playwright"
    assert calls == [("https://listado.mercadolibre.com.mx/bolsas", 2, 3)]


def test_bitbrowser_mode_uses_parallel_playwright_connection(monkeypatch):
    calls = []

    def fake_collect(source_url, requested_count, **kwargs):
        calls.append(kwargs)
        return {"browser_mode": "playwright", "rows": []}

    monkeypatch.setattr(
        playwright_collector, "collect_marketplace_listing_playwright", fake_collect
    )
    collector.collect_marketplace_listing(
        "https://listado.mercadolibre.com.mx/cosplay",
        5,
        browser_mode="bitbrowser",
        window_id="window-123",
        max_workers=6,
    )

    assert calls[0]["window_id"] == "window-123"
    assert calls[0]["max_workers"] == 6


def test_playwright_plugin_reader_targets_zying_shadow_dom():
    assert ".zying-meli-detail-metric-line" in playwright_collector.SHADOW_PLUGIN_TEXT_SCRIPT
    assert "shadowRoot" in playwright_collector.SHADOW_PLUGIN_TEXT_SCRIPT
    assert "__reactFiber$" in playwright_collector.PLUGIN_REACT_METRICS_SCRIPT
    assert "chrome-extension://" in (
        f"chrome-extension://{playwright_collector.ZYING_EXTENSION_ID}"
    )


def test_playwright_plugin_reader_skips_empty_extension_world():
    class FakeSession:
        async def send(self, _method, payload):
            if payload["contextId"] == 2:
                value = {"found": True, "metrics": {}, "data": {}}
            else:
                value = {"found": True, "metrics": {"weight": {"value": "509g"}}, "data": {}}
            return {"result": {"value": value}}

    reader = playwright_collector._PluginMetricReader(FakeSession())
    reader.context_ids = [1, 2]

    result = asyncio.run(reader.read())

    assert result["metrics"]["weight"]["value"] == "509g"


def test_playwright_plugin_reader_discards_destroyed_contexts():
    class FakeSession:
        def __init__(self):
            self.handlers = {}

        def on(self, name, callback):
            self.handlers[name] = callback

        async def send(self, method, _payload=None):
            assert method == "Runtime.enable"
            return {}

        async def detach(self):
            return None

    class Context:
        def __init__(self, session):
            self.session = session

        async def new_cdp_session(self, _page):
            return self.session

    session = FakeSession()
    page = type("Page", (), {"context": Context(session)})()
    reader = asyncio.run(playwright_collector._PluginMetricReader.open(page))

    session.handlers["Runtime.executionContextCreated"]({
        "context": {"id": 11, "origin": "chrome-extension://zying"}
    })
    session.handlers["Runtime.executionContextCreated"]({
        "context": {"id": 12, "origin": "https://www.mercadolibre.com.mx"}
    })
    assert reader.context_ids == [11]
    session.handlers["Runtime.executionContextDestroyed"]({"executionContextId": 11})
    assert reader.context_ids == []
    reader.context_ids.extend([21, 22])
    session.handlers["Runtime.executionContextsCleared"]({})
    assert reader.context_ids == []


def test_plugin_metric_wait_releases_slot_after_actual_weight_arrives(monkeypatch):
    class Reader:
        def __init__(self):
            self.calls = 0

        async def read(self):
            self.calls += 1
            return {
                "found": True,
                "data": {"weight_g": 196},
                "metrics": {},
            }

    async def no_dom_lines(_page):
        return []

    async def no_sleep(_seconds):
        return None

    reader = Reader()
    monkeypatch.setattr(playwright_collector, "_plugin_dom_lines", no_dom_lines)
    monkeypatch.setattr(playwright_collector.asyncio, "sleep", no_sleep)

    metrics, _ = asyncio.run(
        playwright_collector._wait_for_plugin_metrics(
            object(), 1, None, react_reader=reader
        )
    )

    assert reader.calls == 4
    assert metrics["weight_g"] == 196
    assert metrics["package_length_cm"] is None


def test_plugin_metric_wait_keeps_reading_after_delayed_us_metadata(monkeypatch):
    class Reader:
        def __init__(self):
            self.calls = 0

        async def read(self):
            self.calls += 1
            data = {"weight_g": 196}
            if self.calls >= 3:
                data["self_ship_origin"] = "US"
            return {"found": True, "data": data, "metrics": {}}

    async def no_dom_lines(_page):
        return []

    async def no_sleep(_seconds):
        return None

    reader = Reader()
    monkeypatch.setattr(playwright_collector, "_plugin_dom_lines", no_dom_lines)
    monkeypatch.setattr(playwright_collector.asyncio, "sleep", no_sleep)

    metrics, lines = asyncio.run(
        playwright_collector._wait_for_plugin_metrics(
            object(), 1, None, react_reader=reader
        )
    )

    assert reader.calls == 4
    assert metrics["weight_g"] == 196
    assert playwright_collector._plugin_self_ship_origin(lines) == "US"


def test_playwright_detects_visually_protected_plugin_text():
    assert playwright_collector._plugin_text_is_visually_protected(
        ["lp3lp3lp435s97l97l97kdztxutxutxut"]
    )
    assert not playwright_collector._plugin_text_is_visually_protected(
        ["重量：509g", "尺寸：31 × 26 × 7"]
    )


def test_playwright_synthesizes_noindex_pagination_url():
    source = (
        "https://listado.mercadolibre.com.mx/"
        "cosplay_NoIndex_True_SHIPPING*ORIGIN_10215069"
    )
    assert playwright_collector._synthesized_listing_page_url(source, 2) == (
        "https://listado.mercadolibre.com.mx/"
        "cosplay_Desde_49_NoIndex_True_SHIPPING*ORIGIN_10215069"
    )
    assert "_Desde_97_" in playwright_collector._synthesized_listing_page_url(
        source, 3
    )


def test_playwright_decodes_plugin_metrics_from_extension_react_props():
    metrics, lines = playwright_collector._metrics_from_react_payload(
        {
            "found": True,
            "data": {
                "weight_g": 509,
                "size_cm": [31, 26, 7],
                "volume_weight_g": 940.3333333333334,
                "self_ship_origin": "CN",
            },
            "metrics": {
                "weight": {"value": "509g"},
                "size": {"value": "31 × 26 × 7", "sub_value": "（计抛940g）"},
            },
        }
    )

    assert metrics["weight_g"] == 509
    assert metrics["package_length_cm"] == 31
    assert metrics["package_width_cm"] == 26
    assert metrics["package_height_cm"] == 7
    assert metrics["volumetric_weight_kg"] == pytest.approx(0.9403)
    assert lines == [
        "重量：509g",
        "尺寸：31 × 26 × 7",
        "计抛：940.333g",
        "__ZYING_SELF_SHIP_ORIGIN__:CN",
    ]
    assert playwright_collector._plugin_self_ship_origin(lines) == "CN"
    assert playwright_collector._plugin_self_ship_origin(
        ["chrome-extension://example/assets/US.svg"]
    ) == "US"
    assert playwright_collector._plugin_self_ship_origin([
        "__ZYING_SELF_SHIP_ORIGIN__:CN",
        "__ZYING_SELF_SHIP_ORIGIN__:US",
    ]) == "US"


def test_keyword_containing_item_like_text_is_not_treated_as_detail_url():
    assert playwright_collector._detail_item_id_from_url(
        "https://listado.mercadolibre.com.mx/MLM3016972321-cosplay"
    ) == ""
    assert playwright_collector._detail_item_id_from_url(
        "https://www.mercadolibre.com.mx/producto/p/MLM3016972321"
    ) == "MLM3016972321"


def test_playwright_skips_us_self_ship_without_persisting_or_retrying(monkeypatch):
    monkeypatch.setenv("MERCADO_PLAYWRIGHT_NAVIGATION_STAGGER_SECONDS", "0")
    monkeypatch.setenv("MERCADO_PLAYWRIGHT_RETRY_SECONDS", "0")
    candidates = [
        {
            "source_item_id": "MLM3016972321",
            "source_url": "https://example.test/us",
            "title": "US item",
        },
        {
            "source_item_id": "MLM3016972322",
            "source_url": "https://example.test/unknown",
            "title": "Unknown item",
        },
        {
            "source_item_id": "MLM3016972323",
            "source_url": "https://example.test/cn",
            "title": "CN item",
        },
    ]
    runtime = type("Runtime", (), {"connection_mode": "test"})()
    attempts = []
    saved = []

    async def fake_open():
        return runtime

    async def fake_close(_runtime):
        return None

    async def fake_candidates(*_args, **_kwargs):
        return candidates

    async def fake_detail(_runtime, candidate, **_kwargs):
        attempts.append(candidate["source_item_id"])
        if candidate["source_item_id"].endswith("321"):
            raise playwright_collector.USSelfShipSkipped("US.svg")
        if candidate["source_item_id"].endswith("322"):
            raise playwright_collector.UnverifiedChinaSelfShipSkipped("missing CN.svg")
        return {**candidate, "scrape_status": "ok", "main_image_url": "image"}

    monkeypatch.setattr(playwright_collector, "_open_runtime", fake_open)
    monkeypatch.setattr(playwright_collector, "_close_runtime", fake_close)
    monkeypatch.setattr(playwright_collector, "_listing_candidates", fake_candidates)
    monkeypatch.setattr(playwright_collector, "_collect_detail", fake_detail)

    result = asyncio.run(
        playwright_collector._collect_async(
            "https://example.test/list",
            3,
            max_workers=2,
            plugin_timeout=1,
            on_page=None,
            on_item=saved.append,
            on_progress=None,
            stop_event=None,
        )
    )

    assert attempts.count("MLM3016972321") == 1
    assert result["completed_count"] == 1
    assert result["failed_count"] == 0
    assert result["skipped_us_count"] == 1
    assert result["skipped_unverified_origin_count"] == 1
    assert [row["source_item_id"] for row in result["rows"]] == ["MLM3016972323"]
    assert [row["source_item_id"] for row in saved] == ["MLM3016972323"]


def test_playwright_retries_one_transient_detail_failure(monkeypatch):
    monkeypatch.setenv("MERCADO_PLAYWRIGHT_NAVIGATION_STAGGER_SECONDS", "0")
    monkeypatch.setenv("MERCADO_PLAYWRIGHT_RETRY_SECONDS", "0")
    candidate = {
        "source_item_id": "MLM3016972321",
        "source_url": "https://articulo.mercadolibre.com.mx/MLM-3016972321",
        "title": "Producto",
    }
    runtime = type("Runtime", (), {"connection_mode": "test"})()
    calls = []

    async def fake_open():
        return runtime

    async def fake_close(_runtime):
        return None

    async def fake_candidates(*args, **kwargs):
        return [candidate]

    async def fake_detail(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary navigation failure")
        return {**candidate, "scrape_status": "ok", "main_image_url": "image"}

    monkeypatch.setattr(playwright_collector, "_open_runtime", fake_open)
    monkeypatch.setattr(playwright_collector, "_close_runtime", fake_close)
    monkeypatch.setattr(playwright_collector, "_listing_candidates", fake_candidates)
    monkeypatch.setattr(playwright_collector, "_collect_detail", fake_detail)

    result = asyncio.run(
        playwright_collector._collect_async(
            candidate["source_url"],
            1,
            max_workers=1,
            plugin_timeout=1,
            on_page=None,
            on_item=None,
            on_progress=None,
            stop_event=None,
        )
    )

    assert len(calls) == 2
    assert result["completed_count"] == 1


def test_playwright_verification_block_uses_serial_recovery_probes(monkeypatch):
    monkeypatch.setenv("MERCADO_PLAYWRIGHT_NAVIGATION_STAGGER_SECONDS", "0")
    monkeypatch.setenv("MERCADO_PLAYWRIGHT_VERIFICATION_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("MERCADO_PLAYWRIGHT_VERIFICATION_MAX_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("MERCADO_PLAYWRIGHT_VERIFICATION_RECOVERY_SUCCESSES", "2")
    candidates = [
        {
            "source_item_id": f"MLM301697232{index}",
            "source_url": f"https://example.test/{index}",
            "title": f"Producto {index}",
        }
        for index in range(1, 4)
    ]
    runtime = type("Runtime", (), {"connection_mode": "test"})()
    first_wave_ready = asyncio.Event()
    first_wave_calls = 0
    recovery_active = 0
    max_recovery_active = 0
    attempts = {}

    async def fake_open():
        return runtime

    async def fake_close(_runtime):
        return None

    async def fake_candidates(*args, **kwargs):
        return candidates

    async def fake_detail(_runtime, candidate, **kwargs):
        nonlocal first_wave_calls, recovery_active, max_recovery_active
        item_id = candidate["source_item_id"]
        attempts[item_id] = attempts.get(item_id, 0) + 1
        if attempts[item_id] == 1:
            first_wave_calls += 1
            if first_wave_calls == len(candidates):
                first_wave_ready.set()
            await asyncio.wait_for(first_wave_ready.wait(), timeout=1)
            raise RuntimeError("Mercado 页面进入买家验证页 account-verification")
        recovery_active += 1
        max_recovery_active = max(max_recovery_active, recovery_active)
        await asyncio.sleep(0)
        recovery_active -= 1
        return {**candidate, "scrape_status": "ok", "main_image_url": "image"}

    monkeypatch.setattr(playwright_collector, "_open_runtime", fake_open)
    monkeypatch.setattr(playwright_collector, "_close_runtime", fake_close)
    monkeypatch.setattr(playwright_collector, "_listing_candidates", fake_candidates)
    monkeypatch.setattr(playwright_collector, "_collect_detail", fake_detail)

    result = asyncio.run(
        playwright_collector._collect_async(
            "https://listado.mercadolibre.com.mx/cardgame",
            3,
            max_workers=3,
            plugin_timeout=1,
            on_page=None,
            on_item=None,
            on_progress=None,
            stop_event=None,
        )
    )

    assert result["completed_count"] == 3
    assert result["failed_count"] == 0
    assert max_recovery_active == 1
    assert set(attempts.values()) == {2}


def test_playwright_dynamic_pool_does_not_wait_for_slowest_batch_member(monkeypatch):
    monkeypatch.setenv("MERCADO_PLAYWRIGHT_NAVIGATION_STAGGER_SECONDS", "0")
    candidates = [
        {
            "source_item_id": f"MLM301697232{index}",
            "source_url": f"https://example.test/{index}",
            "title": f"Producto {index}",
        }
        for index in range(1, 4)
    ]
    runtime = type("Runtime", (), {"connection_mode": "test"})()
    third_started = asyncio.Event()

    async def fake_open():
        return runtime

    async def fake_close(_runtime):
        return None

    async def fake_candidates(*args, **kwargs):
        return candidates

    async def fake_detail(_runtime, candidate, **kwargs):
        if candidate["source_item_id"].endswith("1"):
            await asyncio.wait_for(third_started.wait(), timeout=1)
        elif candidate["source_item_id"].endswith("3"):
            third_started.set()
        return {**candidate, "scrape_status": "ok", "main_image_url": "image"}

    monkeypatch.setattr(playwright_collector, "_open_runtime", fake_open)
    monkeypatch.setattr(playwright_collector, "_close_runtime", fake_close)
    monkeypatch.setattr(playwright_collector, "_listing_candidates", fake_candidates)
    monkeypatch.setattr(playwright_collector, "_collect_detail", fake_detail)

    result = asyncio.run(
        asyncio.wait_for(
            playwright_collector._collect_async(
                "https://listado.mercadolibre.com.mx/cosplay",
                3,
                max_workers=2,
                plugin_timeout=1,
                on_page=None,
                on_item=None,
                on_progress=None,
                stop_event=None,
            ),
            timeout=2,
        )
    )

    assert result["completed_count"] == 3


def test_playwright_listing_direct_mode_opens_no_detail_pages(monkeypatch):
    candidate = {
        "source_item_id": "MLM9000000001",
        "source_url": "https://articulo.mercadolibre.com.mx/MLM-9000000001",
        "listing_url": "https://www.mercadolibre.com.mx/producto/p/MLMU1?pdp_filters=item_id:MLM9000000001",
        "title": "Producto directo",
        "main_image_url": "https://http2.mlstatic.com/image.jpg",
        "price": 1299,
        "currency_id": "MXN",
        "_plugin_data": {"id": "MLM9000000001", "weight_g": 196, "size_cm": [10, 20, 30]},
        "_direct_detail_ready": True,
    }
    runtime = type("Runtime", (), {"connection_mode": "test", "pages": []})()

    async def fake_open():
        return runtime

    async def fake_close(_runtime):
        return None

    async def fake_candidates(*_args, **_kwargs):
        return [candidate]

    async def fail_open_pool(*_args, **_kwargs):
        raise AssertionError("列表直读模式不应创建详情页池")

    monkeypatch.setattr(playwright_collector, "_open_runtime", fake_open)
    monkeypatch.setattr(playwright_collector, "_close_runtime", fake_close)
    monkeypatch.setattr(playwright_collector, "_listing_candidates", fake_candidates)
    monkeypatch.setattr(playwright_collector, "_open_detail_page_pool", fail_open_pool)

    result = asyncio.run(
        playwright_collector._collect_async(
            "https://listado.mercadolibre.com.mx/cosplay",
            1,
            max_workers=6,
            plugin_timeout=4,
            on_page=None,
            on_item=None,
            on_progress=None,
            stop_event=None,
        )
    )

    assert result["detail_mode"] == "listing_page_batch"
    assert result["completed_count"] == 1
    assert result["rows"][0]["weight_g"] == 196
    assert result["rows"][0]["page_snapshot"]["detail_acquisition"] == "listing_page_batch"


def test_playwright_reuses_preheated_detail_pages_across_items(monkeypatch):
    monkeypatch.setenv("MERCADO_PLAYWRIGHT_NAVIGATION_STAGGER_SECONDS", "0")
    candidates = [
        {
            "source_item_id": f"MLM900000000{index}",
            "source_url": f"https://example.test/{index}",
            "title": f"Producto {index}",
        }
        for index in range(4)
    ]
    created_pages = []
    used_pages = []

    class Page:
        def __init__(self):
            self.closed = False

        def set_default_timeout(self, _timeout):
            return None

        async def route(self, *_args):
            return None

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    class Context:
        async def new_page(self):
            page = Page()
            created_pages.append(page)
            return page

    class Reader:
        async def close(self):
            return None

    runtime = type(
        "Runtime",
        (),
        {"connection_mode": "test", "context": Context(), "pages": []},
    )()

    async def fake_open():
        return runtime

    async def fake_close(_runtime):
        return None

    async def fake_candidates(*_args, **_kwargs):
        return candidates

    async def fake_reader_open(_page):
        return Reader()

    async def fake_detail(_runtime, candidate, **kwargs):
        used_pages.append(kwargs["page"])
        await asyncio.sleep(0)
        return {**candidate, "scrape_status": "ok", "main_image_url": "image"}

    monkeypatch.setattr(playwright_collector, "_open_runtime", fake_open)
    monkeypatch.setattr(playwright_collector, "_close_runtime", fake_close)
    monkeypatch.setattr(playwright_collector, "_listing_candidates", fake_candidates)
    monkeypatch.setattr(
        playwright_collector._PluginMetricReader, "open", fake_reader_open
    )
    monkeypatch.setattr(playwright_collector, "_collect_detail", fake_detail)

    result = asyncio.run(
        playwright_collector._collect_async(
            "https://listado.mercadolibre.com.mx/cosplay",
            4,
            max_workers=2,
            plugin_timeout=1,
            on_page=None,
            on_item=None,
            on_progress=None,
            stop_event=None,
        )
    )

    assert result["completed_count"] == 4
    assert len(created_pages) == 2
    assert len({id(page) for page in used_pages}) == 2
    assert all(page.closed for page in created_pages)


def test_playwright_quality_repair_stops_after_repeated_no_hit(monkeypatch):
    monkeypatch.setenv("MERCADO_PLAYWRIGHT_REPAIR_FAILURE_LIMIT", "3")
    candidates = [
        {
            "source_item_id": f"MLM30169723{index:02d}",
            "source_url": f"https://example.test/{index}",
            "scrape_status": "partial",
        }
        for index in range(10)
    ]
    runtime = type("Runtime", (), {"connection_mode": "test"})()
    calls = []

    async def fake_open(_window_id):
        return runtime

    async def fake_close(_runtime):
        return None

    async def fake_detail(_runtime, candidate, **_kwargs):
        calls.append(candidate["source_item_id"])
        return {**candidate, "scrape_status": "partial", "error_message": "no metrics"}

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(playwright_collector, "_open_runtime", fake_open)
    monkeypatch.setattr(playwright_collector, "_close_runtime", fake_close)
    monkeypatch.setattr(playwright_collector, "_collect_detail", fake_detail)
    monkeypatch.setattr(playwright_collector.asyncio, "sleep", fake_sleep)

    result = asyncio.run(
        playwright_collector._repair_items_async(
            candidates,
            window_id="window",
            plugin_timeout=1,
            attempts=1,
            on_item=None,
            on_progress=None,
            stop_event=None,
        )
    )

    assert len(calls) == 3
    assert result["attempted_count"] == 3
    assert result["skipped_count"] == 7


def test_playwright_reopens_browser_before_retrying_closed_context(monkeypatch):
    candidate = {
        "source_item_id": "MLM3016972321",
        "source_url": "https://articulo.mercadolibre.com.mx/MLM-3016972321",
        "title": "Producto",
    }
    runtimes = [
        type("Runtime", (), {"connection_mode": "first"})(),
        type("Runtime", (), {"connection_mode": "second"})(),
    ]
    open_calls = []
    close_calls = []
    detail_calls = []

    async def fake_open():
        runtime = runtimes[len(open_calls)]
        open_calls.append(runtime)
        return runtime

    async def fake_close(runtime):
        close_calls.append(runtime)

    async def fake_candidates(*args, **kwargs):
        return [candidate]

    async def fake_detail(runtime, *args, **kwargs):
        detail_calls.append(runtime)
        if runtime is runtimes[0]:
            raise RuntimeError(
                "BrowserContext.new_page: Target page, context or browser has been closed"
            )
        return {**candidate, "scrape_status": "ok", "main_image_url": "image"}

    monkeypatch.setattr(playwright_collector, "_open_runtime", fake_open)
    monkeypatch.setattr(playwright_collector, "_close_runtime", fake_close)
    monkeypatch.setattr(playwright_collector, "_listing_candidates", fake_candidates)
    monkeypatch.setattr(playwright_collector, "_collect_detail", fake_detail)

    result = asyncio.run(
        playwright_collector._collect_async(
            candidate["source_url"],
            1,
            max_workers=1,
            plugin_timeout=1,
            on_page=None,
            on_item=None,
            on_progress=None,
            stop_event=None,
        )
    )

    assert open_calls == runtimes
    assert detail_calls == runtimes
    assert close_calls == runtimes
    assert result["completed_count"] == 1
    assert result["failed_count"] == 0


def test_playwright_retries_evaluate_when_navigation_destroys_context():
    class FakePage:
        def __init__(self):
            self.evaluate_calls = 0
            self.load_calls = 0

        def is_closed(self):
            return False

        async def wait_for_load_state(self, *args, **kwargs):
            self.load_calls += 1

        async def evaluate(self, script):
            self.evaluate_calls += 1
            if self.evaluate_calls == 1:
                raise RuntimeError(
                    "Page.evaluate: Execution context was destroyed, most likely because of a navigation"
                )
            return {"rows": [1]}

    page = FakePage()
    result = asyncio.run(
        playwright_collector._evaluate_after_navigation(page, "() => ({rows: [1]})")
    )

    assert result == {"rows": [1]}
    assert page.evaluate_calls == 2
    assert page.load_calls == 1


def test_playwright_does_not_hide_non_navigation_evaluate_errors():
    class FakePage:
        def is_closed(self):
            return False

        async def wait_for_load_state(self, *args, **kwargs):
            return None

        async def evaluate(self, script):
            raise RuntimeError("JavaScript syntax error")

    with pytest.raises(RuntimeError, match="syntax error"):
        asyncio.run(
            playwright_collector._evaluate_after_navigation(FakePage(), "invalid")
        )


def test_playwright_goto_rejects_timeout_when_same_document_is_still_loaded():
    class FakePage:
        url = "https://www.mercadolibre.com.mx/producto"
        marker = ""

        async def evaluate(self, _script, marker=None):
            if marker is not None:
                self.marker = marker
                return None
            return self.marker

        async def goto(self, *args, **kwargs):
            raise RuntimeError("Page.goto: Timeout 15000ms exceeded")

        async def wait_for_load_state(self, *args, **kwargs):
            raise RuntimeError("still navigating")

    with pytest.raises(RuntimeError, match="Timeout"):
        asyncio.run(
            playwright_collector._goto(
                FakePage(), "https://www.mercadolibre.com.mx/producto"
            )
        )


def test_playwright_goto_accepts_timeout_after_reaching_matching_new_url():
    class FakePage:
        url = "https://www.mercadolibre.com.mx/old"

        async def evaluate(self, *_args):
            return None

        async def goto(self, url, **_kwargs):
            self.url = url
            raise RuntimeError("Page.goto: Timeout 15000ms exceeded")

        async def wait_for_load_state(self, *args, **kwargs):
            raise RuntimeError("still navigating")

    asyncio.run(
        playwright_collector._goto(
            FakePage(), "https://www.mercadolibre.com.mx/producto"
        )
    )


def test_playwright_goto_keeps_timeout_when_page_is_still_blank():
    class FakePage:
        url = "about:blank"

        async def goto(self, *args, **kwargs):
            raise RuntimeError("Page.goto: Timeout 15000ms exceeded")

        async def wait_for_load_state(self, *args, **kwargs):
            raise RuntimeError("still blank")

    with pytest.raises(RuntimeError, match="Timeout"):
        asyncio.run(
            playwright_collector._goto(
                FakePage(), "https://www.mercadolibre.com.mx/producto"
            )
        )


def test_listing_pagination_continues_after_page_with_no_new_items(monkeypatch):
    class FakeDriver:
        def __init__(self):
            self.current_url = ""
            self.snapshots = {
                "https://listado.mercadolibre.com.mx/page-1": {
                    "rows": [],
                    "next_url": "https://listado.mercadolibre.com.mx/page-2",
                    "body": "lista",
                },
                "https://listado.mercadolibre.com.mx/page-2": {
                    "rows": [{
                        "href": "https://articulo.mercadolibre.com.mx/MLM-3016972321",
                        "title": "Producto de pagina dos",
                    }],
                    "next_url": "",
                    "body": "lista",
                },
            }

        def get(self, url):
            self.current_url = url

        def execute_script(self, script):
            if script == "return document.readyState":
                return "complete"
            return self.snapshots[self.current_url]

    monkeypatch.setattr(collector.time, "sleep", lambda _seconds: None)
    pages = []
    rows = collector._collect_listing_pages(
        FakeDriver(),
        "https://listado.mercadolibre.com.mx/page-1",
        1,
        on_page=pages.append,
    )

    assert [page["page"] for page in pages] == [1, 2]
    assert rows[0]["source_item_id"] == "MLM3016972321"


def test_listing_us_rows_are_not_reintroduced_by_detail_fallback(monkeypatch):
    class FakeDriver:
        current_url = ""

        def get(self, url):
            self.current_url = url

        def execute_script(self, script):
            if script == "return document.readyState":
                return "complete"
            if "const h1" in script:
                return {"title": "USA detail", "main_image_url": "image"}
            return {
                "rows": [{
                    "href": "https://articulo.mercadolibre.com.mx/MLM-3016972342",
                    "title": "USA flag",
                    "shipping_origin_country": "US",
                }],
                "next_url": "",
                "body": "lista",
            }

    monkeypatch.setattr(collector.time, "sleep", lambda _seconds: None)
    rows = collector._collect_listing_pages(
        FakeDriver(),
        "https://listado.mercadolibre.com.mx/search?item_id=MLM3016972342",
        1,
    )

    assert rows == []


def test_parse_edge_page_source_for_cards_and_detail_fields():
    listing = parse_listing_html(
        """
        <html><body><li class="ui-search-layout__item">
          <a class="ui-search-link" href="/MLM-3016972321-producto">
            <h2>Producto Edge</h2><img data-src="//http2.mlstatic.com/item.jpg">
          </a><span class="andes-money-amount__fraction">151</span>
        </li><a title="Siguiente" href="/pagina-2">next</a></body></html>
        """,
        "https://listado.mercadolibre.com.mx/bolsas",
    )
    assert listing["rows"][0]["title"] == "Producto Edge"
    assert listing["next_url"] == "https://listado.mercadolibre.com.mx/pagina-2"

    detail = parse_detail_html(
        """
        <html><head><link rel="canonical" href="https://www.mercadolibre.com.mx/p/MLM1">
        <meta property="product:price:amount" content="151.38">
        <meta property="product:price:currency" content="MXN">
        <meta property="og:image" content="https://http2.mlstatic.com/item.jpg"></head>
        <body><h1>Producto Edge</h1><div class="ui-pdp-description__content">Descripción</div></body></html>
        """,
        "https://articulo.mercadolibre.com.mx/MLM-3016972321",
    )
    assert detail["title"] == "Producto Edge"
    assert detail["price"] == "151.38"
    assert detail["pictures"][0].endswith("item.jpg")
