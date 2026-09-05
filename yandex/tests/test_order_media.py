from __future__ import annotations

import copy
import unittest
from unittest.mock import AsyncMock

from yandex.app.order_finance import build_order_finance, enrich_order_finances
from yandex.app.order_media import build_order_item_media, safe_image_url, safe_product_url
from yandex.app.yandex_api import YandexSellerClient


IMAGE = "https://avatars.mds.yandex.net/get-mpic/123/orig"
PRODUCT = "https://market.yandex.ru/product--sample/456?sku=9001&offerid=opaque"


def catalogue() -> dict:
    return {
        "offerId": "SKU-1", "price": {"value": 56, "currencyId": "CNY"},
        "pictures": [IMAGE, "https://images.example.test/second.jpg"],
        "mediaFiles": {"pictures": [{"url": IMAGE}, {"url": "https://images.example.test/third.jpg"}]},
        "mapping": {"marketSku": 9001},
        "showcaseUrls": [
            {"showcaseType": "B2B", "showcaseUrl": "https://business.market.yandex.ru/product/456"},
            {"showcaseType": "B2C", "showcaseUrl": PRODUCT},
        ],
        "campaigns": [{"campaignId": 20, "status": "PUBLISHED"}],
    }


def sample_order() -> dict:
    return {
        "orderId": 101, "campaignId": 20,
        "items": [{"id": 1001, "offerId": "SKU-1", "offerName": "订单原始名称", "count": 2,
                   "prices": {"payment": {"value": 12.02, "currencyId": "CNY"}, "vat": "NO_VAT"},
                   "itemStatuses": [{"status": "SHIPPED", "count": 1}, {"status": "CREATED", "count": 1}]}],
    }


class OrderMediaTests(unittest.TestCase):
    def test_catalogue_picture_priority_deduplication_and_official_b2c_link(self):
        media = build_order_item_media({}, catalogue(), campaign_id=20)
        self.assertEqual(media["image_url"], IMAGE)
        self.assertEqual(len(media["pictures"]), 3)
        self.assertEqual(media["product_url"], PRODUCT)
        self.assertEqual(media["product_url_source"], "showcase")
        self.assertEqual(media["market_sku"], "9001")

    def test_media_files_fallback_and_original_order_media_are_supported(self):
        record = {"mediaFiles": {"pictures": [{"url": "javascript:alert(1)"}, {"url": IMAGE}]}}
        self.assertEqual(build_order_item_media({}, record)["image_url"], IMAGE)
        media = build_order_item_media({"image_url": "http://images.example.test/order.jpg",
                                        "product_url": PRODUCT}, catalogue(), campaign_id=20)
        self.assertEqual(media["image_url"], "http://images.example.test/order.jpg")
        self.assertEqual(media["product_url"], PRODUCT)

    def test_missing_link_is_not_fabricated_from_sku_and_other_store_links_are_not_used(self):
        for record in (
            {"mapping": {"marketSku": 9001}},
            {**catalogue(), "campaigns": [{"campaignId": 99}]},
            {**catalogue(), "showcaseUrls": [{"showcaseType": "B2C", "showcaseUrl": PRODUCT, "campaignId": 99}]},
        ):
            with self.subTest(record=record):
                self.assertIsNone(build_order_item_media({"offerId": "looks-like-123"}, record, campaign_id=20)["product_url"])

    def test_exact_campaign_link_wins_if_upstream_supplies_campaign_context(self):
        record = catalogue()
        campaign_url = PRODUCT + "&campaignId=20"
        record["showcaseUrls"].extend([
            {"showcaseType": "B2C", "showcaseUrl": PRODUCT + "&wrong=99", "campaignId": 99},
            {"showcaseType": "B2C", "showcaseUrl": campaign_url, "campaignId": "20"},
        ])
        self.assertEqual(build_order_item_media({}, record, campaign_id=20)["product_url"], campaign_url)

    def test_dangerous_and_ambiguous_urls_are_rejected(self):
        for value in (None, {}, "", "javascript:alert(1)", "data:image/png;base64,AAAA",
                      "//market.yandex.ru/product/1", "file:///c:/private.png", "https://a.test/\nimage.png",
                      "https://market.yandex.ru\\@evil.test/p.png", "https://user:pass@market.yandex.ru/p",
                      "https://[invalid/p", "https://market.yandex.ru:invalid/p"):
            with self.subTest(value=value):
                self.assertIsNone(safe_image_url(value))
                self.assertIsNone(safe_product_url(value))
        for value in ("http://market.yandex.ru/product/1", "https://market.yandex.ru.evil.test/product/1",
                      "https://market.yandex.ru@evil.test/product/1", "https://evil.test/?next=market.yandex.ru",
                      "https://market.yandex.ru:444/product/1", "https://market.yandex.ru./product/1",
                      "https://market.yandex.ru%2f.evil.test/product/1"):
            with self.subTest(value=value):
                self.assertIsNone(safe_product_url(value))
        self.assertEqual(safe_product_url("https://www.market.yandex.ru/product/1"),
                         "https://www.market.yandex.ru/product/1")

    def test_malformed_catalogue_data_and_oversized_ids_do_not_break_orders(self):
        media = build_order_item_media({}, {"pictures": [None, {}, "data:x", IMAGE],
                                            "mapping": {"marketSku": "9" * 5000},
                                            "showcaseUrls": [None, 2, {}], "campaigns": "bad"})
        self.assertEqual(media["image_url"], IMAGE)
        self.assertIsNone(media["market_sku"])
        self.assertIsNone(media["product_url"])
        media = build_order_item_media({"product_url": PRODUCT, "product_url_source": []})
        self.assertEqual(media["product_url"], PRODUCT)
        self.assertEqual(media["product_url_source"], "order")

    def test_finance_keeps_line_identity_status_vat_and_money_without_sku_matching(self):
        order = sample_order()
        duplicate = copy.deepcopy(order["items"][0])
        duplicate.update({"id": 1002, "itemStatuses": [{"status": "RETURNED", "count": 2}]})
        duplicate["prices"]["vat"] = "VAT_20"
        order["items"].append(duplicate)
        finance = build_order_finance(order)
        self.assertEqual([item["item_id"] for item in finance["items"]], [1001, 1002])
        self.assertEqual(finance["items"][1]["item_statuses"], [{"status": "RETURNED", "count": 2}])
        self.assertEqual(finance["items"][1]["vat"], "VAT_20")
        self.assertEqual(finance["items"][0]["buyer_payment"]["value"], 12.02)


class OrderMediaApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_business_price_request_keeps_media_without_more_requests(self):
        client = YandexSellerClient("test-token")
        record = catalogue()
        client._request = AsyncMock(return_value={"result": {"offerMappings": [{
            "offer": {"offerId": record["offerId"], "basicPrice": record["price"],
                      "pictures": record["pictures"], "mediaFiles": record["mediaFiles"],
                      "campaigns": record["campaigns"]},
            "mapping": record["mapping"], "showcaseUrls": record["showcaseUrls"],
        }]}})
        rows = await client.get_business_offer_prices(10, ["SKU-1", "SKU-1"])
        client._request.assert_awaited_once_with("POST", "/v2/businesses/10/offer-mappings",
                                               json_body={"offerIds": ["SKU-1"]}, attempts=1)
        # This shared catalogue reader may expose additional product metadata;
        # require the media/price contract without forbidding unrelated fields.
        self.assertEqual({key: rows[0][key] for key in record}, record)

    async def test_batch_enrichment_gives_both_item_views_same_media_without_mutation(self):
        client = YandexSellerClient("test-token")
        client.get_campaign_offer_prices = AsyncMock(return_value=[{"offerId": "SKU-1", "price": {"value": 56, "currencyId": "CNY"}}])
        client.get_business_offer_prices = AsyncMock(return_value=[catalogue(), {**catalogue(), "offerId": "UNREQUESTED"}])
        client.get_order_stats = AsyncMock(return_value=[])
        order = sample_order()
        original = copy.deepcopy(order)
        rows = await enrich_order_finances(client, 10, 20, [order, {**order, "orderId": 102}])
        client.get_business_offer_prices.assert_awaited_once_with(10, ["SKU-1"])
        self.assertEqual(order, original)
        for row in rows:
            for field in ("image_url", "pictures", "product_url", "product_url_source", "market_sku"):
                self.assertEqual(row["items"][0][field], row["finance"]["items"][0][field])
            self.assertEqual(row["items"][0]["product_url"], PRODUCT)
            self.assertEqual(row["finance"]["items"][0]["item_id"], 1001)
            self.assertEqual(row["finance"]["listing_total"]["value"], 112)

    async def test_failed_catalogue_is_nonfatal_and_retains_safe_order_media(self):
        client = YandexSellerClient("test-token")
        client.get_campaign_offer_prices = AsyncMock(return_value=[])
        client.get_business_offer_prices = AsyncMock(side_effect=RuntimeError("secret upstream message"))
        client.get_order_stats = AsyncMock(return_value=[])
        order = sample_order()
        order["items"][0].update({"image_url": IMAGE, "product_url": PRODUCT})
        rows = await enrich_order_finances(client, 10, 20, [order])
        self.assertEqual(rows[0]["items"][0]["image_url"], IMAGE)
        self.assertEqual(rows[0]["finance"]["items"][0]["product_url"], PRODUCT)
        self.assertEqual(rows[0]["finance"]["buyer_payment"]["value"], 12.02)
        self.assertNotIn("secret", str(rows))


if __name__ == "__main__":
    unittest.main()
