from __future__ import annotations

import copy
import unittest
from unittest.mock import AsyncMock

from yandex.app.order_finance import build_order_finance, enrich_order_finances
from yandex.app.yandex_api import YandexSellerClient


def money(value: float, currency: str = "CNY") -> dict:
    return {"value": value, "currencyId": currency}


def sample_order() -> dict:
    prices = {"payment": money(12.02), "cashback": money(42.46), "subsidy": money(57.58)}
    return {
        "orderId": 101, "status": "PROCESSING",
        "items": [{"offerId": "SKU-1", "offerName": "商品", "count": 2, "prices": copy.deepcopy(prices)}],
        "prices": {**prices, "delivery": {"payment": money(0), "subsidy": money(0)}},
    }


def settled_stats() -> dict:
    return {
        "id": 101, "currency": "CNY",
        "payments": [
            {"id": "pay-1", "type": "PAYMENT", "source": "BUYER", "total": 100,
             "paymentOrder": {"id": "doc-1", "date": "2026-09-01"}},
            {"id": "refund-1", "type": "REFUND", "source": "BUYER", "total": 15,
             "paymentOrder": {"id": "doc-2", "date": "2026-09-02"}},
        ],
        "commissions": [{"type": "FEE", "actual": 5}, {"type": "DELIVERY_TO_CUSTOMER", "actual": 3}],
        "subsidies": [],
    }


class OrderFinanceTests(unittest.TestCase):
    def test_line_amounts_already_include_quantity_and_listing_price_is_separate(self):
        order = sample_order()
        finance = build_order_finance(order, listing_prices={"SKU-1": money(56)})
        self.assertEqual(finance["listing_total"], {"value": 112, "currency": "CNY"})
        self.assertEqual(finance["items"][0]["buyer_payment"]["value"], 12.02)
        self.assertEqual(finance["buyer_payment"]["value"], 12.02)
        self.assertEqual(finance["cashback"]["value"], 42.46)
        self.assertEqual(finance["seller_subsidy"]["value"], 57.58)
        self.assertEqual(finance["buyer_shipping"]["value"], 0)
        self.assertEqual(finance["delivery_total"]["value"], 0)
        self.assertIsNone(finance["seller_net"])
        self.assertEqual(finance["settlement_status"], "pending")

    def test_missing_and_mixed_currency_totals_never_become_zero_or_partial_sum(self):
        order = sample_order()
        order["items"].append({"offerId": "SKU-2", "count": 1, "prices": {"payment": money(2, "RUR")}})
        order["prices"].pop("payment")
        finance = build_order_finance(order, listing_prices={"SKU-1": money(56)})
        self.assertIsNone(finance["listing_total"])
        self.assertIsNone(finance["buyer_payment"])
        self.assertIsNone(finance["items"][1]["cashback"])
        order["items"][1]["prices"]["payment"] = {"value": "NaN", "currencyId": "CNY"}
        self.assertIsNone(build_order_finance(order)["buyer_payment"])

    def test_complete_cash_and_fee_records_enable_only_an_estimate_and_refunds_are_signed(self):
        order = sample_order()
        order["prices"]["cashback"] = money(0)
        order["prices"]["subsidy"] = money(0)
        stats = settled_stats()
        finance = build_order_finance(order, stats=stats)
        self.assertEqual(finance["seller_gross"]["value"], 85)
        self.assertEqual(finance["platform_fees"]["value"], 8)
        self.assertEqual(finance["seller_shipping"]["value"], 3)
        self.assertEqual(finance["seller_net"]["value"], 77)
        self.assertEqual(finance["settlement_status"], "estimate")
        self.assertEqual(finance["seller_gross_kind"], "reported_transfers")
        # A duplicate transfer must not double the reported funds.
        stats["payments"].append(copy.deepcopy(stats["payments"][0]))
        self.assertEqual(build_order_finance(order, stats=stats)["seller_net"]["value"], 77)

    def test_points_empty_fees_missing_payment_order_and_currency_mismatch_prevent_settlement(self):
        order = sample_order()
        finance = build_order_finance(order, stats=settled_stats())
        self.assertIsNone(finance["seller_net"])
        self.assertEqual(finance["seller_gross"]["value"], 85)
        for key in ("cashback", "subsidy"):
            order["prices"][key] = money(0)
        for change in (
            {"commissions": []},
            {"currency": "RUR"},
            {"payments": [{"type": "PAYMENT", "total": 100}]},
            {"subsidies": [{"operationType": "ACCRUAL", "type": "SUBSIDY", "amount": 2}]},
            {"commissions": [{"type": "FEE", "actual": None}]},
        ):
            with self.subTest(change=change):
                finance = build_order_finance(order, stats={**settled_stats(), **change})
                self.assertIsNone(finance["seller_net"])
                self.assertEqual(finance["settlement_status"], "pending")

    def test_shipping_subsidy_is_not_seller_logistics_fee(self):
        order = sample_order()
        order["prices"]["delivery"] = {"payment": money(4), "subsidy": money(6)}
        finance = build_order_finance(order, stats=settled_stats())
        self.assertEqual(finance["buyer_shipping"]["value"], 4)
        self.assertEqual(finance["delivery_subsidy"]["value"], 6)
        self.assertEqual(finance["delivery_total"]["value"], 10)
        self.assertEqual(finance["seller_shipping"]["value"], 3)


class FinanceApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_enrichment_batches_duplicate_skus_and_prioritizes_campaign_prices(self):
        client = YandexSellerClient("test-token")
        client.get_campaign_offer_prices = AsyncMock(return_value=[{"offerId": "SKU-1", "price": money(56)}])
        client.get_business_offer_prices = AsyncMock(return_value=[{"offerId": "SKU-1", "price": money(90)}])
        client.get_order_stats = AsyncMock(return_value=[])
        order = sample_order()
        rows = await enrich_order_finances(client, 10, 20, [order, {**order, "orderId": 102}])
        client.get_campaign_offer_prices.assert_awaited_once_with(20, ["SKU-1"])
        client.get_business_offer_prices.assert_awaited_once_with(10, ["SKU-1"])
        client.get_order_stats.assert_awaited_once_with(20, [101, 102])
        self.assertEqual(rows[0]["finance"]["listing_total"]["value"], 112)
        self.assertNotIn("finance", order)

    async def test_supplement_failure_keeps_order_and_does_not_assume_business_price_is_active(self):
        client = YandexSellerClient("test-token")
        client.get_campaign_offer_prices = AsyncMock(side_effect=RuntimeError("secret upstream message"))
        client.get_business_offer_prices = AsyncMock(return_value=[{"offerId": "SKU-1", "price": money(90)}])
        client.get_order_stats = AsyncMock(side_effect=RuntimeError("secret upstream message"))
        rows = await enrich_order_finances(client, 10, 20, [sample_order()])
        self.assertEqual(rows[0]["orderId"], 101)
        self.assertEqual(rows[0]["finance"]["buyer_payment"]["value"], 12.02)
        self.assertIsNone(rows[0]["finance"]["listing_total"])
        self.assertNotIn("secret", str(rows))

    async def test_price_requests_use_explicit_skus_without_pagination(self):
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(side_effect=[
            {"status": "OK", "result": {"offers": [{"offerId": "SKU-1", "price": money(56)}]}},
            {"status": "OK", "result": {"offerMappings": [{"offer": {"offerId": "SKU-1", "basicPrice": money(90)}}]}},
        ])
        campaign = await client.get_campaign_offer_prices(20, ["SKU-1", "SKU-1"])
        business = await client.get_business_offer_prices(10, ["SKU-1"])
        self.assertEqual(campaign[0]["price"]["value"], 56)
        self.assertEqual(business[0]["price"]["value"], 90)
        self.assertEqual(client._request.await_args_list[0].args, ("POST", "/v2/campaigns/20/offer-prices"))
        self.assertEqual(client._request.await_args_list[1].args, ("POST", "/v2/businesses/10/offer-mappings"))
        for call in client._request.await_args_list:
            self.assertEqual(call.kwargs["json_body"], {"offerIds": ["SKU-1"]})

    async def test_stats_paging_collects_only_requested_orders(self):
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(side_effect=[
            {"result": {"orders": [{"id": 101}, {"id": 999}], "paging": {"nextPageToken": "page-2"}}},
            {"result": {"orders": [{"id": 102}]}},
        ])
        result = await client.get_order_stats(20, [101, 102])
        self.assertEqual([item["id"] for item in result], [101, 102])
        self.assertEqual(client._request.await_args_list[0].kwargs["json_body"], {"orders": [101, 102]})
        self.assertIn("pageToken=page-2", client._request.await_args_list[1].args[1])


if __name__ == "__main__":
    unittest.main()
