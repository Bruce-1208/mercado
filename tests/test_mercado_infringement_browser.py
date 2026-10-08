"""Exercise shared list infringement flows with all business APIs intercepted."""
import json

import pytest

from test_console_ui_browser import console_browser, console_page  # noqa: F401


def response(route, data):
    route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))


def prepare_list(page, mode="collection"):
    page.evaluate("""mode => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-mercado-' + (mode === 'products' ? 'products' : 'collection')).classList.add('active');
        const host = document.getElementById('mercado-list-' + (mode === 'products' ? 'products' : 'collection') + '-host');
        host.appendChild(mercadoListModule);
        mercadoListMode = mode;
        mercadoListPage = 3;
        mercadoListPages = 4;
        mercadoListTotal = 1800;
        mercadoListModule.classList.toggle('collection-mode', mode === 'collection');
        mercadoListModule.classList.toggle('product-mode', mode === 'products');
        renderMercadoListHeader();
        renderMercadoListRows([{id: 7, title: '测试商品', source_item_id: 'MLM7', weight_g: 100,
          net_proceeds_usd: 15, infringement: {status: 'completed', risk_level: 2, detection_scope: 'title', reason: '品牌线索'}}]);
    }""", mode)


@pytest.mark.parametrize("mode", ["collection", "products"])
def test_both_lists_render_clickable_risk_and_single_item_detection(console_page, mode):
    page = console_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    prepare_list(page, mode)
    assert page.locator("#mercado-infringement-selected").is_visible()
    if mode == "collection":
        assert page.locator("#mercado-list-head th").count() == 15
        assert page.locator("#mercado-list-body tr:first-child td").count() == 15
    else:
        assert not page.locator("#mercado-list-head").is_visible()
        card_row = page.locator('#mercado-list-body .market-product-card-row[data-product-id="7"]')
        assert card_row.get_attribute("role") == "listitem"
        assert card_row.locator("td").count() == 1
        assert card_row.locator(".market-product-card").is_visible()
    assert page.locator(".mi-risk-badge").inner_text() == "高风险"
    assert "侵权" not in page.locator(".mi-risk-badge").inner_text()
    page.locator(".mi-row-actions button").filter(has_text="再检测").click()
    assert page.locator("#mi-start-dialog").is_visible()
    assert page.locator('input[name="mi-range"][value="selected"]').is_checked()
    assert page.locator('input[name="mi-range"][value="filtered"]').is_disabled()
    assert page.locator("#mi-recheck").is_checked()
    assert not errors


def test_selected_task_keeps_scope_filter_snapshot_and_page(console_page):
    page = console_page
    prepare_list(page)
    bodies = []
    task = {"id": "test-task", "task_id": "test-task", "list_mode": "collection", "status": "completed",
            "total": 1, "completed": 1, "failed": 0, "detection_scope": "title_image"}
    page.route("**/api/mercado-collection/items?*", lambda route: response(route, {
        "total": 1800, "rows": [{"id": 7, "title": "已检商品", "weight_g": 100}]}))

    def tasks(route):
        if route.request.method == "POST":
            bodies.append(route.request.post_data_json)
            response(route, task)
        else:
            response(route, {"tasks": [task]})

    page.route("**/api/mercado/infringement/tasks", tasks)
    page.evaluate("""() => {
        selectedMercadoCollectionIds.add(7);
        document.getElementById('mercado-list-search').value = '蓝色玩具';
        document.getElementById('mercado-infringement-filter').value = 'high';
        openMercadoInfringement();
        document.getElementById('mercado-list-search').value = '另一个筛选';
    }""")
    page.locator('input[name="mi-scope"][value="title_image"]').check()
    page.locator("#mi-recheck").check()
    page.locator("#mi-start-submit").click()
    page.wait_for_function("document.getElementById('mi-task-dialog').open && mercadoListRows[0]?.title === '已检商品'")
    assert bodies[0]["item_ids"] == [7]
    assert bodies[0]["selection_mode"] == "selected"
    assert bodies[0]["detection_scope"] == "title_image"
    assert bodies[0]["filters"]["search"] == "蓝色玩具"
    assert bodies[0]["filters"]["risk_status"] == "high"
    assert bodies[0]["recheck"] is True
    assert page.evaluate("mercadoListPage") == 3
    assert page.evaluate("[...selectedMercadoCollectionIds]") == [7]


def test_detail_escapes_evidence_and_requires_scoped_release(console_page):
    page = console_page
    prepare_list(page, "products")
    detail = {"item": {"id": 7, "title": "图文商品", "source_item_id": "MLM7"}, "infringement": {
        "status": "completed", "risk_level": 0, "detection_scope": "title_image", "image_method": "ocr",
        "reason": '<script>unsafe</script> 标题未命中', "content_hash": "abc123", "rule_version": 9},
        "rules": [{"name": '<img src=x onerror="alert(1)">', "reason": "待人工核对"}], "history": []}
    reviews = []
    page.route("**/api/mercado/infringement/items/products/7", lambda route: response(route, detail))
    page.route("**/api/mercado-products?*", lambda route: response(route, {"total": 1800, "rows": [detail["item"]]}))

    def review(route):
        reviews.append(route.request.post_data_json)
        response(route, {"saved": True})

    page.route("**/api/mercado/infringement/items/products/7/review", review)
    page.locator(".mi-risk-badge").click()
    page.wait_for_function("!document.getElementById('mi-review-form').hidden")
    assert "仅识别图中文字" in page.locator("#mi-detail-content").inner_text()
    assert "未发现标题及图中文字风险" in page.locator("#mi-detail-content").inner_text()
    assert page.locator("#mi-detail-content script, #mi-detail-content img").count() == 0
    assert "abc123" in page.locator("#mi-detail-content").inner_text()
    page.locator("#mi-review-decision").select_option("released")
    page.locator("#mi-review-reason").fill("已核实供应商授权")
    page.locator("#mi-review-submit").click()
    assert not reviews
    assert "证据引用" in page.locator("#mi-review-feedback").inner_text()
    page.locator("#mi-review-evidence").fill("授权文件 AUTH-7")
    page.locator("#mi-review-submit").click()
    assert not reviews
    page.locator("#mi-review-sites").fill("MLM, MLB")
    page.locator("#mi-review-expires").fill("2030-01-01T12:00")
    page.locator("#mi-review-submit").click()
    page.wait_for_function("document.getElementById('mi-review-feedback').textContent.includes('已保存')")
    assert reviews[0]["decision"] == "released"
    assert reviews[0]["site_ids"] == ["MLM", "MLB"]
    assert reviews[0]["evidence_ref"] == "授权文件 AUTH-7"
    assert reviews[0]["expires_at"]


def test_mobile_dialog_stays_inside_viewport_and_range_is_explicit(console_page):
    page = console_page
    page.set_viewport_size({"width": 390, "height": 844})
    prepare_list(page)
    page.evaluate("openMercadoInfringement()")
    assert page.locator('input[name="mi-range"][value="unchecked"]').is_checked()
    page.locator('input[name="mi-range"][value="filtered"]').check()
    assert "包含所有分页" in page.locator("#mi-start-summary").inner_text()
    bounds = page.locator("#mi-start-dialog").bounding_box()
    assert bounds["x"] >= 0 and bounds["y"] >= 0
    assert bounds["x"] + bounds["width"] <= 390
    assert bounds["y"] + bounds["height"] <= 844


@pytest.mark.parametrize("selection_mode", ["filtered", "unchecked"])
def test_cross_page_ranges_send_explicit_filters_without_page_limit(console_page, selection_mode):
    page = console_page
    prepare_list(page, "products")
    bodies = []
    task = {"task_id": "empty-task", "list_mode": "products", "status": "completed", "total": 0, "completed": 0}

    def tasks(route):
        if route.request.method == "POST":
            bodies.append(route.request.post_data_json)
            response(route, task)
        else:
            response(route, {"tasks": [task]})

    page.route("**/api/mercado/infringement/tasks", tasks)
    page.route("**/api/mercado-products?*", lambda route: response(route, {"total": 1800, "rows": [{"id": 7}]}))
    page.evaluate("""() => {
        document.getElementById('mercado-list-search').value = '过滤范围';
        openMercadoInfringement();
    }""")
    page.locator(f'input[name="mi-range"][value="{selection_mode}"]').check()
    if selection_mode == "unchecked":
        assert page.locator("#mi-recheck").is_disabled()
    page.locator("#mi-start-submit").click()
    page.wait_for_function("document.getElementById('mi-task-dialog').open")
    assert bodies[0]["selection_mode"] == selection_mode
    assert bodies[0]["item_ids"] == []
    assert bodies[0]["filters"]["search"] == "过滤范围"
    assert "limit" not in bodies[0]["filters"]
    assert "offset" not in bodies[0]["filters"]


def test_publish_readiness_retains_risky_product_and_checks_review_sites(console_page):
    page = console_page
    prepare_list(page, "products")
    page.evaluate("""() => {
        mercadoListRows[0].review_status = 'approved';
        selectedMercadoCollectionIds.add(7);
        mercadoPublishSite.innerHTML = '<option value="MLM" selected>墨西哥</option>';
        updateMercadoSelection();
    }""")
    summary = page.locator("#mercado-selection-summary").inner_text()
    assert "侵权风险待处理，保留产品列表" in summary
    assert "将自动移回" not in summary
    assert page.evaluate("mercadoProductPublishIssues(mercadoListRows[0])") == ["高风险，需人工复核"]
    page.evaluate("""() => {
        Object.assign(mercadoListRows[0].infringement, {manual_decision:'allow', review_current:true, review_site_ids:'["MLM"]', review_evidence_ref:'AUTH-7', review_expires_at:'2030-01-01T00:00:00Z'});
        mercadoPublishSite.innerHTML += '<option value="MLB" selected>巴西</option>';
    }""")
    assert page.evaluate("mercadoProductPublishIssues(mercadoListRows[0])") == ["人工放行未覆盖站点 MLB"]
    page.evaluate("mercadoPublishSite.options[1].selected = false")
    assert page.evaluate("mercadoProductPublishIssues(mercadoListRows[0])") == []
    page.evaluate("mercadoListRows[0].infringement.review_current = false")
    assert page.evaluate("mercadoProductPublishIssues(mercadoListRows[0])") == ["高风险，需人工复核"]
    page.evaluate("""() => {
        Object.assign(mercadoListRows[0].infringement, {manual_decision:'', status:'stale'});
        updateMercadoSelection();
    }""")
    assert page.evaluate("mercadoProductPublishIssues(mercadoListRows[0])") == []
    assert "将补检后上架" in page.locator("#mercado-selection-summary").inner_text()


def test_task_failures_offer_details_and_retry_only_unfinished(console_page):
    page = console_page
    prepare_list(page)
    calls = []
    original = {"task_id": "failed-task", "params": {"list_mode": "collection"}, "status": "partial",
                "total": 2, "completed": 1, "failed": 1, "detection_scope": "title_image", "logs": ["保留已完成结果"],
                "results": [{"source_type": "collection_list", "row_id": 7, "status": "failed", "error": "图片读取失败"}]}
    retry = {"task_id": "retry-task", "params": {"list_mode": "collection"}, "status": "completed",
             "total": 1, "completed": 1, "failed": 0, "detection_scope": "title_image"}
    page.route("**/api/mercado/infringement/tasks", lambda route: response(route, {"tasks": [original]}))

    def retry_task(route):
        calls.append(route.request.method)
        response(route, retry)

    page.route("**/api/mercado/infringement/tasks/failed-task/retry", retry_task)
    page.route("**/api/mercado-collection/items?*", lambda route: response(route, {"total": 1800, "rows": [{"id": 7}]}))
    page.evaluate("task => openMercadoInfringementTasks(task)", original)
    page.locator("#mi-task-current .mi-history").filter(has_text="商品处理结果").locator("summary").click()
    assert "图片读取失败" in page.locator("#mi-task-current").inner_text()
    assert page.locator('#mi-task-current [data-mi-item="collection:7"]').count() == 1
    page.locator('#mi-task-current [data-mi-retry="failed-task"]').click()
    page.wait_for_function("document.getElementById('mi-task-current').textContent.includes('retry-task')")
    assert calls == ["POST"]


def test_review_permission_is_separate_and_candidates_preserve_manual_rules(console_page):
    page = console_page
    prepare_list(page, "products")
    detail = {"item": {"id": 7, "title": "候选商品", "source_item_id": "MLM7"}, "infringement": {
        "status": "high", "risk_level": 2, "keywords": ["BrandTest"], "reason": "原始检测原因",
        "detection_scope": "title_image", "image_method": "vision", "image_checked": True,
        "content_hash": "content-v1", "rule_version": "rules-v1"}}
    candidates = []
    page.route("**/api/mercado/infringement/items/products/7", lambda route: response(route, detail))

    def candidate(route):
        candidates.append(route.request.post_data_json)
        response(route, {"id": 1, "candidate_status": "pending", "list_type": "blacklist", "preserved_manual": True})

    page.route("**/api/infringement-knowledge/candidates", candidate)
    page.evaluate("""() => {
        currentWorkbenchPermissions.clear();
        currentWorkbenchPermissions.add('risk_check.execute');
        currentWorkbenchPermissions.add('infringement_knowledge.manage');
        openMercadoInfringementDetail('products', 7);
    }""")
    page.wait_for_function("document.getElementById('mi-detail-content').textContent.includes('候选商品')")
    assert page.locator("#mi-review-form").is_hidden()
    assert page.locator("#mi-detail-recheck").is_visible()
    page.locator("#mi-detail-candidate").click()
    assert page.locator("#mi-candidate-term").input_value() == "BrandTest"
    page.locator("#mi-candidate-submit").click()
    page.wait_for_function("document.getElementById('mi-candidate-feedback').textContent.includes('待复核线索')")
    assert candidates[0]["brand_name"] == "BrandTest"
    assert candidates[0]["source_row_id"] == 7
    assert candidates[0]["product_source"] == "product_list"
    assert candidates[0]["reason"] == "原始检测原因"
    assert candidates[0]["content_hash"] == "content-v1"
    assert candidates[0]["detection_scope"] == "title_image"
    assert "list_type" not in candidates[0]
