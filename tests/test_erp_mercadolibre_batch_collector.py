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
        ]
    )

    assert [row["id"] for row in rows] == ["BRAND", "GENDER", "COMPOSITION"]


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


def test_collection_request_requires_mercado_host_and_bounded_count():
    assert validate_collection_request(
        "https://listado.mercadolibre.com.mx/bolsas", 25
    )[1] == 25
    with pytest.raises(ValueError, match="Mercado Libre"):
        validate_collection_request("https://example.test/products", 10)
    with pytest.raises(ValueError, match="1-500"):
        validate_collection_request("https://listado.mercadolibre.com.mx/bolsas", 501)

    assert normalize_collection_workers(8) == 8
    with pytest.raises(ValueError, match="1-8"):
        normalize_collection_workers(9)


def test_keyword_builds_country_frontend_search_urls():
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
            },
            {
                "href": "https://articulo.mercadolibre.com.mx/MLM-3016972322",
                "title": "Producto local",
                "is_cross_border": False,
            },
        ],
        20,
        collection_scope="cross_border",
    )

    assert [row["source_item_id"] for row in rows] == ["MLM3016972321"]
    assert rows[0]["is_cross_border"] is True
    assert rows[0]["listing_url"].endswith("/MLM-3016972321")


def test_listing_html_marks_international_seller_cards():
    listing = parse_listing_html(
        """
        <html><body><ol>
          <li class="ui-search-layout__item">
            <a class="ui-search-link" href="/MLM-3016972321">Bolsa</a>
            <h2>Bolsa</h2><span>China Internacional China</span>
          </li>
          <li class="ui-search-layout__item">
            <a class="ui-search-link" href="/MLM-3016972322">Local</a>
            <h2>Local</h2><span>Llega mañana</span>
          </li>
        </ol></body></html>
        """,
        "https://listado.mercadolibre.com.mx/bolsas",
    )

    assert [row["is_cross_border"] for row in listing["rows"]] == [True, False]


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
    assert lines == ["重量：509g", "尺寸：31 × 26 × 7", "计抛：940.333g"]


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
    assert page.load_calls == 2


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


def test_playwright_goto_continues_after_timeout_on_loaded_http_page():
    class FakePage:
        url = "https://www.mercadolibre.com.mx/producto"

        async def goto(self, *args, **kwargs):
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
