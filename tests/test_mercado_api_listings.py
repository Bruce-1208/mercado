import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from mercado_api.mercado_api_listings import MercadoLibreClient, sync_listings


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, search_pages, items, item_errors=None):
        self.headers = {}
        self.search_pages = list(search_pages)
        self.items = items
        self.item_errors = item_errors or {}
        self.search_call = 0
        self.calls = []
        self.closed = False

    def request(self, method, url, params=None, timeout=None):
        path = urlparse(url).path
        params = dict(params or {})
        self.calls.append((method, path, params, timeout))
        if path == "/users/me":
            return FakeResponse(200, {"id": 42, "nickname": "TEST_SHOP", "site_id": "CBT"})
        if path == "/marketplace/users/42/items/search":
            page = self.search_pages[self.search_call]
            self.search_call += 1
            return FakeResponse(200, page)
        if path == "/items":
            payload = []
            for item_id in params["ids"].split(","):
                if item_id in self.item_errors:
                    payload.append({"code": 404, "body": {"id": item_id, "message": self.item_errors[item_id]}})
                else:
                    payload.append({"code": 200, "body": self.items[item_id]})
            return FakeResponse(200, payload)
        raise AssertionError(f"Unexpected request: {path} {params}")

    def close(self):
        self.closed = True


def item(item_id, price, *, variation=False):
    result = {
        "id": item_id,
        "site_id": "CBT",
        "title": f"Item {item_id}",
        "category_id": "CBT123",
        "domain_id": "CBT-TEST",
        "currency_id": "USD",
        "price": price,
        "available_quantity": 8,
        "sold_quantity": 2,
        "status": "active",
        "sub_status": [],
        "listing_type_id": "gold_special",
        "pictures": [{"secure_url": f"https://img.example/{item_id}.jpg"}],
        "attributes": [{"id": "SELLER_SKU", "value_name": f"SKU-{item_id}"}],
        "date_created": "2026-01-01T00:00:00.000Z",
        "last_updated": "2026-01-02T00:00:00.000Z",
    }
    if variation:
        result["variations"] = [
            {
                "id": 9001,
                "price": price,
                "available_quantity": 3,
                "sold_quantity": 1,
                "picture_ids": ["picture-1"],
                "attributes": [{"id": "SELLER_SKU", "value_name": "VAR-SKU-1"}],
                "attribute_combinations": [{"id": "COLOR", "value_name": "Blue"}],
            }
        ]
    return result


class MercadoListingsTests(unittest.TestCase):
    def test_scan_keeps_first_page_and_uses_scroll(self):
        session = FakeSession(
            [
                {"results": ["CBT1", "CBT2"], "scroll_id": "next-1", "paging": {"total": 3}},
                {"results": ["CBT3"], "scroll_id": "next-2", "paging": {"total": 3}},
            ],
            {},
        )
        client = MercadoLibreClient("token", session=session, backoff_seconds=0)

        listing_ids = client.list_all_listing_ids("42", page_size=2)

        self.assertEqual(["CBT1", "CBT2", "CBT3"], listing_ids)
        self.assertNotIn("scroll_id", session.calls[0][2])
        self.assertEqual("next-1", session.calls[1][2]["scroll_id"])

    def test_full_sync_creates_queryable_database(self):
        session = FakeSession(
            [
                {"results": ["CBT1", "CBT2"], "scroll_id": "next-1", "paging": {"total": 3}},
                {"results": ["CBT3"], "scroll_id": "next-2", "paging": {"total": 3}},
            ],
            {
                "CBT1": item("CBT1", 10.5, variation=True),
                "CBT2": item("CBT2", 20),
                "CBT3": item("CBT3", 30),
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "listings.db"
            result = sync_listings("Bearer test-token", database_path, session=session)

            self.assertEqual((3, 3, 0), (result.discovered, result.stored, result.failed))
            self.assertTrue(session.closed)
            self.assertEqual("Bearer test-token", session.headers["Authorization"])

            connection = sqlite3.connect(database_path)
            connection.row_factory = sqlite3.Row
            listings = connection.execute(
                "SELECT * FROM mercado_listings ORDER BY item_id"
            ).fetchall()
            variation = connection.execute(
                "SELECT * FROM mercado_listing_variations"
            ).fetchone()
            run = connection.execute("SELECT * FROM mercado_sync_runs").fetchone()
            connection.close()

        self.assertEqual(3, len(listings))
        self.assertEqual("SKU-CBT1", listings[0]["seller_sku"])
        self.assertEqual("https://img.example/CBT1.jpg", listings[0]["thumbnail"])
        self.assertEqual("CBT1", json.loads(listings[0]["raw_json"])["id"])
        self.assertEqual("VAR-SKU-1", variation["seller_sku"])
        self.assertEqual("success", run["status"])

    def test_second_sync_marks_missing_rows_not_current(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "listings.db"
            first = FakeSession(
                [{"results": ["CBT1", "CBT2"], "scroll_id": "s1", "paging": {"total": 2}}],
                {"CBT1": item("CBT1", 10), "CBT2": item("CBT2", 20)},
            )
            sync_listings("token", database_path, session=first)

            second = FakeSession(
                [{"results": ["CBT1"], "scroll_id": "s2", "paging": {"total": 1}}],
                {"CBT1": item("CBT1", 99)},
            )
            sync_listings("token", database_path, session=second)

            connection = sqlite3.connect(database_path)
            rows = dict(
                connection.execute(
                    "SELECT item_id, is_current FROM mercado_listings ORDER BY item_id"
                ).fetchall()
            )
            price = connection.execute(
                "SELECT price FROM mercado_listings WHERE item_id='CBT1'"
            ).fetchone()[0]
            connection.close()

        self.assertEqual({"CBT1": 1, "CBT2": 0}, rows)
        self.assertEqual(99, price)

    def test_item_level_error_is_recorded_without_losing_other_items(self):
        session = FakeSession(
            [{"results": ["CBT1", "CBT2"], "scroll_id": "s1", "paging": {"total": 2}}],
            {"CBT1": item("CBT1", 10)},
            {"CBT2": "not found"},
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "listings.db"
            result = sync_listings("token", database_path, session=session)
            connection = sqlite3.connect(database_path)
            failed_row = connection.execute(
                "SELECT fetch_status, fetch_error FROM mercado_listings WHERE item_id='CBT2'"
            ).fetchone()
            run_status = connection.execute("SELECT status FROM mercado_sync_runs").fetchone()[0]
            connection.close()

        self.assertEqual((2, 1, 1), (result.discovered, result.stored, result.failed))
        self.assertEqual("error", failed_row[0])
        self.assertIn("not found", failed_row[1])
        self.assertEqual("partial", run_status)


if __name__ == "__main__":
    unittest.main()
