import json

from test_console_ui_browser import console_browser, console_page


def test_video_dialog_upload_and_review_message(console_page):
    page = console_page
    requests = []

    def submit(route):
        requests.append(route.request)
        route.fulfill(content_type="application/json", body=json.dumps({
            "status": "success", "message": "视频已提交美客多，等待平台审核",
        }))

    page.route("**/api/store-links/42/video", submit)
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-store-links').classList.add('active');
        renderStoreLinkRows([{id: 42, item_id: 'MLM123', site_id: 'MLM', status: 'active', title: '测试商品'}]);
    }""")
    page.get_by_role("button", name="上传视频", exact=True).click()
    assert page.locator("#store-link-video-dialog").is_visible()
    assert "MLM123" in page.locator("#store-link-video-target").inner_text()
    page.get_by_role("button", name="上传到美客多", exact=True).click()
    assert "请选择非空" in page.locator("#store-link-video-message").inner_text()
    assert not requests
    page.locator("#store-link-video-file").set_input_files({
        "name": "demo.mp4", "mimeType": "video/mp4", "buffer": b"test-video",
    })
    page.get_by_role("button", name="上传到美客多", exact=True).click()
    page.wait_for_function("document.getElementById('store-link-video-message').textContent.includes('等待平台审核')")
    assert len(requests) == 1
    assert "multipart/form-data" in requests[0].headers["content-type"]
    assert page.get_by_role("button", name="上传到美客多", exact=True).is_disabled()
    page.locator("#store-link-video-close").click()
    assert not page.locator("#store-link-video-dialog").is_visible()
