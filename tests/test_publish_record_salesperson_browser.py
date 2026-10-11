"""Publish history salesperson selection and linked store options."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.mark.parametrize("width", [1440, 390])
def test_publish_record_salesperson_filter_and_store_linkage(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    requests = []

    def records(route):
        query = parse_qs(urlsplit(route.request.url).query)
        requests.append(query)
        selected = query.get("salesperson", [""])[0]
        total = 1 if selected else 3
        route.fulfill(content_type="application/json", body=json.dumps({
            "status": "success", "data": {
                "rows": [{"id": 1, "title": "筛选上架记录", "status": "published"}],
                "total": total, "counts": {"all": total, "published": total},
            },
        }))

    page.route("**/api/mercado-publish-records?*", records)
    page.evaluate("""() => {
        currentWorkbenchUser.permissions = ['*'];
        document.querySelector('.tab-button[data-tab="mercado-publish-records"]').hidden = false;
        mercadoStoreTokenRows = [
            {id: 7, display_name: '甲店', site_settings: [
                {site_id: 'MLM', salesperson: '张三', group_name: '精品组'},
                {site_id: 'MLB', salesperson: '李四', group_name: '普通组'}]},
            {id: 9, display_name: '乙店', site_settings: [
                {site_id: 'MLM', salesperson: '李四', group_name: '精品组'}]},
            {id: 10, display_name: '未分配店', site_settings: [
                {site_id: 'MLM', salesperson: '', group_name: ''}]},
            {id: 11, display_name: '未配置店', site_settings: []},
        ];
        mercadoPublishStoresLoaded = true;
        switchTab('mercado-publish-records');
    }""")
    page.wait_for_function("mercadoPublishRecordRows.length === 1")
    selector = page.locator("#publish-record-salesperson")
    assert selector.is_visible()
    assert set(selector.locator("option").all_text_contents()) == {"全部业务员", "未分配", "张三", "李四"}
    bounds = selector.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width

    # A selected store outside the salesperson scope is cleared.
    page.evaluate("publishRecordStore.value = '9'")
    selector.select_option("张三")
    page.wait_for_function("document.getElementById('publish-record-summary').textContent.includes('共 1 条')")
    assert requests[-1]["salesperson"] == ["张三"]
    assert "token_id" not in requests[-1]
    assert page.locator("#publish-record-store option").all_text_contents() == ["甲店"]
    assert page.locator("#publish-record-count-all").inner_text() == "1"
    page.evaluate("renderMercadoPublishRecordGroups()")
    assert selector.input_value() == "张三"
    page.evaluate("publishRecordStore.value = '7'; loadMercadoPublishRecords()")
    page.wait_for_function("document.getElementById('publish-record-message').textContent.startsWith('最近刷新')")
    assert requests[-1]["token_id"] == ["7"]
    assert requests[-1]["salesperson"] == ["张三"]

    # Group and salesperson must match the same site setting.
    page.locator("#publish-record-group").select_option("普通组")
    assert page.locator("#publish-record-store option").count() == 0
    page.locator("#publish-record-group").select_option("")
    selector.select_option("__unassigned__")
    page.wait_for_function("document.getElementById('publish-record-message').textContent.startsWith('最近刷新')")
    assert requests[-1]["salesperson"] == ["__unassigned__"]
    assert page.locator("#publish-record-store option").all_text_contents() == ["未分配店", "未配置店"]

    selector.select_option("")
    page.wait_for_function("document.getElementById('publish-record-summary').textContent.includes('共 3 条')")
    assert "salesperson" not in requests[-1]
    assert page.locator("#publish-record-store option").count() == 4
    artifacts = ROOT / "artifacts/publish-record-salesperson-20261010"
    artifacts.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifacts / f"publish-records-{width}.png"), full_page=True)
