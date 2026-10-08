"""Offline product-card visual QA, using the console browser fixtures.

Run from the repository root:
    python -m pytest -q artifacts/product-list-cards-20261007/test_product_cards_qa.py

Every request is fulfilled by a local route. The products and their SVG main
images are synthetic; no database, account, or real business API is contacted.
"""

import json
import sys
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import pytest

OUTPUT = Path(__file__).resolve().parent
ROOT = OUTPUT.parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_console_ui_browser import console_browser, console_page  # noqa: E402,F401


PRODUCTS = [
    (101, "都市轻量双肩背包 · 多口袋防泼水款", "backpack", "zying", 29.90, 14.62, 320, "户外 / 出行", "陈晓"),
    (102, "陶瓷咖啡杯 350ml · 奶油白", "mug", "collected", 16.80, 6.35, 260, "家居 / 餐厨", "林悦"),
    (103, "LED 阅读台灯 · 可调节暖光", "lamp", "pulled", 42.50, 21.80, 450, "家居 / 照明", "陈晓"),
    (104, "可折叠棉绳收纳篮 · 自然色大容量", "basket", "zying", 24.60, -1.20, 280, "家居 / 收纳", "周宁"),
    (105, "女士宽松短袖 T 恤 · 柔软棉质基础款", "shirt", "zying", 19.80, 8.40, 150, "服饰 / 女装", "林悦"),
    (106, "头戴式无线耳机 · 雾蓝色", "headphones", "collected", 35.90, None, 210, "数码 / 音频", "周宁"),
]

SHAPES = {
    "backpack": '<path d="M83 78h34v18H83z" fill="none" stroke="#52675f" stroke-width="9"/><rect x="53" y="91" width="94" height="118" rx="29" fill="#678d79"/><path d="M70 110h60v31H70z" fill="#789d88"/><rect x="68" y="151" width="64" height="41" rx="10" fill="#4e7362"/><path d="M43 124v69m114-69v69" stroke="#617666" stroke-width="9" stroke-linecap="round"/><path d="M81 105h39" stroke="#e9eddf" stroke-width="3"/>',
    "mug": '<path d="M131 116h14c36 0 36 56 0 56h-15" fill="none" stroke="#c6b895" stroke-width="15"/><path d="M53 102h86v73c0 29-86 29-86 0z" fill="#e5d6b1"/><ellipse cx="96" cy="102" rx="43" ry="12" fill="#f4e8cf"/><ellipse cx="96" cy="102" rx="34" ry="7" fill="#8d6e50"/><path d="M76 77c-9-14 6-20 0-34m31 34c-8-12 7-19 0-34" fill="none" stroke="#d5ccb9" stroke-width="5" stroke-linecap="round"/>',
    "lamp": '<ellipse cx="103" cy="208" rx="49" ry="11" fill="#64767b"/><path d="M105 201v-48l-32-49" fill="none" stroke="#71868b" stroke-width="11" stroke-linecap="round"/><path d="M81 94l49-40" fill="none" stroke="#71868b" stroke-width="9" stroke-linecap="round"/><path d="M112 48c30-19 58 12 57 39l-70-12z" fill="#849ca1"/><path d="M101 79l62 15" stroke="#f9ebbe" stroke-width="8" stroke-linecap="round"/><circle cx="77" cy="101" r="9" fill="#a9b6b6"/>',
    "basket": '<path d="M46 99h108l-9 105H55z" fill="#c5b499"/><ellipse cx="100" cy="99" rx="54" ry="14" fill="#d9cbb3"/><ellipse cx="100" cy="99" rx="45" ry="9" fill="#98866f"/><path d="M58 120h84m-85 13h84m-83 13h82m-81 13h80m-79 13h78m-77 13h76m-75 13h74" stroke="#e2d5be" stroke-width="6"/><path d="M53 102L35 87v30l17 12m95-27l18-15v30l-17 12" fill="none" stroke="#a49275" stroke-width="7"/>',
    "shirt": '<path d="M72 64l-37 26-16 39 35 13 14-23v92h64v-92l14 23 35-13-16-39-37-26c-7 20-49 20-56 0z" fill="#aaabbc"/><path d="M76 65c4 18 43 18 48 0" fill="none" stroke="#83869b" stroke-width="6"/><path d="M74 91v107m52-107v107" stroke="#bfc0d0" stroke-width="3" opacity=".65"/>',
    "headphones": '<path d="M47 151v-43c0-72 106-72 106 0v43" fill="none" stroke="#668eaa" stroke-width="18"/><path d="M48 111c0-62 104-62 104 0" fill="none" stroke="#a0bfcc" stroke-width="6"/><rect x="32" y="124" width="36" height="72" rx="17" fill="#507d9b"/><rect x="132" y="124" width="36" height="72" rx="17" fill="#507d9b"/><rect x="55" y="132" width="16" height="56" rx="8" fill="#a0bfcc"/><rect x="129" y="132" width="16" height="56" rx="8" fill="#a0bfcc"/>',
}


def svg_image(name):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="240" height="280" viewBox="0 0 200 240">'
            '<rect width="200" height="240" rx="16" fill="#f5f4ef"/>'
            '<ellipse cx="100" cy="215" rx="63" ry="8" fill="#deded5" opacity=".5"/>'
            + SHAPES[name] + '</svg>')


def sample_rows():
    rows = []
    for index, (item_id, title, image, source, price, net, weight, category, developer) in enumerate(PRODUCTS):
        rows.append({
            "id": item_id, "title": title, "source_type": source,
            "source_item_id": f"ZY{item_id}06218" if source == "zying" else f"MLM{item_id}06218",
            "source_url": f"http://console.test/sample/product/{item_id}",
            "main_image_url": f"http://console.test/__qa/main-images/{image}.svg",
            "salesperson": ["泽顺 · 张敏", "泽顺 · 李航", "泽顺 · 王琳"][index % 3],
            "product_developer_id": str(17 + index % 3), "product_developer_name": developer,
            "zying_category": category, "zying_category_id": str(index + 1), "zying_status": "在售",
            "management_category_id": index % 3 + 1,
            "management_category_name": ["新品测试", "重点运营", "日常维护"][index % 3],
            "price": price * 19.6, "currency_id": "MXN", "sale_price_usd": price,
            "weight_g": weight, "weight_basis": "zying_actual",
            "package_length_cm": 24, "package_width_cm": 16, "package_height_cm": 32,
            "volumetric_weight_kg": 2.048, "commission_amount_usd": round(price * .15, 2),
            "commission_rate": 15, "category_name": category, "category_id": "MLM123456",
            "shipping_fee_usd": round(price - price * .15 - (net or 4), 2),
            "shipping_weight_rule": "actual_weight_only:official_global_selling_cainiao_rate_card",
            "profitability_source": "official", "net_proceeds_usd": net,
            "review_status": "unreviewed" if index == 0 else "approved",
            "last_publish_status": ["unpublished", "published", "pending", "failed", "unpublished", "unpublished"][index],
            "last_publish_store_name": "墨西哥精选店" if index == 1 else "",
            "last_published_item_id": "MLM77850621" if index == 1 else "",
            "last_publish_error": "缺少平台必填属性，请补齐后重试" if index == 3 else "",
            "collected_at": "2026-10-06 15:42:18", "added_at": "2026-10-07 09:18:06",
            "description_text": "适合日常生活使用。无品牌标识，包装尺寸与实际重量已登记。",
            "infringement": {"status": "completed", "risk_level": 1 if index == 3 else 0,
                             "detection_scope": "title_image", "image_method": "vision",
                             "reason": "已核对标题和主图" if index != 3 else "外观线索需人工复核",
                             "checked_at": "2026-10-07 08:30:00"},
        })
    return rows


def respond(route, data):
    route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))


@pytest.mark.parametrize("width", [1440, 1024, 390])
def test_offline_product_cards_layout_and_interactions(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    rows = sample_rows()
    mutations, errors, blocked_external = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def local_only(route):
        if urlsplit(route.request.url).hostname == "console.test":
            route.fallback()
        else:
            blocked_external.append(route.request.url)
            route.abort()

    page.route("**/*", local_only)
    page.route("**/__qa/main-images/*.svg", lambda route: route.fulfill(
        content_type="image/svg+xml", body=svg_image(Path(urlsplit(route.request.url).path).stem)))
    page.route("**/api/mercado-products?*", lambda route: respond(route, {"rows": rows, "total": len(rows)}))
    page.route("**/api/mercado-products/developers", lambda route: respond(route, {
        "rows": [{"id": str(17 + i), "name": name} for i, name in enumerate(["陈晓", "林悦", "周宁"])]}))
    page.route("**/api/mercado-management-categories", lambda route: respond(route, {
        "rows": [{"id": i + 1, "name": name, "item_count": 2}
                 for i, name in enumerate(["新品测试", "重点运营", "日常维护"])]}))

    def review(route):
        payload = route.request.post_data_json
        mutations.append({"kind": "review", "payload": payload})
        for row in rows:
            if row["id"] in payload["product_item_ids"]:
                row["review_status"] = payload["review_status"]
        respond(route, {"changed": len(payload["product_item_ids"])})

    def edit(route):
        payload = route.request.post_data_json
        mutations.append({"kind": "edit", "payload": payload})
        rows[0].update(payload)
        respond(route, {"product_item_id": rows[0]["id"]})

    page.route("**/api/mercado-products/review-status", review)
    page.route("**/api/mercado-products/101", edit)
    page.route("**/api/mercado/infringement/items/products/101", lambda route: respond(route, {
        "item": deepcopy(rows[0]), "infringement": deepcopy(rows[0]["infringement"]),
        "rules": [{"name": "离线检测证据", "reason": "样例主图未发现品牌标识"}], "history": [],
    }))
    page.evaluate("""() => {
        currentWorkbenchUser.permissions = ['*'];
        document.querySelector('.tab-button[data-tab="mercado-products"]').hidden = false;
        switchTab('mercado-products');
    }""")
    page.wait_for_function("mercadoListRows.length === 6 && mercadoCollectionLoaded")
    cards = page.locator("#mercado-list-body .market-product-card")
    assert cards.count() == 6
    # Scroll each lazy main image into view before checking its load and
    # producing screenshots, then restore the initial page position.
    for card in cards.all():
        card.scroll_into_view_if_needed()
        page.wait_for_function(
            "img => img.complete && img.naturalWidth > 0",
            arg=card.locator("img").element_handle(), timeout=10000,
        )
    page.evaluate("window.scrollTo(0, 0)")
    assert not page.locator("#mercado-list-head").is_visible()

    metrics = page.evaluate("""() => {
        const cards = [...document.querySelectorAll('#mercado-list-body .market-product-card')];
        const boxes = cards.map(el => { const box = el.getBoundingClientRect(); return {x: box.x, y: box.y, width: box.width, height: box.height}; });
        const firstRow = boxes.filter(box => Math.abs(box.y - boxes[0].y) < 3);
        const wrap = document.querySelector('#mercado-list-module .market-table-wrap');
        return {viewport: innerWidth, documentWidth: document.documentElement.scrollWidth,
                documentClientWidth: document.documentElement.clientWidth,
                listWidth: wrap.clientWidth, listScrollWidth: wrap.scrollWidth,
                columns: firstRow.length, cards: boxes,
                cardOverflow: cards.map(el => Math.max(0, el.scrollWidth - el.clientWidth))};
    }""")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # Preserve visual evidence even when the following layout check fails.
    (OUTPUT / f"layout-{width}.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")
    page.screenshot(path=str(OUTPUT / f"products-{width}.png"), full_page=True)
    page.locator("#mercado-list-body").screenshot(path=str(OUTPUT / f"cards-{width}.png"))
    assert metrics["documentWidth"] <= metrics["documentClientWidth"] + 1, metrics
    assert metrics["listScrollWidth"] <= metrics["listWidth"] + 1, metrics
    assert max(metrics["cardOverflow"]) <= 1, metrics
    assert metrics["columns"] == 1, metrics
    if width >= 900:
        assert max(box["height"] for box in metrics["cards"]) < 300, metrics

    first = cards.first
    first.locator(".mercado-item-select").check()
    assert page.evaluate("[...selectedMercadoCollectionIds]") == [101]
    assert "is-selected" in first.get_attribute("class").split()
    assert "已选择 1 件" in page.locator("#mercado-selection-summary").inner_text()
    page.locator("#mercado-select-all").check()
    assert page.evaluate("selectedMercadoCollectionIds.size") == 6
    page.locator("#mercado-select-all").uncheck()
    assert page.evaluate("selectedMercadoCollectionIds.size") == 0
    assert "is-selected" not in first.get_attribute("class").split()
    first.locator(".mercado-item-select").check()
    details = first.locator("details").first
    assert details.get_attribute("open") is None
    details.locator("summary").click()
    assert details.get_attribute("open") is not None
    assert "24 × 16 × 32 cm" in first.inner_text()
    assert "计费重量" in details.inner_text()
    first.screenshot(path=str(OUTPUT / f"expanded-card-{width}.png"))

    first.locator(".market-review-select").select_option("approved")
    page.wait_for_function("mercadoListRows[0]?.review_status === 'approved' && mercadoCollectionLoaded")
    assert mutations[0] == {"kind": "review", "payload": {"product_item_ids": [101], "review_status": "approved"}}
    first = page.locator('.market-product-card-row[data-product-id="101"] .market-product-card')
    first.locator(".market-product-edit-button").click()
    assert page.locator("#mercado-product-edit-dialog").is_visible()
    edited_title = rows[0]["title"] + " · 离线编辑验证"
    page.locator("#mercado-product-edit-title").fill(edited_title)
    page.locator("#mercado-product-edit-save").click()
    page.wait_for_function("!document.getElementById('mercado-product-edit-dialog').open && mercadoListRows[0]?.title?.includes('离线编辑验证') && mercadoCollectionLoaded")
    assert mutations[-1]["kind"] == "edit"
    assert mutations[-1]["payload"]["title"] == edited_title

    first.locator(".mi-risk-badge").click()
    page.wait_for_function("document.getElementById('mi-detail-content').textContent.includes('离线检测证据')")
    assert page.locator("#mi-detail-dialog").is_visible()
    page.locator('#mi-detail-dialog [data-mi-close="mi-detail-dialog"]').click()
    first.locator(".mi-row-actions button").filter(has_text="再检测").click()
    assert page.locator("#mi-start-dialog").is_visible()
    assert page.locator('input[name="mi-range"][value="selected"]').is_checked()
    assert page.locator('input[name="mi-range"][value="filtered"]').is_disabled()
    page.locator('#mi-start-dialog [data-mi-close="mi-start-dialog"]').first.click()
    first.locator(".mercado-item-select").check()
    page.locator(".zs-bulk-menu > summary").click()
    page.locator("#mercado-bulk-edit-selected").click()
    assert page.locator("#mercado-bulk-edit-dialog").is_visible()
    page.locator('#mercado-bulk-edit-dialog [aria-label="关闭批量产品编辑"]').click()
    page.evaluate("""() => {
        mercadoListRows[0].ai_copy = {title_es: 'Producto de prueba', title_pt: 'Produto de teste',
            description_es: 'Descripción de prueba', description_pt: 'Descrição de teste'};
        renderMercadoListRows(mercadoListRows);
    }""")
    first.locator(".market-card-details > summary").click()
    first.locator(".market-copy-preview-trigger").click()
    assert page.locator("#mercado-copy-preview").is_visible()
    assert "Descrição de teste" in page.locator("#mercado-copy-preview-content").inner_text()
    page.locator('#mercado-copy-preview [aria-label="关闭双语文案"]').click()
    assert not errors, errors
    assert not blocked_external, blocked_external
    page.evaluate("renderMercadoListRows([])")
    empty = page.locator("#mercado-list-body td.empty-state")
    assert empty.is_visible()
    assert "产品列表暂无商品" in empty.inner_text()
    empty_metrics = page.evaluate("""() => {
        const body = document.getElementById('mercado-list-body');
        const row = body.firstElementChild;
        return {bodyWidth: body.getBoundingClientRect().width,
                rowWidth: row.getBoundingClientRect().width,
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                overflowing: [...document.querySelectorAll('body *')].map(el => ({
                    name: el.id || el.className || el.tagName, right: el.getBoundingClientRect().right
                })).filter(box => box.right > innerWidth + 1).slice(0, 20)};
    }""")
    assert abs(empty_metrics["bodyWidth"] - empty_metrics["rowWidth"]) <= 1, empty_metrics
    assert empty_metrics["scrollWidth"] <= empty_metrics["clientWidth"] + 1, json.dumps(empty_metrics, ensure_ascii=False)
    metrics["emptyState"] = empty_metrics
    metrics["interactionChecks"] = ["single_selection", "selected_card_style", "select_all", "expand_details", "review_update", "edit_save", "risk_detail", "single_item_detection", "bulk_edit_dialog", "bilingual_preview_dialog", "full_width_empty_state"]
    metrics["mutations"] = mutations
    metrics["pageErrors"] = errors
    (OUTPUT / f"metrics-{width}.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")
