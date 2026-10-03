"""Offline browser coverage for reviewed Amazon/Yandex publish actions."""
import copy
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from erp.ai_original_platforms import listing_profiles_catalog
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401


@pytest.fixture
def publishing_page(console_page):
    page = console_page
    requests, errors = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    image = "https://images.test/real-product.png"
    page.route("https://images.test/**", lambda route: route.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><rect width="200" height="200" fill="white"/><rect x="50" y="40" width="100" height="120" rx="8" fill="#b7cade"/></svg>'))
    draft = {"template_id": "standard", "status": "completed", "title": "Metal Desk Organizer", "description": "Metal desk storage.", "bullet_points": ["Three compartments"], "main_image_url": image, "validation": {"errors": [], "warnings": []}}
    rows = [{"id": product_id, "title": f"金属收纳盒 {product_id}", "source_type": "ai_original", "review_status": "approved", "weight_g": 300, "package_length_cm": 20, "package_width_cm": 15, "package_height_cm": 10, "original_1688": {"title": f"金属收纳盒 {product_id}", "main_image_url": image}, "marketplace_listings": {"amazon": {**draft, "target_platform": "amazon"}, "yandex": {**draft, "target_platform": "yandex", "title": "Органайзер для стола", "description": "Металлический органайзер для стола."}}} for product_id in (1, 2)]
    backend = {"jobs": [], "preview": None, "fail_preview": False, "config": None, "polls": 0}

    def api(route):
        request = route.request
        path = urlsplit(request.url).path
        query = parse_qs(urlsplit(request.url).query)
        body = request.post_data_json if request.method in {"POST", "PATCH"} else None
        requests.append({"method": request.method, "path": path, "body": body, "query": query})
        data = {}
        if path.endswith("/profiles"):
            data = listing_profiles_catalog()
        elif path == "/api/ai-original-products":
            data = {"rows": rows}
        elif path.endswith("/publish/stores"):
            platform = query["target_platform"][0]
            data = {"rows": [{"id": f"{platform}-store", "label": f"{platform} 测试店铺"}], "configuration": {"amazon_configured": True}, "image_base_url_configured": True}
        elif path.endswith("/publish/schema"):
            data = {"fields": [{"id": "material", "label": "真实材质", "required": True, "type": "text", "choices": [{"value": "metal", "label": "金属"}], "description": "填写与实物一致的材质。"}]}
        elif path.endswith("/publish/preview"):
            items = []
            for item in body["items"]:
                settings = {**item["settings"], "image_url": image}
                items.append({"product_id": item["product_id"], "sku": settings["sku"], "title": f"Product {item['product_id']}", "settings": settings, "errors": ["请补齐材质字段"] if backend["fail_preview"] else [], "warnings": ["提交前核对实物及品牌"], "payload": {"sku": settings["sku"], "image_url": image, "price": settings["price"]}})
            data = {"preview_id": "preview-001", "items": items, "valid": not backend["fail_preview"], "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()}
            backend["preview"] = {"body": body, "data": copy.deepcopy(data)}
        elif path.endswith("/publish") and request.method == "POST":
            assert body == {"preview_id": "preview-001"}
            preview = backend["preview"]
            job = {"id": "job-001", "platform": preview["body"]["target_platform"], "store_id": preview["body"]["store_id"], "store_label": "已确认测试店铺", "state": "running", "running": True, "created_at": "2026-10-03T02:00:00+00:00", "items": [{**item, "state": "submitted", "live": False, "issues": []} for item in preview["data"]["items"]]}
            backend["jobs"] = [job]
            data = job
        elif path.endswith("/publish/jobs"):
            data = {"rows": backend["jobs"]}
        elif re.search(r"/publish/jobs/[^/]+$", path):
            backend["polls"] += 1
            job = backend["jobs"][0]
            job.update(state="submitted", running=False)
            data = job
        elif path.endswith("/refresh"):
            job = backend["jobs"][0]
            job.update(state="live", running=False)
            for item in job["items"]:
                item.update(state="live", live=True, remote_status="PUBLISHED", remote_id=f"remote-{item['product_id']}")
            data = job
        elif path.endswith("/retry"):
            job = backend["jobs"][0]
            for item in job["items"]:
                if item["state"] == "failed":
                    item.update(state="submitted", live=False)
            job.update(state="submitted", running=False)
            data = job
        elif path.endswith("/amazon-store"):
            backend["config"] = body
            data = {"id": "amazon-store", "label": body["label"]}
        route.fulfill(content_type="application/json", body=json.dumps({"status": "success", "data": data}))

    page.route(re.compile(r".*/api/ai-original-products(?:[/?].*)?$"), api)
    page.evaluate("""async () => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        await loadAiOriginalProfiles();
    }""")
    return page, backend, requests, errors


def select_and_fill(page, platform, count=2):
    page.locator("#ai-original-platform").select_option(platform)
    page.wait_for_function("document.getElementById('aip-store').value !== ''")
    page.wait_for_function("aiOriginalRows.length === 2")
    for index in range(count):
        page.locator('#ai-original-grid input[type="checkbox"]').nth(index).check()
    page.locator("#aip-category").fill("HOME_ORGANIZER" if platform == "amazon" else "12345")
    for product_id in range(1, count + 1):
        row = page.locator(f'#aip-products [data-product="{product_id}"]')
        for field, value in {"sku": f"SKU-{platform}-{product_id}", "brand": "Real Brand", "gtin": ["4006381333931", "5901234123457"][product_id - 1], "price": str(20 + product_id), "quantity": str(product_id + 3)}.items():
            row.locator(f'[data-field="{field}"]').fill(value)
        row.locator('[data-field="condition"]').select_option("new_new")
        row.locator('[data-field="additional_attributes"]').fill('{"material":"Metal"}' if platform == "yandex" else '{"material":[{"value":"Metal","language_tag":"en_US"}]}')


@pytest.mark.parametrize("platform,width", [("amazon", 1440), ("yandex", 390)])
def test_preview_confirm_poll_and_live_status(publishing_page, platform, width):
    page, backend, requests, errors = publishing_page
    page.set_viewport_size({"width": width, "height": 1000})
    select_and_fill(page, platform)
    assert page.locator("#aip-panel").is_visible()
    assert not page.locator(".ai-original-publish-panel").is_visible()
    page.locator("#aip-schema-load").click()
    page.wait_for_function("document.getElementById('aip-schema').textContent.includes('真实材质')")
    assert "material" in page.locator("#aip-schema").inner_text()
    page.locator("#aip-prepare").click()
    page.locator("#aip-preview").wait_for(state="visible")
    assert page.locator("#aip-confirm").is_enabled()
    assert backend["preview"]["body"]["target_platform"] == platform
    assert [item["settings"]["gtin"] for item in backend["preview"]["body"]["items"]] == ["4006381333931", "5901234123457"]
    assert [item["settings"]["quantity"] for item in backend["preview"]["body"]["items"]] == [4, 5]
    assert [item["settings"]["price"] for item in backend["preview"]["body"]["items"]] == [21, 22]
    assert not any(item["path"].endswith("/publish") for item in requests)
    screenshots = ROOT / "artifacts" / "ai-original-autopublish"
    screenshots.mkdir(parents=True, exist_ok=True)
    page.locator("#aip-panel").screenshot(path=str(screenshots / f"{platform}-preview-{width}.png"))
    page.evaluate("AiOriginalPublisher.confirmPublish(); AiOriginalPublisher.confirmPublish();")
    page.wait_for_function("document.getElementById('aip-jobs').textContent.includes('平台已接收')")
    assert len([item for item in requests if item["path"].endswith("/publish")]) == 1
    assert page.locator("#aip-jobs .aip-live").count() == 0
    page.wait_for_function("document.querySelector('#aip-jobs [data-job-action=refresh]')?.disabled === false")
    assert backend["polls"] >= 1
    page.locator('#aip-jobs [data-job-action="refresh"]').click()
    page.wait_for_function("document.querySelectorAll('#aip-jobs .aip-live').length === 2")
    assert page.locator("#aip-jobs .aip-live").all_inner_texts() == ["已可售", "已可售"]
    assert not errors
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator("#aip-panel").screenshot(path=str(screenshots / f"{platform}-live-{width}.png"))


def test_invalid_preview_stale_selection_and_duplicate_identifiers_block_submission(publishing_page):
    page, backend, requests, errors = publishing_page
    select_and_fill(page, "amazon")
    second = page.locator('#aip-products [data-product="2"]')
    second.locator('[data-field="gtin"]').fill("4006381333931")
    page.locator("#aip-prepare").click()
    assert "条码与其他商品重复" in page.locator("#aip-status").inner_text()
    assert not any(item["path"].endswith("/preview") for item in requests)
    second.locator('[data-field="gtin"]').fill("5901234123457")
    backend["fail_preview"] = True
    page.locator("#aip-prepare").click()
    page.locator("#aip-preview").wait_for(state="visible")
    assert page.locator("#aip-confirm").is_disabled()
    assert "请补齐材质字段" in page.locator("#aip-preview").inner_text()
    backend["fail_preview"] = False
    page.locator("#aip-prepare").click()
    page.wait_for_function("document.getElementById('aip-confirm').disabled === false")
    second.locator('[data-field="quantity"]').fill("8")
    assert not page.locator("#aip-preview").is_visible()
    assert page.locator("#aip-confirm").is_disabled()
    page.locator("#aip-prepare").click()
    page.wait_for_function("document.getElementById('aip-confirm').disabled === false")
    page.locator('#ai-original-grid input[type="checkbox"]').nth(1).uncheck()
    assert not page.locator("#aip-preview").is_visible()
    awaitable = page.evaluate("AiOriginalPublisher.confirmPublish()")
    assert not any(item["path"].endswith("/publish") for item in requests)
    assert not errors


def test_authorization_passwords_cleared_and_job_recovery_never_retries_unknown(publishing_page):
    page, backend, requests, errors = publishing_page
    select_and_fill(page, "amazon", count=1)
    page.locator("#aip-amazon-config-actions button").click()
    form = page.locator("#aip-amazon-form")
    for name, value in {"label": "美国旗舰店", "seller_id": "SELLER-DEMO", "client_id": "CLIENT-DEMO", "client_secret": "do-not-persist-secret", "refresh_token": "do-not-persist-refresh"}.items():
        form.locator(f'[name="{name}"]').fill(value)
    assert form.locator('[name="client_secret"]').get_attribute("type") == "password"
    assert form.locator('[name="refresh_token"]').get_attribute("type") == "password"
    page.locator("#aip-amazon-save").click()
    page.locator("#aip-amazon-config").wait_for(state="hidden")
    assert backend["config"]["refresh_token"] == "do-not-persist-refresh"
    assert form.locator('[name="client_secret"]').input_value() == ""
    assert form.locator('[name="refresh_token"]').input_value() == ""
    assert "do-not-persist" not in page.evaluate("JSON.stringify({...localStorage})")
    assert "do-not-persist" not in page.locator("body").inner_text()
    backend["jobs"] = [{"id": "recovered-001", "platform": "amazon", "store_label": "恢复的店铺", "state": "unknown", "running": False, "items": [{"product_id": 1, "sku": "known-sku", "state": "unknown", "live": False, "message": "网络响应中断，结果未确认"}]}]
    page.locator(".aip-history-heading button").click()
    page.wait_for_function("document.getElementById('aip-jobs').textContent.includes('recovered-001')")
    assert page.locator('#aip-jobs [data-job-action="retry"]').is_disabled()
    page.evaluate("AiOriginalPublisher.jobAction('recovered-001', 'retry')")
    assert not any(item["path"].endswith("/retry") for item in requests)
    backend["jobs"][0]["state"] = "partial"
    backend["jobs"][0]["items"] = [{"product_id": 1, "sku": "success-sku", "state": "submitted", "live": False}, {"product_id": 2, "sku": "failed-sku", "state": "failed", "live": False, "issues": ["库存同步失败"]}]
    page.locator(".aip-history-heading button").click()
    page.wait_for_function("document.querySelector('#aip-jobs [data-job-action=retry]')?.disabled === false")
    page.locator('#aip-jobs [data-job-action="retry"]').click()
    page.wait_for_function("document.getElementById('aip-status').textContent.includes('已启动失败步骤重试')")
    assert len([item for item in requests if item["path"].endswith("/retry")]) == 1
    assert not errors
