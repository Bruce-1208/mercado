import json
import io
from unittest.mock import patch

import pytest

from erp import mercadolibre_follow_sell as follow_sell_module
from erp.mercadolibre_follow_sell import (
    MercadoLibreClient,
    MercadoLibreError,
    _converted_usd_amount,
    _picture_sources,
    build_global_payload,
    build_user_product_payload,
    exchange_authorization_code,
    extract_authorization_code,
    extract_item_id,
    follow_sell,
    infer_cbt_category,
)


def test_publish_price_conversion_uses_daily_database_rate(monkeypatch):
    class Cache:
        def get_exchange_rate(self, source, target):
            assert (source, target) == ("MXN", "USD")
            return {"rate": 0.05}

    class Client:
        def request(self, *args, **kwargs):
            raise AssertionError("fresh conversion API must not be called")

    monkeypatch.setattr(
        "erp.mercadolibre_profitability_cache.DatabaseProfitabilityCache",
        lambda: Cache(),
    )

    assert _converted_usd_amount(Client(), 400, "MXN") == 20


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.ok = 200 <= status_code < 300
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class CategoryClient:
    def request(self, method, path, **kwargs):
        assert method == "GET"
        if path == "/marketplace/domain_discovery/search":
            return [{"category_id": "CBT301", "attributes": []}]
        if path == "/categories/CBT301":
            return {"id": "CBT301"}
        if path == "/categories/MLM301/attributes":
            return [
                {"id": "BRAND", "name": "Marca"},
                {"id": "GENDER", "name": "Género"},
                {"id": "COLOR", "name": "Color"},
                {"id": "ITEM_CONDITION", "name": "Condición del ítem"},
                {"id": "EMPTY_GTIN_REASON", "name": "Motivo de GTIN vacío"},
                {"id": "SELLER_SKU", "name": "SKU"},
            ]
        if path == "/categories/CBT301/attributes":
            return [
                {"id": attribute_id}
                for attribute_id in (
                    "BRAND",
                    "GENDER",
                    "ITEM_CONDITION",
                    "EMPTY_GTIN_REASON",
                    "SELLER_SKU",
                )
            ]
        raise AssertionError(path)


class DiscoveryClient(CategoryClient):
    def __init__(self):
        self.discovery_query = ""

    def request(self, method, path, **kwargs):
        if path == "/marketplace/domain_discovery/search":
            self.discovery_query = kwargs["params"]["q"]
            return [{"category_id": "CBT301"}]
        return super().request(method, path, **kwargs)


class RequiredModelCategoryClient(CategoryClient):
    def request(self, method, path, **kwargs):
        if path == "/categories/CBT301/attributes":
            return [
                {"id": "BRAND", "tags": {"required": True}},
                {"id": "MODEL", "tags": {"required": True}},
            ]
        return super().request(method, path, **kwargs)


class RequiredUnknownCategoryClient(CategoryClient):
    def request(self, method, path, **kwargs):
        if path == "/categories/CBT301/attributes":
            return [
                {"id": "BRAND", "tags": {"required": True}},
                {"id": "COLLECTION", "tags": {"required": True}},
            ]
        return super().request(method, path, **kwargs)


class RequiredGtinCategoryClient(CategoryClient):
    def request(self, method, path, **kwargs):
        if path == "/categories/CBT301/attributes":
            return [
                {"id": "BRAND"},
                {"id": "GTIN", "tags": {"required": True}},
                {"id": "EMPTY_GTIN_REASON"},
            ]
        return super().request(method, path, **kwargs)


class RequiredFactoryKitCategoryClient(CategoryClient):
    def request(self, method, path, **kwargs):
        if path == "/categories/CBT301/attributes":
            return [
                {"id": "BRAND"},
                {
                    "id": "IS_FACTORY_KIT",
                    "tags": {"required": True},
                    "values": [
                        {"id": "242085", "name": "Yes"},
                        {"id": "242084", "name": "No"},
                    ],
                },
            ]
        return super().request(method, path, **kwargs)


class LocalizedRequiredCategoryClient(CategoryClient):
    def request(self, method, path, **kwargs):
        if path == "/categories/CBT301/attributes":
            return [
                {"id": attribute_id, "tags": {"required": True}}
                for attribute_id in (
                    "BOARD_GAME_NAME",
                    "PRODUCT_TYPE",
                    "PLAYING_CARDS_TYPE",
                    "IS_SET",
                    "SURVEILLANCE_CAMERA_TYPE",
                    "CAMERA_LOCATIONS",
                    "IS_WIRELESS",
                )
            ]
        return super().request(method, path, **kwargs)


class GenderCategoryClient(CategoryClient):
    def request(self, method, path, **kwargs):
        if path == "/categories/CBT301/attributes":
            return [
                {"id": "BRAND"},
                {
                    "id": "GENDER",
                    "tags": {"required": True},
                    "values": [
                        {"id": "339665", "name": "Woman"},
                        {"id": "339666", "name": "Man"},
                        {"id": "339668", "name": "Girls"},
                        {"id": "371795", "name": "Babies"},
                        {"id": "110461", "name": "Gender neutral"},
                        {"id": "339667", "name": "Boys"},
                    ],
                },
                {"id": "ITEM_CONDITION"},
                {"id": "EMPTY_GTIN_REASON"},
                {"id": "SELLER_SKU"},
            ]
        return super().request(method, path, **kwargs)


class RequiredColorCategoryClient(CategoryClient):
    def request(self, method, path, **kwargs):
        if path == "/categories/CBT301/attributes":
            return [
                {"id": "BRAND"},
                {
                    "id": "COLOR",
                    "value_type": "list",
                    "tags": {"required": True},
                    "values": [
                        {"id": "black-id", "name": "Black"},
                        {"id": "white-id", "name": "White"},
                    ],
                },
            ]
        return super().request(method, path, **kwargs)


class SourceSchemaValueIdClient(CategoryClient):
    def request(self, method, path, **kwargs):
        if path == "/categories/MLM301/attributes":
            return [{
                "id": "IS_FACTORY_KIT",
                "name": "Champ localisé inconnu",
                "value_type": "boolean",
                "values": [
                    {"id": "242085", "name": "Valeur positive inconnue"},
                    {"id": "242084", "name": "Valeur négative inconnue"},
                ],
            }]
        if path == "/categories/CBT301/attributes":
            return [{
                "id": "IS_FACTORY_KIT",
                "name": "Factory kit",
                "value_type": "boolean",
                "tags": {"required": True},
                "values": [
                    {"id": "242085", "name": "Yes"},
                    {"id": "242084", "name": "No"},
                ],
            }]
        return super().request(method, path, **kwargs)


class VariationSchemaClient(CategoryClient):
    def request(self, method, path, **kwargs):
        if path == "/categories/MLM301/attributes":
            return [{
                "id": "SIZE",
                "name": "Talla",
                "value_type": "list",
                "values": [{"id": "large-id", "name": "Grande"}],
            }]
        if path == "/categories/CBT301/attributes":
            return [{
                "id": "SIZE",
                "name": "Size",
                "value_type": "list",
                "tags": {"required": True, "allow_variations": True},
                "values": [{"id": "large-id", "name": "Large"}],
            }]
        return super().request(method, path, **kwargs)


class MissingSchemaClient(CategoryClient):
    def request(self, method, path, **kwargs):
        if path == "/categories/CBT301/attributes":
            return None
        return super().request(method, path, **kwargs)


class PowerSupplyCategoryClient(CategoryClient):
    def request(self, method, path, **kwargs):
        if path == "/categories/MLM301/attributes":
            return [
                {
                    "id": "CON_USB",
                    "name": "Con USB",
                    "value_type": "boolean",
                    "values": [
                        {"id": "242085", "name": "Sí"},
                        {"id": "242084", "name": "No"},
                    ],
                },
                {"id": "VOLTAJE_DE_LA_BATERIA", "name": "Voltaje de la batería"},
            ]
        if path == "/categories/CBT301/attributes":
            return [
                {
                    "id": "WITH_USB",
                    "name": "With USB",
                    "value_type": "boolean",
                    "tags": {"required": True},
                    "values": [
                        {"id": "242085", "name": "Yes"},
                        {"id": "242084", "name": "No"},
                    ],
                },
                {
                    "id": "POWER_SUPPLY_TYPE",
                    "name": "Power supply type",
                    "tags": {"required": True},
                    "values": [
                        {"id": "domestic", "name": "Domestic current"},
                        {"id": "hybrid", "name": "Battery/Domestic current"},
                    ],
                },
            ]
        return super().request(method, path, **kwargs)


class PredictorAttributeClient(CategoryClient):
    def __init__(self):
        self.queries = []

    def request(self, method, path, **kwargs):
        if path == "/marketplace/domain_discovery/search":
            query = kwargs["params"]["q"]
            self.queries.append(query)
            return [{
                "category_id": "CBT4559",
                "attributes": [{
                    "id": "ACCESSORY_TYPE",
                    "value_id": "19545565",
                    "value_name": "Bracelet",
                }],
            }]
        if path == "/categories/CBT4559/attributes":
            return [
                {"id": "BRAND", "tags": {"required": True}},
                {
                    "id": "ACCESSORY_TYPE",
                    "value_type": "list",
                    "tags": {"catalog_required": True},
                    "values": [{"id": "19545565", "name": "Bracelet"}],
                },
            ]
        return super().request(method, path, **kwargs)


def sample_source():
    return {
        "id": "MLM3016972321",
        "site_id": "MLM",
        "title": "Producto de prueba",
        "category_id": "MLM301",
        "price": 340,
        "currency_id": "MXN",
        "condition": "new",
        "attributes": [
            {"id": "BRAND", "name": "Marca", "value_name": "Genérica"},
            {"id": "SELLER_SKU", "value_name": "COMPETITOR-SKU"},
        ],
        "pictures": [
            {
                "id": "123-MLM",
                "secure_url": "https://http2.mlstatic.com/D_123.jpg",
            }
        ],
    }


def test_extract_ids_from_urls():
    assert extract_item_id("https://articulo.mercadolibre.com.mx/MLM-3016972321") == "MLM3016972321"
    assert extract_authorization_code("https://example.test/cb?code=TG-abc-123") == "TG-abc-123"


def test_exchange_code_uses_form_body_and_saves_tokens(tmp_path):
    token_file = tmp_path / "tokens.json"
    session = FakeSession(
        FakeResponse(
            200,
            {
                "access_token": "secret-access",
                "refresh_token": "secret-refresh",
                "user_id": 123,
            },
        )
    )

    result = exchange_authorization_code(
        "TG-fresh-123",
        client_id="app-id",
        client_secret="app-secret",
        token_file=token_file,
        session=session,
    )

    assert result["user_id"] == 123
    assert json.loads(token_file.read_text(encoding="utf-8"))["refresh_token"] == "secret-refresh"
    _, request = session.calls[0]
    assert request["data"]["code"] == "TG-fresh-123"
    assert "params" not in request


def test_global_payload_uses_per_site_pictures_and_replaces_seller_sku():
    payload = build_global_payload(
        CategoryClient(),
        sample_source(),
        {"plain_text": "Descripción"},
        site_id="MLB",
        quantity=1,
        net_proceeds=20,
    )

    assert payload["category_id"] == "CBT301"
    assert payload["sites_to_sell"][0]["site_id"] == "MLB"
    assert "pictures" not in payload
    assert payload["sites_to_sell"][0]["pictures"] == [
        {"source": "https://http2.mlstatic.com/D_123.jpg"}
    ]
    assert payload["sites_to_sell"][0]["net_proceeds"] == 20
    skus = [a for a in payload["attributes"] if a["id"] == "SELLER_SKU"]
    assert skus == [{"id": "SELLER_SKU", "value_name": "FOLLOW-MLM3016972321"}]
    assert any(a["id"] == "ITEM_CONDITION" for a in payload["attributes"])


def test_browser_picture_sources_are_accepted_and_non_product_logos_are_filtered():
    pictures, _ = _picture_sources(
        {
            "pictures": [
                {"source": "https://http2.mlstatic.com/D_Q_NP_123-CBT456-R-product.webp"},
                {"source": "https://http2.mlstatic.com/D_NQ_NP_2X_123-CBT456-F-product.webp"},
                {"source": "https://http2.mlstatic.com/storage/logos-api-admin/card.svg"},
            ]
        }
    )

    assert pictures == [
        {"source": "https://http2.mlstatic.com/D_NQ_NP_2X_123-CBT456-F-product.webp"}
    ]


def test_payload_defaults_brand_and_canonicalizes_spanish_attribute_ids():
    source = sample_source()
    source["attributes"] = [
        {"id": "MARCA", "name": "Marca", "value_name": ""},
        {"id": "G_NERO", "name": "Género", "value_name": "Sin género"},
        {"id": "CAMPO_INVENTADO", "name": "Campo inventado", "value_name": "x"},
    ]

    payload = build_global_payload(
        CategoryClient(), source, {}, quantity=1, net_proceeds=20
    )
    by_id = {attribute["id"]: attribute for attribute in payload["attributes"]}

    assert by_id["BRAND"]["value_name"] == "Generic"
    assert by_id["GENDER"]["value_name"] == "Sin género"
    assert "MARCA" not in by_id
    assert "G_NERO" not in by_id
    assert "CAMPO_INVENTADO" not in by_id


@pytest.mark.parametrize(
    ("source_value", "expected_id", "expected_name"),
    [
        ("Sin género", "110461", "Gender neutral"),
        ("Mujer", "339665", "Woman"),
        ("Niñas", "339668", "Girls"),
        ("Niños", "339667", "Boys"),
        ("Hombre", "339666", "Man"),
        ("Sem gênero", "110461", "Gender neutral"),
        ("Feminino", "339665", "Woman"),
        ("Meninos", "339667", "Boys"),
    ],
)
def test_payload_maps_localized_gender_to_target_category_value(
    source_value, expected_id, expected_name
):
    source = sample_source()
    source["attributes"].append(
        {"id": "G_NERO", "name": "Género", "value_name": source_value}
    )

    payload = build_user_product_payload(
        GenderCategoryClient(), source, {}, quantity=1, net_proceeds=20
    )
    gender = next(
        attribute for attribute in payload["attributes"] if attribute["id"] == "GENDER"
    )

    assert gender["value_id"] == expected_id
    assert gender["value_name"] == expected_name


def test_payload_maps_portuguese_color_to_target_enum_without_translation():
    source = sample_source()
    source["attributes"].append({"name": "Cor", "value_name": "Preto"})

    payload = build_user_product_payload(
        RequiredColorCategoryClient(), source, {}, quantity=1, net_proceeds=20
    )

    color = next(attribute for attribute in payload["attributes"] if attribute["id"] == "COLOR")
    assert color["value_id"] == "black-id"
    assert color["value_name"] == "Black"


def test_unknown_required_closed_enum_reports_actionable_mapping_error():
    source = sample_source()
    source["attributes"].append({"name": "Cor", "value_name": "Azul petróleo"})

    with pytest.raises(
        MercadoLibreError,
        match=r"必填属性 COLOR.*Azul petróleo.*Black, White",
    ):
        build_user_product_payload(
            RequiredColorCategoryClient(), source, {}, quantity=1, net_proceeds=20
        )


def test_payload_overrides_source_brand_with_generic_without_adding_a_duplicate():
    payload = build_global_payload(
        CategoryClient(), sample_source(), {}, quantity=1, net_proceeds=20
    )

    brands = [attribute for attribute in payload["attributes"] if attribute["id"] == "BRAND"]
    assert brands == [{"id": "BRAND", "name": "Marca", "value_name": "Generic"}]


def test_payload_fills_known_required_category_attribute_defaults():
    source = sample_source()
    source["attributes"] = []

    payload = build_user_product_payload(
        RequiredModelCategoryClient(),
        source,
        {},
        quantity=1,
        net_proceeds=20,
    )

    by_id = {attribute["id"]: attribute for attribute in payload["attributes"]}
    assert by_id["BRAND"]["value_name"] == "Generic"
    assert by_id["MODEL"]["value_name"] == "Generic"


def test_payload_still_reports_unknown_required_category_attributes():
    source = sample_source()
    source["attributes"] = []

    with pytest.raises(MercadoLibreError, match="COLLECTION"):
        build_user_product_payload(
            RequiredUnknownCategoryClient(),
            source,
            {},
            quantity=1,
            net_proceeds=20,
        )


@pytest.mark.parametrize(
    ("source_name", "source_value", "expected_id", "expected_name"),
    [
        ("Es un kit de fábrica", "No", "242084", "No"),
        ("Es un kit de fábrica", "Sí", "242085", "Yes"),
        ("É um kit de fábrica", "Sim", "242085", "Yes"),
    ],
)
def test_payload_maps_factory_kit_name_and_boolean_value(
    source_name, source_value, expected_id, expected_name
):
    source = sample_source()
    source["attributes"] = [
        {"name": source_name, "value_name": source_value},
    ]

    payload = build_user_product_payload(
        RequiredFactoryKitCategoryClient(),
        source,
        {},
        quantity=1,
        net_proceeds=20,
    )

    factory_kit = next(
        attribute
        for attribute in payload["attributes"]
        if attribute["id"] == "IS_FACTORY_KIT"
    )
    assert factory_kit["value_id"] == expected_id
    assert factory_kit["value_name"] == expected_name


def test_source_category_api_recovers_ids_without_a_language_alias():
    source = sample_source()
    source["attributes"] = [{
        "name": "Champ localisé inconnu",
        "value_name": "Valeur négative inconnue",
    }]

    payload = build_user_product_payload(
        SourceSchemaValueIdClient(), source, {}, quantity=1, net_proceeds=20
    )

    mapped = next(
        row for row in payload["attributes"] if row["id"] == "IS_FACTORY_KIT"
    )
    assert mapped == {
        "name": "Champ localisé inconnu",
        "value_name": "No",
        "id": "IS_FACTORY_KIT",
        "value_id": "242084",
    }


def test_required_variation_attribute_is_mapped_and_checked_in_every_variation():
    source = sample_source()
    source["attributes"] = []
    source["variations"] = [{
        "attribute_combinations": [{"name": "Talla", "value_name": "Grande"}],
        "attributes": [],
        "picture_ids": ["123-MLM"],
    }]

    payload = build_global_payload(
        VariationSchemaClient(), source, {}, quantity=2, net_proceeds=20
    )

    assert payload["sites_to_sell"][0]["variations"][0]["attribute_combinations"] == [{
        "name": "Talla",
        "value_name": "Large",
        "id": "SIZE",
        "value_id": "large-id",
    }]


def test_target_category_schema_failure_stops_publication_validation():
    with pytest.raises(
        MercadoLibreError,
        match=r"无法从 Mercado Libre API 获取类目 CBT301 的属性规则.*上架已停止",
    ):
        build_user_product_payload(
            MissingSchemaClient(), sample_source(), {}, quantity=1, net_proceeds=20
        )


def test_payload_maps_collected_spanish_attribute_ids_to_cbt_schema():
    source = sample_source()
    source["attributes"] = [
        {"id": "NOMBRE_DEL_JUEGO_DE_MESA", "value_name": "The Mind"},
        {"id": "TIPO_DE_PRODUCTO", "value_name": "Intercomunicador"},
        {"id": "TIPO_DE_CARTAS", "value_name": "Coleccionables"},
        {"id": "ES_SET", "value_name": "Sí"},
        {"id": "TIPO_DE_CAMARA_DE_VIGILANCIA", "value_name": "IP"},
        {"id": "LOCACIONES_DE_LA_CAMARA", "value_name": "Exterior"},
        {"id": "ES_INALAMBRICO", "value_name": "Sí"},
    ]

    payload = build_user_product_payload(
        LocalizedRequiredCategoryClient(),
        source,
        {},
        quantity=1,
        net_proceeds=20,
    )

    by_id = {attribute["id"]: attribute for attribute in payload["attributes"]}
    assert by_id["BOARD_GAME_NAME"]["value_name"] == "The Mind"
    assert by_id["PRODUCT_TYPE"]["value_name"] == "Intercomunicador"
    assert by_id["PLAYING_CARDS_TYPE"]["value_name"] == "Coleccionables"
    assert by_id["IS_SET"]["value_name"] == "Sí"
    assert by_id["SURVEILLANCE_CAMERA_TYPE"]["value_name"] == "IP"
    assert by_id["CAMERA_LOCATIONS"]["value_name"] == "Exterior"
    assert by_id["IS_WIRELESS"]["value_name"] == "Sí"


def test_payload_maps_spanish_usb_and_infers_required_power_supply_from_evidence():
    source = sample_source()
    source["title"] = "Proyector USB con batería"
    source["attributes"] = [
        {"id": "CON_USB", "name": "Con USB", "value_name": "Sí"},
        {
            "id": "VOLTAJE_DE_LA_BATERIA",
            "name": "Voltaje de la batería",
            "value_name": "5 V",
        },
    ]

    payload = build_user_product_payload(
        PowerSupplyCategoryClient(), source, {}, quantity=1, net_proceeds=20
    )

    by_id = {attribute["id"]: attribute for attribute in payload["attributes"]}
    assert by_id["WITH_USB"]["value_id"] == "242085"
    assert by_id["WITH_USB"]["value_name"] == "Yes"
    assert by_id["POWER_SUPPLY_TYPE"]["value_id"] == "hybrid"
    assert by_id["POWER_SUPPLY_TYPE"]["value_name"] == "Battery/Domestic current"


def test_category_discovery_retries_with_deterministic_english_keywords():
    class Client:
        def __init__(self):
            self.queries = []

        def request(self, method, path, **kwargs):
            assert method == "GET"
            if path == "/categories/CBT11889/attributes":
                return [{"id": "BRAND", "tags": {"required": True}}]
            if path == "/marketplace/domain_discovery/search":
                query = kwargs["params"]["q"]
                self.queries.append(query)
                if query == "Lámpara proyector de onda de agua efecto aurora boreal":
                    return []
                assert "lamp" in query
                assert "projector" in query
                assert "water ripple" in query
                assert "northern lights" in query
                return [{"category_id": "CBT11889"}]
            raise AssertionError(path)

    client = Client()
    category_id = infer_cbt_category(client, {
        "category_id": "MLM18022",
        "title": "Lámpara proyector de onda de agua efecto aurora boreal",
    })

    assert category_id == "CBT11889"
    assert len(client.queries) == 2


def test_category_name_is_safer_fallback_than_a_non_english_title():
    class Client:
        def __init__(self):
            self.queries = []

        def request(self, method, path, **kwargs):
            if path == "/categories/CBT4559/attributes":
                return [{"id": "BRAND", "tags": {"required": True}}]
            assert path == "/marketplace/domain_discovery/search"
            query = kwargs["params"]["q"]
            self.queries.append(query)
            if query == "Pulseras":
                return [{"category_id": "CBT4559", "attributes": []}]
            return [{"category_id": "CBT373472", "attributes": []}]

    client = Client()
    category_id = infer_cbt_category(client, {
        "id": "MLM123",
        "category_id": "MLM1434",
        "category_name": "Pulseras",
        "title": "Pulseras Obsidian Pixiu En Oro Natural Y Obsidiana",
    })

    assert category_id == "CBT4559"
    assert client.queries == ["Pulseras"]


def test_category_fallback_does_not_escape_to_an_unrelated_title_prediction():
    class Client:
        def __init__(self):
            self.queries = []

        def request(self, method, path, **kwargs):
            if path == "/categories/CBT7093/attributes":
                return [{"id": "TEAM", "tags": {"required": True}}]
            if path == "/categories/CBT457501/attributes":
                return [{"id": "BRAND", "tags": {"required": True}}]
            query = kwargs["params"]["q"]
            self.queries.append(query)
            if query == "Chamarras":
                return [{"category_id": "CBT7093", "attributes": []}]
            return [{"category_id": "CBT457501", "attributes": []}]

    client = Client()
    category_id = infer_cbt_category(client, {
        "id": "MLM123",
        "category_id": "MLM1234",
        "category_name": "Chamarras",
        "title": "Gabardina Spider-man Noir Cosplay",
    })

    assert category_id == "CBT7093"
    assert client.queries == ["Chamarras"]


def test_english_translation_is_sent_to_official_category_predictor_first():
    class Client:
        def __init__(self):
            self.queries = []

        def request(self, method, path, **kwargs):
            if path == "/categories/CBT4559/attributes":
                return [{"id": "BRAND", "tags": {"required": True}}]
            query = kwargs["params"]["q"]
            self.queries.append(query)
            return [{"category_id": "CBT4559", "attributes": []}]

    client = Client()
    category_id = infer_cbt_category(
        client,
        {
            "id": "MLM123",
            "category_id": "MLM1434",
            "category_name": "Pulseras",
            "title": "Pulsera de cuarzo natural",
        },
        translator=lambda texts, source, target: [
            "Natural quartz bracelet",
            "Bracelets",
        ],
    )

    assert category_id == "CBT4559"
    assert client.queries == ["Natural quartz bracelet"]


def test_predictor_attributes_fill_target_category_requirements():
    source = sample_source()
    source["category_name"] = "Pulseras"
    source["title"] = "Pulsera de cuarzo natural"
    client = PredictorAttributeClient()

    payload = build_user_product_payload(
        client, source, {}, quantity=1, net_proceeds=20
    )

    assert payload["category_id"] == "CBT4559"
    accessory_type = next(
        attribute
        for attribute in payload["attributes"]
        if attribute["id"] == "ACCESSORY_TYPE"
    )
    assert accessory_type["value_id"] == "19545565"
    assert accessory_type["value_name"] == "Bracelet"


def test_user_product_payload_uses_uploaded_picture_ids():
    payload = build_user_product_payload(
        CategoryClient(),
        sample_source(),
        {"plain_text": "Descripción"},
        site_id="MCO",
        quantity=2,
        net_proceeds=22,
        picture_ids=["uploaded-CBT-picture"],
    )

    assert "title" not in payload
    assert payload["family_name"] == "Producto de prueba"
    assert payload["sites_to_sell"][0]["site_id"] == "MCO"
    assert payload["global_net_proceeds"] == 22
    assert payload["pictures"] == [{"id": "uploaded-CBT-picture"}]
    no_gtin = [a for a in payload["attributes"] if a["id"] == "EMPTY_GTIN_REASON"]
    assert no_gtin == [
        {
            "id": "EMPTY_GTIN_REASON",
            "value_id": "17055160",
            "value_name": "The product does not have registered code",
        }
    ]


def test_required_gtin_accepts_documented_empty_reason():
    source = sample_source()
    source["attributes"] = []

    payload = build_user_product_payload(
        RequiredGtinCategoryClient(),
        source,
        {},
        quantity=1,
        net_proceeds=22,
        picture_ids=["uploaded-CBT-picture"],
    )

    assert any(
        attribute["id"] == "EMPTY_GTIN_REASON"
        for attribute in payload["attributes"]
    )


def test_empty_gtin_placeholder_is_replaced_by_documented_reason():
    source = sample_source()
    source["attributes"] = [{"id": "GTIN"}]

    payload = build_user_product_payload(
        RequiredGtinCategoryClient(),
        source,
        {},
        quantity=1,
        net_proceeds=22,
        picture_ids=["uploaded-CBT-picture"],
    )

    by_id = {attribute["id"]: attribute for attribute in payload["attributes"]}
    assert "GTIN" not in by_id
    assert by_id["EMPTY_GTIN_REASON"]["value_id"] == "17055160"


def test_user_product_payload_limits_family_name_to_platform_maximum():
    source = sample_source()
    source["title"] = "A" * 80

    payload = build_user_product_payload(
        CategoryClient(),
        source,
        {},
        quantity=1,
        net_proceeds=22,
        picture_ids=["uploaded-CBT-picture"],
    )

    assert payload["family_name"] == "A" * 60


def test_api_client_retries_explicit_rate_limit_for_publish(tmp_path):
    class Response:
        def __init__(self, status_code, payload, headers=None):
            self.status_code = status_code
            self.ok = 200 <= status_code < 300
            self._payload = payload
            self.content = json.dumps(payload).encode()
            self.headers = headers or {}

        def json(self):
            return self._payload

    class Session:
        def __init__(self):
            self.responses = [
                Response(429, {"message": "local_rate_limited"}, {"Retry-After": "0"}),
                Response(201, {"id": "CBT123"}),
            ]
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return self.responses.pop(0)

    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"access_token": "token"}), encoding="utf-8")
    session = Session()
    client = MercadoLibreClient(
        token_file,
        client_id="client",
        client_secret="secret",
        session=session,
    )

    with patch("erp.mercadolibre_follow_sell.time.sleep") as sleep:
        result = client.request("POST", "/global/items", json_body={"title": "test"})

    assert result == {"id": "CBT123"}
    assert len(session.calls) == 2
    sleep.assert_called_once()


def test_picture_upload_adds_margin_to_boundary_size_image(tmp_path):
    from PIL import Image

    source_image = io.BytesIO()
    Image.new("RGB", (500, 400), "white").save(source_image, format="JPEG")

    class Response:
        def __init__(self, *, content=b"", payload=None):
            self.status_code = 200
            self.ok = True
            self.content = content
            self.headers = {"Content-Type": "image/jpeg"}
            self._payload = payload or {}

        def json(self):
            return self._payload

    class Session:
        uploaded = b""

        def get(self, url, timeout):
            return Response(content=source_image.getvalue())

        def post(self, url, **kwargs):
            self.uploaded = kwargs["files"]["file"][1]
            return Response(payload={"id": "uploaded-picture"})

    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"access_token": "token"}), encoding="utf-8")
    session = Session()
    client = MercadoLibreClient(
        token_file,
        client_id="client",
        client_secret="secret",
        session=session,
    )

    assert client.upload_picture_from_url("https://example.test/image.jpg") == "uploaded-picture"
    with Image.open(io.BytesIO(session.uploaded)) as uploaded:
        assert max(uploaded.size) == 520


@pytest.mark.parametrize("source_url", [
    "http://127.0.0.1:5000/api/ai-original-products/images/1688-123456-ai-white.jpg",
    "/api/ai-original-products/images/1688-123456-ai-white.jpg",
])
def test_picture_upload_reads_ai_original_loopback_image_directly(
    tmp_path, monkeypatch, source_url,
):
    from PIL import Image

    image_path = tmp_path / "1688-123456-ai-white.jpg"
    Image.new("RGB", (800, 800), "white").save(image_path, format="JPEG")

    class Response:
        status_code = 200
        ok = True

        def json(self):
            return {"id": "uploaded-ai-original"}

    class Session:
        uploaded = b""

        def get(self, *_args, **_kwargs):
            raise AssertionError("loopback AI image must not be fetched over HTTP")

        def post(self, _url, **kwargs):
            self.uploaded = kwargs["files"]["file"][1]
            return Response()

    monkeypatch.setattr("erp.ai_original_products.IMAGE_DIR", tmp_path)
    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"access_token": "token"}), encoding="utf-8")
    session = Session()
    client = MercadoLibreClient(
        token_file,
        client_id="client",
        client_secret="secret",
        session=session,
    )

    picture_id = client.upload_picture_from_url(source_url)

    assert picture_id == "uploaded-ai-original"
    assert session.uploaded == image_path.read_bytes()


def test_user_product_payload_derives_seller_warranty_from_description():
    payload = build_user_product_payload(
        CategoryClient(),
        sample_source(),
        {"plain_text": "Garantía del vendedor: 6 meses"},
        quantity=1,
        net_proceeds=22,
        picture_ids=["uploaded-CBT-picture"],
    )

    assert payload["sale_terms"] == [
        {
            "id": "WARRANTY_TYPE",
            "value_id": "2230280",
            "value_name": "Seller warranty",
        },
        {"id": "WARRANTY_TIME", "value_name": "6 months"},
    ]


def test_user_product_payload_defaults_to_no_warranty_when_source_has_none():
    payload = build_user_product_payload(
        CategoryClient(),
        sample_source(),
        {},
        quantity=1,
        net_proceeds=22,
        picture_ids=["uploaded-CBT-picture"],
    )

    assert payload["sale_terms"] == [
        {
            "id": "WARRANTY_TYPE",
            "value_id": "6150835",
            "value_name": "No warranty",
        }
    ]


def test_user_product_payload_reads_portuguese_warranty_days():
    payload = build_user_product_payload(
        CategoryClient(),
        sample_source(),
        {"plain_text": "Garantia do vendedor: 90 dias"},
        quantity=1,
        net_proceeds=22,
        picture_ids=["uploaded-CBT-picture"],
    )

    assert payload["sale_terms"][1] == {
        "id": "WARRANTY_TIME",
        "value_name": "90 days",
    }


def test_follow_sell_skips_one_failed_picture_upload_and_publishes_remaining():
    class GlobalUserProductClient(CategoryClient):
        def __init__(self):
            self.posted_payload = None

        def request(self, method, path, **kwargs):
            if path == "/users/me":
                return {"id": 77, "site_id": "CBT", "tags": ["user_product_seller"]}
            if path == "/pictures/uploaded-good-picture":
                return {"id": "uploaded-good-picture", "max_size": "800x800"}
            if method == "POST" and path == "/global/user-products":
                self.posted_payload = kwargs["json_body"]
                return {"id": "CBT999"}
            return super().request(method, path, **kwargs)

        def upload_picture_from_url(self, source_url):
            if "111-CBT456" in source_url:
                raise MercadoLibreError("源图片尺寸不足 500px (70x70)")
            return "uploaded-good-picture"

    source = sample_source()
    source["pictures"] = [
        {"source": "https://http2.mlstatic.com/D_Q_NP_111-CBT456-R-small.webp"},
        {"source": "https://http2.mlstatic.com/D_NQ_NP_2X_222-CBT456-F-good.webp"},
    ]
    client = GlobalUserProductClient()
    with patch(
        "erp.mercadolibre_source_store.load_listing_for_publish",
        return_value=(source, {}),
    ), patch("erp.mercadolibre_source_store.record_publish_result"):
        result = follow_sell(
            client,
            "MLM3016972321",
            destination_site_id="MLM",
            source_from_database=True,
            publish=True,
            net_proceeds=20,
        )

    assert client.posted_payload["pictures"] == [{"id": "uploaded-good-picture"}]
    assert "70x70" in result["picture_upload_errors"][0]
    assert result["endpoint"] == "/global/user-products"
    assert result["timings"]["total"] >= 0


def test_follow_sell_skips_picture_that_shrinks_below_limit_after_upload():
    class GlobalUserProductClient(CategoryClient):
        def __init__(self):
            self.posted_payload = None

        def request(self, method, path, **kwargs):
            if path == "/users/me":
                return {"id": 77, "site_id": "CBT", "tags": ["user_product_seller"]}
            if path == "/pictures/uploaded-small-picture":
                return {"id": "uploaded-small-picture", "max_size": "358x495"}
            if path == "/pictures/uploaded-good-picture":
                return {"id": "uploaded-good-picture", "max_size": "480x854"}
            if method == "POST" and path == "/global/user-products":
                self.posted_payload = kwargs["json_body"]
                return {"id": "CBT999"}
            return super().request(method, path, **kwargs)

        def upload_picture_from_url(self, source_url):
            if "111-CBT456" in source_url:
                return "uploaded-small-picture"
            return "uploaded-good-picture"

    source = sample_source()
    source["pictures"] = [
        {"source": "https://http2.mlstatic.com/D_NQ_NP_111-CBT456-O-small.webp"},
        {"source": "https://http2.mlstatic.com/D_NQ_NP_2X_222-CBT456-F-good.webp"},
    ]
    client = GlobalUserProductClient()
    with patch(
        "erp.mercadolibre_source_store.load_listing_for_publish",
        return_value=(source, {}),
    ), patch("erp.mercadolibre_source_store.record_publish_result"):
        result = follow_sell(
            client,
            "MLM3016972321",
            destination_site_id="MLM",
            source_from_database=True,
            publish=True,
            net_proceeds=20,
        )

    assert client.posted_payload["pictures"] == [{"id": "uploaded-good-picture"}]
    assert "358x495" in result["picture_upload_errors"][0]


def test_validated_picture_upload_is_shared_across_workers_for_same_account():
    class Client:
        token_id = 99123

        def __init__(self):
            self.upload_calls = 0
            self._uploaded_picture_metadata = {
                "uploaded-shared": {"id": "uploaded-shared", "max_size": "800x800"}
            }

        def upload_picture_from_url(self, _source_url):
            self.upload_calls += 1
            return "uploaded-shared"

    client = Client()
    source_url = "https://http2.mlstatic.com/D_NQ_NP_123-CBT456-F.webp"
    with follow_sell_module._PICTURE_CACHE_LOCK:
        follow_sell_module._PICTURE_ID_CACHE.clear()
        follow_sell_module._PICTURE_KEY_LOCKS.clear()

    first = follow_sell_module._upload_validated_picture(client, source_url)
    second = follow_sell_module._upload_validated_picture(client, source_url)

    assert first == second == "uploaded-shared"
    assert client.upload_calls == 1


def test_database_client_user_profile_is_loaded_and_cached():
    class Client:
        token_id = 99124

        def __init__(self):
            self.request_calls = 0

        def request(self, method, path):
            assert (method, path) == ("GET", "/users/me")
            self.request_calls += 1
            return {"id": 77, "site_id": "CBT", "tags": []}

    client = Client()
    with follow_sell_module._USER_PROFILE_CACHE_LOCK:
        follow_sell_module._USER_PROFILE_CACHE.clear()

    first = follow_sell_module._cached_user_profile(client)
    second = follow_sell_module._cached_user_profile(client)

    assert first == second == {"id": 77, "site_id": "CBT", "tags": []}
    assert client.request_calls == 1


def test_user_products_endpoint_falls_back_only_on_explicit_not_found():
    class FallbackClient(CategoryClient):
        def __init__(self):
            self.paths = []

        def request(self, method, path, **kwargs):
            self.paths.append((method, path))
            if path == "/users/me":
                return {"id": 77, "site_id": "CBT", "tags": ["user_product_seller"]}
            if path == "/pictures/uploaded-picture":
                return {"id": "uploaded-picture", "max_size": "800x800"}
            if method == "POST" and path == "/global/user-products":
                raise MercadoLibreError("endpoint unavailable", status_code=404)
            if method == "POST" and path == "/global/items":
                return {"id": "CBT-fallback"}
            return super().request(method, path, **kwargs)

        def upload_picture_from_url(self, _source_url):
            return "uploaded-picture"

    client = FallbackClient()
    result = follow_sell(
        client,
        "MLM3016972321",
        destination_site_id="MLM",
        prepared_listing=(sample_source(), {}),
        publish=True,
        net_proceeds=20,
    )

    assert result["endpoint"] == "/global/items"
    assert result["result"]["id"] == "CBT-fallback"
    assert ("POST", "/global/user-products") in client.paths
    assert ("POST", "/global/items") in client.paths


def test_embedded_target_site_error_is_not_reported_as_success():
    class EmbeddedErrorClient(CategoryClient):
        def request(self, method, path, **kwargs):
            if path == "/users/me":
                return {"id": 77, "site_id": "CBT", "tags": []}
            if method == "POST" and path == "/global/items":
                return {
                    "site_id": "CBT",
                    "site_items": [{
                        "site_id": "MLM",
                        "error": {
                            "status": 403,
                            "error": "seller.unable_to_list",
                            "cause": ["restrictions_coliving"],
                        },
                    }],
                }
            return super().request(method, path, **kwargs)

    with pytest.raises(MercadoLibreError, match="restrictions_coliving") as caught:
        follow_sell(
            EmbeddedErrorClient(),
            "MLM3016972321",
            destination_site_id="MLM",
            prepared_listing=(sample_source(), {}),
            publish=True,
            net_proceeds=20,
        )
    assert caught.value.status_code == 403


def test_repeated_user_product_conflict_is_reconciled_with_existing_resource():
    class ConflictClient(CategoryClient):
        def request(self, method, path, **kwargs):
            if path == "/users/me":
                return {
                    "id": 77,
                    "site_id": "CBT",
                    "tags": ["user_product_seller"],
                }
            if method == "POST" and path == "/global/user-products":
                raise MercadoLibreError(
                    'Validation error; cause=[{"code":"user_product.repeated.conflict",'
                    '"message":"user product already exists. Conflict id: MLMU123"}]',
                    status_code=400,
                )
            if method == "GET" and path == "/marketplace/user-products/U123/mapping":
                return []
            if method == "POST" and path == "/global/user-products/U123":
                return {
                    "parent_user_product_id": "CBTU123",
                    "site_items": [{"site_id": "MLM", "item_id": "MLM123"}],
                }
            return super().request(method, path, **kwargs)

    with patch.object(
        follow_sell_module, "_upload_validated_picture", return_value="picture-1"
    ):
        result = follow_sell(
            ConflictClient(),
            "MLM3016972321",
            destination_site_id="MLM",
            prepared_listing=(sample_source(), {}),
            publish=True,
            net_proceeds=20,
        )

    assert result["publication_action"] == "add_marketplace"
    assert result["endpoint"] == "/global/user-products/U123"
    assert result["result"]["site_items"][0]["item_id"] == "MLM123"


def test_existing_user_product_adds_marketplace_without_recreating_or_uploading():
    class ReuseClient(CategoryClient):
        def __init__(self):
            self.paths = []
            self.add_payload = None

        def request(self, method, path, **kwargs):
            self.paths.append((method, path))
            if path == "/users/me":
                return {"id": 77, "site_id": "CBT", "tags": ["user_product_seller"]}
            if path == "/marketplace/user-products/U123/mapping":
                return [{"site_items": [{"site_id": "MLM", "item_id": "MLM1"}]}]
            if method == "POST" and path == "/global/user-products/U123":
                self.add_payload = kwargs["json_body"]
                return {
                    "parent_user_product_id": "CBTU123",
                    "site_items": [{"site_id": "MLB", "item_id": "MLB2"}],
                }
            return super().request(method, path, **kwargs)

        def upload_picture_from_url(self, _source_url):
            raise AssertionError("existing UP must reuse its pictures")

    client = ReuseClient()
    result = follow_sell(
        client,
        "MLM3016972321",
        destination_site_id="MLB",
        prepared_listing=(sample_source(), {}),
        existing_user_product_id="CBTU123",
        publish=True,
        net_proceeds=20,
    )

    assert result["publication_action"] == "add_marketplace"
    assert result["endpoint"] == "/global/user-products/U123"
    assert client.add_payload == {
        "sites_to_sell": [{
            "site_id": "MLB",
            "logistic_type": "remote",
            "net_proceeds": 20,
        }]
    }
    assert ("POST", "/global/user-products") not in client.paths


def test_existing_user_product_skips_add_when_marketplace_mapping_already_exists():
    class AlreadyMappedClient(CategoryClient):
        def request(self, method, path, **kwargs):
            if path == "/users/me":
                return {"id": 77, "site_id": "CBT", "tags": ["user_product_seller"]}
            if path == "/marketplace/user-products/U123/mapping":
                return [{"site_items": [{"site_id": "MLB", "item_id": "MLB2"}]}]
            if method == "POST":
                raise AssertionError("mapped site must not be created again")
            return super().request(method, path, **kwargs)

    result = follow_sell(
        AlreadyMappedClient(),
        "MLM3016972321",
        destination_site_id="MLB",
        prepared_listing=(sample_source(), {}),
        existing_user_product_id="CBTU123",
        publish=True,
        net_proceeds=20,
    )

    assert result["publication_action"] == "already_available"
    assert result["result"]["site_items"][0]["item_id"] == "MLB2"


def test_follow_sell_uses_rules_without_calling_translator_for_brazil_destination():
    class GlobalUserProductClient(DiscoveryClient):
        def request(self, method, path, **kwargs):
            if path == "/users/me":
                return {"id": 77, "site_id": "CBT", "tags": ["user_product_seller"]}
            return super().request(method, path, **kwargs)

    client = GlobalUserProductClient()
    source = sample_source()
    source["category_id"] = ""
    with patch(
        "erp.mercadolibre_source_store.load_listing_for_publish",
        return_value=(source, {"plain_text": "Descripción"}),
    ):
        result = follow_sell(
            client,
            "MLM3016972321",
            destination_site_id="MLB",
            translator=lambda *_args: pytest.fail("listing must not call a translator"),
            source_from_database=True,
            publish=False,
            net_proceeds=20,
        )

    assert result["destination_site_id"] == "MLB"
    assert result["payload"]["sites_to_sell"][0]["site_id"] == "MLB"
    assert result["payload"]["family_name"] == "Producto de prueba"
    assert result["payload"]["description"]["plain_text"] == "Descripción"
    assert result["translation"]["translated"] is False
    assert result["translation"]["strategy"] == "deterministic_attribute_rules"
    assert client.discovery_query == "Producto de prueba"
