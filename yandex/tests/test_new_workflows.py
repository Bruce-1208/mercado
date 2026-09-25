from __future__ import annotations

import io
import json
import unittest
import zipfile
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from yandex.app.main import app
from yandex.app.schemas import OrderListRequest, ReturnDecisionRequest, SettlementReportRequest
from yandex.app.settlement import parse_payment_report_archive
from yandex.app.service import TaskService
from yandex.app.yandex_api import StoreContext, YandexSellerClient


def report_zip(files: dict[str, object]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, rows in files.items():
            archive.writestr(name, json.dumps(rows))
    return output.getvalue()


class NewWorkflowSchemaTests(unittest.TestCase):
    def test_force_sync_is_explicit_and_default_off(self) -> None:
        self.assertFalse(OrderListRequest(store_id=1).force_sync)
        self.assertTrue(OrderListRequest(store_id=1, force_sync=True).force_sync)

    def test_return_partial_refund_requires_amount_and_decline_requires_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "部分退款必须"):
            ReturnDecisionRequest(
                store_id=1, order_id=2, return_id=3,
                decisions=[{"return_item_id": 4, "decision_type": "PARTIAL_MONEY_REFUND"}],
            )
        with self.assertRaisesRegex(ValueError, "拒绝退款必须"):
            ReturnDecisionRequest(
                store_id=1, order_id=2, return_id=3,
                decisions=[{"return_item_id": 4, "decision_type": "DECLINE_REFUND"}],
            )

    def test_settlement_report_limits_period_to_about_three_months(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能超过"):
            SettlementReportRequest(store_id=1, date_from="2026-01-01", date_to="2026-04-10")

    def test_settlement_report_uses_calendar_month_boundary(self) -> None:
        SettlementReportRequest(store_id=1, date_from="2026-01-31", date_to="2026-04-30")
        with self.assertRaisesRegex(ValueError, "不能超过三个月"):
            SettlementReportRequest(store_id=1, date_from="2026-01-01", date_to="2026-04-02")


class PaymentReportParserTests(unittest.TestCase):
    def test_groups_report_sheets_and_matches_cached_order_ids(self) -> None:
        payload = report_zip({
            "netting_report_payments.json": [
                {"orderId": 101, "transactionSum": 25.5, "paymentStatus": "PAID", "bankOrderId": 9, "bankOrderDate": "2026-09-01", "bankSum": 25.5},
                {"orderId": 101, "transactionSum": -2, "paymentStatus": "PAID", "bankOrderId": 9, "bankOrderDate": "2026-09-01", "bankSum": 25.5},
            ],
            "netting_report_returns.json": {"rows": [
                {"orderId": 202, "transactionSum": -7, "transactionSource": "refund"},
            ]},
        })
        report = parse_payment_report_archive(payload, {"101"})
        self.assertEqual(report["stats"]["line_count"], 3)
        self.assertEqual(report["stats"]["transaction_sum"], 16.5)
        self.assertEqual(report["stats"]["cached_order_count"], 1)
        self.assertEqual(report["stats"]["uncached_order_count"], 1)
        self.assertEqual(report["stats"]["bank_order_count"], 1)
        self.assertEqual(report["stats"]["orders"][0]["order_id"], "202")

    def test_rejects_non_zip_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "不是有效"):
            parse_payment_report_archive(b"not a zip")

    def test_summary_uses_primary_sheet_without_double_counting_detail_sheets(self) -> None:
        payload = report_zip({
            "transaction_date.json": [
                {"orderId": 101, "transactionSum": 25.5, "paymentStatus": "PAID", "bankOrderId": 9, "bankSum": 25.5},
            ],
            "netting_report_accruals.json": [
                {"orderId": 101, "transactionSum": 25.5},
            ],
        })
        report = parse_payment_report_archive(payload, {"101"})
        self.assertEqual(report["stats"]["transaction_sum"], 25.5)
        self.assertEqual(report["stats"]["line_count"], 1)
        self.assertEqual(report["stats"]["summary_source"], "transaction_date.json")
        self.assertEqual(len(report["files"]), 2)


class SearchStatusApiTests(unittest.TestCase):
    def test_status_endpoint_does_not_load_product_payloads(self) -> None:
        run = {"id": 42, "keyword": "camera", "status": "running", "found_count": 3}
        with patch("yandex.app.main.database.get_search_run", return_value=run), \
             patch("yandex.app.main.database.list_products_for_run", side_effect=AssertionError("products should not load")):
            response = TestClient(app).get("/api/search/42/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"run": run})


class NewYandexApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_return_decision_methods_use_platform_paths_and_payloads(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(side_effect=[
            {"status": "OK", "result": {"return": {"id": 3, "orderId": 2}}},
            {"status": "OK", "result": {"availableDecisions": [{"decisionType": "REFUND_MONEY"}]}},
            {"status": "OK"},
        ])
        record = await client.get_return(20, 2, 3)
        options = await client.get_return_available_decisions(10, 20, 3)
        await client.submit_return_decisions(20, 2, 3, [{"returnItemId": 4, "decisionType": "REFUND_MONEY"}])
        calls = client._request.await_args_list
        self.assertEqual(record["orderId"], 2)
        self.assertEqual(options[0]["decisionType"], "REFUND_MONEY")
        self.assertIn("/v2/campaigns/20/orders/2/returns/3", calls[0].args[1])
        self.assertEqual(calls[1].kwargs["json_body"], {"campaignId": 20, "returnId": 3})
        self.assertEqual(calls[2].kwargs["json_body"], {"returnItemDecisions": [{"returnItemId": 4, "decisionType": "REFUND_MONEY"}]})
        self.assertEqual(calls[2].kwargs["attempts"], 1)

    async def test_payment_report_generation_and_status_follow_async_api(self) -> None:
        client = YandexSellerClient("test-token")
        client._request = AsyncMock(side_effect=[
            {"status": "OK", "result": {"reportId": "report-1", "estimatedGenerationTime": 2500}},
            {"status": "OK", "result": {"status": "PROCESSING"}},
        ])
        created = await client.generate_payment_report(10, 20, "2026-09-01", "2026-09-25")
        status = await client.get_report_info("report-1")
        calls = client._request.await_args_list
        self.assertEqual(created["reportId"], "report-1")
        self.assertEqual(status["status"], "PROCESSING")
        self.assertIn("format=JSON", calls[0].args[1])
        self.assertEqual(calls[0].kwargs["json_body"]["campaignIds"], [20])
        self.assertIn("/v2/reports/info/report-1", calls[1].args[1])


class ForcedOrderSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_force_sync_bypasses_the_normal_sync_interval(self) -> None:
        service = TaskService()
        store = StoreContext(1, "Business", 2, "Store", "FBS", "AVAILABLE", ["all-methods"])
        service.resolve_store = AsyncMock(return_value=("token", store, {"id": 5}))
        service.sync_store_orders = AsyncMock(return_value={"mode": "full"})
        recent = "2099-01-01T00:00:00+00:00"
        with patch("yandex.app.service.database.get_order_sync_state", side_effect=[{
            "new_orders_synced_at": recent, "old_orders_synced_at": recent,
        }, {"new_orders_synced_at": recent, "old_orders_synced_at": recent}]), \
             patch("yandex.app.service.database.count_cached_orders", return_value=1), \
             patch("yandex.app.service.database.list_cached_orders", return_value=([], False)), \
             patch("yandex.app.service.enrich_order_finances", new=AsyncMock(return_value=[])):
            await service.get_orders(5, force_sync=True)
        service.sync_store_orders.assert_awaited_once()
        self.assertEqual(service.sync_store_orders.await_args.kwargs["mode"], "full")
        self.assertTrue(service.sync_store_orders.await_args.kwargs["force"])

    async def test_failed_chat_queue_does_not_hide_reviews_or_questions(self) -> None:
        service = TaskService()
        store = StoreContext(1, "Business", 2, "Store", "FBS", "AVAILABLE", ["all-methods"])
        client = AsyncMock()
        client.get_returns.return_value = {"returns": []}
        client.get_chats.side_effect = RuntimeError("chat API temporarily unavailable")
        client.get_feedbacks.return_value = {"feedbacks": [{"id": 8, "author": "buyer"}]}
        client.get_questions.return_value = {"questions": [{"id": 9, "text": "Question?"}]}
        service.resolve_store = AsyncMock(return_value=("token", store, {"id": 1}))
        service.get_orders = AsyncMock(return_value=({}, {}))
        with patch("yandex.app.service.authorization_store.list_stores", return_value=[{"id": 1, "alias": "Shop"}]), \
             patch("yandex.app.service.database.list_cached_order_todos", return_value=[]), \
             patch("yandex.app.service.YandexSellerClient", return_value=client):
            todos = await service.get_todos()
        self.assertEqual({item["kind"] for item in todos["items"]}, {"feedback", "question"})
        self.assertTrue(any("消息待办读取失败" in warning for warning in todos["warnings"]))


if __name__ == "__main__":
    unittest.main()
