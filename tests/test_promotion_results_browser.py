"""Verify enrollment outcomes and net prices with real templates and offline APIs."""
import json
from urllib.parse import urlsplit, parse_qs

import pytest
from jinja2 import Environment, FileSystemLoader

from test_console_ui_browser import ROOT, console_browser  # noqa: F401


ARTIFACTS = ROOT / "artifacts/promotion-results-ui"
FAILURE = '价格超出活动限制 <img src=x onerror="window.outcomeXss=1"> & 请重新核价'


class PromotionResultsMock:
    def __init__(self, page, refresh_mode="complete", embedded=False):
        self.page = page
        self.refresh_mode = refresh_mode
        self.submitted = False
        self.item_refresh_failed = False
        self.calls = []
        self.enrollments = []
        self.preview_submissions = []
        self.job_requests = []
        self.enrollment_response = None
        self.running_job_polls = 0
        self.on_job_complete = None
        self.errors = []
        self.campaign = {
            "id": 71, "promotion_id": "MLM-TEST-71", "name": "十月报名结果测试",
            "store_name": "墨西哥旗舰店", "seller_id": "offline-seller", "site_id": "MLM",
            "promotion_type": "DEAL", "status_raw": "started", "candidate_count": 3,
            "active_item_count": 2, "item_count": 5,
            "last_synced_at": "2026-10-07T02:00:00Z",
            "enrollment_summary": {"succeeded": 1, "failed": 1, "unknown": 1, "total": 3},
        }
        self.results = [
            {"item_id": "MLM100", "status": "succeeded", "message": "已通过平台确认"},
            {"item_id": "MLM200", "status": "failed", "message": FAILURE},
            {"item_id": "MLM300", "status": "unknown", "message": ""},
        ]
        env = Environment(loader=FileSystemLoader(ROOT / "bit/templates"))
        self.html = env.get_template("mercado_promotions.html").render(
            current_user={"username": "preview", "display_name": "离线结果测试", "is_platform_admin": True},
            embedded=embedded, url_for=lambda endpoint, filename: "/static/" + filename,
        )
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.route("**/*", self.route)
        page.goto("http://promotion.test/promotions?organization_key=results-enterprise")
        page.locator("#campaignRows button").wait_for()

    def job(self):
        return {"job_id": "submitted-job-2" if self.submitted else "saved-job-1",
                "action": "enroll", "created_at": "2026-10-07T02:00:00Z", "results": self.results}

    def items(self):
        common = {"original_price": 500, "price": 450, "min_price": 350, "max_price": 450,
                  "currency_id": "MXN", "last_synced_at": "2026-10-07T02:00:00Z"}
        before = {"amount": 370.5, "currency_id": "MXN", "source": "listing_api", "applies_to_price": 500}
        after = {"amount": 32.2, "currency_id": "USD", "source": "promotion_api", "applies_to_price": 450,
                 "price_currency_id": "MXN"}
        rows = []
        for index, result in enumerate(self.results, start=1):
            rows.append(dict(common, id=index, item_id=result["item_id"], title=["报名成功商品", "报名失败商品", "结果待核实商品"][index - 1],
                             status_raw="started" if result["status"] == "succeeded" else "candidate",
                             enrollment_result=dict(result, created_at="2026-10-07T02:00:00Z", job_id=self.job()["job_id"]),
                             net_proceeds_before=before,
                             net_proceeds_after=after if index == 1 else None))
        rows.extend([
            dict(common, id=4, item_id="MLM400", title="官方已参与商品", status_raw="started"),
            dict(common, id=5, item_id="MLM500", title="未报名候选商品", status_raw="candidate"),
        ])
        return rows

    @staticmethod
    def json(route, data, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps({"status": "success", "data": data}))

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path
        self.calls.append((request.method, path, request.post_data_json if request.method == "POST" else None))
        if url.netloc != "promotion.test":
            route.abort()
        elif path == "/promotions":
            route.fulfill(body=self.html, content_type="text/html")
        elif path.startswith("/static/"):
            asset = ROOT / "bit" / path.lstrip("/")
            if asset.is_file():
                route.fulfill(path=str(asset))
            else:
                route.fulfill(status=404, body="")
        elif path == "/api/promotions/stores":
            self.json(route, [{"id": 1, "name": "墨西哥旗舰店", "enabled": True,
                               "sites": [{"site_id": "MLM"}]}])
        elif path == "/api/promotions":
            self.json(route, {"rows": [self.campaign], "summary": {
                "total": 1, "candidate_items": 3, "active_items": 2, "ending_soon": 0}})
        elif path == "/api/promotions/71/items":
            if self.item_refresh_failed or (self.submitted and self.refresh_mode == "failed"):
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"status": "error", "message": "离线刷新失败"}))
            else:
                rows = self.items()
                enrolled = parse_qs(url.query).get('enrolled', [''])[0]
                if enrolled:
                    rows = [row for row in rows if (row['status_raw'] == 'started') == (enrolled == '1')]
                data = {"promotion": self.campaign, "rows": rows}
                if not (self.submitted and self.refresh_mode == "no_job"):
                    data["latest_job"] = self.job()
                self.json(route, data)
        elif path == "/api/promotions/preview":
            self.json(route, {"preview_id": "results-preview", "action": request.post_data_json["action"],
                               "ready_count": len(request.post_data_json["rows"]), "blocked_count": 0,
                               "rows": [{"item_id": f"MLM{row['item_local_id'] * 100}", "status": "ready",
                                         "deal_price": float(row["deal_price"]), "currency_id": "MXN",
                                         "net_proceeds_before": {"amount": 370.5, "currency_id": "MXN", "applies_to_price": 500,
                                                                 "source": "listing_api"},
                                         "net_proceeds_after": {"amount": 28 if float(row["deal_price"]) == 400 else 32.2,
                                                                "currency_id": "USD", "applies_to_price": float(row["deal_price"]),
                                                                "price_currency_id": "MXN", "source": "promotion_api"}}
                                        for row in request.post_data_json["rows"]]})
        elif path == "/api/promotions/enroll" and request.method == "POST":
            payload = request.post_data_json
            self.enrollments.append(payload)
            self.submitted = True
            self.json(route, self.enrollment_response or {
                "status": "running", "job_id": "submitted-job-2",
                "submitted_count": len(payload["rows"]), "excluded_count": 0,
                "excluded_reasons": [],
            })
        elif path == "/api/promotions/jobs" and request.method == "POST":
            self.preview_submissions.append(request.post_data_json)
            self.json(route, self.job())
        elif path == "/api/promotions/jobs":
            query = parse_qs(url.query)
            if query.get("status") == ["running"] and query.get("action") in (["withdraw"], ["enroll"]):
                self.json(route, {"rows": []})
            else:
                if query.get("job_id"):
                    self.job_requests.append(query)
                    job_id = query["job_id"][0]
                    if self.running_job_polls:
                        self.running_job_polls -= 1
                        self.json(route, {"rows": [dict(self.job(), job_id=job_id, actor="离线测试",
                                                         status="running", total_count=3, success_count=0,
                                                         failed_count=0, unknown_count=0)]})
                        return
                    if self.on_job_complete:
                        self.on_job_complete()
                else:
                    job_id = self.job()["job_id"]
                self.json(route, {"rows": [dict(self.job(), job_id=job_id, actor="离线测试",
                                                 status="partial", total_count=3, success_count=1,
                                                 failed_count=1, unknown_count=1)]})
        else:
            route.abort()

    def open(self):
        self.page.locator('#campaignRows button').click()
        self.page.locator("#itemsTitle").filter(has_text=self.campaign["name"]).wait_for()
        self.page.locator("#itemRows tr").first.wait_for()

    def screenshot(self, name):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(ARTIFACTS / name), full_page=True)


@pytest.fixture
def promotion_page(console_browser):
    page = console_browser.new_page(viewport={"width": 1440, "height": 1100})
    yield page
    page.close()


def test_outer_campaign_uses_current_counts_and_products_keep_saved_results(promotion_page):
    mock = PromotionResultsMock(promotion_page)
    summary = promotion_page.locator("#campaignRows").inner_text()
    assert all(text in summary for text in ["候选 3", "参与 2", "当前共 5 条"])
    assert promotion_page.locator("#campaignRows .result-counts").count() == 0
    mock.screenshot("01-activity-results.png")
    mock.open()
    headers = promotion_page.locator(".item-table thead").inner_text()
    assert all(text in headers for text in ["当前报名状态", "当前折扣比例", "促销前净收益价", "促销后净收益价"])
    expected = {1: "已报名", 2: "报名失败", 3: "待核实", 4: "已报名", 5: "未报名"}
    for row_id, label in expected.items():
        assert label in promotion_page.locator(f"#item-row-{row_id} td").nth(3).inner_text()
    assert FAILURE in promotion_page.locator("#item-row-2 td").nth(3).inner_text()
    before = promotion_page.locator("#item-row-1 td").nth(8).inner_text()
    after = promotion_page.locator("#net-after-1").inner_text()
    assert "MXN 370.50" in before and "对应售价 MXN 500.00" in before
    assert "USD 32.20" in after and "对应售价 MXN 450.00" in after
    assert promotion_page.locator("#net-after-2").inner_text() == "待获取"
    assert promotion_page.locator("#enrollmentResults").count() == 0
    assert "降价 10%（9 折）" in promotion_page.locator("#item-row-1").inner_text()
    assert promotion_page.locator("#enrollmentResults img, #itemRows .result-cell img").count() == 0
    assert promotion_page.evaluate("window.outcomeXss || 0") == 0
    assert not mock.errors
    mock.screenshot("02-product-results-and-net-prices.png")

    promotion_page.get_by_role("button", name="刷新结果", exact=True).click()
    promotion_page.locator("#item-row-2").wait_for()
    assert "MLM200" in promotion_page.locator("#itemRows").inner_text()
    promotion_page.reload()
    mock.open()
    assert "报名失败" in promotion_page.locator("#item-row-2").inner_text()
    assert "USD 32.20" in promotion_page.locator("#net-after-1").inner_text()


def test_refresh_failure_preserves_saved_results_and_allows_a_successful_retry(promotion_page):
    mock = PromotionResultsMock(promotion_page)
    mock.open()
    saved_panel = promotion_page.locator("#itemRows").inner_text()
    mock.item_refresh_failed = True
    promotion_page.get_by_role("button", name="刷新结果", exact=True).click()
    promotion_page.wait_for_function("document.getElementById('itemsSubtitle').textContent.includes('刷新失败，保留上次读取')")
    assert promotion_page.locator("#enrollmentResults").count() == 0
    assert promotion_page.locator("#itemRows").inner_text() == saved_panel
    assert promotion_page.locator("#itemRows tr").count() == 5
    assert "报名失败" in promotion_page.locator("#item-row-2").inner_text()
    assert FAILURE in promotion_page.locator("#item-row-2").inner_text()
    assert "USD 32.20" in promotion_page.locator("#net-after-1").inner_text()
    assert promotion_page.evaluate("state.currentCampaign.id") == 71
    assert promotion_page.evaluate("state.currentJob.job_id") == "saved-job-1"

    mock.item_refresh_failed = False
    mock.results[1]["message"] = "刷新恢复：最新失败原因"
    promotion_page.get_by_role("button", name="刷新结果", exact=True).click()
    promotion_page.wait_for_function("document.getElementById('itemRows').textContent.includes('刷新恢复：最新失败原因')")
    assert "刷新失败" not in promotion_page.locator("#itemsSubtitle").inner_text()
    assert "刷新恢复：最新失败原因" in promotion_page.locator("#item-row-2").inner_text()
    assert promotion_page.locator("#itemRows tr").count() == 5
    assert len([path for method, path, _ in mock.calls if path == "/api/promotions/71/items"]) == 3
    assert not mock.errors


def test_editing_activity_price_hides_old_net_quote_and_bulk_discount_updates_the_warning(promotion_page):
    mock = PromotionResultsMock(promotion_page)
    mock.open()
    price = promotion_page.locator("#price-1")
    price.fill("400")
    assert promotion_page.locator("#net-after-1").inner_text() == "报价已变更，待重新核价"
    assert "32.20" not in promotion_page.locator("#net-after-1").inner_text()
    assert "MXN 370.50" in promotion_page.locator("#item-row-1 td").nth(8).inner_text()
    price.fill("450.00")
    assert "USD 32.20" in promotion_page.locator("#net-after-1").inner_text()

    # Apply the same interaction to an eligible candidate with a known quote.
    promotion_page.evaluate("""() => {
        state.items.find(row => row.id === 2).net_proceeds_after = {
            amount: 300, currency_id: 'MXN', applies_to_price: 450, source: 'official'};
        renderItems();
    }""")
    promotion_page.locator('#itemRows input[value="2"]').check()
    promotion_page.locator("#batchDiscountValue").fill("20")
    promotion_page.locator("#applyDiscountBtn").click()
    assert promotion_page.locator("#price-2").input_value() == "400.00"
    assert promotion_page.locator("#net-after-2").inner_text() == "报价已变更，待重新核价"
    assert not mock.errors
    mock.screenshot("03-edited-price-needs-new-quote.png")


def test_zero_price_candidates_show_matching_net_quote_for_suggested_or_maximum_price(promotion_page):
    mock = PromotionResultsMock(promotion_page)
    mock.open()
    promotion_page.evaluate("""() => {
        const suggested = state.items.find(row => row.id === 2);
        suggested.price = 0;
        suggested.raw = {suggested_discounted_price: 420};
        suggested.net_proceeds_after = {amount: 310, currency_id: 'MXN', applies_to_price: 420, source: 'promotion_api'};
        const maximum = state.items.find(row => row.id === 3);
        maximum.price = 0;
        maximum.net_proceeds_after = {amount: 325, currency_id: 'MXN', applies_to_price: 450, source: 'promotion_api'};
        renderItems();
    }""")
    assert promotion_page.locator("#price-2").input_value() == "420"
    assert "MXN 310.00" in promotion_page.locator("#net-after-2").inner_text()
    assert "对应售价 MXN 420.00" in promotion_page.locator("#net-after-2").inner_text()
    assert promotion_page.locator("#price-3").input_value() == "450"
    assert "MXN 325.00" in promotion_page.locator("#net-after-3").inner_text()
    assert not mock.errors


def test_direct_enrollment_submits_the_edited_quote_without_confirmation(promotion_page):
    mock = PromotionResultsMock(promotion_page)
    mock.open()
    promotion_page.locator('#itemRows input[value="2"]').check()
    promotion_page.locator("#price-2").fill("400")
    promotion_page.locator("#enrollBtn").click()
    assert mock.enrollments == [{
        "promotion_id": 71,
        "rows": [{"item_local_id": 2, "deal_price": "400", "start_date": "", "finish_date": ""}],
    }]
    assert promotion_page.locator("#previewDialog").is_hidden()
    assert not any(path == "/api/promotions/preview" for _, path, _ in mock.calls)
    assert not any(path == "/api/promotions/jobs" and method == "POST" for method, path, _ in mock.calls)
    promotion_page.locator("#feedback").filter(has_text="提交完成").wait_for()
    assert len(mock.job_requests) == 1
    poll_query = mock.job_requests[0]
    assert poll_query["summary"] == ["1"]
    assert poll_query["job_id"] == ["submitted-job-2"]
    assert not mock.errors


def test_official_price_options_show_the_matching_net_quote_after_edit(promotion_page):
    mock = PromotionResultsMock(promotion_page)
    mock.open()
    promotion_page.evaluate("""() => {
        state.items.find(row => row.id === 1).net_proceeds_options = [
            {amount: 99, currency_id: 'USD', price_currency_id: 'BRL', applies_to_price: 400, source: 'promotion_api'},
            {amount: 28, currency_id: 'USD', price_currency_id: 'MXN', applies_to_price: 400, source: 'promotion_api'}];
    }""")
    promotion_page.locator("#price-1").fill("400")
    assert "USD 28.00" in promotion_page.locator("#net-after-1").inner_text()
    assert "对应售价 MXN 400.00" in promotion_page.locator("#net-after-1").inner_text()
    assert "99.00" not in promotion_page.locator("#net-after-1").inner_text()
    promotion_page.locator("#price-1").fill("410")
    assert promotion_page.locator("#net-after-1").inner_text() == "报价已变更，待重新核价"
    assert not mock.errors


def test_direct_enrollment_reports_automatically_excluded_products(promotion_page):
    mock = PromotionResultsMock(promotion_page)
    mock.open()
    mock.enrollment_response = {
        "status": "running", "job_id": "screened-job",
        "submitted_count": 1, "excluded_count": 1,
        "excluded_reasons": [{"reason": "活动价高于官方上限 450", "count": 1}],
    }
    promotion_page.locator('#itemRows input[value="2"]').check()
    promotion_page.locator('#itemRows input[value="3"]').check()
    promotion_page.locator("#enrollBtn").click()
    promotion_page.locator("#feedback").filter(has_text="自动屏蔽 1 件").wait_for()
    assert "活动价高于官方上限 450" in promotion_page.locator("#feedback").inner_text()
    assert promotion_page.locator("#previewDialog").is_hidden()
    assert len(mock.enrollments) == 1
    assert not any(path == "/api/promotions/preview" for _, path, _ in mock.calls)
    assert not any(path == "/api/promotions/jobs" and method == "POST" for method, path, _ in mock.calls)
    assert not mock.errors


def test_withdrawal_still_uses_preview_and_confirmed_job_submission(promotion_page):
    mock = PromotionResultsMock(promotion_page)
    mock.open()
    promotion_page.locator('#itemRows input[value="2"]').check()
    promotion_page.locator("#withdrawBtn").click()
    promotion_page.locator("#previewDialog").wait_for(state="visible")
    preview = next(payload for method, path, payload in mock.calls
                   if method == "POST" and path == "/api/promotions/preview")
    assert preview["action"] == "withdraw"
    assert not mock.enrollments
    promotion_page.locator("#confirmBtn").click()
    promotion_page.locator("#previewDialog").wait_for(state="hidden")
    assert mock.preview_submissions == [{"preview_id": "results-preview", "confirmed": True}]
    assert not mock.errors


@pytest.mark.parametrize("refresh_mode", ["complete", "no_job", "failed"])
def test_submission_keeps_per_item_results_visible_even_if_refresh_is_incomplete(promotion_page, refresh_mode):
    mock = PromotionResultsMock(promotion_page, refresh_mode=refresh_mode)
    mock.open()
    for row_id in [1, 2, 3]:
        checkbox = promotion_page.locator(f'#itemRows input[value="{row_id}"]')
        if checkbox.is_enabled():
            checkbox.check()
    promotion_page.locator("#enrollBtn").click()
    promotion_page.wait_for_function("document.getElementById('feedback').textContent.includes('提交完成')")
    assert promotion_page.locator("#enrollmentResults").count() == 0
    assert all(text in promotion_page.locator("#itemRows").inner_text() for text in ["已报名", "报名失败", "待核实", "MLM100", "MLM200", "MLM300", FAILURE])
    assert "提交完成：成功 1，待核实 1，失败 1" in promotion_page.locator("#feedback").inner_text()
    assert "info" in promotion_page.locator("#feedback").get_attribute("class")
    assert promotion_page.locator("#previewDialog").is_hidden()
    assert len(mock.enrollments) == 1
    assert not mock.preview_submissions
    assert not mock.errors
    mock.screenshot(f"04-submitted-results-{refresh_mode}.png")


def test_results_remain_accessible_in_mobile_embedded_view(promotion_page):
    promotion_page.set_viewport_size({"width": 390, "height": 844})
    mock = PromotionResultsMock(promotion_page, embedded=True)
    mock.open()
    assert promotion_page.locator("#enrollmentResults").count() == 0
    assert "报名失败" in promotion_page.locator("#itemRows").inner_text()
    assert promotion_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert promotion_page.locator(".item-table").evaluate("table => table.closest('.table-wrap').scrollWidth > table.closest('.table-wrap').clientWidth")
    assert not mock.errors
    mock.screenshot("05-mobile-result-panel.png")


def test_current_enrollment_filter_clears_selection_and_prices_do_not_change_current_discount(promotion_page):
    mock = PromotionResultsMock(promotion_page)
    mock.open()
    promotion_page.locator('#itemRows input[value="2"]').check()
    promotion_page.locator('#itemEnrollmentFilter').select_option('1')
    promotion_page.wait_for_function("state.itemTotal === 2")
    assert promotion_page.locator('#itemRows tr').count() == 2
    assert promotion_page.locator('#selectionText').inner_text() == '已选择 0 件商品'
    promotion_page.locator('#price-1').fill('350')
    assert '降价 10%（9 折）' in promotion_page.locator('#item-row-1').inner_text()
    promotion_page.locator('#itemEnrollmentFilter').select_option('0')
    promotion_page.wait_for_function("state.itemTotal === 3")
    assert promotion_page.locator('#itemRows tr').count() == 3
    assert all('未报名' in row for row in promotion_page.locator('#itemRows tr').all_inner_texts())
    assert not mock.errors


def test_background_enrollment_refreshes_product_state_without_duplicate_submission(promotion_page):
    mock = PromotionResultsMock(promotion_page)
    mock.open()
    mock.running_job_polls = 1
    def finish():
        mock.results[1]['status'] = 'succeeded'
    mock.on_job_complete = finish
    promotion_page.locator('#itemRows input[value="2"]').check()
    promotion_page.locator('#enrollBtn').click()
    promotion_page.wait_for_function("state.pendingJob?.id === 'submitted-job-2'")
    assert promotion_page.locator('#enrollBtn').is_disabled()
    assert promotion_page.locator('#itemsView').is_visible()
    promotion_page.wait_for_function("!state.pendingJob && document.getElementById('feedback').textContent.includes('提交完成')")
    assert '已报名' in promotion_page.locator('#item-row-2').inner_text()
    assert len(mock.enrollments) == 1
    assert all(query['summary'] == ['1'] for query in mock.job_requests)
    assert all(query['job_id'] == ['submitted-job-2'] for query in mock.job_requests)
    assert not mock.errors
