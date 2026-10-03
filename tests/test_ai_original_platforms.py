import json
from copy import deepcopy
from io import BytesIO

import pytest

from erp.ai_original_platforms import (
    build_listing_export, listing_profiles_catalog, normalize_listing_options,
    validate_platform_listing,
)
from erp import ai_original_products as products


def draft(platform="amazon", **updates):
    row = {
        "title": "Desktop Storage Box" if platform == "amazon" else "Настольный органайзер",
        "description": "A box for storing desktop accessories." if platform == "amazon" else "Органайзер для хранения принадлежностей на столе.",
        "bullet_points": ["Stores desktop accessories"] if platform == "amazon" else [],
        "main_image_url": "/api/ai-original-products/images/example.jpg",
        "attributes": [],
    }
    return {**row, **updates}


def test_catalog_is_defensive_and_options_reject_unknowns():
    catalog = listing_profiles_catalog()
    assert {p["id"] for p in catalog["platforms"]} == {"mercadolibre", "yandex", "amazon"}
    amazon = next(p for p in catalog["platforms"] if p["id"] == "amazon")
    assert amazon["language"] == "en_US"
    assert amazon["title_limit"] == 75
    assert amazon["item_highlights_limit"] == 125
    amazon["notes"].clear()
    assert next(p for p in listing_profiles_catalog()["platforms"] if p["id"] == "amazon")["notes"]
    assert normalize_listing_options() == ("mercadolibre", "standard")
    for platform, template in (("other", "standard"), ("yandex", "unknown")):
        with pytest.raises(ValueError):
            normalize_listing_options(platform, template)


@pytest.mark.parametrize("value,expected", [
    ({"title": "x" * 76}, "75"),
    ({"title": "Box Box Box"}, "同词"),
    ({"title": "Storage Box!"}, "受限字符"),
    ({"item_highlights": "a" * 126}, "125"),
    ({"description": "See https://example.com"}, "外部链接"),
    ({"description": "Free shipping for a limited time"}, "促销"),
    ({"bullet_points": ["Useful 🌟"]}, "emoji"),
    ({"bullet_points": ["中文卖点"]}, "目标平台语言"),
    ({"item_highlights": "中文补充信息"}, "目标平台语言"),
    ({"bullet_points": ["x"] * 6}, "5 条"),
    ({"bullet_points": []}, "至少"),
])
def test_amazon_rules_are_applied(value, expected):
    errors = validate_platform_listing(draft(**value), "amazon")["errors"]
    assert any(expected in error for error in errors)


def test_title_word_repetition_exempts_function_words():
    value = draft(title="Box for Pens and Tools for Crafts and Parts for Desks")
    assert not validate_platform_listing(value, "amazon")["errors"]


def test_yandex_limits_and_russian_language():
    assert not validate_platform_listing(draft("yandex"), "yandex")["errors"]
    assert validate_platform_listing(draft("yandex", title="a" * 257), "yandex")["errors"]
    assert validate_platform_listing(draft("yandex", description="а" * 6001), "yandex")["errors"]
    assert "俄语" in " ".join(validate_platform_listing(draft(), "yandex")["errors"])
    assert "emoji" in " ".join(validate_platform_listing(draft("yandex", title="Органайзер 🌟"), "yandex")["errors"])


def test_templates_change_prompt_without_removing_platform_rules():
    standard = products.build_platform_copy_prompt({}, "amazon", "standard")
    specs = products.build_platform_copy_prompt({}, "amazon", "specs")
    assert standard != specs
    assert "75字符" in standard and "75字符" in specs
    assert "source_index" in specs
    assert products.build_copy_prompt({}, "specs") != products.build_copy_prompt({}, "features")


def test_translations_anchor_properties_and_reject_invented_identifiers():
    original = {"properties": [{"name": "材质", "value": "塑料"}, {"name": "品牌", "value": "Acme"}]}
    generated = [
        {"source_index": 0, "name": "Material", "value_name": "Plastic"},
        {"source_index": 1, "name": "Brand", "value_name": "Other Brand"},
        {"name": "GTIN", "value_name": "1234567890123"},
        {"source_index": 0, "name": "Condition", "value_name": "New"},
    ]
    attrs = products._translated_source_attributes(original, generated)
    assert len(attrs) == 2
    assert attrs[0]["value_name"] == "Plastic"
    assert attrs[1]["value_name"] == "Acme"
    assert not any(item["name"] in {"GTIN", "Condition"} for item in attrs)


def test_non_ml_generation_never_touches_mercado_or_calls_category(monkeypatch):
    old_draft = {"title_es": "Original Mercado", "status": "completed"}
    snapshot = {
        "ai_original": deepcopy(old_draft),
        "source": {"category_id": "CBT123", "title": "Original Mercado"},
        "description": {"plain_text": "Mercado description"},
        "original_1688": {"source_1688_item_id": "123456789", "title": "桌面收纳盒",
                          "properties": [{"name": "材质", "value": "塑料"}],
                          "main_image_url": "https://cbu01.alicdn.com/source.jpg"},
        "marketplace_listings": {"yandex": {"title": "Сохранить"}},
    }
    row = {"source_snapshot_json": snapshot, "title": "Original Mercado"}
    image_calls = []
    monkeypatch.setattr(products, "_download_source_image", lambda *a, **kw: b"image")
    def image(source, item_id, **kwargs):
        image_calls.append((item_id, kwargs))
        return None, "/generated/amazon.jpg"
    monkeypatch.setattr(products, "create_white_background_image", image)
    monkeypatch.setattr(products, "complete_marketplace_category", lambda *a, **kw: pytest.fail("must not use Mercado category"))
    prepared = products.prepare_ai_original_product(
        row, target_platform="amazon", template_id="features", category_client=object(),
        chat=lambda *a, **kw: json.dumps({**draft(), "attributes": [
            {"source_index": 0, "name": "Material", "value_name": "Plastic"}]}),
    )
    result = json.loads(prepared["source_snapshot_json"])
    assert result["ai_original"] == old_draft
    assert result["source"] == snapshot["source"]
    assert result["description"] == snapshot["description"]
    assert result["marketplace_listings"]["yandex"] == snapshot["marketplace_listings"]["yandex"]
    assert prepared["ai_original"] == result["marketplace_listings"]["amazon"]
    assert prepared["ai_original"]["template_id"] == "features"
    assert prepared["ai_original"]["status"] == "completed"
    assert prepared["ai_original"]["validation"]["warnings"]
    assert {a["name"] for a in prepared["ai_original"]["attributes"]} == {"Material"}
    assert "amazon" in image_calls[0][0]
    assert image_calls[0][1]["target_size"] == 1600
    assert snapshot["ai_original"] == old_draft  # no nested input mutation


def test_mercado_still_uses_original_path_and_adds_profile_snapshot(monkeypatch):
    monkeypatch.setattr(products, "_download_source_image", lambda *a, **kw: b"image")
    monkeypatch.setattr(products, "create_white_background_image", lambda *a, **kw: (None, "/ml.jpg"))
    monkeypatch.setattr(products, "generate_marketplace_copy", lambda *a, **kw: {
        "title_es": "Caja", "title_pt": "Caixa", "description_es": "Una caja.",
        "description_pt": "Uma caixa.", "attributes": [],
    })
    row = {"source_snapshot_json": {"original_1688": {"source_1688_item_id": "123456789", "main_image_url": "https://cbu01.alicdn.com/source.jpg"},
                                    "marketplace_listings": {"amazon": draft()}}}
    prepared = products.prepare_ai_original_product(row)
    snapshot = json.loads(prepared["source_snapshot_json"])
    assert snapshot["marketplace_listings"]["mercadolibre"] == snapshot["ai_original"]
    assert snapshot["marketplace_listings"]["amazon"] == draft()
    assert prepared["title"] == "Caja"
    assert snapshot["source"]["site_id"] == "CBT"


def test_export_labels_unverified_schema_and_revalidates_manual_edits():
    row = {"id": 4, "source_snapshot_json": {"marketplace_listings": {"amazon": draft(title="x" * 76), "yandex": draft("yandex")}}}
    exported = build_listing_export(row, "amazon")
    assert exported["draft_only"] is True
    assert exported["ready_for_submission"] is False
    assert exported["validation"]["errors"]
    assert any("GTIN" in text for text in exported["missing_information"])
    assert any("包装" in text for text in build_listing_export(row, "yandex")["missing_information"])
    with pytest.raises(ValueError):
        build_listing_export(row, "mercadolibre")


def test_mercado_export_and_validation_use_current_localized_fields():
    listing = {"title": "stale", "description": "stale", "title_es": "Caja nueva",
               "description_es": "Descripción nueva.", "main_image_url": "/image.jpg"}
    row = {"source_snapshot_json": {"ai_original": listing}}
    exported = build_listing_export(row)
    assert exported["listing"]["title"] == "Caja nueva"
    assert exported["listing"]["description"] == "Descripción nueva."
    assert not exported["validation"]["errors"]
    listing["title_es"] = ""
    assert "标题不能为空" in " ".join(validate_platform_listing(listing)["errors"])


def test_platform_image_size_and_filename_isolation(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    source = Image.new("RGB", (600, 600), "blue")
    buffer = BytesIO()
    source.save(buffer, "PNG")
    for platform in ("amazon", "yandex"):
        path, _ = products.create_white_background_image(
            "https://cbu01.alicdn.com/source.png", "123456789-" + platform,
            source_bytes=buffer.getvalue(), image_dir=tmp_path, target_size=1600,
            background_remove=lambda image: image.convert("RGBA"),
        )
        assert platform in path.name
        with Image.open(path) as image:
            assert image.size == (1600, 1600)
            assert image.getpixel((100, 800))[2] > 200
            assert image.getpixel((100, 800))[0] < 50
    assert len(list(tmp_path.iterdir())) == 2


def test_small_alpha_subject_is_cropped_and_fills_platform_canvas(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    ImageChops = pytest.importorskip("PIL.ImageChops")
    source = Image.new("RGB", (100, 100), "white")
    buffer = BytesIO()
    source.save(buffer, "PNG")
    cutout = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    cutout.paste((0, 0, 255, 255), (30, 30, 70, 70))
    path, _ = products.create_white_background_image(
        "https://cbu01.alicdn.com/source.png", "123456789-amazon",
        source_bytes=buffer.getvalue(), image_dir=tmp_path, target_size=1600,
        background_remove=lambda _: cutout,
    )
    with Image.open(path) as output:
        bbox = ImageChops.difference(output, Image.new("RGB", output.size, "white")).getbbox()
        assert bbox[2] - bbox[0] >= 1440
        assert bbox[3] - bbox[1] >= 1440
