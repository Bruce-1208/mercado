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
    state = {"running": True, "message": "等待 Agent 执行"}
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


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("phase", [None, "waiting_login", "syncing", "stopping"])
def test_active_task_does_not_lock_next_execution_selection(sync_page, width, phase):
    page, state, starts = sync_page
    state.update(phase=phase, stop_requested=phase == "stopping")
    page.set_viewport_size({"width": width, "height": 1000})
    page.locator("#order-zying-sync-open").click()
    page.wait_for_function("document.getElementById('order-zying-target-note').hidden === false")
    target = page.locator("#order-zying-target")
    agent = page.locator("#order-zying-agent")
    assert target.is_enabled() and agent.is_enabled()
    assert page.locator("#order-zying-start").is_disabled()
    assert agent.locator("option").all_text_contents() == ["电脑 A", "电脑 B"]

    # A real click must reach the enabled control before choosing an option.
    target.click()
    assert target.evaluate("el => document.activeElement === el")
    target.select_option("server")
    assert target.input_value() == "server"
    assert agent.is_hidden()
    page.evaluate("pollZyingOrderSync()")
    assert target.input_value() == "server" and target.is_enabled()
    target.select_option("agent")
    agent.select_option("agent-b")
    page.evaluate("pollZyingOrderSync()")
    assert agent.input_value() == "agent-b"
    assert not starts


@pytest.mark.parametrize("target", ["agent", "server"])
def test_stop_then_start_uses_new_selection(sync_page, target):
    page, state, starts = sync_page
    page.evaluate("openZyingOrderSync()")
    page.locator("#order-zying-target").select_option(target)
    if target == "agent":
        page.locator("#order-zying-agent").select_option("agent-b")
    page.locator("#order-zying-stop").click()
    page.wait_for_function("!document.getElementById('order-zying-start').disabled")
    assert page.locator("#order-zying-target-note").is_hidden()
    page.locator("#order-zying-start").click()
    page.wait_for_function("document.getElementById('order-zying-start').disabled")
    assert starts == [{"execution_target": target, "agent_id": "agent-b" if target == "agent" else "agent-a"}]


@pytest.mark.parametrize("sync_page", ["operator", "enterprise_admin"], indirect=True)
def test_non_super_admin_can_choose_computer_but_not_server(sync_page):
    page, state, starts = sync_page
    page.evaluate("openZyingOrderSync()")
    assert page.locator("#order-zying-target option").all_text_contents() == ["本机 Agent"]
    page.locator("#order-zying-agent").select_option("agent-b")
    assert page.locator("#order-zying-agent").input_value() == "agent-b"
    assert not starts


@pytest.mark.parametrize("width,source", [(1440, "file"), (390, "drop")])
def test_excel_upload_and_drop_use_current_filters_and_render_results(sync_page, width, source):
    page, state, starts = sync_page
    state.update(running=False, phase="idle")
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
    state.update(running=False, phase="idle")
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
    assert page.locator("#order-zying-start").is_enabled()
    assert not starts
