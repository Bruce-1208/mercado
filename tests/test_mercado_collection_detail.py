import json

import pytest

from bit.mercado_collection_detail import build_collection_detail, safe_url


@pytest.fixture
def detail_row():
    pictures = [{"id": str(i), "secure_url": f"https://images.test/product-{i}.jpg"} for i in range(1, 15)]
    return {
        "id": 91, "title": "便携蓝牙音箱 · 完整采集资料", "source_item_id": "MLM123456",
        "source_url": "https://www.mercadolibre.com.mx/product", "main_image_url": pictures[0]["secure_url"],
        "organization_key": "wuhan-zeshun", "visibility": "enterprise", "salesperson": "业务甲",
        "price": 499, "currency_id": "MXN", "sale_price_usd": 25, "shipping_fee_usd": 6,
        "weight_g": 420, "package_length_cm": 18, "package_width_cm": 10, "package_height_cm": 8,
        "scrape_status": "ok", "collected_at": "2026-10-08 14:35:12",
        "source_json": json.dumps({
            "id": "MLM123456", "site_id": "MLM", "title": "Altavoz portátil", "condition": "new",
            "available_quantity": 20, "pictures": pictures,
            "attributes": [{"name": "品牌", "value_name": "Generic"},
                           {"name": "颜色", "values": [{"name": "蓝色"}, {"name": "黑色"}]},
                           {"name": "续航", "value_struct": {"number": 12, "unit": "h"}},
                           {"name": "防水", "value": False}],
            "variations": [{"id": 201, "attribute_combinations": [{"name": "颜色", "value_name": "蓝色"}],
                            "price": 499, "available_quantity": 0, "seller_custom_field": "SPEAKER-BLUE",
                            "picture_ids": ["2"], "pictures": [{"source": "https://images.test/variant.jpg"}]}],
            "sale_terms": [{"name": "保修", "value_name": "12 个月"}], "warranty": "一年保修",
        }),
        "page_snapshot_json": json.dumps({"pictures": [pictures[0]["secure_url"], "https://images.test/page-only.jpg"],
                                          "specs": [{"name": "材质", "value": "ABS"}]}),
        "description_json": json.dumps({"plain_text": "完整描述第一段\n" + "说明正文 " * 500 + "\n完整描述最后一行"}),
        "ai_copy_json": json.dumps({"title_es": "Altavoz portátil", "description_es": "Descripción completa",
                                    "title_pt": "Caixa de som portátil", "description_pt": "Descrição completa",
                                    "source_description": "原始采集描述，完整保留"}),
    }


def test_detail_merges_all_images_specs_variations_and_full_descriptions(detail_row):
    detail = build_collection_detail(detail_row)
    assert len(detail["images"]) == 16
    assert len(detail["images"]) == len(set(detail["images"]))
    assert detail["images"][-1].endswith("variant.jpg")
    attributes = {field["name"]: field["value"] for field in detail["attributes"]}
    assert attributes == {"品牌": "Generic", "颜色": "蓝色 / 黑色", "续航": "12 h", "防水": "否", "材质": "ABS"}
    assert detail["descriptions"][0]["text"].endswith("完整描述最后一行")
    assert len(detail["descriptions"]) == 4
    assert detail["variants"][0]["quantity"] == "0"
    assert len(detail["variants"][0]["images"]) == 2
    assert {f["name"]: f["value"] for f in detail["financial"]}["净收益"] == "15.25 USD"


def test_missing_bad_json_and_html_only_description():
    detail = build_collection_detail({"source_json": "{bad", "description_json": json.dumps({
        "body": "<p>第一段 &amp; 内容</p><script>alert(1)</script><p>第二段<br>最后一行</p>"})})
    assert detail["images"] == []
    assert detail["attributes"] == []
    assert detail["descriptions"][0]["text"] == "第一段 & 内容\n\n第二段\n最后一行"
    detail = build_collection_detail({"sale_price_usd": 10})
    prices = {field["name"]: field["value"] for field in detail["financial"]}
    assert prices["佣金（固定 15%）"] == "1.5 USD"
    assert prices["净收益"] == "—"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:image/png;base64,abc", "https://[bad", ""])
def test_untrusted_image_and_source_urls_are_rejected(url):
    assert safe_url(url) == ""
    assert build_collection_detail({"main_image_url": url, "source_url": url})["images"] == []


@pytest.fixture
def detail_client(monkeypatch):
    from bit import bit_interface as web
    current = {"id": 7, "username": "collector", "access_version": 1,
               "permissions": ["catalog.view"], "organization_key": "wuhan-zeshun"}
    monkeypatch.setattr(web, "get_current_workbench_user", lambda: current)
    monkeypatch.setattr(web, "_workbench_billing_blocked", lambda actor: None)
    monkeypatch.setattr(web, "workbench_selected_organization", lambda *args: "wuhan-zeshun")
    return web, current, web.app.test_client()


def test_page_fetches_complete_record_and_escapes_collected_content(detail_client, detail_row, monkeypatch):
    web, _, client = detail_client
    calls = []
    detail_row["title"] = '<script>alert("title")</script>'
    monkeypatch.setattr(web, "db_get_mercado_collection_items_by_ids", lambda ids: calls.append(ids) or [detail_row])
    response = client.get('/mercado-collection/items/91')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "完整描述最后一行" in html and "page-only.jpg" in html and "SPEAKER-BLUE" in html
    assert "&lt;script&gt;" in html and '<script>alert("title")</script>' not in html
    assert "<details" not in html
    assert calls == [[91]]


@pytest.mark.parametrize("changes", [{"visibility": "private", "owner_user_id": 99}, {"organization_key": "other"}])
def test_page_rejects_private_or_other_enterprise_record(detail_client, detail_row, monkeypatch, changes):
    web, _, client = detail_client
    detail_row.update(changes)
    monkeypatch.setattr(web, "db_get_mercado_collection_items_by_ids", lambda ids: [detail_row])
    response = client.get('/mercado-collection/items/91')
    assert response.status_code == 403
    assert detail_row["title"] not in response.get_data(as_text=True)


def test_page_missing_failure_and_login_states(detail_client, monkeypatch):
    web, current, client = detail_client
    monkeypatch.setattr(web, "db_get_mercado_collection_items_by_ids", lambda ids: [])
    assert client.get('/mercado-collection/items/91').status_code == 404

    def failure(ids):
        raise RuntimeError("private connection details")
    monkeypatch.setattr(web, "db_get_mercado_collection_items_by_ids", failure)
    response = client.get('/mercado-collection/items/91')
    assert response.status_code == 503
    assert "private connection details" not in response.get_data(as_text=True)
    current.clear()
    assert client.get('/mercado-collection/items/91').status_code == 302
