"""Real sync dialog interactions with isolated, offline API responses."""
import json
from urllib.parse import urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader

from test_console_ui_browser import ROOT, console_browser


@pytest.fixture
def sync_page(console_browser, request):
    role = getattr(request, "param", "super_admin")
    html = Environment(loader=FileSystemLoader(ROOT / "bit/templates")).get_template("index.html").render(
        current_user={"username": "preview", "display_name": "测试", "role_key": role, "is_admin": True},
        can_change_task_workers=False, runtime_role="server",
        url_for=lambda endpoint, filename: "/static/" + filename,
    )
    page = console_browser.new_page(viewport={"width": 1440, "height": 1000})
    state = {"running": True, "message": "正在同步 Excel"}
    starts = []

    def route(handler):
        path = urlsplit(handler.request.url).path
        if path == "/":
            handler.fulfill(body=html, content_type="text/html")
            return
        if path.startswith("/static/"):
            handler.fulfill(path=str(ROOT / "bit" / path.lstrip("/")))
            return
        if path == "/api/execution-agents":
            data = {"agents": [
                {"agent_id": "agent-a", "name": "电脑 A", "online": True},
                {"agent_id": "agent-b", "name": "电脑 B", "online": True},
                {"agent_id": "agent-offline", "name": "离线电脑", "online": False},
            ]}
        elif path == "/api/orders/zying-sync/stop":
            state.update(running=False, phase="stopped", message="任务已结束")
            data = state
        elif path == "/api/orders/zying-sync/start":
            starts.append(handler.request.post_data_json)
            state.update(running=True, phase="loading")
            data = state
        elif path == "/api/orders/zying-sync/status":
            data = state
        else:
            data = []
        handler.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))

    page.route("**/*", route)
    page.goto("http://console.test/")
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-orders').classList.add('active');
    }""")
    yield page, state, starts
    page.close()


@pytest.mark.parametrize("width", [1440, 1024, 390])
def test_sync_only_offers_excel_and_plugin(sync_page, width):
    page, state, starts = sync_page
    state.update(running=False, phase="idle", message="等待上传 Excel")
    page.set_viewport_size({"width": width, "height": 1000})
    calls = []
    page.on("request", lambda request: calls.append(urlsplit(request.url).path))
    page.locator("#order-zying-sync-open").click()
    dialog = page.locator("#order-zying-sync-dialog")
    assert dialog.is_visible()
    assert dialog.locator('#order-zying-target, #order-zying-agent, #order-zying-start, #order-zying-confirm').count() == 0
    assert dialog.get_by_role("link", name="下载泽顺插件（Chrome / Edge）").is_visible()
    assert dialog.locator('#order-zying-file').is_enabled()
    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
    page.keyboard.press("Tab")
    assert dialog.evaluate("el => el.contains(document.activeElement)")
    page.keyboard.press("Escape")
    assert not dialog.is_visible()
    assert '/api/execution-agents' not in calls
    assert not starts


@pytest.mark.parametrize("width,source", [(1440, "file"), (390, "drop")])
def test_excel_upload_and_drop_use_current_filters_and_render_results(sync_page, width, source):
    page, state, starts = sync_page
    state.update(running=False, phase="idle", message="等待上传 Excel")
    page.set_viewport_size({"width": width, "height": 1000})
    uploads = []
    def upload(route):
        uploads.append(route.request)
        state.update(running=False, phase="completed", message="Excel 同步完成", total=1, completed=1,
                     updated=1, skipped=0, failed=0, results=[{
                         "order_id": "20000000000000001", "message": "已同步", "purchase_order": "P1",
                         "purchase_tracking": "00123", "purchase_cost": "18.50",
                     }])
        route.fulfill(status=202, content_type="application/json", body=json.dumps({"status": "success", "data": state}))
    page.route("**/api/orders/zying-sync/upload?**", upload)
    page.evaluate("openZyingOrderSync()")
    assert page.locator("#order-zying-upload").is_disabled()
    if source == "file":
        page.locator("#order-zying-file").set_input_files({"name": "智赢订单.xlsx", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "buffer": b"workbook"})
    else:
        page.locator("#order-zying-dropzone").evaluate("""el => {
            const dataTransfer = new DataTransfer();
            dataTransfer.items.add(new File(['workbook'], '智赢订单.xlsx'));
            el.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer}));
        }""")
    assert page.locator("#order-zying-file-name").inner_text() == "智赢订单.xlsx"
    assert page.locator("#order-zying-file").evaluate("el => el.files[0].name") == "智赢订单.xlsx"
    assert page.locator("#order-zying-upload").is_enabled()
    output = ROOT / "artifacts/zying-excel-upload-20261009"
    output.mkdir(parents=True, exist_ok=True)
    page.locator("#order-zying-sync-dialog").screenshot(path=str(output / f"upload-{width}.png"))
    assert page.locator("#order-zying-sync-dialog").evaluate("el => el.scrollWidth <= el.clientWidth")
    page.locator("#order-zying-upload").click()
    page.wait_for_function("document.getElementById('order-zying-status').textContent.includes('Excel 同步完成')")
    assert len(uploads) == 1
    assert 'multipart/form-data; boundary=' in uploads[0].headers['content-type']
    assert b'name="file"' in uploads[0].post_data_buffer
    assert "采购 P1" in page.locator("#order-zying-results").inner_text()
    assert "物流 00123" in page.locator("#order-zying-results").inner_text()
    assert "¥18.50" in page.locator("#order-zying-results").inner_text()
    assert page.locator("#order-zying-upload").is_disabled()
    assert not starts


def test_excel_upload_validation_and_retry_after_rejection(sync_page):
    page, state, starts = sync_page
    state.update(running=False, phase="idle", message="等待上传 Excel")
    page.evaluate("openZyingOrderSync()")
    page.locator("#order-zying-file").set_input_files({"name": "订单.xls", "mimeType": "application/vnd.ms-excel", "buffer": b"old"})
    assert page.locator("#order-zying-upload").is_disabled()
    assert "另存为" in page.locator("#order-zying-upload-message").inner_text()
    page.route("**/api/orders/zying-sync/upload?**", lambda route: route.fulfill(
        status=400, content_type="application/json", body=json.dumps({"status": "error", "message": "未识别到平台订单号"})))
    page.locator("#order-zying-file").set_input_files({"name": "订单.xlsx", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "buffer": b"workbook"})
    page.locator("#order-zying-upload").click()
    page.wait_for_function("document.getElementById('order-zying-upload-message').textContent.includes('未识别到平台订单号')")
    assert page.locator("#order-zying-upload").is_enabled()
    assert page.locator("#order-zying-file").is_enabled()
    assert not starts
