"""Source labels and source filters use the same persisted product types."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize("width", [1440, 390])
def test_ai_original_source_label_and_filter(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    rows = [
        {"id": index + 1, "source_item_id": f"SOURCE-{index}",
         "title": label, "source_type": source}
        for index, (source, label) in enumerate([
            ("collected", "美客多采集产品"), ("pulled", "店铺同步产品"),
            ("zying", "智赢采集产品"), ("ai_original", "1688 原创候选产品"),
        ])
    ]
    requests = []

    def products(route):
        query = parse_qs(urlsplit(route.request.url).query)
        requests.append(query)
        source = query.get("source_type", [""])[0]
        result = [row for row in rows if not source or row["source_type"] == source]
        route.fulfill(content_type="application/json", body=json.dumps({
            "status": "success", "data": {"rows": result, "total": len(result)},
        }))

    page.route("**/api/mercado-products?*", products)
    page.evaluate("""() => {
        currentWorkbenchUser.permissions = ['*'];
        switchTab('mercado-products');
    }""")
    page.wait_for_function("mercadoListRows.length === 4")
    labels = page.locator("#mercado-list-body .market-source-tag strong")
    assert labels.all_text_contents() == ["美客多采集", "店铺同步", "智赢采集", "AI 原创"]
    assert "其他来源" not in page.locator("#mercado-list-body").inner_text()
    page.evaluate("""() => {
        mercadoListPage = 3;
        selectedMercadoCollectionIds.add(1);
        mercadoSelectAll.checked = true;
    }""")
    button = page.locator("#mercado-source-ai-original")
    button.click()
    page.wait_for_function("mercadoListRows.length === 1 && mercadoListRows[0].source_type === 'ai_original'")
    assert requests[-1]["source_type"] == ["ai_original"]
    assert requests[-1]["offset"] == ["0"]
    assert "active" in button.get_attribute("class").split()
    assert page.locator(".market-source-option.active").count() == 1
    assert page.evaluate("selectedMercadoCollectionIds.size") == 0
    assert labels.all_text_contents() == ["AI 原创"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    artifacts = ROOT / "artifacts/product-source-fix-20261009"
    artifacts.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifacts / f"products-{width}.png"), full_page=True)
    page.locator("#mercado-source-all").click()
    page.wait_for_function("mercadoListRows.length === 4")
    assert "source_type" not in requests[-1]
