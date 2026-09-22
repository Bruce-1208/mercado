import json
import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension" / "zeshun_collector"


def test_manifest_is_chrome_edge_manifest_v3_and_declares_supported_sites():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert manifest["background"]["service_worker"] == "background.js"
    assert "default_popup" not in manifest["action"]
    assert manifest["version"] == "1.7.1"
    matches = manifest["content_scripts"][0]["matches"]
    assert any("mercadolibre.com.mx" in pattern for pattern in matches)
    assert any("mercadolivre.com.br" in pattern for pattern in matches)
    assert any(
        "1688.com" in pattern
        for content_script in manifest["content_scripts"]
        for pattern in content_script["matches"]
    )
    assert "https://meli.zying.net/*" in manifest["host_permissions"]
    assert any(
        "meli.zying.net" in pattern
        for content_script in manifest["content_scripts"]
        for pattern in content_script["matches"]
    )
    assert "cookies" in manifest["permissions"]
    assert "scripting" in manifest["permissions"]
    assert "http://127.0.0.1/*" in manifest["host_permissions"]


def test_extension_files_referenced_by_manifest_exist():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    referenced = [
        manifest["background"]["service_worker"],
        "popup.html",
        manifest["options_page"],
        *(filename for item in manifest["content_scripts"] for filename in item.get("js", [])),
        *(filename for item in manifest["content_scripts"] for filename in item.get("css", [])),
    ]

    assert all((EXTENSION / filename).is_file() for filename in referenced)


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not installed")
def test_all_extension_javascript_has_valid_syntax():
    for filename in (
        "collector-core.js",
        "product-batch.js",
        "content.js",
        "background.js",
        "popup.js",
        "options.js",
        "content-1688.js",
        "content-zying.js",
        "zying-page.js",
    ):
        subprocess.run(
            [shutil.which("node"), "--check", str(EXTENSION / filename)],
            check=True,
            capture_output=True,
            text=True,
        )


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not installed")
def test_core_normalizes_item_ids_and_zying_metrics():
    script = r"""
const core = require(process.argv[1]);
const metrics = core.parsePluginMetrics('商品重量 540 g 尺寸 11 x 10 x 17 cm 计抛重 0.32 kg');
if (core.normalizeItemId('https://x.test/MLM-3016972321') !== 'MLM3016972321') process.exit(2);
if (metrics.weight_g !== 540) process.exit(3);
if (metrics.package_length_cm !== 11 || metrics.package_width_cm !== 10 || metrics.package_height_cm !== 17) process.exit(4);
if (metrics.volumetric_weight_kg !== 0.32) process.exit(5);
"""
    subprocess.run(
        [shutil.which("node"), "-e", script, str(EXTENSION / "collector-core.js")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_1688_collector_targets_ai_original_products():
    content = (EXTENSION / "content-1688.js").read_text(encoding="utf-8")
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")

    assert 'source_platform: "1688"' in content
    assert "collectImages" in content
    assert "collectProperties" in content
    assert 'sourcePlatform: result.source_platform' in background
    assert '"/?tab=ai-original-products"' in background
    assert "zeshun-collector-floating" in content
    assert "采集到泽顺" in content


def test_1688_search_results_expose_per_card_collection_buttons():
    content = (EXTENSION / "content-1688.js").read_text(encoding="utf-8")

    assert 'location.hostname !== "s.1688.com"' in content
    assert 'document.querySelectorAll(".search-offer-wrapper")' in content
    assert 'className = "zeshun-card-collect"' in content
    assert 'button.textContent = "采集"' in content
    assert 'source_platform: "1688"' in content
    assert 'scrape_status: "partial"' in content
    assert 'chrome.runtime.sendMessage({type: "SUBMIT_PRODUCT", product})' in content


def test_console_downloads_complete_zeshun_extension_package():
    import bit.bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    workbench.app.secret_key = "extension-test-secret"
    client = workbench.app.test_client()
    with client.session_transaction() as login_session:
        login_session["workbench_user"] = _browser_extension_user()

    response = client.get("/api/browser-extension/download")

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/zip")
    assert response.headers["X-Zeshun-Extension-Version"] == "1.7.1"
    assert "zeshun-collector-extension.zip" in response.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        names = set(archive.namelist())
        assert "zeshun_collector/manifest.json" in names
        assert "zeshun_collector/product-batch.js" in names
        assert "zeshun_collector/content-1688.js" in names
        assert "zeshun_collector/content-zying.js" in names
        assert "zeshun_collector/zying-page.js" in names
        assert "zeshun_collector/README.md" in names


def test_console_topbar_exposes_extension_download():
    template = (
        ROOT / "bit" / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'class="topbar-plugin-download"' in template
    assert 'href="/api/browser-extension/download"' in template
    assert 'aria-label="下载泽顺插件"' in template


def test_background_requires_console_login_and_keeps_offline_queue():
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")

    assert "/api/browser-extension/login" in source
    assert "/api/browser-extension/session" in source
    assert "/api/browser-extension/collect" in source
    assert "Authorization = `Bearer ${auth.token}`" in source
    assert 'apiRequest("/api/login"' in source
    assert 'mode: "legacy"' in source
    assert "/api/db/mercado-collection/items" in source
    assert "pendingProducts" in source
    assert "periodInMinutes: 1" in source
    assert "/api/browser-extension/zying/start" in source
    assert "/api/browser-extension/zying/status" in source
    assert 'world: "MAIN"' in source
    assert "/api/browser-extension/notifications/settings" not in source
    assert "/api/browser-extension/notifications/send" in source
    assert "notifyAttention" in source
    assert "chrome.action?.onClicked" in source
    assert 'type: "popup"' in source
    assert 'chrome.runtime.getURL("popup.html")' in source


def test_weight_price_pause_email_waits_ten_minutes_and_is_one_shot():
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")

    assert 'AI_WEIGHT_PRICE_PAUSE_NOTICE_MS = 10 * 60 * 1000' in source
    assert 'AI_WEIGHT_PRICE_PAUSE_KEY = "aiWeightPricePauseNotification"' in source
    assert 'if (pause.attempted || Date.now()' in source
    assert 'pause = {...pause, attempted: true, attemptedAt: Date.now()}' in source
    assert source.index('attempted: true, attemptedAt: Date.now()') < source.index(
        'await notifyAttention(reason, {source: "AI核重核价"})'
    )
    assert 'storageRemove("local", [AI_WEIGHT_PRICE_PAUSE_KEY])' in source


def test_extension_options_link_to_account_integration_settings():
    options = (EXTENSION / "options.html").read_text(encoding="utf-8")
    script = (EXTENSION / "options.js").read_text(encoding="utf-8")

    assert 'id="open-integrations"' in options
    assert "/settings/integrations" in script
    assert "SMTP 授权码" not in options
    assert "SAVE_NOTIFICATION_SETTINGS" not in script


def test_extension_options_do_not_store_model_api_settings():
    options = (EXTENSION / "options.html").read_text(encoding="utf-8")
    script = (EXTENSION / "options.js").read_text(encoding="utf-8")
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")

    assert 'id="deepseek-api-key"' not in options
    assert 'id="dashscope-api-key"' not in options
    assert "SAVE_MODEL_SETTINGS" not in script
    assert "GET_MODEL_SETTINGS" not in script
    assert "/api/browser-extension/model-settings" not in background


def test_zying_collection_controls_live_in_extension_and_use_current_browser():
    popup = (EXTENSION / "popup.html").read_text(encoding="utf-8")
    content = (EXTENSION / "zying-page.js").read_text(encoding="utf-8")

    assert 'id="zying-mode"' in popup
    assert 'id="zying-start-product-id"' in popup
    assert 'id="zying-max-items"' in popup
    assert 'id="zying-category"' in popup
    assert 'id="zying-developer"' in popup
    assert 'id="zying-infringement-mode"' in popup
    assert 'id="zying-infringement-start-product-id"' in popup
    assert 'id="zying-infringement-max-items"' in popup
    assert 'id="zying-infringement-category"' in popup
    assert 'id="zying-infringement-developer"' in popup
    assert "每 20 个" in popup
    assert "待审核列表" in popup
    assert "登录浏览器" not in popup
    assert "比特浏览器" not in popup
    assert 'localStorage.getItem("token")' in content
    assert "__reactFiber$" in content


def test_ai_weight_price_launch_controls_live_in_extension():
    popup = (EXTENSION / "popup.html").read_text(encoding="utf-8")
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")

    assert 'id="weight-price-mode"' in popup
    assert 'id="weight-price-start-product-id"' in popup
    assert 'id="weight-price-limit"' in popup
    assert 'id="weight-price-category"' in popup
    assert 'id="weight-price-start"' in popup
    assert 'id="weight-price-stop"' in popup
    assert 'id="weight-price-current-meta"' in popup
    assert "/api/browser-extension/ai-weight-price/status" in background
    assert 'aiWeightPriceAction("start"' in background
    assert 'aiWeightPriceAction("stop"' in background


def test_ai_weight_price_console_keeps_product_table_stable_and_shows_owner():
    template = (ROOT / "bit" / "templates" / "ai_weight_price.html").read_text(
        encoding="utf-8"
    )
    backend = (ROOT / "bit" / "bit_interface.py").read_text(encoding="utf-8")

    assert 'id="refresh-list"' in template
    assert "refreshStatusOnly();},2000" in template
    assert "setInterval(()=>{if(document.visibilityState==='visible')refresh();" not in template
    assert "row.owner_display_name||row.owner_username" in template
    assert "entry.owner_name" in template
    assert "所有业务员商品" in template
    assert "ai_weight_price_service.bind_actor" in backend
    assert 'actor = data.get("actor")' in backend


def test_integrated_ai_weight_price_page_points_launch_to_extension(monkeypatch):
    import bit.bit_interface as workbench

    monkeypatch.setattr(workbench, "get_current_workbench_user", _browser_extension_user)
    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    client = workbench.app.test_client()
    with client.session_transaction() as login_session:
        login_session["workbench_user"] = _browser_extension_user()

    response = client.get("/ai-weight-price")

    assert response.status_code == 200
    assert "任务启动已迁移到泽顺插件" in response.text
    assert "智赢分类 / 执行终端" in response.text
    assert "status.execution_terminal" in response.text
    assert '<div class="launch-block" hidden>' in response.text


def test_integrated_ai_weight_price_is_read_only_from_other_terminals(monkeypatch):
    import bit.bit_interface as workbench

    monkeypatch.setattr(workbench, "get_current_workbench_user", _browser_extension_user)
    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    client = workbench.app.test_client()
    with client.session_transaction() as login_session:
        login_session["workbench_user"] = _browser_extension_user()
    remote = {
        "base_url": "https://zeshun.example.com",
        "environ_base": {"REMOTE_ADDR": "192.0.2.20"},
    }

    page = client.get("/ai-weight-price", **remote)
    status = client.get("/api/ai-weight-price/status", **remote)
    write = client.put(
        "/api/ai-weight-price/config",
        json={"daily_limit": 13},
        headers={"X-AWP-Request": "1"},
        **remote,
    )

    assert page.status_code == 200
    assert "当前终端为只读查看" in page.text
    assert 'id="live-console"' in page.text
    assert "const canExecute=false" in page.text
    assert status.status_code == 200
    assert write.status_code == 403


def test_list_card_collection_only_opens_china_or_managed_products():
    content = (EXTENSION / "content.js").read_text(encoding="utf-8")
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")

    assert 'window.open(candidate.url, "_blank", "noopener")' in content
    assert "extractCardProduct(candidate.card, candidate.url)" not in content
    assert "仅采集自发货和半托管" in content
    core = (EXTENSION / "collector-core.js").read_text(encoding="utf-8")
    assert "cardHasUsFlag" in core
    assert "cardShippingProfile" in core
    assert "商品列表检测到 US.svg：美国自发货商品不采集" in core
    assert "COLLECT_URL" not in content
    assert "COLLECT_URL" not in background
    batch = (EXTENSION / "product-batch.js").read_text(encoding="utf-8")
    assert 'type === "READ_PRODUCT_LIST" || type === "EXTRACT_BATCH_PRODUCT"' in batch
    assert "active: false" in batch
    assert "智赢详情浮层" in batch


def test_detail_collection_requires_china_or_managed_origin_and_actual_weight():
    core = (EXTENSION / "collector-core.js").read_text(encoding="utf-8")

    assert "plugin.self_ship_origin === \"US\"" not in core
    assert 'plugin.self_ship_origin !== "CN"' not in core
    assert "仅采集自发货和半托管" in (
        EXTENSION / "content.js"
    ).read_text(encoding="utf-8")
    assert "actualWeightComplete" in core
    assert 'const weightBasis = actualWeightComplete ? "plugin_actual" : ""' in core


def _browser_extension_user():
    return {
        "id": 7,
        "username": "collector",
        "display_name": "采集员",
        "access_version": 0,
    }


def test_browser_extension_login_returns_signed_short_lived_session(monkeypatch):
    import bit.bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    workbench.app.secret_key = "extension-test-secret"
    monkeypatch.setattr(
        workbench,
        "authenticate_workbench_user",
        lambda username, password: (
            _browser_extension_user()
            if (username, password) == ("collector", "secret")
            else None
        ),
    )
    client = workbench.app.test_client()

    response = client.post(
        "/api/browser-extension/login",
        json={"username": "collector", "password": "secret"},
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["token"]
    assert data["expires_in"] == workbench.WORKBENCH_REMEMBER_HOURS * 60 * 60
    session_response = client.get(
        "/api/browser-extension/session",
        headers={"Authorization": f"Bearer {data['token']}"},
    )
    assert session_response.status_code == 200
    assert session_response.get_json()["data"]["user"]["username"] == "collector"


def test_account_integration_settings_route_never_returns_api_keys(monkeypatch):
    import bit.bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    workbench.app.secret_key = "extension-test-secret"
    captured = {}

    def save_settings(user_id, payload, secret_key):
        captured.update(user_id=user_id, payload=payload, secret_key=secret_key)
        return {
            "deepseek_configured": True,
            "dashscope_configured": True,
        }

    monkeypatch.setattr(
        workbench.browser_extension_models, "save_settings", save_settings
    )
    client = workbench.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = _browser_extension_user()
    response = client.put(
        "/api/account-integrations/tokens",
        json={
            "deepseek_api_key": "deepseek-secret",
            "dashscope_api_key": "dashscope-secret",
        },
    )

    assert response.status_code == 200
    body = response.get_json()["data"]
    assert body == {
        "deepseek_configured": True,
        "dashscope_configured": True,
    }
    assert captured["user_id"] == 7
    assert "deepseek-secret" not in response.get_data(as_text=True)
    assert "dashscope-secret" not in response.get_data(as_text=True)


def test_browser_extension_collect_requires_login_and_writes_one_quick_item(monkeypatch):
    import bit.bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    workbench.app.secret_key = "extension-test-secret"
    user = _browser_extension_user()
    token = workbench.create_browser_extension_token(user)
    created = []
    written = []
    updated = []
    monkeypatch.setattr(
        workbench,
        "db_create_mercado_collection_task",
        lambda source_url, requested_count, created_by: (
            created.append((source_url, requested_count, created_by)) or 88
        ),
    )
    monkeypatch.setattr(
        workbench,
        "db_upsert_mercado_collection_items",
        lambda task_id, rows: written.append((task_id, rows)) or 1,
    )
    monkeypatch.setattr(
        workbench,
        "db_update_mercado_collection_task",
        lambda task_id, **changes: updated.append((task_id, changes)),
    )
    client = workbench.app.test_client()
    product = {
        "source_item_id": "MLM3016972321",
        "source_url": "https://articulo.mercadolibre.com.mx/MLM-3016972321",
        "title": "列表快速采集商品",
        "price": 418,
        "currency_id": "MXN",
        "scrape_status": "partial",
    }

    unauthorized = client.post(
        "/api/browser-extension/collect", json={"product": product}
    )
    response = client.post(
        "/api/browser-extension/collect",
        json={"product": product},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 201
    assert response.get_json()["data"]["task_id"] == 88
    assert created[0][1] == 1
    assert "collector" in created[0][2]
    assert written[0][0] == 88
    assert written[0][1][0]["source_item_id"] == "MLM3016972321"
    assert updated[0][1]["status"] == "partial"


def test_browser_extension_collect_routes_1688_to_ai_original_products(monkeypatch):
    import bit.bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    workbench.app.secret_key = "extension-test-secret"
    token = workbench.create_browser_extension_token(_browser_extension_user())
    captured = []
    monkeypatch.setattr(
        workbench,
        "db_upsert_ai_original_product",
        lambda product, created_by="": (
            captured.append((product, created_by))
            or {
                "id": 91,
                "source_item_id": "1688123456789",
                "ai_status": "pending",
            }
        ),
    )
    client = workbench.app.test_client()

    response = client.post(
        "/api/browser-extension/collect",
        json={"product": {
            "source_platform": "1688",
            "source_item_id": "123456789",
            "source_url": "https://detail.1688.com/offer/123456789.html",
            "title": "1688 测试商品",
            "images": ["https://cbu01.alicdn.com/test.jpg"],
        }},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    assert response.get_json()["data"] == {
        "product_id": 91,
        "source_item_id": "1688123456789",
        "source_platform": "1688",
        "ai_status": "pending",
    }
    assert captured[0][0]["source_platform"] == "1688"
    assert "collector" in captured[0][1]


def test_ai_original_source_image_proxy_allows_only_1688_cdn(monkeypatch):
    import bit.bit_interface as workbench

    captured = []

    class Upstream(io.BytesIO):
        headers = {"Content-Type": "image/webp"}

        def geturl(self):
            return "https://cbu01.alicdn.com/img/ibank/test.webp"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request, timeout):
        captured.append((request, timeout))
        return Upstream(b"webp-image-bytes")

    monkeypatch.setattr(workbench, "urlopen", fake_urlopen)
    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    workbench.app.secret_key = "extension-test-secret"
    client = workbench.app.test_client()
    with client.session_transaction() as login_session:
        login_session["workbench_user"] = _browser_extension_user()

    response = client.get(
        "/api/ai-original-products/source-image",
        query_string={
            "url": "https://cbu01.alicdn.com/img/ibank/test.webp",
        },
    )
    blocked = client.get(
        "/api/ai-original-products/source-image",
        query_string={"url": "http://127.0.0.1/private.jpg"},
    )

    assert response.status_code == 200
    assert response.data == b"webp-image-bytes"
    assert response.headers["Content-Type"].startswith("image/webp")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert captured[0][1] == 15
    assert captured[0][0].get_header("Referer") == "https://detail.1688.com/"
    assert blocked.status_code == 400
    assert len(captured) == 1


def test_browser_extension_starts_zying_collection_from_current_browser_credential(monkeypatch):
    import bit.bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    workbench.app.secret_key = "extension-test-secret"
    token = workbench.create_browser_extension_token(_browser_extension_user())
    captured = {}

    class ImmediateThread:
        def __init__(self, target, args=(), **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    def collect_products(**kwargs):
        captured.update(kwargs)
        return {
            "collected_count": 1,
            "inserted_count": 1,
            "skipped_existing_count": 0,
            "duplicate_count": 0,
            "detail_failed_count": 0,
        }

    monkeypatch.setattr(workbench.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(workbench.bit_zying_caiji, "collect_zying_products", collect_products)
    monkeypatch.setattr(
        workbench.bit_zying_caiji,
        "list_zying_product_developers",
        lambda credential: [{"id": "17", "name": "产品开发甲"}],
    )
    monkeypatch.setattr(
        workbench,
        "db_sync_zying_product_developers",
        lambda rows: {"zying_products": 0, "product_list": 0},
    )
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "credential": "local-browser-token",
        "categories": [{"category_id": "202170568", "category_name": "家电类"}],
        "start_page": 2,
        "end_page": 3,
        "category": "202170568",
        "category_name": "家电类",
        "product_developer_id": "17",
        "product_developer_name": "产品开发甲",
    }
    client = workbench.app.test_client()
    with workbench._zying_collection_state_lock:
        previous_state = dict(workbench._zying_collection_state)
        previous_logs = list(workbench._zying_collection_logs)
    try:
        options = client.post(
            "/api/browser-extension/zying/options", json=body, headers=headers
        )
        response = client.post(
            "/api/browser-extension/zying/start", json=body, headers=headers
        )
        status = client.get(
            "/api/browser-extension/zying/status", headers=headers
        )
    finally:
        with workbench._zying_collection_state_lock:
            workbench._zying_collection_state.clear()
            workbench._zying_collection_state.update(previous_state)
            workbench._zying_collection_logs.clear()
            workbench._zying_collection_logs.extend(previous_logs)

    assert options.status_code == 200
    assert options.get_json()["data"]["developers"][0]["id"] == "17"
    assert response.status_code == 200
    assert captured["auth_token"] == "local-browser-token"
    assert captured["start_page"] == 2
    assert captured["number"] == 3
    assert captured["category"] == "202170568"
    status_data = status.get_json()["data"]
    assert status_data["status"] == "success"
    assert "auth_token" not in status_data["params"]


def test_browser_extension_starts_zying_infringement_with_fixed_title_batches(monkeypatch):
    import bit.bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    workbench.app.secret_key = "extension-test-secret"
    token = workbench.create_browser_extension_token(_browser_extension_user())
    captured = {}
    monkeypatch.setattr(
        workbench.browser_extension_models,
        "get_api_key",
        lambda user_id, provider, secret: (
            "deepseek-from-settings"
            if (user_id, provider) == (7, "deepseek")
            else ""
        ),
    )

    class ImmediateThread:
        def __init__(self, target, args=(), **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    def review_products(**kwargs):
        captured.update(kwargs)
        return {
            "pages": 2,
            "pending_count": 4,
            "checked_count": 4,
            "approved_count": 3,
            "suspected_count": 1,
            "skipped_changed_count": 0,
            "failed_count": 0,
        }

    monkeypatch.setattr(workbench.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        workbench.bit_zying_infringement,
        "review_pending_products",
        review_products,
    )
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "credential": "current-page-token",
        "start_product_id": "801623017",
        "max_items": 12,
        "category": "202170568",
        "category_name": "家电类",
        "product_developer_id": "17",
        "product_developer_name": "产品开发甲",
    }
    with workbench._zying_infringement_state_lock:
        previous_state = dict(workbench._zying_infringement_state)
        previous_logs = list(workbench._zying_infringement_logs)
    try:
        client = workbench.app.test_client()
        response = client.post(
            "/api/browser-extension/zying-infringement/start",
            json=body,
            headers=headers,
        )
        status = client.get(
            "/api/browser-extension/zying-infringement/status",
            headers=headers,
        )
    finally:
        with workbench._zying_infringement_state_lock:
            workbench._zying_infringement_state.clear()
            workbench._zying_infringement_state.update(previous_state)
            workbench._zying_infringement_logs.clear()
            workbench._zying_infringement_logs.extend(previous_logs)

    assert response.status_code == 200
    assert captured["auth_token"] == "current-page-token"
    assert captured["start_page"] == 1
    assert captured["end_page"] == 10000
    assert captured["start_product_id"] == "801623017"
    assert captured["max_items"] == 12
    assert captured["category"] == "202170568"
    assert captured["deepseek_api_key"] == "deepseek-from-settings"
    status_data = status.get_json()["data"]
    assert status_data["status"] == "success"
    assert status_data["batch_size"] == 20
    assert "auth_token" not in status_data["params"]
    assert "deepseek_api_key" not in status_data["params"]


def test_browser_extension_zying_options_uses_developers_from_current_page(monkeypatch):
    import bit.bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    workbench.app.secret_key = "extension-test-secret"
    token = workbench.create_browser_extension_token(_browser_extension_user())
    monkeypatch.setattr(
        workbench.bit_zying_caiji,
        "list_zying_product_developers",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("网页已提供开发人员时不应启动后端智赢读取")
        ),
    )
    monkeypatch.setattr(
        workbench,
        "db_sync_zying_product_developers",
        lambda rows: {"zying_products": len(rows), "product_list": len(rows)},
    )
    response = workbench.app.test_client().post(
        "/api/browser-extension/zying/options",
        json={
            "credential": "page-credential",
            "categories": [{"category_id": "17", "category_name": "测试分类"}],
            "developers": [{"id": 17, "name": "产品开发甲"}],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["developers"] == [
        {"id": "17", "name": "产品开发甲"}
    ]


def test_browser_extension_starts_ai_weight_price_on_local_workstation(monkeypatch):
    import bit.bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    workbench.app.secret_key = "extension-test-secret"
    token = workbench.create_browser_extension_token(_browser_extension_user())
    headers = {"Authorization": f"Bearer {token}"}
    captured = {}
    monkeypatch.setattr(
        workbench.browser_extension_models,
        "get_api_key",
        lambda *_args: "account-dashscope-key",
    )

    def start(
        mode="pipeline", task_id=None, selection=None, max_items=10,
        resume=False, runtime_api_key="",
    ):
        captured.update({
            "mode": mode,
            "task_id": task_id,
            "selection": selection,
            "max_items": max_items,
            "resume": resume,
            "runtime_api_key": runtime_api_key,
        })

    monkeypatch.setattr(workbench.ai_weight_price_service, "start", start)
    client = workbench.app.test_client()
    response = client.post(
        "/api/browser-extension/ai-weight-price/start",
        json={
            "selection": {
                "category": "",
                "start_product_id": "848332340",
            },
            "max_items": 12,
        },
        headers=headers,
    )
    status = client.get(
        "/api/browser-extension/ai-weight-price/status", headers=headers
    )
    remote = client.post(
        "/api/browser-extension/ai-weight-price/start",
        json={
            "selection": {
                "category": "",
                "start_page": 1,
                "end_page": 1,
                "start_item": 1,
            },
            "max_items": 1,
        },
        headers=headers,
        environ_base={"REMOTE_ADDR": "192.0.2.20"},
    )

    assert response.status_code == 200
    assert captured == {
        "mode": "pipeline",
        "task_id": None,
        "selection": {
            "category": "",
            "start_product_id": "848332340",
        },
        "max_items": 12,
        "resume": False,
        "runtime_api_key": "account-dashscope-key",
    }
    assert status.status_code == 200
    assert "categories" in status.get_json()["data"]
    assert remote.status_code == 403
    assert "本机泽顺控制台" in remote.get_json()["message"]


def test_weight_price_extension_login_and_resume_routes(monkeypatch):
    import bit.bit_interface as workbench

    calls = []
    monkeypatch.setattr(workbench, "_browser_extension_user_from_token", lambda _: _browser_extension_user())
    monkeypatch.setattr(workbench, "_browser_extension_ai_weight_price_snapshot", lambda: {"running": False})
    monkeypatch.setattr(
        workbench.browser_extension_models,
        "get_api_key",
        lambda user_id, provider, _secret: (
            calls.append(("credential", user_id, provider))
            or "account-dashscope-key"
        ),
    )
    monkeypatch.setattr(workbench.ai_weight_price_service, "open_login", lambda **kw: calls.append(kw))
    monkeypatch.setattr(
        workbench.ai_weight_price_service,
        "continue_after_human",
        lambda **kw: calls.append(("continue", kw)),
    )
    client = workbench.app.test_client()
    root = "/api/browser-extension/ai-weight-price/"
    assert client.post(root + "login/open", json={}).status_code == 200
    assert calls == [{"include_supplier": False}]
    assert client.post(root + "continue", json={}).status_code == 400
    assert client.post(root + "continue", json={"acknowledged": True},
                       environ_base={"REMOTE_ADDR": "192.0.2.20"}).status_code == 403
    assert calls == [{"include_supplier": False}]
    assert client.post(root + "continue", json={"acknowledged": True}).status_code == 200
    assert calls[-2:] == [
        ("credential", 7, "dashscope"),
        ("continue", {"runtime_api_key": "account-dashscope-key"}),
    ]
    monkeypatch.setattr(workbench, "_browser_extension_user_from_token", lambda _: {
        **_browser_extension_user(), "access_version": 1, "permissions": ["ai_weight_price.view"]})
    assert client.post(root + "continue", json={"acknowledged": True}).status_code == 403
    assert len(calls) == 3
