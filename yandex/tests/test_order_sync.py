from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from yandex.app.database import Database
from yandex.app.service import TaskService
from yandex.app.yandex_api import StoreContext


def order(order_id: int, status: str, created: str) -> dict:
    return {
        "orderId": order_id,
        "status": status,
        "creationDate": created,
        "updateDate": created,
        "items": [],
    }


class OrderCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "orders.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_new_order_pass_does_not_refresh_existing_status(self) -> None:
        self.database.cache_orders(
            7, [order(101, "PENDING", "2026-09-08T01:00:00Z")], mode="full"
        )
        changes = self.database.cache_orders(
            7,
            [
                order(101, "DELIVERED", "2026-09-08T01:00:00Z"),
                order(102, "PROCESSING", "2026-09-09T01:00:00Z"),
            ],
            mode="insert",
        )

        records, has_more = self.database.list_cached_orders(7, limit=50)
        by_id = {item["orderId"]: item for item in records}
        self.assertEqual(changes, {"inserted": 1, "updated": 0})
        self.assertEqual(by_id[101]["status"], "PENDING")
        self.assertEqual(by_id[102]["status"], "PROCESSING")
        self.assertFalse(has_more)

    def test_old_status_pass_only_updates_known_orders(self) -> None:
        self.database.cache_orders(
            7, [order(101, "PENDING", "2026-09-08T01:00:00Z")], mode="full"
        )
        changes = self.database.cache_orders(
            7,
            [
                order(101, "DELIVERED", "2026-09-08T01:00:00Z"),
                order(999, "PENDING", "2026-09-09T01:00:00Z"),
            ],
            mode="update",
        )

        records, _ = self.database.list_cached_orders(7, statuses=["DELIVERED"])
        self.assertEqual(changes, {"inserted": 0, "updated": 1})
        self.assertEqual([item["orderId"] for item in records], [101])
        self.assertEqual(self.database.count_cached_orders(7), 1)

    def test_cached_paging_and_date_filters_are_stable(self) -> None:
        self.database.cache_orders(
            7,
            [
                order(101, "PENDING", "2026-09-07T01:00:00Z"),
                order(102, "PENDING", "2026-09-08T01:00:00Z"),
                order(103, "DELIVERED", "2026-09-09T01:00:00Z"),
            ],
            mode="full",
        )
        first, has_more = self.database.list_cached_orders(
            7, date_from="2026-09-08", date_to="2026-09-09", limit=1
        )
        second, second_has_more = self.database.list_cached_orders(
            7, date_from="2026-09-08", date_to="2026-09-09", offset=1, limit=1
        )
        self.assertEqual([item["orderId"] for item in first], [103])
        self.assertTrue(has_more)
        self.assertEqual([item["orderId"] for item in second], [102])
        self.assertFalse(second_has_more)


class OrderSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "orders.db")
        self.database.initialize()
        self.service = TaskService()
        self.stored = {
            "id": 7,
            "alias": "俄罗斯店",
            "access_token": "secret-token",
            "business_id": 10,
            "business_name": "Business",
            "campaign_id": 20,
            "store_name": "Store",
            "placement_type": "FBS",
            "api_availability": "AVAILABLE",
            "auth_scopes": ["inventory-and-order-processing:read-only"],
        }

    async def asyncTearDown(self) -> None:
        await self.service.stop_order_sync_scheduler()
        self.temporary.cleanup()

    async def test_due_scheduler_keeps_new_and_old_refresh_cadences_separate(self) -> None:
        first_now = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
        api_orders = [order(101, "PENDING", "2026-09-09T00:30:00Z")]

        async def get_orders(*_args, **_kwargs):
            return {"orders": list(api_orders), "paging": {"nextPageToken": ""}}

        client = unittest.mock.Mock()
        client.get_orders = AsyncMock(side_effect=get_orders)
        with (
            patch("yandex.app.service.database", self.database),
            patch("yandex.app.service.authorization_store.list_stores", return_value=[self.stored]),
            patch("yandex.app.service.authorization_store.get_store", return_value=self.stored),
            patch("yandex.app.service.YandexSellerClient", return_value=client),
        ):
            first = await self.service.run_due_order_syncs(now=first_now)
            self.assertEqual(first[0]["mode"], "full")

            api_orders[:] = [
                order(101, "DELIVERED", "2026-09-09T00:30:00Z"),
                order(102, "PENDING", "2026-09-09T01:10:00Z"),
            ]
            second = await self.service.run_due_order_syncs(
                now=first_now + timedelta(minutes=15)
            )

        self.assertEqual(second[0]["mode"], "insert")
        records, _ = self.database.list_cached_orders(7)
        by_id = {item["orderId"]: item for item in records}
        self.assertEqual(by_id[101]["status"], "PENDING")
        self.assertEqual(by_id[102]["status"], "PENDING")
        state = self.database.get_order_sync_state(7)
        self.assertEqual(state["old_orders_synced_at"], first_now.isoformat(timespec="seconds"))
        self.assertEqual(
            state["new_orders_synced_at"],
            (first_now + timedelta(minutes=15)).isoformat(timespec="seconds"),
        )

    async def test_order_page_reads_fresh_cache_with_local_paging(self) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.database.cache_orders(
            7,
            [
                order(101, "PENDING", "2026-09-09T00:30:00Z"),
                order(102, "PROCESSING", "2026-09-09T00:45:00Z"),
            ],
            mode="full",
        )
        self.database.update_order_sync_state(
            7, new_orders_synced_at=now, old_orders_synced_at=now
        )
        context = StoreContext(
            business_id=10,
            business_name="Business",
            campaign_id=20,
            store_name="Store",
            placement_type="FBS",
            api_availability="AVAILABLE",
            auth_scopes=["inventory-and-order-processing:read-only"],
        )
        self.service.resolve_store = AsyncMock(
            return_value=("secret-token", context, {"id": 7, "alias": "俄罗斯店"})
        )
        self.service.sync_store_orders = AsyncMock()

        async def unchanged(_client, _business_id, _campaign_id, orders):
            return orders

        with (
            patch("yandex.app.service.database", self.database),
            patch("yandex.app.service.enrich_order_finances", side_effect=unchanged),
        ):
            first, _ = await self.service.get_orders(7, limit=1)
            second, _ = await self.service.get_orders(
                7, page_token=first["paging"]["nextPageToken"], limit=1
            )

        self.assertEqual([item["orderId"] for item in first["orders"]], [102])
        self.assertEqual(first["paging"]["nextPageToken"], "cache:1")
        self.assertEqual([item["orderId"] for item in second["orders"]], [101])
        self.assertEqual(second["paging"]["nextPageToken"], "")
        self.service.sync_store_orders.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
