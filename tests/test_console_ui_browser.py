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


def test_reputation_columns_toggle_numeric_sort_and_keep_missing_values_last(console_page):
    page = console_page
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-reputation').classList.add('active');
        reputationRows = [
            {'店铺名': '店铺甲', '站点': '墨西哥', '侵权数量': 2, '权利人数量': 1,
             '声誉颜色': '红色', '总单量': '10', '投诉率': '1.5%', '延误率': '8%',
             '取消率': '0.5%', '增加或减少': '下滑'},
            {'店铺名': '店铺乙', '站点': '巴西', '侵权数量': 10, '权利人数量': 0,
             '声誉颜色': '绿色', '总单量': '1,200', '投诉率': '0.5%', '延误率': '12%',
             '取消率': '2%', '增加或减少': '增长'},
            {'店铺名': '店铺丙', '站点': '智利', '侵权数量': -1, '权利人数量': null,
             '声誉颜色': '黄色', '总单量': '-', '投诉率': '-', '延误率': '-',
             '取消率': '-', '增加或减少': '持平'},
        ];
        reputationSortKey = '';
        reputationSortDirection = 'asc';
        renderLatestReputationRows();
    }""")

    buttons = page.locator(".reputation-sort-button")
    assert buttons.count() == 9
    infraction = page.locator('.reputation-sort-button[data-sort-key="infraction_count"]')
    infraction.click()
    assert page.locator("#reputation-body tr td:first-child span").all_inner_texts() == [
        "店铺甲", "店铺乙", "店铺丙",
    ]
    assert infraction.locator(".reputation-sort-indicator").inner_text() == "↑"
    assert infraction.get_attribute("aria-sort") == "ascending"

    infraction.click()
    assert page.locator("#reputation-body tr td:first-child span").all_inner_texts() == [
        "店铺乙", "店铺甲", "店铺丙",
    ]
    assert infraction.locator(".reputation-sort-indicator").inner_text() == "↓"
    assert infraction.get_attribute("aria-sort") == "descending"

    page.locator('.reputation-sort-button[data-sort-key="color"]').click()
    assert page.locator("#reputation-body tr td:first-child span").all_inner_texts() == [
        "店铺甲", "店铺丙", "店铺乙",
    ]

    for key in (
        "rights_holder_count", "order_total", "complaint_rate", "delay_rate",
        "cancellation_rate", "change_direction",
    ):
        button = page.locator(f'.reputation-sort-button[data-sort-key="{key}"]')
        button.click()
        assert button.get_attribute("aria-sort") == "ascending"
        button.click()
        assert button.get_attribute("aria-sort") == "descending"


def test_reputation_defaults_to_site_seven_day_orders_and_toggles_sort(console_page):
    page = console_page
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-reputation').classList.add('active');
        reputationRows = [
            {'店铺名': '同店铺', '站点': '墨西哥', '七天单量': 12, 'orders_current_7d': 999},
            {'店铺名': '同店铺', '站点': '巴西', '七天单量': '1,200'},
            {'店铺名': '无数据', '站点': '智利', '七天单量': null},
            {'店铺名': '兼容数据', '站点': '哥伦比亚', 'orders_current_7d': 35},
            {'店铺名': '零单量', '站点': '阿根廷', '七天单量': 0},
        ];
        renderLatestReputationRows();
    }""")
    order_button = page.locator('.reputation-sort-button[data-sort-key="orders_7d"]')
    assert order_button.get_attribute('aria-sort') == 'descending'
    assert order_button.locator('.reputation-sort-indicator').inner_text() == '↓'
    assert page.locator('#reputation-body tr td:nth-child(2)').all_inner_texts() == [
        '巴西', '哥伦比亚', '墨西哥', '阿根廷', '智利',
    ]
    assert page.locator('#reputation-body tr td:nth-child(7)').all_inner_texts() == [
        '1,200', '35', '12', '0', '-',
    ]
    assert page.evaluate("reputationCountSortNumber('   ')") is None
    assert page.locator('.reputation-data-table thead th').count() == 17
    assert page.locator('#reputation-body tr:first-child td').count() == 17

    order_button.click()
    assert order_button.get_attribute('aria-sort') == 'ascending'
    assert page.locator('#reputation-body tr td:nth-child(2)').all_inner_texts() == [
        '阿根廷', '墨西哥', '哥伦比亚', '巴西', '智利',
    ]
    order_button.click()
    assert order_button.get_attribute('aria-sort') == 'descending'
    assert page.locator('#reputation-body tr td:nth-child(2)').all_inner_texts()[-1] == '智利'
    page.evaluate('reputationRows = []; renderLatestReputationRows()')
    assert page.locator('#reputation-body td.empty-state').get_attribute('colspan') == '17'


def test_reputation_warning_hover_preserves_latest_message_full_text(console_page):
    page = console_page
    message = ('Listing restriction — Oct 1, at 09:12\n'
               'Review all affected listings and upload the requested documents.\n'
               'Details: <script>untrusted alert</script> & "seller requirements".\n'
               'This final paragraph must remain visible in the full alert.')
    page.evaluate("""message => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-reputation').classList.add('active');
        reputationRows = [{'店铺名': '告警店铺', '站点': '墨西哥', '七天单量': 18, '系统告警': message}];
        renderLatestReputationRows();
    }""", message)
    cell = page.locator('#reputation-body .reputation-warning-cell')
    assert cell.locator('.reputation-warning-preview').text_content() == message
    assert cell.get_attribute('data-warning-detail') == message
    assert cell.locator('script').count() == 0
    cell.hover()
    tooltip = page.locator('#traffic-tooltip')
    assert tooltip.is_visible()
    assert tooltip.inner_text() == message
    cell.click()
    assert page.locator('#reputation-warning-popover').is_visible()
    assert page.locator('#reputation-warning-popover-body').inner_text() == message
    page.keyboard.press('Escape')
    assert not page.locator('#reputation-warning-popover').is_visible()


def test_long_reputation_warning_hover_stays_in_view_and_can_scroll_to_end(console_page):
    page = console_page
    message = '\n'.join(
        ['Latest account-risk warning']
        + [f'Paragraph {index}: Please review the restrictions and requested documents.' for index in range(80)]
        + ['FINAL PARAGRAPH: Complete instructions for this latest warning.']
    )
    page.evaluate("""message => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-reputation').classList.add('active');
        reputationRows = [{'店铺名': '长告警店铺', '站点': '墨西哥', '七天单量': 12, '系统告警': message}];
        renderLatestReputationRows();
    }""", message)
    cell = page.locator('#reputation-body .reputation-warning-cell')
    cell.hover()
    tooltip = page.locator('#traffic-tooltip')
    assert tooltip.is_visible()
    bounds = tooltip.bounding_box()
    viewport = page.viewport_size
    assert bounds['x'] >= 0 and bounds['y'] >= 0
    assert bounds['x'] + bounds['width'] <= viewport['width']
    assert bounds['y'] + bounds['height'] <= viewport['height']
    assert tooltip.evaluate('el => el.scrollHeight > el.clientHeight')
    page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + bounds['height'] / 2, steps=4)
    page.wait_for_timeout(250)
    assert tooltip.is_visible()
    page.mouse.wheel(0, 10000)
    page.wait_for_function("""() => {
        const tooltip = document.getElementById('traffic-tooltip');
        return tooltip.scrollTop + tooltip.clientHeight >= tooltip.scrollHeight - 2;
    }""")
    assert tooltip.inner_text().endswith('FINAL PARAGRAPH: Complete instructions for this latest warning.')
    assert tooltip.is_visible()
    page.mouse.move(1, 1)
    tooltip.wait_for(state='hidden')
    cell.hover()
    assert tooltip.is_visible()
    assert tooltip.evaluate('el => el.scrollTop') == 0
    page.mouse.move(1, 1)
    tooltip.wait_for(state='hidden')


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


def test_algorithm_strategy_preserves_selected_task_types(console_page):
    page = console_page
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-tasks').classList.add('active');
    }""")
    algorithm = page.locator('#daily-task-algorithm-mode')
    mixed = page.locator('#daily-task-mixed-mode')
    complaint = page.locator('input[name="daily-task-appeal-type"][value="投诉"]')
    algorithm.locator("..").click()
    assert page.evaluate("getDailyTaskPayload().task_strategy") == "algorithm"
    assert page.evaluate("getDailyTaskPayload().appeal_types") == ["侵权"]
    assert not mixed.is_checked()
    complaint.locator("..").click()
    assert algorithm.is_checked()
    assert page.evaluate("getDailyTaskPayload().appeal_types") == ["侵权", "投诉"]
    mixed.locator("..").click()
    assert not complaint.is_checked()
    assert page.evaluate("getDailyTaskPayload().appeal_types") == ["混合模式"]
    assert algorithm.is_checked()
    page.locator('#daily-task-normal-mode').locator('..').click()
    assert not algorithm.is_checked()
    assert page.evaluate("getDailyTaskPayload().task_strategy") == "normal"
    assert page.evaluate("getDailyTaskPayload().appeal_types") == ["混合模式"]


@pytest.fixture
def console_page(console_browser):
    env = Environment(loader=FileSystemLoader(ROOT / "bit/templates"))
    html = env.get_template("index.html").render(
        current_user={"username": "preview", "display_name": "界面预览", "is_admin": True},
        can_change_task_workers=False,
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
    heights = page.locator("#tab-orders .order-filters").evaluate("""el =>
        [...el.querySelectorAll('input:not([type=checkbox]), select:not([multiple]), .market-multi-trigger')]
        .map(e => e.getBoundingClientRect().height).filter(Boolean)
    """)
    assert set(heights) == {32}
    trigger = page.locator("#order-store-filter-trigger")
    if not trigger.is_visible():
        trigger.locator('xpath=ancestor::details[1]').locator(':scope > summary').click()
    trigger.click()
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


def test_background_export_queues_polls_and_downloads_without_navigation(console_page):
    page = console_page
    calls = []
    def export_route(route):
        calls.append((route.request.method, urlsplit(route.request.url).path))
        path = calls[-1][1]
        if route.request.method == 'POST':
            assert route.request.post_data_json['url'].startswith('/api/risk-check/results/export?')
            route.fulfill(status=202, content_type='application/json', body=json.dumps({
                'status':'success','data':{'id':'test-export','status':'queued'}}))
        elif path.endswith('/download'):
            route.fulfill(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                          headers={'Content-Disposition':"attachment; filename*=UTF-8''report.xlsx"}, body=b'test-xlsx')
        else:
            route.fulfill(content_type='application/json', body=json.dumps({
                'status':'success','data':{'id':'test-export','status':'ready'}}))
    page.route('**/api/exports', export_route)
    page.route('**/api/exports/**', export_route)
    previous_url = page.url
    with page.expect_download(timeout=15000) as pending:
        page.evaluate('exportRiskResults()')
    assert pending.value.suggested_filename == 'report.xlsx'
    assert page.url == previous_url
    assert calls == [('POST','/api/exports'),('GET','/api/exports/test-export'),('GET','/api/exports/test-export/download')]


def test_background_export_stops_after_permission_revoked(console_page):
    page = console_page
    calls = []
    def export_route(route):
        calls.append(route.request.method)
        if route.request.method == 'POST':
            route.fulfill(status=202, content_type='application/json', body=json.dumps({
                'status':'success','data':{'id':'revoked','status':'queued'}}))
        else:
            route.fulfill(status=403, content_type='application/json', body=json.dumps({'message':'导出权限已失效'}))
    page.route('**/api/exports', export_route)
    page.route('**/api/exports/**', export_route)
    page.evaluate('exportRiskResults()')
    page.get_by_text('导出失败：导出权限已失效', exact=True).wait_for(timeout=10000)
    assert calls == ['POST','GET']

def test_clearance_alerts_opens_as_lazy_workspace_tab(console_page):
    page = console_page
    env = Environment(loader=FileSystemLoader(ROOT / "bit/templates"))
    embedded = env.get_template("clearance_alerts.html").render(
        current_user={"username": "preview"}, can_manage=True, embedded=True,
        url_for=lambda endpoint, filename: "/static/" + filename,
    )
    page.route("**/clearance-alerts?embedded=1*", lambda route: route.fulfill(body=embedded, content_type="text/html"))
    page.route("**/api/clearance-alerts?*", lambda route: route.fulfill(content_type="application/json", body=json.dumps({
        "status": "success", "data": {"rows": [{
            "link_id": 1, "token_id": 1, "site_id": "MLM", "item_id": "MLM100",
            "title": "产品主图测试", "seller_sku": "SKU-100", "thumbnail_url": "http://console.test/main-image.svg",
            "available_quantity": 12, "sales_7d": 0, "sales_14d": 2,
            "alert": {"status": "pending_data", "summary": "待补充入仓资料"},
        }], "total": 1, "counts": {"pending_data": 1}, "stores": []},
    })))
    page.route("**/main-image.svg", lambda route: route.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect x="14" y="10" width="36" height="44" rx="8" fill="#2563eb"/></svg>'))
    frame = page.locator("#clearance-alerts-frame")
    assert frame.get_attribute("src") is None
    page.get_by_role('searchbox', name='搜索功能').fill('半托管清退告警')
    page.locator('.tab-button[data-tab="clearance-alerts"]').click()
    assert page.locator("#tab-clearance-alerts").is_visible()
    assert page.locator('.workspace-tab[data-tab="clearance-alerts"]').is_visible()
    assert "/clearance-alerts?embedded=1" in frame.get_attribute("src")
    assert page.url == "http://console.test/"
    content = page.frame_locator("#clearance-alerts-frame")
    image = content.locator(".ca-product-image")
    image.wait_for(state="visible")
    assert image.evaluate("image => image.complete && image.naturalWidth > 0")
    assert content.get_by_text("返回工作台").count() == 0
    assert content.locator("body").get_attribute("class") == "zs-standalone ca-embedded"
    screenshot = ROOT / "artifacts" / "clearance-alerts-ui" / "console.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshot), full_page=True)
    page.locator('.tab-button[data-tab="overview"]').click()
    page.locator('.workspace-tab[data-tab="clearance-alerts"]').click()
    assert page.locator("#tab-clearance-alerts").is_visible()


def test_size_charts_opens_as_lazy_workspace_tab_and_keeps_editor_state(console_page):
    page = console_page
    env = Environment(loader=FileSystemLoader(ROOT / "bit/templates"))
    embedded = env.get_template("size_charts.html").render(
        current_user={"username": "preview"}, can_manage=True, embedded=True,
        url_for=lambda endpoint, filename: "/static/" + filename,
    )
    loads = []
    api_calls = []

    def open_size_charts(route):
        loads.append(route.request.url)
        route.fulfill(body=embedded, content_type="text/html")

    page.route("**/size-charts?embedded=1*", open_size_charts)
    def size_chart_api(route):
        api_calls.append(route.request.url)
        route.fulfill(content_type="application/json", body=json.dumps({
            "status": "success", "data": {"templates": [], "stores": [], "records": []},
        }))

    page.route("**/api/size-charts/**", size_chart_api)
    frame = page.locator("#size-charts-frame")
    assert frame.get_attribute("src") is None
    page.evaluate("document.getElementById('size-charts-frame').dataset.src = '/size-charts?embedded=1&organization_key=company-a'")
    page.get_by_role('searchbox', name='搜索功能').fill('尺码表')
    page.locator('.tab-button[data-tab="size-charts"]').click()
    assert page.locator("#tab-size-charts").is_visible()
    assert page.locator('.workspace-tab[data-tab="size-charts"]').is_visible()
    assert "/size-charts?embedded=1" in frame.get_attribute("src")
    assert page.url == "http://console.test/"
    content = page.frame_locator("#size-charts-frame")
    content.locator("#sc-new").click()
    content.locator("#sc-name").fill("切换标签后保留的模板")
    assert content.get_by_text("返回工作台").count() == 0
    assert content.locator("body").get_attribute("class") == "zs-standalone sc-embedded"
    page.locator('.tab-button[data-tab="overview"]').click()
    page.locator('.workspace-tab[data-tab="size-charts"]').click()
    assert page.locator("#tab-size-charts").is_visible()
    assert content.locator("#sc-name").input_value() == "切换标签后保留的模板"
    assert len(loads) == 1
    assert api_calls and all("organization_key=company-a" in url for url in api_calls)


def open_standard_size_chart_browser(page, schema):
    env = Environment(loader=FileSystemLoader(ROOT / "bit/templates"))
    embedded = env.get_template("size_charts.html").render(
        current_user={"username": "preview"}, can_manage=True, embedded=True,
        url_for=lambda endpoint, filename: "/static/" + filename,
    )
    calls = []
    stores = [
        {"id": 1, "name": "墨西哥旗舰店", "site_id": "CBT", "enabled": True,
         "sites": [{"site_id": "MLM"}, {"site_id": "MLB"}]},
        {"id": 2, "name": "品牌授权二店", "site_id": "CBT", "enabled": True,
         "sites": [{"site_id": "MLM"}]},
    ]

    def api(route):
        path = urlsplit(route.request.url).path
        payload = route.request.post_data_json if route.request.method == "POST" else None
        calls.append((path, payload))
        if path.endswith("/options"):
            data = {"stores": stores}
        elif path.endswith("/requirements"):
            assert payload["token_id"] == 1
            data = schema if payload["domain_id"] else {"domains": schema["domains"]}
        elif path.endswith("/templates"):
            data = {"templates": []}
        else:
            data = {"records": []}
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))

    page.route("**/size-charts?embedded=1*", lambda route: route.fulfill(body=embedded, content_type="text/html"))
    page.route("**/api/size-charts/**", api)
    page.get_by_role('searchbox', name='搜索功能').fill('尺码表')
    page.locator('.tab-button[data-tab="size-charts"]').click()
    content = page.frame_locator("#size-charts-frame")
    content.locator('#sc-source-store option[value="1"]').wait_for(state="attached")
    return content, calls


def test_standard_size_chart_library_download_and_measured_rows(console_page):
    page = console_page
    schema = {
        "domain_id": "SHIRTS", "domains": [{"id": "SHIRTS", "name": "上衣"}],
        "identity": {"site_id": "CBT", "seller_id": 42}, "max_rows": 75,
        "template_fields": [], "errors": [],
        "row_fields": [
            {"id": "SIZE", "name": "尺码", "value_type": "string", "required": True},
            {"id": "CHEST", "name": "胸宽", "value_type": "number_unit", "allowed_units": ["cm"],
             "required": True, "tags": ["clothing_measure"]},
        ],
        "main_attributes": [{"id": "SIZE", "name": "尺码"}],
    }
    content, calls = open_standard_size_chart_browser(page, schema)
    content.locator(".sc-reference-summary").click()
    assert content.locator("#sc-standard-preset option").count() == 24
    with page.expect_download() as pending:
        content.locator("#sc-standard-download").click()
    download = pending.value
    assert download.suggested_filename == "size-chart-standard-library.json"
    catalog = json.loads(Path(download.path()).read_text(encoding="utf-8"))
    assert catalog["format"] == "mercado-size-chart-standard-library"
    assert len(catalog["presets"]) == 24
    assert len({preset["id"] for preset in catalog["presets"]}) == 24
    assert all(preset["measurements"] and preset["size_systems"] for preset in catalog["presets"])
    screenshot_dir = ROOT / "artifacts" / "size-charts-ui"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshot_dir / "standard-library.png"), full_page=True)

    content.locator("#sc-standard-preset").select_option("adult_tops")
    content.locator("#sc-standard-system").select_option("alpha")
    content.locator("#sc-standard-use").click()
    content.get_by_text("已读取 1 个支持的官方类目", exact=True).wait_for()
    draft = json.loads(content.locator("#sc-json").input_value())
    assert draft["standard_template"] == {"preset_id": "adult_tops", "size_system": "alpha"}
    assert draft["domain_id"] == "" and draft["rows"] == []
    assert {"MLM", "MLB", "CBT"} <= draft["names"].keys()
    assert "token_id" not in draft and "seller_id" not in draft
    assert "标准模板" in content.locator("#sc-name").input_value()
    content.locator("#sc-domain").fill("SHIRTS")
    content.locator("#sc-requirements").click()
    content.locator("#sc-main-attributes select").select_option("SIZE")
    content.locator("#sc-standard-labels").fill("S\nM")
    content.locator("#sc-standard-generate").click()
    assert content.locator('#sc-grid-rows input[aria-label="胸宽"]').count() == 2
    draft = json.loads(content.locator("#sc-json").input_value())
    assert draft["rows"] == [
        {"attributes": [{"id": "SIZE", "values": [{"name": "S"}]}]},
        {"attributes": [{"id": "SIZE", "values": [{"name": "M"}]}]},
    ]
    content.locator('#sc-grid-rows input[aria-label="胸宽"]').first.fill("48")
    content.locator("#sc-standard-labels").fill("S\nM\nL")
    content.locator("#sc-standard-generate").click()
    assert content.locator('#sc-grid-rows input[aria-label="胸宽"]').count() == 3
    assert content.locator('#sc-grid-rows input[aria-label="胸宽"]').first.input_value() == "48"
    assert content.locator('#sc-grid-rows input[aria-label="胸宽"]').nth(1).input_value() == ""
    assert content.locator('#sc-grid-rows input[aria-label="胸宽"]').nth(2).input_value() == ""
    draft = json.loads(content.locator("#sc-json").input_value())
    assert draft["rows"][0]["attributes"][1] == {
        "id": "CHEST", "values": [{"name": "48 cm", "struct": {"number": 48, "unit": "cm"}}],
    }
    assert len(draft["rows"]) == 3
    assert [payload["domain_id"] for path, payload in calls if path.endswith("/requirements")] == ["", "SHIRTS"]
    content.locator("#sc-grid-wrap").scroll_into_view_if_needed()
    page.screenshot(path=str(screenshot_dir / "editor.png"), full_page=True)


def test_standard_shoe_sizes_use_official_numeric_units_without_conversion(console_page):
    page = console_page
    schema = {
        "domain_id": "FOOTWEAR", "domains": [{"id": "FOOTWEAR", "name": "鞋类"}],
        "identity": {"site_id": "CBT", "seller_id": 42}, "max_rows": 75,
        "template_fields": [], "errors": [],
        "row_fields": [
            {"id": "M_US_SIZE", "name": "US 鞋码", "value_type": "number_unit", "allowed_units": ["US"],
             "required": True, "tags": ["main_attribute_candidate"]},
            {"id": "FOOT_LENGTH", "name": "实际足长", "value_type": "number_unit", "allowed_units": ["cm"],
             "required": True, "tags": ["body_measure"]},
        ],
        "main_attributes": [{"id": "M_US_SIZE", "name": "US 鞋码"}],
    }
    content, _ = open_standard_size_chart_browser(page, schema)
    content.locator(".sc-reference-summary").click()
    content.locator("#sc-standard-preset").select_option("adult_footwear")
    content.locator("#sc-standard-system").select_option("us_women")
    content.locator("#sc-standard-use").click()
    content.get_by_text("已读取 1 个支持的官方类目", exact=True).wait_for()
    content.locator("#sc-domain").fill("FOOTWEAR")
    content.locator("#sc-requirements").click()
    content.locator("#sc-main-attributes select").select_option("M_US_SIZE")
    assert content.locator("#sc-standard-unit").input_value() == "US"
    content.locator("#sc-standard-labels").fill("5\n5.5 US")
    content.locator("#sc-standard-generate").click()
    draft = json.loads(content.locator("#sc-json").input_value())
    assert draft["standard_template"] == {"preset_id": "adult_footwear", "size_system": "us_women"}
    assert draft["rows"] == [
        {"attributes": [{"id": "M_US_SIZE", "values": [{"name": "5 US", "struct": {"number": 5, "unit": "US"}}]}]},
        {"attributes": [{"id": "M_US_SIZE", "values": [{"name": "5.5 US", "struct": {"number": 5.5, "unit": "US"}}]}]},
    ]
    assert content.locator('#sc-grid-rows input[aria-label="实际足长"]').first.input_value() == ""
    assert content.locator('#sc-grid-rows input[aria-label="实际足长"]').nth(1).input_value() == ""
    assert content.locator('#sc-grid-rows input[aria-label="US 鞋码"]').first.input_value() == "5"
    assert content.locator('#sc-grid-rows select[aria-label="US 鞋码 单位"]').first.input_value() == "US"
    assert content.locator("#sc-error").is_hidden()


def test_filled_builtin_download_save_and_upload_to_other_store(console_page):
    page = console_page
    content, calls = open_standard_size_chart_browser(page, {"domains": []})
    saved, uploaded = [], []

    def templates(route):
        if route.request.method == "POST":
            chart = {**route.request.post_data_json, "id": 41, "revision": 1}
            saved.append(chart)
            data = {"template": chart}
        else:
            data = {"templates": saved}
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))

    def preview(route):
        request = route.request.post_data_json
        assert request == {"template_id": 41, "revision": 1, "targets": [{"token_id": 2, "site_id": "MLM"}]}
        data = {"preview_id": "filled-preview", "template_id": 41, "revision": 1,
                "results": [{"status": "ready", "token_id": 2, "sites": ["MLM"],
                             "notices": ["已按目标店铺官方选项匹配尺码编号，保留原参考数值"]}]}
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))

    def upload(route):
        uploaded.append(route.request.post_data_json)
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": {
            "succeeded": 1, "results": [{"status": "succeeded", "token_id": 2, "sites": ["MLM"], "chart_id": "offline-chart"}]}}))

    page.route("**/api/size-charts/templates", templates)
    page.route("**/api/size-charts/preview", preview)
    page.route("**/api/size-charts/upload", upload)
    assert content.locator("#sc-builtin-select option").count() == 21
    assert content.locator("#sc-builtin-rows tr").count() == 7
    content.locator("#sc-builtin-select").select_option("builtin_v1_women_dresses")
    assert "76–82" in content.locator("#sc-builtin-rows").inner_text()
    with page.expect_download() as pending:
        content.locator("#sc-builtin-download").click()
    exported = json.loads(Path(pending.value.path()).read_text(encoding="utf-8"))
    assert exported["domain_id"] == "DRESSES"
    assert exported["rows"][0]["attributes"][2]["values"][0]["struct"] == {"number": 76, "unit": "cm"}
    assert "系统预设" in content.locator("#sc-builtin-note").inner_text()
    screenshot_dir = ROOT / "artifacts" / "size-charts-ui"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    content.locator(".sc-builtins").screenshot(path=str(screenshot_dir / "filled-library.png"))
    content.locator("#sc-builtin-use").click()
    content.locator('#sc-template-list .sc-template-card').wait_for()
    assert saved[0]["rows"] == exported["rows"]
    assert content.locator("#sc-template-settings").get_attribute("open") is None
    assert content.locator("#sc-grid-rows tr").count() == 7
    content.locator(".sc-target-card").filter(has_text="品牌授权二店").locator('input[type="checkbox"]').check()
    content.locator("#sc-preview").click()
    content.locator("#sc-preview-results .sc-adaptation-note").wait_for()
    assert content.locator("#sc-upload").is_enabled()
    assert "保留原参考数值" in content.locator("#sc-preview-results").inner_text()
    content.locator("#sc-upload").click()
    content.locator("#sc-confirm-submit").click()
    content.get_by_text("ID offline-chart", exact=True).wait_for()
    assert uploaded == [{"preview_id": "filled-preview"}]
    assert not any(path.endswith("/requirements") for path, _ in calls)


def test_reputation_store_notices_appear_in_matching_warning_cell(console_page):
    page = console_page
    page.route('**/api/reputation/mercado-notices*', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status': 'success', 'data': {
            'total_stores': 2, 'success_stores': 2, 'store_notices': {
                '1': [{'title': '店铺提醒', 'description': '完整内容 <script>文本</script>'}],
                '2': [{'title': '另一店铺提醒', 'description': '不能串店'}],
            },
        }})))
    page.evaluate('''async () => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-reputation').classList.add('active');
        reputationRows = [{token_id: 1, '店铺名': '店铺甲', '系统告警': '原有告警', '站点状态': '活跃中'}];
        await refreshReputationMercadoNotices(true);
    }''')
    cell = page.locator('#reputation-body .reputation-warning-cell')
    expected = '原有告警\n\n店铺提醒\n完整内容 <script>文本</script>'
    assert cell.get_attribute('data-warning-detail') == expected
    assert cell.locator('script').count() == 0
    cell.hover()
    assert page.locator('#traffic-tooltip').inner_text() == expected
    cell.click()
    assert page.locator('#reputation-warning-popover-body').inner_text() == expected
    assert page.locator('#reputation-body .reputation-site-status').inner_text() == '活跃中'
