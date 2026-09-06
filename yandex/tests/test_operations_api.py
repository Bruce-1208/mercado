from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from yandex.app.schemas import (
    FeedbackListRequest,
    FeedbackReplyRequest,
    InventoryListRequest,
    InventoryStockUpdateRequest,
    ListingDeleteRequest,
    ListingListRequest,
    ListingPriceUpdateRequest,
    OrderActionRequest,
    QuestionListRequest,
    ReturnListRequest,
)
from yandex.app.service import _require_scope
from yandex.app.yandex_api import StockTarget, StoreContext, YandexApiError, YandexSellerClient


class OperationsSchemaTests(unittest.TestCase):
    @staticmethod
    def store(scopes: list[str]) -> StoreContext:
        return StoreContext(
            business_id=1,
            business_name="测试",
            campaign_id=2,
            store_name="测试店",
            placement_type="FBS",
            api_availability="AVAILABLE",
            auth_scopes=scopes,
        )

    def test_inventory_and_feedback_payloads_are_normalized(self) -> None:
        inventory = InventoryListRequest(
            store_id=1, offer_ids=[" sku-1 ", "sku-1", "sku-2"], page_token=" next "
        )
        self.assertEqual(inventory.offer_ids, ["sku-1", "sku-2"])
        self.assertEqual(inventory.page_token, "next")
        self.assertEqual(InventoryStockUpdateRequest(store_id=1, offer_id=" sku ", count=0).count, 0)

        feedback = FeedbackListRequest(
            store_id=1, rating_values=[5, 1, 5], offer_ids=[" a "]
        )
        self.assertEqual(feedback.rating_values, [5, 1])
        self.assertEqual(feedback.offer_ids, ["a"])
        self.assertEqual(
            FeedbackReplyRequest(store_id=1, feedback_id=2, text=" 谢谢反馈 ").text,
            "谢谢反馈",
        )

        listings = ListingListRequest(
            store_id=1,
            offer_ids=[" sku-1 ", "sku-1"],
            statuses=[" published ", "PUBLISHED"],
        )
        self.assertEqual(listings.offer_ids, ["sku-1"])
        self.assertEqual(listings.statuses, ["PUBLISHED"])
        self.assertEqual(
            ListingDeleteRequest(store_id=1, offer_ids=[" a ", "a"]).offer_ids,
            ["a"],
        )
        self.assertEqual(
            ListingPriceUpdateRequest(
                store_id=1, offer_id=" sku-1 ", value=95, currency_id="cny", discount_base=100
            ).currency_id,
            "CNY",
        )
        with self.assertRaisesRegex(ValueError, "折扣必须"):
            ListingPriceUpdateRequest(
                store_id=1, offer_id="sku-1", value=99, currency_id="CNY", discount_base=100
            )

    def test_dates_and_order_actions_are_constrained(self) -> None:
        returns = ReturnListRequest(
            store_id=1,
            return_type="RETURN",
            statuses=[" refunded ", "REFUNDED"],
            date_from="2026-08-01",
            date_to="2026-09-01",
        )
        self.assertEqual(returns.statuses, ["REFUNDED"])
        with self.assertRaisesRegex(ValueError, "不能超过 31 天"):
            QuestionListRequest(
                store_id=1, date_from="2026-08-01", date_to="2026-09-02"
            )
        self.assertEqual(
            OrderActionRequest(store_id=1, order_id=2, action="READY_TO_SHIP").action,
            "READY_TO_SHIP",
        )

    def test_read_only_tokens_can_connect_but_cannot_mutate(self) -> None:
        store = self.store(["inventory-and-order-processing:read-only"])
        _require_scope(
            store, {"inventory-and-order-processing"}, "订单", read_only=True
        )
        with self.assertRaisesRegex(YandexApiError, "管理权限"):
            _require_scope(store, {"inventory-and-order-processing"}, "订单")


class OperationsClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_store_links_with_status_and_page_token(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            return_value={
                "status": "OK",
                "result": {
                    "offers": [{"offerId": "sku-1", "status": "PUBLISHED"}],
                    "paging": {"nextPageToken": "next"},
                },
            }
        )
        result = await client.get_campaign_offers(
            20, statuses=["PUBLISHED"], page_token="page-1", limit=200
        )
        self.assertEqual(result["offers"][0]["offerId"], "sku-1")
        self.assertEqual(result["paging"]["nextPageToken"], "next")
        call = client._request.await_args
        self.assertIn("/v2/campaigns/20/offers?", call.args[1])
        self.assertIn("pageToken=page-1", call.args[1])
        self.assertEqual(call.kwargs["json_body"], {"statuses": ["PUBLISHED"]})

    async def test_updates_store_or_business_price_from_official_setting(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            side_effect=[
                {"status": "OK", "result": {"settings": {"onlyDefaultPrice": False, "currency": "CNY"}}},
                {"status": "OK"},
                {"status": "OK", "result": {"settings": {"onlyDefaultPrice": True, "currency": "CNY"}}},
                {"status": "OK"},
            ]
        )
        campaign = await client.update_listing_price(
            10, 20, "sku-1", value=95, currency_id="CNY", discount_base=100
        )
        business = await client.update_listing_price(
            10, 20, "sku-1", value=88, currency_id="CNY"
        )
        self.assertEqual(campaign["priceScope"], "campaign")
        self.assertEqual(business["priceScope"], "business")
        self.assertEqual(
            client._request.await_args_list[1].args[1],
            "/v2/campaigns/20/offer-prices/updates",
        )
        self.assertEqual(
            client._request.await_args_list[1].kwargs["json_body"],
            {"offers": [{"offerId": "sku-1", "price": {"value": 95, "currencyId": "CNY", "discountBase": 100}}]},
        )
        self.assertEqual(
            client._request.await_args_list[3].args[1],
            "/v2/businesses/10/offer-prices/updates",
        )

    async def test_deletes_links_only_from_selected_store(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            return_value={"status": "OK", "result": {"notDeletedOfferIds": ["sku-2"]}}
        )
        result = await client.delete_campaign_offers(20, ["sku-1", "sku-2"])
        self.assertEqual(result["deleted"], ["sku-1"])
        self.assertEqual(result["notDeletedOfferIds"], ["sku-2"])
        client._request.assert_awaited_once_with(
            "POST",
            "/v2/campaigns/20/offers/delete",
            json_body={"offerIds": ["sku-1", "sku-2"]},
        )

    async def test_reads_independent_warehouse_stock(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            return_value={
                "status": "OK",
                "result": {
                    "partnerWarehouseId": 55,
                    "offers": [{"offerId": "sku-1", "stocks": [{"type": "AVAILABLE", "count": 7}]}],
                    "paging": {"nextPageToken": "page-2"},
                },
            }
        )
        target = StockTarget(method="business", warehouse_id=55, warehouse_name="主仓")

        result = await client.get_offer_stocks(10, 20, target, page_token="page-1")

        self.assertEqual(result["warehouses"][0]["warehouseName"], "主仓")
        self.assertEqual(result["paging"]["nextPageToken"], "page-2")
        call = client._request.await_args
        self.assertEqual(call.args[0], "POST")
        self.assertIn("/v3/businesses/10/offers/stocks?", call.args[1])
        self.assertIn("pageToken=page-1", call.args[1])
        self.assertEqual(call.kwargs["json_body"], {"archived": False, "partnerWarehouseId": 55})

    async def test_reads_grouped_warehouse_stock_by_sku(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            return_value={
                "status": "OK",
                "result": {"warehouses": [{"warehouseId": 9, "offers": [{"offerId": "sku-1"}]}]},
            }
        )
        result = await client.get_offer_stocks(
            10,
            20,
            StockTarget(method="campaign", warehouse_id=None, warehouse_name="仓库组"),
            offer_ids=["sku-1"],
        )
        self.assertEqual(result["warehouses"][0]["warehouseName"], "仓库组")
        call = client._request.await_args
        self.assertEqual(call.args[1], "/v2/campaigns/20/offers/stocks")
        self.assertEqual(call.kwargs["json_body"], {"offerIds": ["sku-1"]})

    async def test_resolves_current_grouped_warehouse_response(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            side_effect=[
                YandexApiError("warehouse groups enabled", status_code=420),
                {
                    "status": "OK",
                    "result": {
                        "warehouses": [
                            {
                                "id": 77,
                                "name": "店铺仓",
                                "campaignId": 20,
                                "groupInfo": {"id": 3, "name": "莫斯科仓库组"},
                            }
                        ]
                    },
                },
            ]
        )
        target = await client.resolve_stock_target(10, 20, "FBS")
        self.assertEqual(target, StockTarget("campaign", 77, "莫斯科仓库组"))
        second = client._request.await_args_list[1]
        self.assertEqual(second.args[:2], ("POST", "/v2/businesses/10/warehouses"))
        self.assertEqual(second.kwargs["json_body"], {"campaignIds": [20]})

    async def test_zero_stock_is_a_valid_explicit_update(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(return_value={"status": "OK"})
        await client.update_offer_stock(
            10,
            20,
            "sku-1",
            0,
            StockTarget(method="business", warehouse_id=55, warehouse_name="主仓"),
        )
        self.assertEqual(
            client._request.await_args.kwargs["json_body"]["skuItems"][0]["count"], 0
        )

    async def test_returns_use_current_date_parameters_and_paging(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            return_value={"status": "OK", "result": {"returns": [{"id": 3}], "paging": {}}}
        )
        result = await client.get_returns(
            20,
            return_type="RETURN",
            statuses=["WAITING_FOR_DECISION"],
            date_from="2026-08-01",
            date_to="2026-09-01",
        )
        self.assertEqual(result["returns"][0]["id"], 3)
        path = client._request.await_args.args[1]
        self.assertIn("fromDate=2026-08-01", path)
        self.assertIn("toDate=2026-09-01", path)
        self.assertIn("type=RETURN", path)

    async def test_feedback_list_reply_and_skip_payloads(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            side_effect=[
                {"status": "OK", "result": {"feedbacks": [{"feedbackId": 7}], "paging": {}}},
                {"status": "OK", "result": {"id": 8, "status": "UNMODERATED"}},
                {"status": "OK"},
            ]
        )
        listed = await client.get_feedbacks(10, reaction_status="NEED_REACTION", rating_values=[1])
        replied = await client.reply_to_feedback(10, 7, "感谢反馈")
        await client.skip_feedbacks(10, [7, 7])
        self.assertEqual(listed["feedbacks"][0]["feedbackId"], 7)
        self.assertEqual(replied["status"], "UNMODERATED")
        calls = client._request.await_args_list
        self.assertEqual(calls[0].kwargs["json_body"], {"reactionStatus": "NEED_REACTION", "ratingValues": [1]})
        self.assertEqual(calls[1].kwargs["json_body"], {"feedbackId": 7, "comment": {"text": "感谢反馈"}})
        self.assertEqual(calls[2].kwargs["json_body"], {"feedbackIds": [7]})

    async def test_questions_list_and_reply_payloads(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(
            side_effect=[
                {"status": "OK", "result": {"questions": [{"questionIdentifiers": {"id": 9}}], "totalCount": 1, "paging": {}}},
                {"status": "OK", "result": {"entity": {"id": 10, "type": "ANSWER"}}},
            ]
        )
        listed = await client.get_questions(
            10, need_answer=True, date_from="2026-08-07", date_to="2026-09-06"
        )
        replied = await client.reply_to_question(10, 9, "支持该功能")
        self.assertEqual(listed["totalCount"], 1)
        self.assertEqual(replied["entity"]["type"], "ANSWER")
        calls = client._request.await_args_list
        self.assertEqual(
            calls[0].kwargs["json_body"],
            {"needAnswer": True, "sort": "CREATED_AT_DESC", "dateFrom": "2026-08-07", "dateTo": "2026-09-06"},
        )
        self.assertEqual(
            calls[1].kwargs["json_body"],
            {"parentEntityId": {"id": 9, "type": "QUESTION"}, "text": "支持该功能", "operationType": "CREATE"},
        )

    async def test_order_status_action_uses_official_transition_body(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(return_value={"status": "OK", "result": {"order": {"id": 88}}})
        await client.update_order_status(
            20, 88, status="PROCESSING", substatus="READY_TO_SHIP"
        )
        call = client._request.await_args
        self.assertEqual(call.args[:2], ("PUT", "/v2/campaigns/20/orders/88/status"))
        self.assertEqual(
            call.kwargs["json_body"],
            {"order": {"status": "PROCESSING", "substatus": "READY_TO_SHIP"}},
        )


if __name__ == "__main__":
    unittest.main()
