import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

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

    def test_order_search_uses_global_selling_seller_parameter_and_offset(self):
        client = MercadoLibreClient("token")
        requests = []

        def fake_request(method, path, *, params=None):
            requests.append((method, path, dict(params or {})))
            offset = int((params or {}).get("offset") or 0)
            if offset == 0:
                return {
                    "results": [{"orders": [{"id": 101}]}, {"orders": [{"id": 102}]}],
                    "paging": {"total": 3},
                }
            return {
                "results": [{"orders": [{"id": 103}]}],
                "paging": {"total": 3},
            }

        client.request = fake_request

        self.assertEqual(list(client.iter_order_ids("seller-7", sort="date_desc")), ["101", "102", "103"])
        self.assertEqual(requests[0][2]["seller"], "seller-7")
        self.assertNotIn("seller.id", requests[0][2])
        self.assertEqual([request[2]["offset"] for request in requests], [0, 2])

    def test_request_retries_after_transient_network_error(self):
        class Response:
            status_code = 200
            ok = True
            headers = {}

            @staticmethod
            def json():
                return {"ok": True}

        class Session:
            def __init__(self):
                self.calls = 0

            def request(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise requests.ConnectionError("connection reset")
                return Response()

        session = Session()
        client = MercadoLibreClient("token", session=session)
        with patch("mercado_api.client.time.sleep") as sleep:
            result = client.request("GET", "/marketplace/users/seller-1/items/search")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(session.calls, 2)
        sleep.assert_called_once_with(1)

    def test_get_shipment_label_returns_official_pdf_bytes(self):
        class Response:
            status_code = 200
            ok = True
            headers = {"Content-Type": "application/pdf"}
            content = b"%PDF-1.4\nofficial-label\n%%EOF"
            text = ""

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return Response()

        session = Session()
        client = MercadoLibreClient("token-value", session=session)

        result = client.get_shipment_label("47841658738")

        self.assertTrue(result.startswith(b"%PDF"))
        self.assertEqual(
            session.calls[0][1],
            "https://api.mercadolibre.com/marketplace/shipments/47841658738/labels",
        )
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "Bearer token-value")


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
