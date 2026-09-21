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


def test_ai_video_picker_shows_failure_and_success_in_the_active_dialog(console_page):
    page = console_page
    publishes = []

    def jobs(route):
        route.fulfill(content_type="application/json", body=json.dumps({
            "status": "success",
            "data": {
                "rows": [{
                    "id": "abc-123", "name": "测试备选", "status": "succeeded",
                    "duration": 10, "assets": [], "target": {},
                }],
                "settings": {
                    "configured": True, "public_base_configured": True,
                    "available_providers": ["Seedance 2.5"], "local_transcode_available": True,
                },
            },
        }))

    def publish(route):
        publishes.append(route.request)
        if len(publishes) == 1:
            route.fulfill(status=400, content_type="application/json", body=json.dumps({
                "status": "error", "message": "商品物流类型不完整",
            }))
        else:
            route.fulfill(content_type="application/json", body=json.dumps({
                "status": "success", "message": "视频已提交美客多，等待平台审核",
                "data": {"clip_uuid": "clip-accepted-1"},
            }))

    page.route("**/api/ai-videos/jobs?limit=30", jobs)
    page.route("**/api/ai-videos/jobs/abc-123/publish", publish)
    page.on("dialog", lambda dialog: dialog.accept())
    page.evaluate("""async () => {
        storeLinkRows = [{id: 42, item_id: 'MLM123', site_id: 'MLM', status: 'active', title: '测试商品'}];
        aiVideoJobsLoaded = false;
        await openAiVideoLibraryForLink(42);
    }""")

    button = page.get_by_role("button", name="上传到 MLM123", exact=True)
    button.click()
    message = page.locator("#store-link-ai-video-library-message")
    page.wait_for_function("document.getElementById('store-link-ai-video-library-message').textContent.includes('物流类型不完整')")
    assert "上传失败" in message.inner_text()
    assert button.is_enabled()

    button.click()
    page.wait_for_function("document.getElementById('store-link-ai-video-library-message').textContent.includes('clip-accepted-1')")
    assert "等待平台审核" in message.inner_text()
    submitted = page.locator("#store-link-ai-video-library-dialog .ai-video-picker-item button")
    assert submitted.is_disabled()
    assert submitted.inner_text() == "已提交审核"
    assert page.locator("#store-link-ai-video-library-dialog").is_visible()
    assert len(publishes) == 2
