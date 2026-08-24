from decimal import Decimal

from erp import mercadolibre_source_store as store


def test_normalize_snapshot_keeps_plugin_package_values():
    record = store.normalize_snapshot(
        {
            "item_id": "MLM-3016972321",
            "source_url": "https://articulo.mercadolibre.com.mx/MLM-3016972321",
            "source": {
                "title": "Lonchera",
                "price": 151.38,
                "currency_id": "MXN",
            },
            "weight_g": 333,
            "package_length_cm": 20,
            "package_width_cm": 20,
            "package_height_cm": 5,
            "plugin_snapshot": {"weight_basis": "calculated_volumetric"},
        }
    )

    assert record["item_id"] == "MLM3016972321"
    assert record["price"] == Decimal("151.38")
    assert record["weight_g"] == Decimal("333")
    assert record["package_height_cm"] == Decimal("5")
    assert record["plugin_snapshot"]["weight_basis"] == "calculated_volumetric"


def test_normalize_snapshot_uses_main_image_when_source_has_no_picture_array():
    record = store.normalize_snapshot(
        {
            "item_id": "MLM3016972321",
            "source_url": "https://example.test/MLM3016972321",
            "main_image_url": "https://example.test/product.webp",
            "source": {"title": "Lonchera"},
        }
    )

    assert record["pictures"] == [{"source": "https://example.test/product.webp"}]


def test_load_listing_for_publish_injects_package_attributes(monkeypatch):
    monkeypatch.setattr(
        store,
        "load_source_snapshot",
        lambda *args, **kwargs: {
            "item_id": "MLM3016972321",
            "site_id": "MLM",
            "title": "Lonchera",
            "category_id": "MLM412348",
            "price": Decimal("151.38"),
            "currency_id": "MXN",
            "condition_id": "new",
            "available_quantity": 1,
            "pictures_json": '[{"secure_url":"https://example.test/1.jpg"}]',
            "attributes_json": '[{"id":"BRAND","value_name":"Generic"}]',
            "variations_json": "[]",
            "sale_terms_json": "[]",
            "source_json": "{}",
            "description_json": '{"plain_text":"Descripción"}',
            "weight_g": Decimal("333"),
            "package_length_cm": Decimal("20"),
            "package_width_cm": Decimal("20"),
            "package_height_cm": Decimal("5"),
        },
    )

    source, description = store.load_listing_for_publish("MLM3016972321")

    by_id = {attribute["id"]: attribute for attribute in source["attributes"]}
    assert by_id["PACKAGE_WEIGHT"]["value_name"] == "333 g"
    assert by_id["PACKAGE_LENGTH"]["value_name"] == "20 cm"
    assert by_id["PACKAGE_WIDTH"]["value_name"] == "20 cm"
    assert by_id["PACKAGE_HEIGHT"]["value_name"] == "5 cm"
    assert description == {"plain_text": "Descripción"}
