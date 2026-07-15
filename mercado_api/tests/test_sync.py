import json
import tempfile
import unittest
from pathlib import Path

from mercado_api.client import MercadoLibreClient
from mercado_api.database import MercadoDatabase


class ClientTests(unittest.TestCase):
    def test_extracts_real_order_ids_from_cart_results(self):
        results = [
            {"id": 999, "orders": [{"id": 101}, {"id": 102}]},
            {"id": 103},
            {"id": 888, "orders": [{"id": 101}]},
        ]
        self.assertEqual(list(MercadoLibreClient._order_ids(results)), ["101", "102", "103"])


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "mercado.sqlite3"
        self.db = MercadoDatabase(self.path)
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_order_upsert_updates_status_and_replaces_items(self):
        order = {
            "id": 1001,
            "status": "paid",
            "date_created": "2026-07-01T01:00:00+00:00",
            "last_updated": "2026-07-01T01:01:00+00:00",
            "buyer": {"id": 44},
            "shipping": {"id": 55},
            "currency_id": "USD",
            "total_amount": 10.5,
            "order_items": [{"item": {"id": "CBT1", "title": "First"}, "quantity": 1, "unit_price": 10.5}],
        }
        self.assertEqual(self.db.upsert_orders("seller-1", [order]), 1)
        order["status"] = "cancelled"
        order["order_items"] = [{"item": {"id": "CBT2", "title": "Replacement"}, "quantity": 2}]
        self.db.upsert_orders("seller-1", [order])

        with self.db.connect() as connection:
            saved = connection.execute("SELECT * FROM orders WHERE order_id='1001'").fetchone()
            items = connection.execute("SELECT * FROM order_items WHERE order_id='1001'").fetchall()
        self.assertEqual(saved["status"], "cancelled")
        self.assertEqual(json.loads(saved["raw_json"])["status"], "cancelled")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_id"], "CBT2")

    def test_listing_upsert_keeps_full_payload(self):
        listing = {"id": "CBT9", "site_id": "CBT", "title": "Product", "status": "active",
                   "attributes": [{"id": "BRAND", "value_name": "Acme"}]}
        self.db.upsert_listings("merchant-1", [listing])
        with self.db.connect() as connection:
            saved = connection.execute("SELECT * FROM listings WHERE item_id='CBT9'").fetchone()
        self.assertEqual(json.loads(saved["raw_json"])["attributes"][0]["value_name"], "Acme")


if __name__ == "__main__":
    unittest.main()
