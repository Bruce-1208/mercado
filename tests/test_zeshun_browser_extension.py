import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension" / "zeshun_collector"


def test_manifest_is_chrome_edge_manifest_v3_and_declares_supported_sites():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert manifest["background"]["service_worker"] == "background.js"
    assert manifest["action"]["default_popup"] == "popup.html"
    matches = manifest["content_scripts"][0]["matches"]
    assert any("mercadolibre.com.mx" in pattern for pattern in matches)
    assert any("mercadolivre.com.br" in pattern for pattern in matches)
    assert "http://127.0.0.1/*" in manifest["host_permissions"]


def test_extension_files_referenced_by_manifest_exist():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    referenced = [
        manifest["background"]["service_worker"],
        manifest["action"]["default_popup"],
        manifest["options_page"],
        *manifest["content_scripts"][0]["js"],
        *manifest["content_scripts"][0]["css"],
    ]

    assert all((EXTENSION / filename).is_file() for filename in referenced)


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not installed")
def test_all_extension_javascript_has_valid_syntax():
    for filename in (
        "collector-core.js",
        "content.js",
        "background.js",
        "popup.js",
        "options.js",
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


def test_list_card_collection_requires_detail_page_origin_verification():
    content = (EXTENSION / "content.js").read_text(encoding="utf-8")
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")

    assert 'window.open(candidate.url, "_blank", "noopener")' in content
    assert "extractCardProduct(candidate.card, candidate.url)" not in content
    assert "等待智赢显示 CN.svg 后再采集" in content
    assert "列表页无法确认 CN.svg" in (
        EXTENSION / "collector-core.js"
    ).read_text(encoding="utf-8")
    assert "COLLECT_URL" not in content
    assert "COLLECT_URL" not in background
    assert "active: false" not in background


def test_detail_collection_rejects_zying_us_self_ship_and_requires_actual_weight():
    core = (EXTENSION / "collector-core.js").read_text(encoding="utf-8")

    assert "plugin.self_ship_origin === \"US\"" in core
    assert "美国自发货商品不采集" in core
    assert 'plugin.self_ship_origin !== "CN"' in core
    assert "无法确认中国自发货" in core
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
