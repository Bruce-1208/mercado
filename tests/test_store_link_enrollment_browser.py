"""Exercise link-to-promotion enrollment using real templates and offline APIs."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader

from test_console_ui_browser import ROOT, console_browser


ARTIFACTS = ROOT / "artifacts/store-link-enrollment"
ORGANIZATION = "enrollment-test-enterprise"
ITEM_ID = "MLM123"
PROMOTION_ID = 71
ITEM_LOCAL_ID = 501


class EnrollmentMock:
    """Every browser request is fulfilled locally, including submission."""

    def __init__(self, page, target_status="candidate", campaign_status="started"):
        self.page = page
        self.requests = []
        self.promotions_opened = []
        self.item_requests = []
        self.previews = []
        self.submissions = []
        self.job_requests = []
        self.list_refreshes = []
        self.target_status = target_status
        self.campaign = {
            "id": PROMOTION_ID, "promotion_id": "MLM-OFFICIAL-DEAL-900",
            "name": "墨西哥十月大促", "promotion_type": "DEAL",
            "status_raw": campaign_status, "store_name": "墨西哥旗舰店",
            "site_id": "MLM", "seller_id": "test-seller", "candidate_count": 3,
            "item_count": 4, "active_item_count": 1,
            "last_synced_at": "2026-10-07T02:00:00Z",
        }
        self.link = {
            "id": 42, "item_id": ITEM_ID, "site_id": "MLM", "status": "active",
            "is_current": True, "store_name": "墨西哥旗舰店", "title": "活动报名测试商品",
            "currency_id": "MXN", "price": 500, "available_quantity": 20,
            "enrolled_promotions": [],
            "available_promotions": [
                {"promotion_fk": PROMOTION_ID, "promotion_id": self.campaign["promotion_id"],
                 "name": self.campaign["name"], "promotion_type": "DEAL",
                 "status_raw": "candidate", "price": 450, "currency_id": "MXN"},
                {"promotion_fk": 72, "promotion_id": "CLEARANCE-900", "name": "清仓活动",
                 "promotion_type": "CLEARANCE", "status_raw": "candidate",
                 "price": 300, "currency_id": "MXN"},
            ],
        }
        env = Environment(loader=FileSystemLoader(ROOT / "bit/templates"))
        context = {
            "current_user": {"username": "preview", "display_name": "离线报名测试",
                             "is_admin": True, "is_platform_admin": True, "permissions": ["*"]},
            "can_change_task_workers": False, "runtime_role": "server",
            "url_for": lambda endpoint, filename: "/static/" + filename,
        }
        self.console_html = env.get_template("index.html").render(**context)
        self.promotions_html = env.get_template("mercado_promotions.html").render(
            **context, embedded=True,
        )
        page.route("**/*", self.route)
        page.goto("http://console.test/")
        # Let initial enterprise discovery finish before assigning the test scope.
        # Otherwise its late response resets the manually selected enterprise.
        page.locator(f'#global-enterprise-filter option[value="{ORGANIZATION}"]').wait_for(state="attached")
        page.evaluate("""data => {
            selectedWorkbenchOrganization = data.organization;
            const enterprise = document.getElementById('global-enterprise-filter');
            if (enterprise) {
                enterprise.innerHTML = `<option value="${data.organization}">测试企业</option>`;
                enterprise.value = data.organization;
            }
            document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
            document.getElementById('tab-store-links').classList.add('active');
            storeLinksLoaded = true;
            storeLinkPage = 2;
            renderStoreLinkRows([data.link]);
        }""", {"organization": ORGANIZATION, "link": self.link})

    def items(self):
        common = {
            "title": "活动报名测试商品", "original_price": 500, "price": 450,
            "min_price": 350, "max_price": 450, "currency_id": "MXN",
            "last_synced_at": "2026-10-07T02:00:00Z",
        }
        # A previously enrolled offer for the same item must not hide the candidate.
        rows = [dict(common, id=502, item_id=ITEM_ID, status_raw="started")]
        if self.target_status != "missing":
            rows.append(dict(common, id=ITEM_LOCAL_ID, item_id=ITEM_ID,
                             status_raw="started" if self.submissions else self.target_status))
        rows.extend([
            dict(common, id=503, item_id="MLM456", status_raw="candidate", title="另一件候选商品"),
            dict(common, id=504, item_id="MLM1234", status_raw="candidate", title="相似编号候选商品"),
        ])
        return rows

    @staticmethod
    def json(route, data, **extra):
        route.fulfill(content_type="application/json", body=json.dumps({
            "status": "success", "data": data, **extra,
        }))

    def route(self, route):
        request = route.request
        self.requests.append(request.url)
        url = urlsplit(request.url)
        path = url.path
        if url.netloc != "console.test":
            route.abort()
        elif path == "/":
            route.fulfill(body=self.console_html, content_type="text/html")
        elif path == "/promotions":
            self.promotions_opened.append(parse_qs(url.query))
            route.fulfill(body=self.promotions_html, content_type="text/html")
        elif path.startswith("/static/"):
            asset = ROOT / "bit" / path.lstrip("/")
            if asset.is_file():
                route.fulfill(path=str(asset))
            else:
                route.fulfill(status=404, body="")
        elif path == "/api/access/organizations":
            self.json(route, [{"organization_key": ORGANIZATION, "organization_name": "报名测试企业"}], success=True)
        elif path == "/api/promotions/stores":
            self.json(route, [{"id": 1, "store_name": "墨西哥旗舰店", "enabled": True,
                               "site_id": "MLM"}])
        elif path == "/api/promotions":
            self.json(route, {"rows": [self.campaign], "summary": {
                "total": 1, "candidate_items": 3, "active_items": 1, "ending_soon": 0,
            }})
        elif path == f"/api/promotions/{PROMOTION_ID}/items":
            self.item_requests.append(request.url)
            self.json(route, {"promotion": self.campaign, "rows": self.items()})
        elif path == "/api/promotions/preview":
            self.previews.append(request.post_data_json)
            self.json(route, {"preview_id": "offline-preview-1", "ready_count": 1,
                               "blocked_count": 0, "rows": [{
                                   "item_id": ITEM_ID, "deal_price": 400,
                                   "currency_id": "MXN", "status": "ready",
                               }]})
        elif path == "/api/promotions/enroll" and request.method == "POST":
            self.submissions.append(request.post_data_json)
            self.json(route, {"status": "running", "job_id": "offline-enrollment-job",
                              "submitted_count": 1, "excluded_count": 0,
                              "excluded_reasons": []})
        elif path == "/api/promotions/jobs" and request.method == "POST":
            self.json(route, {"results": [{"item_id": ITEM_ID, "status": "succeeded"}]})
        elif path == "/api/promotions/jobs":
            query = parse_qs(url.query)
            if query.get("job_id"):
                self.job_requests.append(query)
                self.json(route, {"rows": [{
                    "job_id": "offline-enrollment-job", "action": "enroll",
                    "status": "succeeded", "total_count": 1,
                    "success_count": 1, "failed_count": 0, "unknown_count": 0,
                }]})
            else:
                self.json(route, {"rows": []})
        elif path == "/api/store-links":
            self.list_refreshes.append(request.url)
            enrolled_link = dict(self.link, available_promotions=[],
                                 enrolled_promotions=[dict(self.link["available_promotions"][0],
                                                          status_raw="started", price=400)])
            self.json(route, {"rows": [enrolled_link], "page": 2, "pages": 2, "total": 201})
        else:
            # Unrelated console initialization APIs also stay strictly offline.
            self.json(route, [], success=True, items=[], stores=[], groups=[], total=0)

    def open_enrollment(self):
        self.page.locator(f'button[onclick="openStoreLinkPromotion(42, {PROMOTION_ID})"]').click()
        frame = self.page.frame_locator("#promotions-frame")
        frame.locator("#itemsTitle").filter(has_text=self.campaign["name"]).wait_for()
        return frame

    def screenshot(self, name):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(ARTIFACTS / name), full_page=True)


@pytest.fixture
def enrollment_page(console_browser):
    page = console_browser.new_page(viewport={"width": 1600, "height": 1000})
    yield page
    page.close()


def test_link_enrollment_opens_exact_campaign_prices_and_submits_directly(enrollment_page):
    mock = EnrollmentMock(enrollment_page)
    button = enrollment_page.locator(f'button[onclick="openStoreLinkPromotion(42, {PROMOTION_ID})"]')
    assert button.is_enabled()
    button.scroll_into_view_if_needed()
    mock.screenshot("01-store-link-enrollment-entry.png")

    frame = mock.open_enrollment()
    assert mock.promotions_opened == [{
        "embedded": ["1"], "promotion_id": [str(PROMOTION_ID)],
        "item_id": [ITEM_ID], "organization_key": [ORGANIZATION],
    }]
    assert len(mock.item_requests) == 1
    assert parse_qs(urlsplit(mock.item_requests[0]).query)["organization_key"] == [ORGANIZATION]
    assert frame.locator('#itemRows input[type="checkbox"]:checked').count() == 1
    assert frame.locator(f'#itemRows input[value="{ITEM_LOCAL_ID}"]').is_checked()
    assert not frame.locator('#itemRows input[value="502"]').is_checked()
    assert not frame.locator('#itemRows input[value="503"]').is_checked()
    assert not frame.locator('#itemRows input[value="504"]').is_checked()
    assert frame.locator("#selectionText").inner_text() == "已选择 1 件商品"
    assert not mock.previews and not mock.submissions

    frame.locator("#batchDiscountValue").fill("20")
    frame.locator("#applyDiscountBtn").click()
    assert frame.locator(f"#price-{ITEM_LOCAL_ID}").input_value() == "400.00"
    assert frame.locator("#price-503").input_value() == "450"
    assert "至少降价 10%" in frame.locator("#itemRows").inner_text()
    mock.screenshot("02-selected-product-and-price.png")

    assert frame.locator("#enrollBtn").is_enabled()
    frame.locator("#enrollBtn").click()
    assert mock.submissions == [{
        "promotion_id": PROMOTION_ID, "rows": [{
            "item_local_id": ITEM_LOCAL_ID, "deal_price": "400.00",
            "start_date": "", "finish_date": "",
        }],
    }]
    assert frame.locator("#previewDialog").is_hidden()
    assert not mock.previews
    frame.locator("#selectionText").filter(has_text="已选择 0 件商品").wait_for()
    assert len(mock.job_requests) == 1
    poll_query = mock.job_requests[0]
    assert poll_query["summary"] == ["1"]
    assert poll_query["job_id"] == ["offline-enrollment-job"]
    assert len(mock.item_requests) == 2
    assert frame.locator('#itemRows input[type="checkbox"]:checked').count() == 0

    enrollment_page.locator('button[data-tab="store-links"]').click()
    enrollment_page.wait_for_function("storeLinksLoaded && !storeLinkListLoading && storeLinkRows[0]?.available_promotions?.length === 0")
    assert 1 <= len(mock.list_refreshes) <= 2
    refresh_query = parse_qs(urlsplit(mock.list_refreshes[-1]).query)
    assert refresh_query["page"] == ["2"]
    assert refresh_query["organization_key"] == [ORGANIZATION]
    assert enrollment_page.locator('button[onclick^="openStoreLinkPromotion"]').count() == 0
    assert "折扣价：MXN 400" in enrollment_page.locator("#store-link-body").inner_text()
    assert all(urlsplit(url).netloc == "console.test" for url in mock.requests)


def test_unsupported_clearance_enrollment_entry_is_disabled(enrollment_page):
    mock = EnrollmentMock(enrollment_page)
    clearance = enrollment_page.locator("#store-link-body .market-metric-note").filter(
        has=enrollment_page.get_by_text("清仓活动", exact=True),
    )
    button = clearance.get_by_role("button", name="报名", exact=True)
    assert button.is_disabled()
    assert button.get_attribute("title") == "该活动类型当前仅支持查看"
    assert "该活动类型当前仅支持查看" in clearance.inner_text()
    enrollment_page.evaluate("openStoreLinkPromotion(42, 72)")
    assert not mock.promotions_opened and not mock.previews and not mock.submissions
    assert "该活动类型当前仅支持查看" in enrollment_page.locator("#store-link-message").inner_text()


@pytest.mark.parametrize("target_status,campaign_status,message", [
    ("started", "started", "当前已不可报名"),
    ("missing", "started", "当前已不可报名"),
    ("candidate", "finished", "该活动已结束"),
])
def test_stale_candidate_does_not_select_other_products(
    enrollment_page, target_status, campaign_status, message,
):
    mock = EnrollmentMock(enrollment_page, target_status, campaign_status)
    frame = mock.open_enrollment()
    assert message in frame.locator("#feedback").inner_text()
    assert frame.locator('#itemRows input[type="checkbox"]:checked').count() == 0
    assert frame.locator("#selectionText").inner_text() == "已选择 0 件商品"
    assert frame.locator("#enrollBtn").is_disabled()
    assert frame.locator("#applyDiscountBtn").is_disabled()
    assert not mock.previews and not mock.submissions


def test_store_link_can_cancel_enrollment_and_refresh_marker(enrollment_page):
    mock = EnrollmentMock(enrollment_page)
    errors = []
    enrollment_page.on('pageerror', lambda error: errors.append(str(error)))
    row = dict(mock.link, available_promotions=[], promotion_applied=True,
        enrolled_promotions=[dict(mock.link['available_promotions'][0], status_raw='pending_approval',
                                  price=400, original_price=500, current_discount_percent=20)])
    enrollment_page.evaluate('row => renderStoreLinkRows([row])', row)
    submissions = []
    def cancel(route):
        submissions.append((route.request.url, route.request.post_data_json))
        mock.json(route, {'cancelled_count': 1})
    enrollment_page.route('**/api/promotions/71/withdraw*', cancel)
    enrollment_page.route('**/api/store-links?*', lambda route: mock.json(route, {
        'rows':[dict(row, enrolled_promotions=[], promotion_applied=False)], 'page':1,'pages':1,'total':1}))
    button = enrollment_page.get_by_role('button',name='取消活动',exact=True)
    button.scroll_into_view_if_needed()
    assert '当前折扣：降价 20%' in enrollment_page.locator('#store-link-body').inner_text()
    mock.screenshot('04-cancel-and-current-discount.png')
    enrollment_page.once('dialog', lambda dialog: dialog.accept())
    button.click()
    enrollment_page.wait_for_function("document.getElementById('store-link-message').textContent === '活动已取消'")
    assert len(submissions) == 1
    assert submissions[0][1] == {'item_id':ITEM_ID}
    assert parse_qs(urlsplit(submissions[0][0]).query)['organization_key'] == [ORGANIZATION]
    assert enrollment_page.get_by_role('button',name='取消活动',exact=True).count() == 0
    assert '已报活动' not in enrollment_page.locator('#store-link-body').inner_text()
    assert not errors
