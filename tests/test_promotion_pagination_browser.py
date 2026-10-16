"""Verify promotion product pagination through the real UI and offline APIs."""
import json
import math
from urllib.parse import parse_qs, urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader

from test_console_ui_browser import ROOT, console_browser  # noqa: F401


ARTIFACTS = ROOT / "artifacts/promotion-pagination-ui"


class PromotionPaginationMock:
    def __init__(self, page, *, target_item_id=None, count=123):
        self.page = page
        self.item_requests = []
        self.previews = []
        self.enrollments = []
        self.job_requests = []
        self.errors = []
        self.held_pages = set()
        self.pending = {}
        self.failed_pages = set()
        self.campaign = {
            "id": 71, "promotion_id": "MLM-PAGED-71", "name": "多商品参保活动",
            "store_name": "墨西哥旗舰店", "seller_id": "offline-seller", "site_id": "MLM",
            "promotion_type": "DEAL", "status_raw": "started", "candidate_count": count,
            "active_item_count": 0, "item_count": count, "last_synced_at": "2026-10-07T02:00:00Z",
        }
        self.rows = [{
            "id": number, "item_id": f"MLM{number:05d}", "title": f"分页测试商品 {number}",
            "status_raw": "candidate", "original_price": 500, "price": 450,
            "min_price": 350, "max_price": 450, "currency_id": "MXN",
            "last_synced_at": "2026-10-07T02:00:00Z",
        } for number in range(1, count + 1)]
        env = Environment(loader=FileSystemLoader(ROOT / "bit/templates"))
        self.html = env.get_template("mercado_promotions.html").render(
            current_user={"username": "preview", "display_name": "离线分页测试", "is_platform_admin": True},
            embedded=False, url_for=lambda endpoint, filename: "/static/" + filename,
        )
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.route("**/*", self.route)
        query = "?organization_key=pagination-enterprise"
        if target_item_id:
            query += f"&promotion_id=71&item_id={target_item_id}"
        page.goto("http://promotion.test/promotions" + query)
        if target_item_id:
            page.locator(f'#itemRows tr:has-text("{target_item_id}")').wait_for()
        else:
            page.locator("#campaignRows button").wait_for()

    @staticmethod
    def json(route, data):
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))

    def item_data(self, query):
        size = int(query.get("page_size", ["50"])[0])
        requested_page = int(query.get("page", ["1"])[0])
        target = query.get("target_item_id", [""])[0]
        if target:
            index = next((index for index, row in enumerate(self.rows) if row["item_id"] == target), None)
            if index is not None:
                requested_page = index // size + 1
        pages = max(1, math.ceil(len(self.rows) / size))
        number = max(1, min(requested_page, pages))
        offset = (number - 1) * size
        return {"promotion": self.campaign, "rows": self.rows[offset:offset + size],
                "page": number, "page_size": size, "total": len(self.rows), "pages": pages}

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path
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
                "total": 1, "candidate_items": len(self.rows), "active_items": 0, "ending_soon": 0}})
        elif path == "/api/promotions/71/items":
            query = parse_qs(url.query)
            self.item_requests.append(query)
            number = int(query.get("page", ["1"])[0])
            if number in self.held_pages:
                self.pending[number] = (route, self.item_data(query))
            elif number in self.failed_pages:
                route.fulfill(status=503, content_type="application/json",
                              body=json.dumps({"status": "error", "message": "离线分页读取失败"}))
            else:
                self.json(route, self.item_data(query))
        elif path == "/api/promotions/preview":
            payload = request.post_data_json
            self.previews.append(payload)
            self.json(route, {"preview_id": "paged-preview", "ready_count": len(payload["rows"]),
                               "blocked_count": 0, "rows": [{
                                   "item_id": f"MLM{row['item_local_id']:05d}", "status": "ready",
                                   "deal_price": float(row["deal_price"]) if row["deal_price"] is not None else None, "currency_id": "MXN",
                               } for row in payload["rows"]]})
        elif path == "/api/promotions/enroll" and request.method == "POST":
            payload = request.post_data_json
            self.enrollments.append(payload)
            self.json(route, {"status": "running", "job_id": "paged-enrollment-job",
                              "submitted_count": len(payload["rows"]), "excluded_count": 0,
                              "excluded_reasons": []})
        elif path == "/api/promotions/jobs":
            query = parse_qs(url.query)
            if query.get("job_id"):
                self.job_requests.append(query)
                submitted = len(self.enrollments[-1]["rows"]) if self.enrollments else 0
                self.json(route, {"rows": [{
                    "job_id": "paged-enrollment-job", "action": "enroll",
                    "status": "succeeded", "total_count": submitted,
                    "success_count": submitted, "failed_count": 0, "unknown_count": 0,
                }]})
            else:
                self.json(route, {"rows": []})
        else:
            route.abort()

    def open(self):
        self.page.locator("#campaignRows button").click()
        self.wait_page(1)

    def wait_page(self, number, size=50):
        self.page.wait_for_function("""({number, size}) =>
            state.itemPage === number && state.itemPageSize === size && !state.itemPageLoading
        """, arg={"number": number, "size": size})

    def release(self, number):
        route, data = self.pending.pop(number)
        with self.page.expect_response(lambda response: urlsplit(response.url).path == "/api/promotions/71/items"):
            self.json(route, data)
        # Give the superseded fetch's JSON handler time to settle before checking the page.
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


@pytest.fixture
def pagination_page(console_browser):
    page = console_browser.new_page(viewport={"width": 1440, "height": 1100})
    yield page
    page.close()


def test_items_render_only_requested_page_and_allow_page_and_size_choices(pagination_page):
    mock = PromotionPaginationMock(pagination_page)
    mock.open()
    assert pagination_page.locator("#itemRows tr").count() == 50
    assert pagination_page.locator("#item-row-1").count() == 1
    assert pagination_page.locator("#item-row-51").count() == 0
    assert pagination_page.locator("#itemPageSelect option").count() == 3
    assert pagination_page.locator("#itemPagePrev").is_disabled()
    assert pagination_page.locator("#itemPageNext").is_enabled()
    assert "123" in pagination_page.locator("#itemPageSummary").inner_text()
    assert mock.item_requests[0]["page"] == ["1"]
    assert mock.item_requests[0]["page_size"] == ["50"]
    assert mock.item_requests[0]["organization_key"] == ["pagination-enterprise"]

    pagination_page.locator("#itemPageSelect").select_option("3")
    mock.wait_page(3)
    assert pagination_page.locator("#itemRows tr").count() == 23
    assert pagination_page.locator("#item-row-101").count() == 1
    assert pagination_page.locator("#item-row-100").count() == 0
    assert pagination_page.locator("#itemPageNext").is_disabled()
    assert pagination_page.locator("#itemPagePrev").is_enabled()

    pagination_page.locator("#itemPagePrev").click()
    mock.wait_page(2)
    assert pagination_page.locator("#item-row-51").count() == 1
    pagination_page.locator("#itemPageNext").click()
    mock.wait_page(3)

    for size, expected_pages in [(20, 7), (100, 2), (50, 3)]:
        pagination_page.locator("#itemPageSize").select_option(str(size))
        mock.wait_page(1, size)
        assert pagination_page.locator("#itemRows tr").count() == size
        assert pagination_page.locator("#itemPageSelect option").count() == expected_pages
        assert pagination_page.locator("#item-row-1").count() == 1
        assert mock.item_requests[-1]["page_size"] == [str(size)]
    assert not mock.errors
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    pagination_page.evaluate("window.scrollTo(0, 0)")
    pagination_page.screenshot(path=str(ARTIFACTS / "pagination-1440.png"))
    pagination_page.set_viewport_size({"width": 390, "height": 844})
    pagination_page.evaluate("window.scrollTo(0, 0)")
    pagination_page.screenshot(path=str(ARTIFACTS / "pagination-390.png"))


def test_switching_pages_preserves_selection_and_edited_prices_in_direct_enrollment(pagination_page):
    mock = PromotionPaginationMock(pagination_page)
    mock.open()
    pagination_page.locator('#itemRows input[value="1"]').check()
    pagination_page.locator("#price-1").fill("400")
    pagination_page.locator("#itemPageSelect").select_option("2")
    mock.wait_page(2)
    assert pagination_page.locator("#selectionText").inner_text() == "已选择 1 件商品"
    assert pagination_page.locator("#selectAll").is_checked() is False
    pagination_page.locator('#itemRows input[value="51"]').check()
    pagination_page.locator("#price-51").fill("420")
    pagination_page.locator("#itemPageSelect").select_option("1")
    mock.wait_page(1)
    assert pagination_page.locator('#itemRows input[value="1"]').is_checked()
    assert pagination_page.locator("#price-1").input_value() == "400"
    assert pagination_page.locator("#selectionText").inner_text() == "已选择 2 件商品"

    # Selecting and clearing this page must preserve the item selected on page 2.
    pagination_page.locator("#selectAll").check()
    assert pagination_page.locator("#selectionText").inner_text() == "已选择 51 件商品"
    pagination_page.locator("#selectAll").uncheck()
    assert pagination_page.locator("#selectionText").inner_text() == "已选择 1 件商品"
    pagination_page.locator('#itemRows input[value="1"]').check()
    pagination_page.locator("#itemPageSelect").select_option("2")
    mock.wait_page(2)
    assert pagination_page.locator('#itemRows input[value="51"]').is_checked()
    assert pagination_page.locator("#price-51").input_value() == "420"
    pagination_page.locator("#enrollBtn").click()
    assert {row["item_local_id"]: row["deal_price"] for row in mock.enrollments[-1]["rows"]} == {1: "400", 51: "420"}
    assert pagination_page.locator("#previewDialog").is_hidden()
    assert not mock.previews
    assert not mock.errors


def test_late_page_response_cannot_replace_a_more_recent_page(pagination_page):
    mock = PromotionPaginationMock(pagination_page)
    mock.open()
    mock.held_pages.add(2)
    pagination_page.locator("#itemPageSelect").select_option("2")
    pagination_page.wait_for_function("state.itemPageLoading")
    assert 2 in mock.pending
    assert pagination_page.locator("#itemPageSelect").is_enabled()
    pagination_page.locator("#itemPageSelect").select_option("3")
    mock.wait_page(3)
    mock.release(2)
    assert pagination_page.locator("#itemPageSelect").input_value() == "3"
    assert pagination_page.locator("#itemRows tr").count() == 23
    assert pagination_page.locator("#item-row-101").count() == 1
    assert pagination_page.locator("#item-row-51").count() == 0
    assert not mock.errors


def test_page_switch_keeps_new_size_while_size_change_is_still_loading(pagination_page):
    mock = PromotionPaginationMock(pagination_page)
    mock.open()
    mock.held_pages.add(1)
    pagination_page.locator("#itemPageSize").select_option("20")
    pagination_page.wait_for_function("state.itemPageLoading")
    assert 1 in mock.pending
    assert pagination_page.locator("#itemPageSize").input_value() == "20"
    pagination_page.locator("#itemPageSelect").select_option("3")
    mock.wait_page(3, 20)
    assert mock.item_requests[-1]["page"] == ["3"]
    assert mock.item_requests[-1]["page_size"] == ["20"]
    assert pagination_page.locator("#itemRows tr").count() == 20
    assert pagination_page.locator("#item-row-41").count() == 1
    assert pagination_page.locator("#item-row-61").count() == 0
    mock.release(1)
    assert pagination_page.locator("#itemPageSelect").input_value() == "3"
    assert pagination_page.locator("#itemPageSize").input_value() == "20"
    assert pagination_page.locator("#itemRows tr").count() == 20
    assert pagination_page.locator("#item-row-41").count() == 1
    assert not mock.errors


def test_failed_page_retains_previous_page_and_can_be_retried(pagination_page):
    mock = PromotionPaginationMock(pagination_page)
    mock.open()
    pagination_page.locator('#itemRows input[value="1"]').check()
    pagination_page.locator("#price-1").fill("400")
    mock.failed_pages.add(2)
    pagination_page.locator("#itemPageSelect").select_option("2")
    pagination_page.locator("#feedback").filter(has_text="商品分页读取失败").wait_for()
    mock.wait_page(1)
    assert pagination_page.locator("#itemPageSelect").input_value() == "1"
    assert pagination_page.locator("#itemRows tr").count() == 50
    assert pagination_page.locator('#itemRows input[value="1"]').is_checked()
    assert pagination_page.locator("#price-1").input_value() == "400"
    mock.failed_pages.clear()
    pagination_page.locator("#itemPageSelect").select_option("2")
    mock.wait_page(2)
    assert pagination_page.locator("#item-row-51").count() == 1
    assert pagination_page.locator("#selectionText").inner_text() == "已选择 1 件商品"
    assert not mock.errors


def test_enrollment_deep_link_opens_target_on_later_page(pagination_page):
    mock = PromotionPaginationMock(pagination_page, target_item_id="MLM00121")
    mock.wait_page(3)
    assert len(mock.item_requests) == 1
    assert mock.item_requests[0]["target_item_id"] == ["MLM00121"]
    assert mock.item_requests[0]["page_size"] == ["50"]
    assert pagination_page.locator("#itemPageSelect").input_value() == "3"
    assert pagination_page.locator("#itemRows tr").count() == 23
    assert pagination_page.locator('#itemRows input[type="checkbox"]:checked').count() == 1
    assert pagination_page.locator('#itemRows input[value="121"]').is_checked()
    assert pagination_page.locator("#item-row-121").get_attribute("class") == "target-item"
    assert pagination_page.locator("#enrollBtn").inner_text() == "报名此商品"
    assert not mock.previews
    assert not mock.errors


def test_product_images_expand_outside_table_and_hide_on_page_change(pagination_page):
    page = pagination_page
    mock = PromotionPaginationMock(page)
    mock.open()
    page.evaluate("""() => {
        state.items.forEach(row => row.thumbnail = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><rect width="300" height="300" fill="blue"/></svg>'));
        state.items[0].net_proceeds_after = {amount:null, warning:'平台净收益超过同币种售价，金额或币种异常，待核实'};
        renderItems();
    }""")
    assert '异常，待核实' in page.locator('#net-after-1').inner_text()
    image = page.locator('#itemRows .product img').first
    image.hover()
    preview = page.locator('#productImagePreview')
    assert preview.is_visible()
    box = preview.bounding_box()
    assert box['width'] == 320
    assert box['x'] >= 0 and box['x'] + box['width'] <= 1440
    assert preview.locator('img').get_attribute('src') == image.get_attribute('src')
    page.mouse.move(5, 5)
    assert not preview.is_visible()
    image.focus()
    assert preview.is_visible()
    page.keyboard.press('Escape')
    assert not preview.is_visible()
    page.locator('#itemPageNext').click()
    mock.wait_page(2)
    assert not preview.is_visible()
    assert not mock.errors


@pytest.mark.parametrize('width',[1440,390])
def test_full_selection_parallel_read_can_be_cancelled_and_retried(pagination_page,width):
    page=pagination_page
    page.set_viewport_size({'width':width,'height':1000})
    mock=PromotionPaginationMock(page,count=3501)
    mock.open()
    mock.held_pages.add(2)
    page.locator('#selectAllPagesBtn').click()
    page.wait_for_function("state.selectionProgress==='7/8 页'")
    assert any(q.get('page')==['8'] and q.get('selection')==['1'] for q in mock.item_requests)
    assert page.locator('#selectAllPagesBtn').is_enabled()
    assert page.locator('#enrollBtn').is_disabled()
    page.locator('#selectAllPagesBtn').click()
    page.wait_for_function('!state.allPagesLoading')
    assert '已取消读取' in page.locator('#feedback').inner_text()
    assert page.locator('#selectionText').inner_text()=='已选择 0 件商品'
    mock.held_pages.clear()
    page.locator('#selectAllPagesBtn').click()
    page.wait_for_function('!state.allPagesLoading && state.selectedItems.size===3501')
    assert page.locator('#enrollBtn').is_enabled()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    output=ROOT/'artifacts/promotion-selection-speed';output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/f'selection-{width}.png'),full_page=True)
    assert not mock.previews and not mock.enrollments and not mock.errors


@pytest.mark.parametrize('width',[1440,390])
def test_smart_campaign_select_all_exposes_enrollment_at_official_price(pagination_page,width):
    page=pagination_page
    page.set_viewport_size({'width':width,'height':1000})
    mock=PromotionPaginationMock(page,count=10)
    mock.campaign.update(name='Best Shared Offers Sep',store_name='向子立5',site_id='MLB',promotion_type='SMART')
    for index,row in enumerate(mock.rows, start=1):
        row['offer_id']=f'OFFER-{index}'
    mock.open()
    assert page.locator('#enrollBtn').is_visible()
    assert page.locator('#withdrawBtn').is_visible()
    assert not page.locator('#batchDiscount').is_visible()
    page.locator('#selectAllPagesBtn').click()
    page.wait_for_function('!state.allPagesLoading && state.selectedItems.size===10')
    assert page.locator('#enrollBtn').is_enabled()
    page.locator('#enrollBtn').focus()
    page.keyboard.press('Enter')
    assert len(mock.enrollments)==1
    assert len(mock.enrollments[0]['rows'])==10
    assert all(r['deal_price'] is None for r in mock.enrollments[0]['rows'])
    assert page.locator('#previewDialog').is_hidden()
    assert not mock.previews
    page.locator('#selectionText').filter(has_text='已选择 0 件商品').wait_for()
    assert len(mock.job_requests)==1
    query=mock.job_requests[0]
    assert query['summary']==['1'] and query['job_id']==['paged-enrollment-job']
    assert mock.job_requests==[query]
    target=ROOT/'artifacts/promotion-smart-enrollment';target.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(target/f'smart-{width}.png'),full_page=True)
    assert not mock.errors
