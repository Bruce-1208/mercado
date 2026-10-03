"""Exercise the knowledge workflow offline with the real console template."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


def knowledge_rows():
    rows = [
        {"id": 1, "brand_name": "Trail Source", "list_type": "pending", "source_type": "wipo",
         "notes": "商标资料需要人工复核", "source_detail": "WIPO 商标记录 · 申请号 123456",
         "evidence_count": 1, "updated_at": "2026-10-03 09:30:00"},
        {"id": 2, "brand_name": "Sample Restriction", "list_type": "blacklist", "source_type": "manual",
         "notes": "历史投诉中出现，需检查实际产品和销售范围。", "source_detail": "历史投诉分析",
         "evidence_count": 3, "updated_at": "2026-10-03 09:20:00"},
        {"id": 3, "brand_name": "Authorized Sample", "list_type": "whitelist", "source_type": "manual",
         "notes": "仅适用于已核验授权的产品和站点。", "source_detail": "人工录入",
         "evidence_count": 0, "updated_at": "2026-10-03 09:10:00"},
    ]
    rows.extend({**rows[0], "id": index, "brand_name": f"Trail Brand {index:03d}"}
                for index in range(4, 54))
    return rows


def list_response(rows, *, page=1, filtered_total=None, summary=None):
    total = len(rows) if filtered_total is None else filtered_total
    return {"status": "success", "data": {
        "rows": rows, "summary": summary or {"total": total, "pending": total, "blacklist": 0, "whitelist": 0},
        "filtered_total": total, "page": page, "page_size": 50,
        "pages": max(1, (total + 49) // 50), "has_more": page * 50 < total,
    }}


def open_knowledge(page, rows=None):
    rows = knowledge_rows() if rows is None else rows
    state = {"queries": [], "writes": [], "errors": []}
    page.on("pageerror", lambda error: state["errors"].append(str(error)))

    def api(route):
        request = route.request
        if request.method != "GET":
            state["writes"].append((request.method, urlsplit(request.url).path, request.post_data_json))
            route.fulfill(content_type="application/json", body=json.dumps({
                "status": "success", "data": {"inserted": 1, "restored": 0, "skipped": 0},
            }))
            return
        query = {key: values[0] for key, values in parse_qs(urlsplit(request.url).query).items()}
        state["queries"].append(query)
        matched = [row for row in rows
                   if (not query.get("list_type") or row["list_type"] == query["list_type"])
                   and (not query.get("source_type") or row["source_type"] == query["source_type"])
                   and (not query.get("search") or query["search"].lower() in
                        (row["brand_name"] + " " + row.get("notes", "")).lower())]
        number = int(query.get("page", 1))
        summary = {"total": len(rows), **{
            name: sum(row["list_type"] == name for row in rows)
            for name in ("pending", "blacklist", "whitelist")}}
        result = list_response(matched[(number - 1) * 50:number * 50], page=number,
                               filtered_total=len(matched), summary=summary)
        route.fulfill(content_type="application/json", body=json.dumps(result))

    page.route("**/api/infringement-knowledge?*", api)
    page.route("**/api/infringement-knowledge/bulk", api)
    page.route("**/api/infringement-knowledge/1", api)
    page.route("**/api/infringement-knowledge/analysis/status", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps({"status": "success", "data": {
            "running": False, "status": "idle", "message": "等待启动", "logs": [],
        }})))
    page.route("**/api/infringement-knowledge/wipo/**", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps({"status": "success", "data": {
            "source_mode": "madrid", "interval_hours": 24, "enabled": False,
            "madrid_from_date": "2026-10-01", "running": False, "status": "idle",
            "history": [], "logs": [], "summary": {},
        }})))
    page.locator('.tab-button[data-tab="infringement-knowledge"]').click()
    page.locator("#infringement-knowledge-body .knowledge-brand").first.wait_for()
    return state


@pytest.mark.parametrize("width", [1440, 390])
def test_knowledge_review_first_layout_and_source_panel(console_page, width):
    page = console_page
    state = open_knowledge(page)
    page.set_viewport_size({"width": width, "height": 1000})
    assert page.locator("#knowledge-records-panel").is_visible()
    assert page.locator("#knowledge-sources-panel").is_hidden()
    assert page.locator("#infringement-knowledge-pending").inner_text() == "51"
    assert page.locator("#infringement-knowledge-total").inner_text() == "53"
    assert page.locator(".knowledge-table thead th").count() == 6
    pending_row = page.locator("#infringement-knowledge-body tr").first
    assert "待复核" in pending_row.inner_text()
    assert "不侵权" not in pending_row.inner_text()
    assert "侵权" not in pending_row.locator("td").nth(1).inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    output = ROOT / "artifacts/infringement-knowledge-ui"
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"knowledge-{width}.png"), full_page=True)
    page.locator("#tab-infringement-knowledge").screenshot(path=str(output / f"knowledge-workspace-{width}.png"))
    page.locator("#tab-infringement-knowledge").screenshot(path=str(output / f"knowledge-workspace-{width}.png"))

    page.locator("#knowledge-sources-tab").click()
    assert page.locator("#knowledge-records-panel").is_hidden()
    assert page.locator("#knowledge-sources-panel").is_visible()
    assert page.locator("#wipo-sync-panel").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(output / f"sources-{width}.png"), full_page=True)
    page.locator("#tab-infringement-knowledge").screenshot(path=str(output / f"sources-workspace-{width}.png"))
    page.locator("#knowledge-records-tab").click()
    assert page.locator("#knowledge-records-panel").is_visible()
    assert not state["errors"]


def test_knowledge_filters_and_pagination_keep_global_summary(console_page):
    page = console_page
    state = open_knowledge(page)
    assert state["queries"][-1].get("page_size") == "50"
    assert page.locator("#knowledge-prev-page").is_disabled()
    page.locator('[data-knowledge-filter="pending"]').click()
    page.wait_for_function("document.querySelectorAll('#infringement-knowledge-body .knowledge-brand').length === 50")
    assert page.locator("#infringement-knowledge-type-filter").input_value() == "pending"
    page.locator("#infringement-knowledge-source-filter").select_option("wipo")
    search = page.locator("#infringement-knowledge-search")
    search.fill("Trail")
    with page.expect_response(lambda response: "/api/infringement-knowledge?" in response.url
                              and parse_qs(urlsplit(response.url).query).get("search") == ["Trail"]):
        search.press("Enter")
    page.locator("#knowledge-next-page").click()
    page.wait_for_function("document.querySelectorAll('#infringement-knowledge-body .knowledge-brand').length === 1")
    assert state["queries"][-1] == {"list_type": "pending", "source_type": "wipo", "search": "Trail",
                                   "page": "2", "page_size": "50"}
    assert page.locator("#knowledge-next-page").is_disabled()
    assert page.locator("#knowledge-prev-page").is_enabled()
    assert page.locator("#infringement-knowledge-total").inner_text() == "53"
    with page.expect_response(lambda response: "/api/infringement-knowledge?" in response.url
                              and parse_qs(urlsplit(response.url).query).get("list_type") == ["blacklist"]):
        page.locator("#infringement-knowledge-type-filter").select_option("blacklist")
    page.wait_for_function("document.querySelector('#infringement-knowledge-body .empty-state') !== null")
    assert state["queries"][-1]["page"] == "1"
    assert page.locator("#infringement-knowledge-body .empty-state").get_attribute("colspan") == "6"


def test_knowledge_late_search_response_cannot_replace_latest_results(console_page):
    page = console_page
    open_knowledge(page)
    deferred = {}

    def defer_search(route):
        query = parse_qs(urlsplit(route.request.url).query)
        deferred[query["search"][0]] = route

    page.route("**/api/infringement-knowledge?*", defer_search)
    search = page.locator("#infringement-knowledge-search")
    search.fill("older")
    with page.expect_request(lambda request: parse_qs(urlsplit(request.url).query).get("search") == ["older"]):
        search.press("Enter")
    search.fill("newer")
    with page.expect_request(lambda request: parse_qs(urlsplit(request.url).query).get("search") == ["newer"]):
        search.press("Enter")
    deferred["newer"].fulfill(content_type="application/json", body=json.dumps(list_response([
        {**knowledge_rows()[0], "brand_name": "Latest matching brand"},
    ])))
    page.get_by_text("Latest matching brand", exact=True).wait_for()
    deferred["older"].fulfill(content_type="application/json", body=json.dumps(list_response([
        {**knowledge_rows()[0], "brand_name": "Outdated matching brand"},
    ])))
    page.wait_for_load_state("networkidle")
    assert page.locator("#infringement-knowledge-body .knowledge-brand").all_inner_texts() == ["Latest matching brand"]


@pytest.mark.parametrize("decision", ["blacklist", "whitelist"])
def test_knowledge_editor_defaults_to_pending_and_requires_decision_basis(console_page, decision):
    page = console_page
    state = open_knowledge(page)
    if decision == "whitelist":
        page.set_viewport_size({"width": 390, "height": 844})
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#add-infringement-knowledge-btn").click()
    editor = page.locator("#infringement-knowledge-dialog")
    assert editor.is_visible()
    assert page.locator("#knowledge-editor-title").inner_text()
    assert page.locator("#infringement-knowledge-list-type").input_value() == "pending"
    bounds = editor.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= page.viewport_size["width"]
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= page.viewport_size["height"]
    output = ROOT / "artifacts/infringement-knowledge-ui"
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"editor-{page.viewport_size['width']}.png"))
    page.locator("#infringement-knowledge-brand").fill("New Pending Brand")
    page.locator("#save-infringement-knowledge-btn").click()
    editor.wait_for(state="hidden")
    assert state["writes"][0] == ("POST", "/api/infringement-knowledge/bulk", {
        "brands_text": "New Pending Brand", "list_type": "pending", "notes": "",
    })
    page.locator("#add-infringement-knowledge-btn").click()
    page.locator("#infringement-knowledge-brand").fill("Scoped Decision Brand")
    page.locator("#infringement-knowledge-list-type").select_option(decision)
    page.locator("#save-infringement-knowledge-btn").click()
    assert editor.is_visible()
    assert len(state["writes"]) == 1
    page.locator("#infringement-knowledge-notes").fill("已核验授权，仅限指定墨西哥店铺和产品。")
    page.locator("#save-infringement-knowledge-btn").click()
    editor.wait_for(state="hidden")
    assert state["writes"][-1][2]["list_type"] == decision
    assert state["writes"][-1][2]["notes"] == "已核验授权，仅限指定墨西哥店铺和产品。"
    assert not state["errors"]


def test_knowledge_review_existing_evidence_and_save_decision(console_page):
    page = console_page
    state = open_knowledge(page)
    page.locator("#infringement-knowledge-body tr").first.locator("button").first.click()
    assert page.locator("#infringement-knowledge-id").input_value() == "1"
    assert page.locator("#infringement-knowledge-brand").input_value() == "Trail Source"
    assert "123456" in page.locator("#knowledge-editor-evidence").inner_text()
    page.locator("#infringement-knowledge-list-type").select_option("blacklist")
    page.locator("#infringement-knowledge-notes").fill("已复核商标记录，限制当前类目使用。")
    page.locator("#save-infringement-knowledge-btn").click()
    page.locator("#infringement-knowledge-dialog").wait_for(state="hidden")
    assert state["writes"] == [("PUT", "/api/infringement-knowledge/1", {
        "brand_name": "Trail Source", "list_type": "blacklist", "notes": "已复核商标记录，限制当前类目使用。",
    })]


def test_knowledge_view_only_user_can_read_details_but_cannot_mutate(console_page):
    page = console_page
    page.evaluate("""() => {
        currentWorkbenchPermissions.clear();
        currentWorkbenchPermissions.add('infringement_knowledge.view');
    }""")
    state = open_knowledge(page)
    page.evaluate("applyWorkbenchPermissions()")
    assert page.locator("#add-infringement-knowledge-btn").is_hidden()
    assert page.locator("#infringement-knowledge-body [data-delete]").count() == 0
    page.evaluate("openInfringementKnowledgeEditor()")
    assert page.locator("#infringement-knowledge-dialog").is_hidden()
    page.locator("#infringement-knowledge-body [data-edit]").first.click()
    assert page.locator("#infringement-knowledge-dialog").is_visible()
    for field in ("brand", "list-type", "notes"):
        assert page.locator(f"#infringement-knowledge-{field}").is_disabled()
    assert page.locator("#save-infringement-knowledge-btn").is_hidden()
    page.locator("#infringement-knowledge-form").dispatch_event("submit")
    page.keyboard.press("Escape")
    assert page.locator("#infringement-knowledge-dialog").is_hidden()
    page.locator("#knowledge-sources-tab").click()
    assert page.locator("#start-infringement-analysis-btn").is_hidden()
    assert page.locator("#fetch-global-ip-btn").is_hidden()
    assert not state["writes"]


def test_knowledge_source_and_notes_are_safe_text(console_page):
    page = console_page
    hostile = '<img src=x onerror="window.knowledgeInjection = true">'
    row = {**knowledge_rows()[0], "brand_name": hostile, "notes": hostile, "source_detail": hostile}
    open_knowledge(page, [row])
    body = page.locator("#infringement-knowledge-body")
    assert body.locator("img, script").count() == 0
    assert hostile in body.inner_text()
    assert not page.evaluate("Boolean(window.knowledgeInjection)")
