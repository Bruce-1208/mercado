"""Verify net save feedback in the real console with business APIs mocked."""
import json
import pytest

from test_console_ui_browser import console_browser, console_page  # noqa: F401


def test_net_save_updates_checklist_even_when_list_reload_fails(console_page):
    page = console_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    saved = {
        "id": 1, "ai_status": "completed", "review_status": "approved", "category_id": "CBT123",
        "title_es": "Mascara", "title_pt": "Mascara", "description_es": "Descripcion",
        "description_pt": "Descricao", "weight_g": 100, "weight_basis": "ai_original_manual",
        "net_proceeds_usd": 12, "package_length_cm": 10, "package_width_cm": 10,
        "package_height_cm": 10, "main_image_url": "/test-ai-white.jpg",
        "ai_original": {"status": "completed", "image_generation_method": "local_background_removal",
                        "attributes": [{"id": "MATERIAL", "value_name": "Plastic"}]},
        "original_1688": {"title": "测试产品"},
    }
    bodies = []

    def save(route):
        bodies.append(route.request.post_data_json)
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": saved}))

    page.route("**/api/ai-original-products/1", save)
    page.route("**/api/ai-original-products?*", lambda route: route.fulfill(
        status=503, content_type="application/json", body=json.dumps({"status": "error", "message": "暂时无法刷新列表"})))
    page.evaluate("""row => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        document.getElementById('ai-original-platform').value = 'mercadolibre';
        aiOriginalRows = [{...row, review_status: 'unreviewed', net_proceeds_usd: null,
                           suggested_net_proceeds_usd: 12}];
        renderAiOriginalProducts(); decorateAiOriginalWorkbench();
    }""", saved)
    checklist = page.locator(".ai-original-manual-checks li").filter(has_text="净收益")
    assert checklist.text_content() == "○净收益已填写待保存"
    page.locator("button").filter(has_text="保存参数并审核通过").click()
    page.wait_for_function("document.querySelector('.ai-original-manual-checks').textContent.includes('净收益已填写') && !document.querySelector('.ai-original-manual-checks').textContent.includes('待保存')")
    assert checklist.text_content() == "✓净收益已填写"
    assert page.locator("#ai-original-task-status").inner_text() == "产品 1 已保存并审核通过"
    assert bodies[0]["net_proceeds_usd"] == 12
    assert page.evaluate("aiOriginalRows[0].net_proceeds_usd") == 12
    page.locator("#ai-original-net-1").fill("15")
    assert checklist.text_content() == "○净收益已填写待保存"
    page.locator("#ai-original-net-1").fill("12")
    assert checklist.text_content() == "✓净收益已填写"
    assert not errors


def generated_fields():
    return {
        "title_es": "Mascara", "title_pt": "Mascara", "description_es": "Descripcion",
        "description_pt": "Descricao", "main_image_url": "/test-ai-white.jpg",
        "ai_original": {"status": "completed", "image_generation_method": "local_background_removal",
                        "attributes": [{"id": "MATERIAL", "value_name": "Plastic"}]},
    }


def test_approval_with_missing_dimensions_updates_card_and_filter(console_page):
    page = console_page
    row = {**generated_fields(), "id": 1, "ai_status": "completed", "review_status": "suspected",
           "weight_g": 100, "weight_basis": "ai_original_manual", "net_proceeds_usd": 5,
           "category_id": "CBT123",
           "original_1688": {"title": "测试审核状态"}}
    def save(route):
        changes = route.request.post_data_json
        route.fulfill(content_type="application/json", body=json.dumps({
            "status": "success", "data": {**row, **changes},
        }))
    page.route("**/api/ai-original-products/1", save)
    page.evaluate("""row => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        document.getElementById('ai-original-platform').value = 'mercadolibre';
        aiOriginalRows = [row]; renderAiOriginalProducts(); decorateAiOriginalWorkbench();
    }""", row)
    page.locator(".ai-original-card").scroll_into_view_if_needed()
    page.wait_for_function("document.querySelector('.ai-original-badge')?.innerText === '疑似侵权'")
    assert page.locator(".ai-original-badge").inner_text() == "疑似侵权"
    page.locator("#ai-original-net-1").fill("14.75")
    page.get_by_role("button", name="保存参数并审核通过", exact=True).click()
    page.wait_for_function("aiOriginalRows[0].review_status === 'approved'")
    page.wait_for_function("document.querySelector('.ai-original-badge')?.innerText === '待补充'")
    assert page.locator(".ai-original-badge").inner_text() == "待补充"
    assert page.evaluate("aiOriginalRows.filter(row => aiOriginalProductStatus(row) === 'incomplete').length") == 1
    page.locator("#ai-original-status-filter").select_option("incomplete")
    assert page.locator(".ai-original-card").count() == 1
    page.locator(".ai-original-card").scroll_into_view_if_needed()
    assert page.locator("#ai-original-net-1").input_value() == "14.75"
    assert "包装尺寸待补齐" in page.locator(".ai-original-issues").inner_text()


def test_partial_bulk_review_retains_failed_inputs_and_continues_approvals(console_page):
    page = console_page
    rows = [{**generated_fields(), "id": pid, "ai_status": "completed", "review_status": "unreviewed",
             "package_length_cm": 10, "package_width_cm": 10, "package_height_cm": 10,
             "weight_g": 100, "net_proceeds_usd": 5, "category_id": "CBT123",
             "original_1688": {"title": f"批量审核 {pid}"}} for pid in (1, 2, 3)]
    def save(route):
        pid = int(route.request.url.rsplit("/", 1)[-1])
        if pid == 2:
            route.fulfill(status=500, content_type="application/json",
                          body=json.dumps({"status": "error", "message": "模拟保存失败"}))
        else:
            route.fulfill(content_type="application/json", body=json.dumps({
                "status": "success", "data": {**rows[pid - 1], **route.request.post_data_json},
            }))
    page.route("**/api/ai-original-products/*", save)
    page.evaluate("""rows => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        document.getElementById('ai-original-platform').value = 'mercadolibre';
        aiOriginalRows = rows; aiOriginalSelected = new Set([1,2,3]);
        renderAiOriginalProducts(); decorateAiOriginalWorkbench();
    }""", rows)
    page.locator("#ai-original-net-2").fill("19")
    page.locator("#ai-original-review-status").select_option("approved")
    page.locator("#ai-original-review-submit").click()
    page.wait_for_function("!aiOriginalReviewRunning")
    assert page.locator("#ai-original-task-status").inner_text().startswith("已更新 2 件产品的审核状态，失败 1 件")
    assert page.evaluate("[...aiOriginalSelected]") == [2]
    assert page.locator("#ai-original-net-2").input_value() == "19"
    assert page.evaluate("aiOriginalRows.filter(row => aiOriginalProductStatus(row) === 'approved').length") == 2


@pytest.mark.parametrize("new_net", ["15", ""])
def test_1688_net_save_updates_ai_card_even_when_list_refreshes_fail(console_page, new_net):
    page = console_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    row = {
        "id": 1, "ai_status": "completed", "review_status": "unreviewed",
        "net_proceeds_usd": 12, "suggested_net_proceeds_usd": 12,
        "source_net_proceeds_usd": 12, "source_purchase_price_cny": 75,
        "ai_original": {"status": "completed"},
        "original_1688": {"title": "测试产品", "price": 75},
    }
    writes = []

    def save(route):
        patch = route.request.post_data_json
        writes.append(patch)
        saved = {**row, "net_proceeds_usd": patch["net_proceeds_usd"],
                 "source_net_proceeds_usd": patch["net_proceeds_usd"],
                 "source_1688_overrides": patch}
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": saved}))

    page.route("**/api/1688-products/1", save)
    for url in ("**/api/1688-products?*", "**/api/ai-original-products?*"):
        page.route(url, lambda route: route.fulfill(status=503, content_type="application/json",
            body=json.dumps({"status": "error", "message": "列表暂时无法刷新"})))
    page.evaluate("""row => {
        aiOriginalRows = [{...row}];
        products1688Rows = [{...row}];
        document.getElementById('ai-original-platform').value = 'mercadolibre';
        renderAiOriginalProducts();
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-1688-products').classList.add('active');
        products1688RenderRows();
        products1688OpenEdit(1);
    }""", row)
    page.locator("#products-1688-edit-net_proceeds_usd").fill(new_net)
    page.locator("#products-1688-edit-save").click()
    page.wait_for_function("!document.getElementById('products-1688-edit-dialog').open")
    expected = int(new_net) if new_net else None
    assert writes == [{"net_proceeds_usd": expected}]
    assert page.evaluate("aiOriginalRows[0].net_proceeds_usd") == expected
    assert page.locator("#ai-original-net-1").input_value() == new_net
    assert "商品资料已保存" in page.locator("#products-1688-status").inner_text()
    assert not errors
