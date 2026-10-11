"""AI billing filters, pagination and unknown estimates work in both viewport sizes."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from flask import Flask, render_template
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("width", [1440, 390])
def test_ai_billing_filters_pagination_unknown_cost_and_recovery(width):
    app = Flask(__name__, template_folder=str(ROOT / "bit/templates"),
                static_folder=str(ROOT / "bit/static"))
    with app.test_request_context():
        html = render_template("ai_billing.html")
    calls, errors = [], []
    fail = False
    record = {"created_at": "2026-10-07T09:10:08+08:00", "task_type": "product_title",
              "task_name": "<script>商品标题生成</script>", "task_id": "TASK-101",
              "salesperson": "业务员甲", "actor_name": "业务员乙", "provider": "阿里云百炼",
              "model": "qwen-plus", "request_id": "REQUEST-101", "input_tokens": 200,
              "output_tokens": 50, "cached_tokens": 100, "total_tokens": 250,
              "usage_known": True, "estimated_cost_cny": .00001}

    def route(handler):
        nonlocal fail
        parsed = urlsplit(handler.request.url)
        if parsed.path == "/ai-billing":
            handler.fulfill(body=html, content_type="text/html")
        elif parsed.path.startswith("/static/"):
            handler.fulfill(path=str(ROOT / "bit" / parsed.path.lstrip("/")))
        elif parsed.path == "/api/ai-billing":
            query = parse_qs(parsed.query)
            calls.append(query)
            if fail:
                handler.fulfill(status=503, body=json.dumps({"status": "error", "message": "账单服务暂时不可用"}),
                                content_type="application/json")
                return
            current = int(query["page"][0])
            # Exercise both the API aliases and the main frontend contract.
            summary = {"record_count": 21, "total_tokens": 250, "estimated_cost_cny": .00001,
                       "unknown_usage_count": 1, "unpriced_record_count": 1}
            options = {"salespersons": [{"actor_id": "2", "salesperson": "业务员甲"}],
                       "task_types": [{"value": "product_title", "label": "商品标题生成"}],
                       "models": ["qwen-plus"]}
            unknown = {**record, "task_name": "AI 风控检测", "task_id": "TASK-102",
                       "total_tokens": None, "input_tokens": None, "output_tokens": None,
                       "cached_tokens": None, "usage_known": False, "estimated_cost_cny": None}
            items = [record, unknown] if current == 1 else [{**record, "task_id": "TASK-201"}]
            if query.get("keyword") == ["没有结果"]:
                items, summary = [], {**summary, "record_count": 0}
            data = {"items": items, "total": 0 if not items else 21, "page": current,
                    "page_size": int(query["page_size"][0]), "summary": summary, "options": options}
            handler.fulfill(body=json.dumps({"status": "success", "data": data}), content_type="application/json")
        else:
            handler.fulfill(status=404, body="Not found")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", route)
        try:
            page.goto("http://ai-billing.test/ai-billing")
            records = page.locator("#ai-billing-records")
            page.wait_for_function("document.querySelector('#ai-billing-records').textContent.includes('TASK-101')")
            assert page.locator("h1").inner_text() == "AI 账单"
            assert page.locator("#ai-billing-calls").inner_text() == "21"
            assert page.locator("#ai-billing-tokens").inner_text() == "250"
            assert page.locator("#ai-billing-cost").inner_text() == "¥<0.0001"
            assert "商品标题生成" in records.inner_text() and "业务员甲" in records.inner_text()
            assert "操作人：业务员乙" in records.inner_text()
            assert "09:10:08" in records.inner_text() and "缓存 100" in records.inner_text()
            assert records.locator("script").count() == 0
            assert "<script>商品标题生成</script>" in records.inner_text()
            assert records.locator("tr").nth(1).inner_text().count("待确认") == 2
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            page.locator("#ai-billing-next").click()
            page.wait_for_function("document.querySelector('#ai-billing-records').textContent.includes('TASK-201')")
            assert page.locator("#ai-billing-next").is_disabled()
            assert calls[-1]["page"] == ["2"]
            page.locator("#ai-billing-start-date-preset").select_option("custom")
            page.locator("#ai-billing-start-date").fill("2026-10-01")
            page.locator("#ai-billing-end-date").fill("2026-10-07")
            page.locator("#ai-billing-salesperson").select_option("业务员甲")
            page.locator("#ai-billing-task-type").select_option("product_title")
            page.locator("#ai-billing-model").select_option("qwen-plus")
            page.locator("#ai-billing-keyword").fill("TASK-101")
            page.locator("#ai-billing-apply").click()
            page.wait_for_function("document.querySelector('#ai-billing-records').textContent.includes('TASK-101')")
            assert calls[-1] == {"start_date": ["2026-10-01"], "end_date": ["2026-10-07"],
                                 "salesperson": ["业务员甲"], "task_type": ["product_title"],
                                 "model": ["qwen-plus"], "keyword": ["TASK-101"],
                                 "page": ["1"], "page_size": ["20"]}
            page.locator("#ai-billing-keyword").fill("没有结果")
            page.locator("#ai-billing-apply").click()
            page.wait_for_function("document.querySelector('#ai-billing-records').textContent.includes('没有符合筛选条件')")
            assert page.locator("#ai-billing-next").is_disabled()
            page.locator("#ai-billing-clear").click()
            page.wait_for_function("document.querySelector('#ai-billing-records').textContent.includes('TASK-101')")
            assert calls[-1] == {"page": ["1"], "page_size": ["20"]}
            assert page.locator("#ai-billing-model").input_value() == ""

            fail = True
            page.locator("#ai-billing-refresh").click()
            page.wait_for_function("document.querySelector('#ai-billing-message').textContent.includes('暂时不可用')")
            assert "账单暂时无法加载" in records.inner_text()
            assert page.locator("#ai-billing-cost").inner_text() == "待确认"
            fail = False
            page.locator("#ai-billing-refresh").click()
            page.wait_for_function("document.querySelector('#ai-billing-records').textContent.includes('TASK-101')")
            assert page.locator("#ai-billing-message").is_hidden()
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_ai_billing_real_store_salesperson_filters_and_summary(width, tmp_path, monkeypatch):
    """Forward browser requests to the actual blueprint and an isolated ledger."""
    from bit.ai_billing import create_blueprint
    from erp.ai_usage_billing import ai_usage_billing

    monkeypatch.setenv("AI_USAGE_BILLING_PATH", str(tmp_path / "ai-billing.sqlite3"))
    monkeypatch.delenv("AI_USAGE_BILLING_RATES_JSON", raising=False)
    monkeypatch.delenv("AI_USAGE_BILLING_RATES_PATH", raising=False)
    common = {"organization_key": "company-a", "created_at": "2026-10-07T09:10:08+08:00",
              "model": "qwen3.7-flash", "provider": "阿里云百炼",
              "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
    samples = [
        {**common, "actor_id": 2, "actor_name": "张明", "salesperson": "张明",
         "task_type": "ai_original", "task_name": "原创商品标题与卖点生成", "task_id": "PRODUCT-101",
         "record_id": "zhang-original", "input_tokens": 1000, "output_tokens": 100},
        {**common, "actor_id": 3, "actor_name": "李华", "salesperson": "李华",
         "task_type": "risk_check", "task_name": "商品侵权风险检查", "task_id": "PRODUCT-102",
         "record_id": "li-risk", "input_tokens": 3000, "output_tokens": 500},
        {**common, "actor_id": 2, "actor_name": "张明", "salesperson": "张明",
         "task_type": "ai_appeal", "task_name": "申诉文案生成", "task_id": "APPEAL-103",
         "record_id": "zhang-pending"},
        {**common, "organization_key": "company-other", "actor_id": 2, "actor_name": "其他企业",
         "salesperson": "其他企业", "record_id": "outside-scope", "task_name": "不可见任务",
         "input_tokens": 10000, "output_tokens": 1000},
    ]
    for record in samples:
        ai_usage_billing({"action": "record", "record": record})
    app = Flask(__name__, template_folder=str(ROOT / "bit/templates"),
                static_folder=str(ROOT / "bit/static"))
    app.config["TESTING"] = True
    queries = []

    def storage(payload):
        queries.append(payload)
        return ai_usage_billing(payload)

    app.register_blueprint(create_blueprint(
        login_required=lambda function: function,
        current_user=lambda: {"id": 1, "role_key": "super_admin", "organization_key": "company-a"},
        selected_organization=lambda user: "", storage=storage,
    ))
    client = app.test_client()

    def route(handler):
        parsed = urlsplit(handler.request.url)
        response = client.get(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
        handler.fulfill(body=response.data, status=response.status_code, headers=dict(response.headers))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", route)
        try:
            page.goto("http://ai-billing.test/ai-billing")
            records = page.locator("#ai-billing-records")
            page.wait_for_function("document.querySelector('#ai-billing-calls').textContent === '3'")
            assert page.locator("#ai-billing-tokens").inner_text() == "4,600"
            assert page.locator("#ai-billing-cost").inner_text() == "¥0.0013"
            assert records.locator("tr").count() == 3
            assert "不可见任务" not in records.inner_text()
            assert page.locator('#ai-billing-salesperson option').evaluate_all(
                "options => options.map(option => option.value)") == ["", "张明", "李华"]
            assert "原创商品标题与卖点生成" in records.inner_text()
            assert "商品侵权风险检查" in records.inner_text()
            output = ROOT / "artifacts" / "ai-billing"
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / f"ai-billing-{width}.png"), full_page=True)

            page.locator("#ai-billing-salesperson").select_option("张明")
            page.locator("#ai-billing-apply").click()
            page.wait_for_function("document.querySelector('#ai-billing-calls').textContent === '2'")
            assert queries[-1]["salesperson"] == "张明"
            assert page.locator("#ai-billing-tokens").inner_text() == "1,100"
            assert page.locator("#ai-billing-cost").inner_text() == "¥0.0003"
            assert records.locator("tr").count() == 2
            assert "李华" not in records.inner_text()
            assert "张明" in records.inner_text()

            page.locator("#ai-billing-task-type").select_option("ai_appeal")
            page.locator("#ai-billing-apply").click()
            page.wait_for_function("document.querySelector('#ai-billing-calls').textContent === '1'")
            assert page.locator("#ai-billing-tokens").inner_text() == "待确认"
            assert page.locator("#ai-billing-cost").inner_text() == "待确认"
            assert "申诉文案生成" in records.inner_text()
            page.locator("#ai-billing-clear").click()
            page.wait_for_function("document.querySelector('#ai-billing-calls').textContent === '3'")
            assert page.locator("#ai-billing-salesperson").input_value() == ""
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
        finally:
            browser.close()
