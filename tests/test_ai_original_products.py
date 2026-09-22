import json
from io import BytesIO

import pytest
from pathlib import Path

from erp.ai_original_products import (
    generate_marketplace_copy,
    generate_ai_white_background_image,
    normalize_1688_product,
    create_white_background_image,
)
from erp.mercadolibre_batch_publish import (
    _prepared_listing_from_product_row,
    product_publish_issues,
)


def test_normalize_1688_product_keeps_source_facts():
    product = normalize_1688_product({
        "source_url": "https://detail.1688.com/offer/123456789.html",
        "title": "  测试   商品  ",
        "price": "12.50",
        "images": ["//cbu01.alicdn.com/a.jpg"],
        "properties": [{"name": "材质", "value": "ABS"}],
    })

    assert product["source_item_id"] == "1688123456789"
    assert product["source_1688_item_id"] == "123456789"
    assert product["title"] == "测试 商品"
    assert product["images"] == ["https://cbu01.alicdn.com/a.jpg"]


def test_ai_copy_removes_declared_brand_and_limits_both_titles():
    response = json.dumps({
        "title_es": "Acme Organizador portátil para escritorio con muchos accesorios y almacenamiento",
        "title_pt": "Acme Organizador portátil para mesa com acessórios e armazenamento",
        "description_es": "Descripción original en español.",
        "description_pt": "Descrição original em português.",
        "attributes": [
            {
                "name_es": "Material",
                "name_pt": "Material",
                "value_name_es": "Plástico",
                "value_name_pt": "Plástico resistente",
            },
            {"name": "Tipo de cierre", "value_name": "Tapa a presión"},
        ],
        "brand_terms": ["Acme"],
    })

    copy = generate_marketplace_copy(
        {"title": "Acme 收纳盒", "properties": [{"name": "品牌", "value": "Acme"}]},
        chat=lambda *_args, **_kwargs: response,
    )

    assert "acme" not in copy["title_es"].lower()
    assert "acme" not in copy["title_pt"].lower()
    assert len(copy["title_es"]) <= 60
    assert len(copy["title_pt"]) <= 60
    assert copy["attributes"][0]["name"] == "Material"
    assert copy["attributes"][0]["value_name"] == "Plástico"


def test_ai_copy_rejects_reused_original_description():
    response = json.dumps({
        "title_es": "Organizador portátil",
        "title_pt": "Organizador portátil",
        "description_es": "Texto original sin cambios.",
        "description_pt": "Novo texto em português.",
        "brand_terms": [],
    })

    with pytest.raises(ValueError, match="原创描述"):
        generate_marketplace_copy(
            {"title": "收纳盒", "description_text": "Texto original sin cambios."},
            chat=lambda *_args, **_kwargs: response,
        )


def test_white_background_image_is_square_and_white(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    source = Image.new("RGB", (700, 500), (230, 230, 230))
    for x in range(220, 480):
        for y in range(110, 410):
            source.putpixel((x, y), (25, 80, 180))
    buffer = BytesIO()
    source.save(buffer, format="PNG")

    class Response:
        content = buffer.getvalue()

        @staticmethod
        def raise_for_status():
            return None

    def remove_background(image):
        cutout = image.convert("RGBA")
        alpha = Image.new("L", cutout.size, 0)
        for x in range(220, 480):
            for y in range(110, 410):
                alpha.putpixel((x, y), 255)
        cutout.putalpha(alpha)
        return cutout

    path, url = create_white_background_image(
        "https://cbu01.alicdn.com/source.png",
        "123456789",
        image_dir=tmp_path,
        http_get=lambda *_args, **_kwargs: Response(),
        background_remove=remove_background,
    )
    with Image.open(path) as output:
        assert output.size == (1024, 1024)
        assert all(channel >= 245 for channel in output.getpixel((0, 0)))
        assert output.getpixel((512, 512))[2] >= 170
    assert url.endswith("/api/ai-original-products/images/1688-123456789-ai-white.jpg")


def test_ai_white_background_image_uses_image_model_callback(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    source = Image.new("RGB", (320, 240), (30, 80, 180))
    source_buffer = BytesIO()
    source.save(source_buffer, format="PNG")
    generated = Image.new("RGB", (512, 512), "white")
    generated_buffer = BytesIO()
    generated.save(generated_buffer, format="PNG")

    class Response:
        content = source_buffer.getvalue()

        @staticmethod
        def raise_for_status():
            return None

    path, url = generate_ai_white_background_image(
        "https://cbu01.alicdn.com/source.png",
        "123456789",
        image_dir=tmp_path,
        http_get=lambda *_args, **_kwargs: Response(),
        image_generate=lambda **_kwargs: generated_buffer.getvalue(),
    )
    with Image.open(path) as output:
        assert output.width == output.height
        assert output.width >= 1024
        assert output.getpixel((0, 0)) == (255, 255, 255)
    assert url.endswith("/api/ai-original-products/images/1688-123456789-ai-white.jpg")


def _ai_row():
    prepared = {
        "status": "completed",
        "title_es": "Organizador portátil de escritorio",
        "title_pt": "Organizador portátil para mesa",
        "description_es": "Nueva descripción en español.",
        "description_pt": "Nova descrição em português.",
        "image_generation_method": "ai_image_edit",
        "attributes": [
            {
                "id": "MATERIAL",
                "name": "Material",
                "name_es": "Material",
                "name_pt": "Material",
                "value_name": "Plástico",
                "value_name_es": "Plástico",
                "value_name_pt": "Plástico resistente",
            },
            {"id": "COLOR", "name": "Color", "value_name": "Azul"},
            {"id": "BRAND", "value_name": "Generic"},
            {"id": "ITEM_CONDITION", "value_name": "New"},
        ],
        "main_image_url": "http://127.0.0.1:5000/api/ai-original-products/images/1688-123-ai-white.jpg",
    }
    return {
        "id": 8,
        "source_type": "ai_original",
        "source_item_id": "1688123",
        "source_url": "https://detail.1688.com/offer/123.html",
        "main_image_url": prepared["main_image_url"],
        "title": prepared["title_es"],
        "description_text": prepared["description_es"],
        "currency_id": "USD",
        "category_id": "CBT123",
        "weight_g": 500,
        "weight_basis": "ai_original_manual",
        "net_proceeds_usd": 20,
        "review_status": "approved",
        "source_snapshot_json": json.dumps({
            "ai_original": prepared,
            "source": {"id": "CBT123", "site_id": "CBT", "pictures": [{"source": prepared["main_image_url"]}]},
            "description": {"plain_text": prepared["description_es"]},
        }),
    }


def test_publish_preparation_uses_portuguese_only_for_brazil():
    row = _ai_row()

    mexico_source, mexico_description = _prepared_listing_from_product_row(row, "MLM")
    brazil_source, brazil_description = _prepared_listing_from_product_row(row, "MLB")

    assert mexico_source["title"].startswith("Organizador portátil de")
    assert mexico_description["plain_text"].startswith("Nueva")
    assert brazil_source["title"].startswith("Organizador portátil para")
    assert brazil_description["plain_text"].startswith("Nova")
    assert {item["id"] for item in mexico_source["attributes"]} >= {"MATERIAL", "COLOR", "BRAND", "ITEM_CONDITION"}
    assert next(item for item in brazil_source["attributes"] if item["id"] == "MATERIAL")["value_name"] == "Plástico resistente"
    assert product_publish_issues(row) == []


def test_ai_original_publish_requires_completed_copy_and_white_image():
    row = _ai_row()
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["ai_original"]["status"] = "pending"
    row["source_snapshot_json"] = json.dumps(snapshot)
    row["main_image_url"] = "https://example.test/original.jpg"

    issues = product_publish_issues(row)

    assert "AI 原创任务尚未完成" in issues
    assert "首图尚未完成 AI 白底生成" in issues


def test_ai_original_publish_requires_generated_listing_attributes():
    row = _ai_row()
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["ai_original"]["attributes"] = [
        {"id": "BRAND", "value_name": "Generic"},
        {"id": "ITEM_CONDITION", "value_name": "New"},
    ]
    row["source_snapshot_json"] = json.dumps(snapshot)

    assert "AI 商品属性尚未生成" in product_publish_issues(row)


def test_ai_original_publish_accepts_free_local_white_background():
    row = _ai_row()
    snapshot = json.loads(row["source_snapshot_json"])
    snapshot["ai_original"]["image_generation_method"] = "local_background_removal"
    row["source_snapshot_json"] = json.dumps(snapshot)

    assert product_publish_issues(row) == []


def test_workbench_exposes_ai_original_module_and_batch_actions():
    root = Path(__file__).resolve().parents[1]
    template = (root / "bit" / "templates" / "index.html").read_text(encoding="utf-8")
    script = (root / "bit" / "static" / "ai-original-products.js").read_text(encoding="utf-8")

    assert 'data-tab="ai-original-products"' in template
    assert 'id="tab-ai-original-products"' in template
    assert "1688 原始资料" in script
    assert "AI 美客多刊登稿" in script
    assert "renderAiOriginalAttributes" in script
    assert "rembg / isnet-general-use" in template
    assert 'id="ai-original-image-model"' not in template
    assert "执行所选 AI 任务" in template
    assert "上架所选到对应店铺" in template
    assert 'fetch("/api/ai-original-products/process"' in script
    assert 'fetch("/api/mercado-products/publish"' in script


def test_server_requirements_include_free_local_background_removal():
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "bit" / "requirements-server.txt").read_text(encoding="utf-8")

    assert "rembg[cpu]==2.0.67" in requirements


def test_workbench_exposes_1688_product_area_for_collector_records():
    root = Path(__file__).resolve().parents[1]
    template = (root / "bit" / "templates" / "index.html").read_text(encoding="utf-8")
    script = (root / "bit" / "static" / "1688-products.js").read_text(encoding="utf-8")

    assert 'data-tab="1688-products"' in template
    assert 'id="tab-1688-products"' in template
    assert "1688产品区" in template
    assert 'fetch(`/api/1688-products?' in script
    assert "products1688OpenDetail" in script
