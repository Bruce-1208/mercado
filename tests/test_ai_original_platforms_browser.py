"""Exercise platform drafts in the real console with all network requests mocked."""
import copy
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from erp.ai_original_platforms import listing_profiles_catalog
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize("width", [1440, 390])
def test_platform_drafts_generate_edit_export_without_mercado_mutations(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    screenshots = ROOT / "artifacts" / "ai-original-platforms"
    screenshots.mkdir(parents=True, exist_ok=True)
    image = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='600' height='600'%3E%3Crect width='600' height='600' fill='white'/%3E%3Crect x='170' y='130' width='260' height='340' rx='30' fill='%23b9cce2'/%3E%3C/svg%3E"
    draft = {
        "target_platform": "amazon", "template_id": "features", "status": "completed",
        "title": "Metal Desk Organizer", "description": "Metal organizer for a tidy desk.",
        "item_highlights": "Three compartments for everyday stationery", "bullet_points": ["Three storage compartments", "Metal construction"],
        "main_image_url": image, "attributes": [{"name": "Material", "value_name": "Metal"}],
        "validation": {"errors": [], "warnings": ["核实品牌、GTIN、类目必填属性与商品主图"]},
    }
    row = {
        "id": 1, "title": "桌面金属收纳盒", "source_item_id": "168800001", "ai_status": "completed", "review_status": "approved",
        "title_es": "SOLO_MERCADO", "title_pt": "SOLO_MERCADO_PT", "main_image_url": "/mercado-only.jpg",
        "original_1688": {"title": "桌面金属收纳盒", "main_image_url": image, "source_1688_item_id": "168800001", "price": 18.5},
        "ai_original": {"main_image_url": "/mercado-only.jpg"},
        "marketplace_listings": {"amazon": draft, "yandex": {
            **draft, "target_platform": "yandex", "title": "Органайзер для стола из металла",
            "description": "Металлический органайзер с тремя отделениями.", "bullet_points": ["Три отделения"],
        }},
    }
    rows = [row, {**copy.deepcopy(row), "id": 2, "source_item_id": "168800002", "marketplace_listings": {}}]
    requests = []

    def api(route):
        request = route.request
        path = urlsplit(request.url).path
        body = request.post_data_json if request.method in {"POST", "PATCH"} else None
        requests.append((request.method, request.url, body))
        data = {}
        if path.endswith("/profiles"):
            data = listing_profiles_catalog()
        elif path.endswith("/listing"):
            row["marketplace_listings"][body["target_platform"]].update(body)
            data = row
        elif path.endswith("/export"):
            platform = parse_qs(urlsplit(request.url).query)["target_platform"][0]
            route.fulfill(content_type="application/json", body=json.dumps(row["marketplace_listings"][platform]))
            return
        elif path.endswith("/process/status"):
            data = {"status": "idle", "running": False}
        elif path.endswith("/process"):
            data = {"status": "idle", "running": False, "target_platform": body["target_platform"]}
        elif path == "/api/ai-original-products":
            data = {"rows": rows}
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))

    page.route(re.compile(r".*/api/ai-original-products(?:[/?].*)?$"), api)
    page.evaluate("""async () => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        await loadAiOriginalProfiles();
    }""")
    page.locator("#ai-original-platform").select_option("amazon")
    page.wait_for_function("aiOriginalRows.length === 2 && document.getElementById('ai-original-count-approved').textContent === '1'")
    assert not page.locator(".ai-original-publish-panel").is_visible()
    assert not page.locator("#ai-original-review-submit").is_visible()
    assert page.locator("#ai-original-count-approved").inner_text() == "1"
    assert "SOLO_MERCADO" not in page.locator("#ai-original-grid").inner_text()
    assert "/mercado-only.jpg" not in page.locator("#ai-original-grid").inner_html()
    assert "俄语" not in page.locator("#ai-original-platform-rules").inner_text()
    page.locator("#ai-original-generator-filter").fill("业务员甲")
    page.locator("#ai-original-platform").select_option("yandex")
    page.wait_for_function("document.querySelector('.ai-original-copy').textContent.includes('Органайзер')")
    page.wait_for_timeout(50)
    assert any("target_platform=yandex" in url and "generator_salesperson=" in url for _, url, _ in requests)
    page.locator("#ai-original-platform").select_option("amazon")
    page.wait_for_function("document.querySelector('.ai-original-copy').textContent.includes('Metal Desk Organizer')")
    page.locator(".ai-original-draft-actions button").filter(has_text="编辑草稿").first.click()
    assert page.locator("#ai-original-platform-editor-title").get_attribute("maxlength") == "75"
    page.locator("#ai-original-platform-editor-title").fill("Metal Desk Organizer with Three Compartments")
    page.locator("#ai-original-platform-editor-highlights").fill("Compact storage for stationery")
    page.locator("#ai-original-platform-editor-bullets").fill("Three compartments\nMetal construction\nDesk storage")
    page.screenshot(path=str(screenshots / f"amazon-editor-{width}.png"))
    page.locator("#ai-original-platform-editor-save").click()
    page.wait_for_function("!aiOriginalPlatformEditorState || !aiOriginalPlatformEditorState.saving")
    assert not page.locator("#ai-original-platform-editor").is_visible(), (page.locator("#ai-original-platform-editor-status").inner_text(), requests)
    patch = next(body for method, _, body in requests if method == "PATCH")
    assert patch["target_platform"] == "amazon"
    assert patch["item_highlights"] == "Compact storage for stationery"
    assert patch["bullet_points"] == ["Three compartments", "Metal construction", "Desk storage"]
    assert row["marketplace_listings"]["yandex"]["title"] == "Органайзер для стола из металла"
    with page.expect_download() as downloaded:
        page.locator(".ai-original-draft-actions button").filter(has_text="导出草稿").first.click()
    assert downloaded.value.suggested_filename == "amazon-draft-1.json"
    assert json.loads(downloaded.value.path().read_text())["target_platform"] == "amazon"
    page.locator('#ai-original-grid input[type="checkbox"]').first.check()
    page.locator("#ai-original-template").select_option("specs")
    page.locator("#ai-original-process").click()
    page.wait_for_function("!aiOriginalSubmitting")
    process = next(body for method, url, body in requests if method == "POST" and url.endswith("/process"))
    assert process["target_platform"] == "amazon" and process["template_id"] == "specs"
    assert process["token_id"] == ""
    assert all("mercado-products" not in url for method, url, _ in requests if method in {"POST", "PATCH"})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not page_errors
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(screenshots / f"amazon-{width}.png"), full_page=True)
    page.locator("#ai-original-platform").select_option("mercadolibre")
    assert page.locator(".ai-original-publish-panel").is_visible()
    assert page.locator("#ai-original-review-submit").is_visible()


@pytest.mark.parametrize('width', [1440, 390])
def test_mercado_unified_status_and_manual_fields(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate('''() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        document.getElementById('ai-original-platform').value = 'mercadolibre';
        const ready = {id:1, ai_status:'completed', review_status:'approved', category_id:'CBT123',
          title_es:'Mascara', title_pt:'Mascara', description_es:'Descripcion', description_pt:'Descricao',
          weight_g:100, net_proceeds_usd:10, package_length_cm:10, package_width_cm:10, package_height_cm:10,
          main_image_url:'/test-ai-white.jpg', ai_original:{status:'completed', image_generation_method:'ai_image_edit',
          attributes:[{id:'MATERIAL',value_name:'Plastic'}]}, original_1688:{title:'测试产品'}};
        aiOriginalRows = [ready, {...ready,id:2,review_status:'unreviewed'},
          {...ready,id:3,ai_status:'failed'}, {...ready,id:4,weight_g:null},
          {...ready,id:5,review_status:'suspected'}];
        renderAiOriginalProducts(); decorateAiOriginalWorkbench();
    }''')
    assert page.locator('.ai-original-badge').all_text_contents() == ['通过', '待审核', '失败', '待补充', '疑似侵权']
    assert page.locator('#ai-original-status-filter option').all_text_contents() == ['全部', '通过', '待审核', '失败', '待补充', '疑似侵权']
    for status in ['approved', 'unreviewed', 'failed', 'incomplete', 'suspected']:
        assert page.locator(f'#ai-original-count-{status}').inner_text() == '1'
    page.locator('#ai-original-status-filter').select_option('incomplete')
    assert page.locator('.ai-original-card').count() == 1
    assert page.locator('.ai-original-badge').inner_text() == '待补充'
    assert page.locator('.ai-original-check-panel').inner_text().find('人工填写的上架资料') >= 0
    assert page.evaluate("aiOriginalRows.find(row => row.id === 4).ai_status") == 'completed'
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    output = ROOT / 'artifacts' / 'ai-original-unified-status'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'manual-fields-{width}.png'), full_page=True)
