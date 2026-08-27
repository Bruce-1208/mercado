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
        if path == "/categories/CBT301":
            return {"id": "CBT301"}
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
        if path == "/sites/CBT/domain_discovery/search":
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


def test_follow_sell_translates_mexico_listing_for_brazil_destination():
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
            translator=lambda texts, source_language, target_language: [
                "Produto de teste",
                "Descrição",
            ],
            source_from_database=True,
            publish=False,
            net_proceeds=20,
        )

    assert result["destination_site_id"] == "MLB"
    assert result["payload"]["sites_to_sell"][0]["site_id"] == "MLB"
    assert result["payload"]["family_name"] == "Produto de teste"
    assert result["payload"]["description"]["plain_text"] == "Descrição"
    assert result["translation"]["translated"] is True
    assert client.discovery_query == "Producto de prueba"
