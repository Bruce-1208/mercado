from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from yandex.app.central_authorization import migrate_legacy_authorizations
from yandex.app.config import _env_bool, _env_int, settings
from yandex.app.database import Database
from yandex.app.exchange_rate import parse_cbr_daily_xml
from yandex.app.product_media import normalize_product_pictures
from yandex.app.schemas import (
    OrderListRequest,
    ProductRecord,
    PublishRequest,
    SearchRequest,
    StoreCreateRequest,
    ZeshunStoreAuthorizeRequest,
    ZeshunStoreCreateRequest,
)
from yandex.app.secret_store import protect_secret, secret_fingerprint, unprotect_secret
from yandex.app.scraper import (
    _jsonld_product,
    category_id_from_breadcrumbs,
    category_id_from_html,
    market_sku_from_values,
    normalize_product_url,
)
from yandex.app.service import TaskService, calculate_listing_price
from yandex.app.yandex_api import StockTarget, YandexSellerClient


def sample_product() -> ProductRecord:
    return ProductRecord(
        run_id=1,
        source_url="https://market.yandex.ru/product--videoregistrator/123456789",
        market_sku=123456789,
        offer_id="YM-CB-123456789",
        name="行车记录仪 TestCam X1",
        description=(
            "这是一款面向日常驾驶记录的行车记录仪，支持夜视、循环录像、碰撞锁定和停车监控。"
            "画面细节清晰，安装方式简单，适用于城市通勤与长途出行；商品参数、使用场景和注意事项均以实物说明为准。"
        ) * 6,
        vendor="TestCam",
        vendor_code="X1",
        category_name="Видеорегистраторы",
        market_category_id=6269371,
        price=7990,
        currency="RUR",
        pictures=[
            f"https://avatars.mds.yandex.net/get-mpic/123456/abcdef-{index}/orig"
            for index in range(6)
        ],
        specifications={"Разрешение": "4K"},
        is_foreign=True,
        foreign_evidence="из-за рубежа",
        raw_data={"keyword": "行车记录仪"},
    )


class ScraperHelperTests(unittest.TestCase):
    def test_multiprocess_scraper_defaults_and_bounds(self) -> None:
        self.assertGreaterEqual(settings.scraper_processes, 1)
        self.assertLessEqual(settings.scraper_processes, 12)
        with patch.dict(os.environ, {"TEST_WORKERS": "99"}):
            self.assertEqual(_env_int("TEST_WORKERS", 6, minimum=1, maximum=12), 12)
        with patch.dict(os.environ, {"TEST_WORKERS": "invalid"}):
            self.assertEqual(_env_int("TEST_WORKERS", 6, minimum=1, maximum=12), 6)

    def test_headless_mode_can_be_switched_with_environment(self) -> None:
        with patch.dict(os.environ, {"TEST_HEADLESS": "true"}):
            self.assertTrue(_env_bool("TEST_HEADLESS", False))
        with patch.dict(os.environ, {"TEST_HEADLESS": "false"}):
            self.assertFalse(_env_bool("TEST_HEADLESS", True))

    def test_search_count_defaults_to_200(self) -> None:
        request = SearchRequest(keyword="  行车记录仪  ")
        self.assertEqual(request.keyword, "行车记录仪")
        self.assertEqual(request.count, 200)

    def test_normalizes_product_url_and_preserves_variant_sku(self) -> None:
        url = normalize_product_url(
            "/product--camera/123456789?sku=987654321&utm_source=test&do-waremd5=abc"
        )
        self.assertEqual(
            url,
            "https://market.yandex.ru/product--camera/123456789?sku=987654321",
        )

    def test_extracts_market_and_category_ids(self) -> None:
        self.assertEqual(
            market_sku_from_values("https://market.yandex.ru/product--camera/123456789"),
            123456789,
        )
        self.assertEqual(category_id_from_html('{"marketCategoryId":"6269371"}'), 6269371)
        self.assertEqual(
            category_id_from_breadcrumbs(
                [{"name": "Видеорегистраторы", "href": "https://market.yandex.ru/catalog/list?hid=6269371"}]
            ),
            6269371,
        )

    def test_finds_json_ld_product(self) -> None:
        product = _jsonld_product(
            [{"@graph": [{"@type": "BreadcrumbList"}, {"@type": "Product", "sku": "123"}]}]
        )
        self.assertEqual(product["sku"], "123")


class ApiPayloadTests(unittest.TestCase):
    def test_store_and_publish_payloads(self) -> None:
        store = StoreCreateRequest(alias="  俄罗斯  主店  ", token="ACMA:12345678901234567890")
        publish = PublishRequest(
            store_id=7,
            product_ids=[1, 2],
            price_percent=200,
            package={"length": 30, "width": 20, "height": 10, "weight": 0.5},
            initial_stock=10,
        )
        self.assertEqual(store.alias, "俄罗斯 主店")
        self.assertEqual(publish.store_id, 7)
        self.assertEqual(publish.price_percent, 200)
        self.assertEqual(publish.package.weight, 0.5)
        self.assertEqual(publish.initial_stock, 10)
        default_publish = PublishRequest(
            store_id=7,
            product_ids=[1],
            package={"length": 30, "width": 20, "height": 10, "weight": 0.5},
            initial_stock=10,
        )
        self.assertEqual(default_publish.price_percent, 200)

    def test_order_filters_normalize_and_limit_date_range(self) -> None:
        payload = OrderListRequest(
            store_id=7,
            statuses=[" processing ", "PROCESSING", "delivery"],
            date_from="2026-08-07",
            date_to="2026-09-05",
            page_token=" next-page ",
        )
        self.assertEqual(payload.statuses, ["PROCESSING", "DELIVERY"])
        self.assertEqual(payload.page_token, "next-page")
        with self.assertRaisesRegex(ValueError, "不能超过 30 天"):
            OrderListRequest(
                store_id=7,
                date_from="2026-08-01",
                date_to="2026-09-05",
            )

    def test_zeshun_authorization_payload_and_token_extraction(self) -> None:
        store = ZeshunStoreCreateRequest(
            alias="  俄罗斯  01 店  ",
            tg_code="TG-001",
            authorization_url="https://auth.example.com/start?state={tg_code}",
        )
        callback = ZeshunStoreAuthorizeRequest(
            authorized_url="https://console.example.com/callback#access_token=ACMA%3A12345678901234567890&state=TG-001"
        )
        self.assertEqual(store.alias, "俄罗斯 01 店")
        self.assertEqual(store.tg_code, "TG-001")
        self.assertIsNone(callback.token)
        self.assertEqual(
            TaskService.build_zeshun_authorization_url(
                store.tg_code,
                store.authorization_url,
            ),
            "https://auth.example.com/start?state=TG-001",
        )
        self.assertEqual(
            TaskService.token_from_authorized_url(callback.authorized_url),
            "ACMA:12345678901234567890",
        )

    def test_filters_non_product_images_and_canonicalizes_yandex_media(self) -> None:
        pictures = normalize_product_pictures(
            [
                "https://avatars.mds.yandex.net/get-mpic/123456/abcdef/180x240_multiply",
                "https://barcode.yandex.net/qrcode/unrelated.svg",
                "https://adfstat.yandex.ru/image/market?req_id=1",
                "https://storage.mds.yandex.net/get-bstor/footer.png",
            ]
        )
        self.assertEqual(
            pictures,
            ["https://avatars.mds.yandex.net/get-mpic/123456/abcdef/orig"],
        )

    def test_converts_rubles_to_cny_then_applies_price_percent(self) -> None:
        self.assertEqual(calculate_listing_price(1000, 200, 0.08), 160.0)
        self.assertEqual(calculate_listing_price(8121.875, 200, 0.08), 1300.0)
        xml = b"""<?xml version='1.0' encoding='windows-1251'?>
        <ValCurs Date='22.08.2026'>
          <Valute><CharCode>CNY</CharCode><Nominal>1</Nominal><Value>12,5000</Value></Valute>
        </ValCurs>"""
        rate, effective_date = parse_cbr_daily_xml(xml)
        self.assertEqual(rate, 0.08)
        self.assertEqual(effective_date, "22.08.2026")

    def test_builds_official_offer_mapping_payload(self) -> None:
        product = sample_product().model_dump()
        product["missing_publish_fields"] = []
        product["weight_dimensions"] = {"length": 30, "width": 20, "height": 10, "weight": 0.5}
        payload = YandexSellerClient.build_offer_mapping(product)
        self.assertEqual(payload["mapping"]["marketSku"], 123456789)
        self.assertEqual(payload["offer"]["marketCategoryId"], 6269371)
        self.assertEqual(payload["offer"]["basicPrice"]["currencyId"], "RUR")
        self.assertEqual(payload["offer"]["offerId"], "YM-CB-123456789")
        self.assertEqual(
            payload["offer"]["weightDimensions"],
            {"length": 30.0, "width": 20.0, "height": 10.0, "weight": 0.5},
        )

    def test_maps_scraped_specs_to_yandex_parameter_values(self) -> None:
        values = YandexSellerClient.build_parameter_values(
            {
                "Тип": "беспроводные TWS",
                "Активное шумоподавление": "да",
                "Максимальная воспроизводимая частота": "16000 Гц",
                "Линейка": "Air Pro",
            },
            [
                {
                    "id": 1,
                    "name": "Тип",
                    "type": "ENUM",
                    "values": [{"id": 101, "value": "Беспроводные TWS"}],
                },
                {"id": 2, "name": "Активное шумоподавление", "type": "BOOLEAN"},
                {
                    "id": 3,
                    "name": "Максимальная воспроизводимая частота",
                    "type": "NUMERIC",
                    "constraints": {"minValue": 1, "maxValue": 100000},
                    "unit": {
                        "defaultUnitId": 301,
                        "units": [{"id": 301, "name": "Гц", "fullName": "герц"}],
                    },
                },
                {
                    "id": 4,
                    "name": "Линейка",
                    "type": "TEXT",
                    "constraints": {"maxLength": 20},
                },
            ],
        )
        self.assertEqual(
            values,
            [
                {"parameterId": 1, "valueId": 101},
                {"parameterId": 2, "value": "true"},
                {"parameterId": 3, "value": "16000", "unitId": 301},
                {"parameterId": 4, "value": "Air Pro"},
            ],
        )

        product = sample_product().model_dump()
        product["missing_publish_fields"] = []
        product["weight_dimensions"] = {"length": 30, "width": 20, "height": 10, "weight": 0.5}
        payload = YandexSellerClient.build_offer_mapping(product, values)
        self.assertEqual(payload["offer"]["parameterValues"], values)

    def test_removes_external_links_from_description(self) -> None:
        product = sample_product().model_dump()
        product["description"] = "详情 https://example.com 联系 test@example.com"
        product["missing_publish_fields"] = []
        product["weight_dimensions"] = {"length": 30, "width": 20, "height": 10, "weight": 0.5}
        payload = YandexSellerClient.build_offer_mapping(product)
        description = payload["offer"]["description"]
        self.assertNotIn("https://", description)
        self.assertNotIn("@example.com", description)

    def test_offer_mapping_accepts_cny_listing_price(self) -> None:
        product = sample_product().model_dump()
        product["price"] = 1299.5
        product["currency"] = "CNY"
        product["missing_publish_fields"] = []
        product["weight_dimensions"] = {"length": 30, "width": 20, "height": 10, "weight": 0.5}
        payload = YandexSellerClient.build_offer_mapping(product)
        self.assertEqual(payload["offer"]["basicPrice"], {"value": 1300, "currencyId": "CNY"})

    def test_quality_gate_allows_empty_description_and_one_picture(self) -> None:
        product = sample_product().model_copy(
            update={
                "description": "",
                "pictures": ["https://avatars.mds.yandex.net/get-mpic/123456/thin/orig"],
            }
        )
        self.assertNotIn("description（至少500字）", product.missing_publish_fields)
        self.assertNotIn("pictures（至少6张）", product.missing_publish_fields)
        self.assertEqual(product.missing_publish_fields, [])

    def test_quality_gate_still_requires_one_picture_and_specification(self) -> None:
        product = sample_product().model_copy(
            update={"pictures": [], "specifications": {}}
        )
        self.assertIn("pictures（至少1张）", product.missing_publish_fields)
        self.assertIn("specifications（至少1项）", product.missing_publish_fields)


class DatabaseTests(unittest.TestCase):
    def test_product_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            db.initialize()
            run_id = db.create_search_run("行车记录仪", 200)
            product = sample_product()
            product.run_id = run_id
            product_id = db.upsert_product(product)
            products = db.list_products_for_run(run_id)
            self.assertEqual(len(products), 1)
            self.assertEqual(products[0]["id"], product_id)
            self.assertTrue(products[0]["ready_to_publish"])
            self.assertEqual(products[0]["specifications"]["Разрешение"], "4K")

    def test_store_directory_and_live_publish_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            db.initialize()
            context = {
                "business_id": 101,
                "business_name": "Test Business",
                "campaign_id": 202,
                "store_name": "Yandex Test Store",
                "placement_type": "FBS",
                "api_availability": "AVAILABLE",
                "auth_scopes": ["ALL_METHODS"],
            }
            stored, created = db.save_store(
                alias="俄罗斯主店",
                encrypted_token=b"encrypted-token",
                token_fingerprint="fingerprint",
                store=context,
            )
            self.assertTrue(created)
            self.assertEqual(stored["store_name"], "Yandex Test Store")
            self.assertNotIn("encrypted_token", stored)
            secret_store = db.get_store(stored["id"], include_secret=True)
            self.assertEqual(secret_store["encrypted_token"], b"encrypted-token")

            run_id = db.create_search_run("行车记录仪", 1)
            product = sample_product()
            product.run_id = run_id
            product_id = db.upsert_product(product)
            job_id = db.create_publish_job(
                1,
                101,
                202,
                stored["id"],
                price_percent=200,
                exchange_rate=0.08,
                exchange_rate_date="22.08.2026",
                target_currency="CNY",
                package={"length": 30, "width": 20, "height": 10, "weight": 0.5},
                initial_stock=10,
                stock_target=StockTarget("business", 303, "FBS Test Warehouse"),
            )
            db.add_publish_result(job_id, product_id, "published", "Yandex 已接收")
            job = db.get_publish_job(job_id)
            self.assertEqual(job["processed"], 1)
            self.assertEqual(job["succeeded"], 1)
            self.assertEqual(job["failed"], 0)
            self.assertEqual(job["price_percent"], 200)
            self.assertEqual(job["exchange_rate"], 0.08)
            self.assertEqual(job["target_currency"], "CNY")
            self.assertEqual(job["package"]["weight"], 0.5)
            self.assertEqual(job["initial_stock"], 10)
            self.assertEqual(job["warehouse_id"], 303)
            self.assertEqual(job["warehouse_name"], "FBS Test Warehouse")
            self.assertEqual(job["stock_method"], "business")
            self.assertTrue(job["results"][0]["success"])
            self.assertEqual(job["results"][0]["product_name"], product.name)

    def test_zeshun_tg_code_directory_and_token_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            db.initialize()
            authorization = db.create_zeshun_authorization(
                alias="俄罗斯 01 店",
                tg_code="TG-001",
                authorization_url="https://auth.example.com/start?tg_code=TG-001",
            )
            self.assertEqual(authorization["tg_code"], "TG-001")
            self.assertFalse(authorization["authorized"])
            with self.assertRaisesRegex(ValueError, "TG 码已经存在"):
                db.create_zeshun_authorization(
                    alias="重复店铺",
                    tg_code="tg-001",
                    authorization_url="",
                )

            context = {
                "business_id": 101,
                "business_name": "Test Business",
                "campaign_id": 202,
                "store_name": "Yandex Test Store",
                "placement_type": "FBS",
                "api_availability": "AVAILABLE",
                "auth_scopes": ["ALL_METHODS"],
            }
            linked_store, _ = db.save_store(
                alias="俄罗斯 01 店",
                encrypted_token=b"encrypted-token",
                token_fingerprint="fingerprint",
                store=context,
            )
            completed = db.complete_zeshun_authorization(
                authorization["id"],
                store_id=linked_store["id"],
                encrypted_authorized_url=b"encrypted-callback",
            )
            self.assertTrue(completed["authorized"])
            self.assertNotIn("encrypted_authorized_url", completed)
            listed = db.list_zeshun_authorizations()
            self.assertEqual(listed[0]["store_name"], "Yandex Test Store")
            self.assertIsNotNone(listed[0]["token_updated_at"])

    def test_local_authorizations_migrate_then_are_removed(self) -> None:
        class CentralStub:
            def __init__(self):
                self.tokens = []
                self.zeshun = []

            def save_store(self, **values):
                self.tokens.append(values)
                return {"id": 900, "alias": values["alias"]}, True

            def import_zeshun_authorization(self, **values):
                self.zeshun.append(values)

        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "legacy.db")
            db.initialize()
            store, _ = db.save_store(
                alias="旧授权",
                encrypted_token=b"dpapi-token",
                token_fingerprint="legacy-fingerprint",
                store={
                    "business_id": 101,
                    "business_name": "Business",
                    "campaign_id": 202,
                    "store_name": "Store",
                    "placement_type": "FBS",
                    "api_availability": "AVAILABLE",
                    "auth_scopes": ["all-methods"],
                },
            )
            authorization = db.create_zeshun_authorization(
                alias="旧授权", tg_code="TG-OLD", authorization_url="https://auth.example/start"
            )
            db.complete_zeshun_authorization(
                authorization["id"], store_id=store["id"], encrypted_authorized_url=b"callback"
            )
            central = CentralStub()
            with patch("yandex.app.central_authorization.authorization_store", central), patch(
                "yandex.app.secret_store.unprotect_secret", return_value="ACMA:central-token"
            ):
                result = migrate_legacy_authorizations(db)

            self.assertEqual(result, {"stores": 1, "zeshun": 1})
            self.assertEqual(central.tokens[0]["token"], "ACMA:central-token")
            self.assertEqual(central.zeshun[0]["store_id"], 900)
            self.assertEqual(db.list_stores(), [])
            self.assertEqual(db.list_zeshun_authorizations(), [])


class ContentApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_business_orders_api_with_store_filters(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            return_value={
                "orders": [{"orderId": 501, "status": "PROCESSING", "items": []}],
                "paging": {"nextPageToken": "page-2"},
            }
        )

        result = await client.get_orders(
            101,
            campaign_id=202,
            statuses=["PROCESSING"],
            date_from="2026-08-07",
            date_to="2026-09-05",
            page_token="page-1",
        )

        self.assertEqual(result["orders"][0]["orderId"], 501)
        self.assertEqual(result["paging"]["nextPageToken"], "page-2")
        call = client._request.await_args
        self.assertEqual(call.args[0], "POST")
        self.assertIn("/v1/businesses/101/orders?", call.args[1])
        self.assertIn("pageToken=page-1", call.args[1])
        self.assertEqual(
            call.kwargs["json_body"],
            {
                "fake": False,
                "campaignIds": [202],
                "statuses": ["PROCESSING"],
                "dates": {
                    "creationDateFrom": "2026-08-07",
                    "creationDateTo": "2026-09-05",
                },
            },
        )

    async def test_publish_fetches_category_parameters_and_submits_values(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            side_effect=[
                {
                    "status": "OK",
                    "result": {
                        "parameters": [
                            {
                                "id": 77,
                                "name": "Разрешение",
                                "type": "TEXT",
                                "constraints": {"maxLength": 30},
                            }
                        ]
                    },
                },
                {"status": "OK", "results": [{"offerId": "YM-CB-123456789"}]},
            ]
        )
        product = sample_product().model_dump()
        product["missing_publish_fields"] = []
        product["weight_dimensions"] = {"length": 30, "width": 20, "height": 10, "weight": 0.5}

        response = await client.publish_product(101, product)

        self.assertEqual(response["_local"]["submittedParameterCount"], 1)
        category_call, publish_call = client._request.await_args_list
        self.assertIn("/v2/category/6269371/parameters?businessId=101", category_call.args[1])
        submitted_offer = publish_call.kwargs["json_body"]["offerMappings"][0]["offer"]
        self.assertEqual(
            submitted_offer["parameterValues"],
            [{"parameterId": 77, "value": "4K"}],
        )

    async def test_offer_card_quality_query(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            return_value={
                "status": "OK",
                "result": {"offerCards": [{"offerId": "YM-CB-1", "contentRating": 82}]},
            }
        )
        cards = await client.get_offer_cards(101, ["YM-CB-1"])
        self.assertEqual(cards[0]["contentRating"], 82)
        self.assertTrue(client._request.await_args.kwargs["json_body"]["withRecommendations"])


class StockApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_fbs_warehouse_and_builds_v3_stock_payload(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            side_effect=[
                {
                    "status": "OK",
                    "result": {
                        "warehouses": [
                            {
                                "id": 303,
                                "name": "FBS Test Warehouse",
                                "models": [
                                    {"placementType": "FBS", "apiAvailability": "AVAILABLE"}
                                ],
                            }
                        ]
                    },
                },
                {"status": "OK"},
            ]
        )

        target = await client.resolve_stock_target(101, 202, "FBS")
        self.assertEqual(target, StockTarget("business", 303, "FBS Test Warehouse"))
        await client.update_offer_stock(101, 202, "YM-CB-1", 10, target)

        stock_call = client._request.await_args_list[1]
        self.assertEqual(stock_call.args[:2], ("POST", "/v3/businesses/101/offers/stocks/update"))
        self.assertEqual(
            stock_call.kwargs["json_body"],
            {
                "skuItems": [
                    {"sku": "YM-CB-1", "partnerWarehouseId": 303, "count": 10}
                ]
            },
        )

    async def test_resumes_offer_display(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(return_value={"status": "OK"})
        await client.resume_offer_display(202, "YM-CB-1")
        client._request.assert_awaited_once_with(
            "POST",
            "/v2/campaigns/202/hidden-offers/delete",
            json_body={"hiddenOffers": [{"offerId": "YM-CB-1"}]},
        )


class ZeshunAuthorizationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_link_updates_linked_store_token_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            db.initialize()
            authorization = db.create_zeshun_authorization(
                alias="俄罗斯 01 店",
                tg_code="TG-001",
                authorization_url="https://auth.example.com/start?state=TG-001",
            )
            linked_store, _ = db.save_store(
                alias="俄罗斯 01 店",
                encrypted_token=b"old-encrypted-token",
                token_fingerprint="old-fingerprint",
                store={
                    "business_id": 101,
                    "business_name": "Test Business",
                    "campaign_id": 202,
                    "store_name": "Yandex Test Store",
                    "placement_type": "FBS",
                    "api_availability": "AVAILABLE",
                    "auth_scopes": ["ALL_METHODS"],
                },
            )
            service = TaskService()
            service.add_store = AsyncMock(return_value=(linked_store, False))
            callback_url = (
                "https://console.example.com/callback"
                "#access_token=ACMA%3Anew-token-123456789&state=TG-001"
            )
            with patch("yandex.app.service.authorization_store", db):
                updated, store, created = await service.authorize_zeshun_store(
                    authorization["id"],
                    authorized_url=callback_url,
                    token=None,
                )
            service.add_store.assert_awaited_once_with(
                "俄罗斯 01 店",
                "ACMA:new-token-123456789",
            )
            self.assertFalse(created)
            self.assertEqual(store["id"], linked_store["id"])
            self.assertTrue(updated["authorized"])
            self.assertIsNotNone(updated["token_updated_at"])


class SecretStoreTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is Windows-only")
    def test_dpapi_round_trip(self) -> None:
        token = "ACMA:local-test-token-123456789"
        encrypted = protect_secret(token)
        self.assertNotIn(token.encode("utf-8"), encrypted)
        self.assertEqual(unprotect_secret(encrypted), token)
        self.assertEqual(len(secret_fingerprint(token)), 64)


if __name__ == "__main__":
    unittest.main()
