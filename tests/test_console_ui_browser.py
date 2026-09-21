"""Exercise the real console template offline; never contact a business API."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("status", ["success", "partial"])
def test_api_reputation_shop_name_and_database_refresh(console_page, status):
    page = console_page
    requests = []

    def latest(route):
        requests.append(route.request.url)
        route.fulfill(content_type="application/json", body=json.dumps({
            "status": "success", "data": {
                "latest_submit_time": "2026-09-15 14:05:00",
                "rows": [{"店铺名": "泽顺测试旗舰店", "站点": "墨西哥",
                          "声誉颜色": "绿色", "总单量": 219}],
            },
        }))

    page.route("**/api/reputation/latest", latest)
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-api-reputation').classList.add('active');
        apiReputationRows = [{store_name: '泽顺测试旗舰店', site_name: '墨西哥', sales_completed: 219}];
        renderApiReputationTable();
    }""")
    name = page.locator("#api-reputation-body td:first-child strong")
    assert name.inner_text() == "泽顺测试旗舰店"
    assert name.is_visible()
    bounds = name.bounding_box()
    cell = page.locator("#api-reputation-body td:first-child").bounding_box()
    assert bounds["x"] >= cell["x"]
    assert bounds["x"] + bounds["width"] <= cell["x"] + cell["width"]
    checkbox = page.locator('#api-reputation-body input[type="checkbox"]')
    assert checkbox.bounding_box()["width"] <= 20
    name.click()
    assert checkbox.is_checked()

    page.evaluate("""status => {
        window.finishedReputation = {running: false, status, finished_at: '2026-09-15 14:05:00', rows: apiReputationRows};
        renderApiReputation({running: true, status: 'running', rows: apiReputationRows});
    }""", status)
    assert not requests
    page.evaluate("renderApiReputation(window.finishedReputation)")
    page.wait_for_function("reputationLoaded && reputationRows[0]?.['总单量'] === 219")
    assert page.locator("#reputation-time").inner_text() == "2026-09-15 14:05:00"
    assert "泽顺测试旗舰店" in page.locator("#reputation-body").inner_text()
    page.evaluate("renderApiReputation(window.finishedReputation)")
    assert len(requests) == 1


@pytest.fixture(scope="module")
def console_browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        candidates = [
            os.environ.get("CONSOLE_TEST_BROWSER", ""), pw.chromium.executable_path,
            "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        ]
        executable = next((path for path in candidates if path and Path(path).is_file()), None)
        if not executable:
            pytest.skip("Install Playwright Chromium or set CONSOLE_TEST_BROWSER")
        browser = pw.chromium.launch(executable_path=executable, headless=True)
        yield browser
        browser.close()


@pytest.fixture
def console_page(console_browser):
    env = Environment(loader=FileSystemLoader(ROOT / "bit/templates"))
    html = env.get_template("index.html").render(
        current_user={"username": "preview", "display_name": "界面预览", "is_admin": True},
        runtime_role="server", url_for=lambda endpoint, filename: "/static/" + filename,
    )
    page = console_browser.new_page(viewport={"width": 1440, "height": 1000})

    def route(request):
        path = urlsplit(request.request.url).path
        if path == "/":
            request.fulfill(body=html, content_type="text/html")
        elif path.startswith("/static/"):
            request.fulfill(path=str(ROOT / "bit" / path.lstrip("/")))
        else:
            request.fulfill(content_type="application/json", body=json.dumps(
                {"success": True, "data": [], "items": [], "stores": [], "groups": [], "total": 0}
            ))

    page.route("**/*", route)
    page.goto("http://console.test/")
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-orders').classList.add('active');
        const select = document.getElementById('order-store-filter');
        select.innerHTML = '<option value="mx">墨西哥旗舰店</option><option value="br">巴西精选店</option>';
        renderMercadoPublishPicker(select);
    }""")
    yield page
    page.close()


def test_picker_search_preserves_selection_and_handles_dynamic_options(console_page):
    page = console_page
    picker = page.locator("#order-store-filter-picker")
    page.evaluate("""() => {
        window.filterChanges = 0;
        document.getElementById('order-store-filter').addEventListener('change', () => filterChanges++);
    }""")
    page.locator("#order-store-filter-trigger").click()
    search = picker.locator('input[type="search"]')
    search.fill("巴西")
    assert picker.locator(".market-multi-option:visible").count() == 1
    page.evaluate("document.querySelector('.order-filters').addEventListener('submit', () => window.unexpectedSubmit = true)")
    search.press("Enter")
    assert not page.evaluate("Boolean(window.unexpectedSubmit)")
    picker.locator('.market-multi-option:visible input').check()
    assert page.locator("#order-store-filter").evaluate("s => [...s.selectedOptions].map(o => o.value)") == ["br"]
    assert page.evaluate("filterChanges") == 1
    search.fill("不存在")
    assert picker.locator(".zs-picker-empty").is_visible()
    assert page.locator("#order-store-filter").evaluate("s => s.selectedOptions.length") == 1
    page.evaluate("""() => {
        const select = document.getElementById('order-store-filter');
        select.add(new Option('不存在于旧列表的新店', 'new'));
        renderMercadoPublishPicker(select);
    }""")
    assert picker.locator(".market-multi-option:visible").count() == 1
    assert not picker.locator(".zs-picker-empty").is_visible()
    assert picker.locator(".zs-picker-search").count() == 1


def test_picker_keyboard_and_bulk_selection(console_page):
    page = console_page
    trigger = page.locator("#order-store-filter-trigger")
    picker = page.locator("#order-store-filter-picker")
    trigger.focus()
    page.keyboard.press("ArrowDown")
    assert picker.locator('input[type="search"]').evaluate("e => e === document.activeElement")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Space")
    assert page.locator("#order-store-filter").evaluate("s => s.selectedOptions.length") == 1
    picker.get_by_role("button", name="全选", exact=True).click()
    assert page.locator("#order-store-filter").evaluate("s => s.selectedOptions.length") == 2
    picker.get_by_role("button", name="清空", exact=True).click()
    assert page.locator("#order-store-filter").evaluate("s => s.selectedOptions.length") == 0
    page.keyboard.press("Escape")
    assert trigger.get_attribute("aria-expanded") == "false"
    assert trigger.evaluate("e => e === document.activeElement")
    page.keyboard.press("ArrowDown")
    page.locator("#order-search-input").focus()
    assert trigger.get_attribute("aria-expanded") == "false"


@pytest.mark.parametrize("width", [1440, 1024, 390])
def test_filter_layout_and_popup_fit_viewport(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    heights = page.locator(".order-filters").evaluate("""el =>
        [...el.querySelectorAll('input:not([type=checkbox]), select:not([multiple]), .market-multi-trigger')]
        .map(e => e.getBoundingClientRect().height).filter(Boolean)
    """)
    assert set(heights) == {38}
    page.locator("#order-store-filter-trigger").click()
    box = page.locator("#order-store-filter-picker .market-multi-panel").bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= width
    assert box["y"] >= 0 and box["y"] + box["height"] <= 1000


def test_order_density_switch_and_compact_bulk_toolbar(console_page):
    page = console_page
    board = page.locator("#order-board")
    bulk_bar = page.locator("#order-bulk-bar")

    assert board.evaluate("el => el.classList.contains('order-density-compact')")
    assert page.get_by_role("button", name="紧凑").get_attribute("aria-pressed") == "true"
    assert bulk_bar.locator(".order-bulk-group").first.evaluate("el => getComputedStyle(el).display") == "none"

    page.get_by_role("button", name="舒适").click()
    assert not board.evaluate("el => el.classList.contains('order-density-compact')")
    assert page.get_by_role("button", name="舒适").get_attribute("aria-pressed") == "true"
    assert page.evaluate("localStorage.getItem('zeshun-order-density')") == "comfortable"

    page.evaluate("selectedOrderIds.add('10001'); updateOrderSelectionState()")
    assert bulk_bar.evaluate("el => el.classList.contains('active')")
    assert bulk_bar.locator(".order-bulk-group").first.evaluate("el => getComputedStyle(el).display") == "flex"
