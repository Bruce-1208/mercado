"""Offline queue interaction against the real workbench template."""
import json
from pathlib import Path

import pytest
from test_console_ui_browser import console_browser, console_page


@pytest.mark.parametrize("width", [1440, 1024, 390])
def test_submit_next_batch_while_first_runs(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    submissions = []
    rows = [{"id": i, "item_id": f"MLM{i}", "title": f"模拟商品 {i}",
             "status": "active", "price": 10} for i in (1, 2, 3)]

    def status(route):
        route.fulfill(content_type="application/json", body=json.dumps({
            "status": "success", "data": {
                "task_id": "batch-1", "running": True, "status": "running",
                "total_links": 1, "processed_links": 0,
                "queued_batches": max(0, len(submissions) - 1),
                "message": "模拟第一批执行中", "logs": ["模拟任务日志"],
            },
        }))

    def submit(route):
        submissions.append(route.request.post_data_json)
        route.fulfill(status=202, content_type="application/json", body=json.dumps({
            "status": "success", "data": {
                "task_id": f"batch-{len(submissions)}", "running": True,
                "status": "queued", "message": "模拟批次已排队",
            },
        }))

    page.route("**/api/store-links/bulk-update/status", status)
    page.route("**/api/store-links/bulk-update", submit)
    page.route("**/api/store-links?*", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps({"status": "success", "data": {
            "rows": rows, "total": 3, "page": 1, "pages": 1,
        }})))
    page.evaluate("""rows => {
        switchTab('store-links');
        storeLinkListLoading = false;
        renderStoreLinkRows(rows);
    }""", rows)
    page.wait_for_function("!storeLinkListLoading")
    first = page.locator('.store-link-row-select[value="1"]')
    first.focus()
    first.press("Space")
    assert first.is_checked()
    assert first.evaluate("el => el === document.activeElement")
    page.locator("#store-link-bulk-weight").fill("500")
    button = page.locator("#store-link-bulk-button")
    button.focus()
    button.press("Enter")
    page.wait_for_function("!storeLinkSubmitting && selectedStoreLinkIds.size === 0")
    assert submissions == [{"link_ids": [1], "weight_g": 500}]
    assert page.evaluate("storeLinkRemoteUpdateRunning")

    page.locator('.store-link-row-select[value="2"]').check()
    page.locator('.store-link-row-select[value="3"]').check()
    page.locator("#store-link-bulk-price").fill("25")
    assert button.is_enabled()
    assert button.inner_text() == "提交下一批修改"
    button.click()
    page.wait_for_function("!storeLinkSubmitting && selectedStoreLinkIds.size === 0")
    assert submissions[1] == {"link_ids": [2, 3], "price": 25}
    assert page.evaluate("storeLinkRemoteUpdateRunning")
    page.wait_for_function("document.getElementById('store-link-message').textContent.includes('后续排队 1 批')")

    # Navigation and polling keep the queue alive and leave selection usable.
    page.evaluate("switchTab('overview'); switchTab('store-links')")
    page.wait_for_function("!storeLinkListLoading")
    page.locator('.store-link-row-select[value="1"]').check()
    assert button.is_enabled()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    button.focus()
    page.keyboard.press("Tab")
    page.keyboard.press("Shift+Tab")
    assert button.evaluate("el => el === document.activeElement && el.matches(':focus-visible') && (getComputedStyle(el).outlineStyle !== 'none' || getComputedStyle(el).boxShadow !== 'none')")
    output = Path("artifacts/store-link-batch-queue-20261015")
    output.mkdir(parents=True, exist_ok=True)
    page.locator("#tab-store-links .store-link-bulk").screenshot(path=str(output / f"queue-{width}.png"))


def test_rejected_submission_preserves_selection_and_fields(console_page):
    page = console_page
    page.route("**/api/store-links/bulk-update", lambda route: route.fulfill(
        status=409, content_type="application/json", body=json.dumps({
            "status": "running", "message": "模拟旧服务拒绝任务",
        })))
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-store-links').classList.add('active');
        renderStoreLinkRows([{id: 1, item_id: 'MLM1', title: '模拟商品', status: 'active'}]);
        selectedStoreLinkIds.add(1);
        updateStoreLinkSelection();
    }""")
    page.locator("#store-link-bulk-price").fill("25")
    page.locator("#store-link-bulk-button").click()
    page.wait_for_function("!storeLinkSubmitting")
    assert page.locator('.store-link-row-select[value="1"]').is_checked()
    assert page.locator("#store-link-bulk-price").input_value() == "25"
    assert "模拟旧服务拒绝任务" in page.locator("#store-link-message").inner_text()


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('body', ['Internal Server Error', '<html>Bad Gateway</html>', 'null'])
def test_non_json_submission_keeps_input_and_does_not_retry(console_page, width, body):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    submissions = []
    def reject(route):
        submissions.append(route.request.post_data_json)
        route.fulfill(status=500, content_type='text/plain', body=body)
    page.route('**/api/store-links/bulk-update', reject)
    page.evaluate('''() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-store-links').classList.add('active');
        renderStoreLinkRows([{id: 1, item_id: 'MLM1', title: '模拟商品', status: 'active'}]);
        selectedStoreLinkIds.add(1);
        updateStoreLinkSelection();
    }''')
    page.locator('#store-link-bulk-price').fill('25')
    page.locator('#store-link-bulk-button').click()
    page.wait_for_function("!storeLinkSubmitting && document.getElementById('store-link-message').textContent.includes('HTTP 500')")
    assert len(submissions) == 1
    assert page.locator('.store-link-row-select[value="1"]').is_checked()
    assert page.locator('#store-link-bulk-price').input_value() == '25'
    assert page.locator('#store-link-bulk-button').is_enabled()
    assert 'Unexpected token' not in page.locator('#store-link-message').inner_text()
