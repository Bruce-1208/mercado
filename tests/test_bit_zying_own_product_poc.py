from bit import bit_zying_own_product_poc as own_poc


def _row(product_id):
    return {
        "id": product_id,
        "title": f"<b>Own product {product_id}</b>",
        "thumb": f"https://example.com/{product_id}.jpg",
        "cur": "USD",
        "cost": 12.5,
    }


def test_extract_list_response_supports_direct_erp_payload():
    rows, total = own_poc._extract_list_response(
        {"list": {"data": [_row(1)], "maxcount": "401234"}}
    )
    assert rows[0]["id"] == 1
    assert total == 401234


def test_extract_list_response_supports_server_envelope():
    rows, total = own_poc._extract_list_response(
        {"data": {"list": {"data": [_row(2)], "maxcount": 20}}}
    )
    assert rows[0]["id"] == 2
    assert total == 20


def test_fetch_index_uses_erp_product_library_without_selection_from_filter():
    calls = []

    def api_call(command, payload):
        calls.append((command, payload))
        start = (payload["page"] - 1) * 60 + 1
        return {
            "list": {
                "data": [_row(value) for value in range(start, start + 60)],
                "maxcount": 400000,
            }
        }

    records, total = own_poc.fetch_product_index(api_call, limit=100, page_size=60)

    assert len(records) == 100
    assert len({row["product_id"] for row in records}) == 100
    assert records[0]["title"] == "Own product 1"
    assert total == 400000
    assert [payload["page"] for _, payload in calls] == [1, 2]
    assert all(command == "sale.stat" for command, _ in calls)
    assert all("from" not in payload for _, payload in calls)


def test_enrich_details_keeps_full_raw_payload_and_common_fields():
    record = own_poc._normalize_list_row(_row(9), 1, "2026-08-11 12:00:00")

    def api_call(command, payload):
        assert command == "sale.detail"
        assert payload == {"id": "9"}
        return {
            "root": [
                {
                    "sale_id": 9,
                    "sale_sku": "SKU-9",
                    "sale_title": '{"en":"Product 9"}',
                    "sale_cur": "CNY",
                    "sale_cost": 88,
                    "sale_netproceed": 60,
                    "sale_weight": 540,
                    "sale_size": [11, 10, 17],
                    "sale_localid": 123,
                    "sale_stat": 1000,
                    "sale_pic": ["https://example.com/detail.jpg"],
                    "sale_attrs": '{"1":{"site":"CBT","kindid":430974}}',
                }
            ]
        }

    own_poc.enrich_product_details(api_call, [record])

    assert record["detail_ok"] is True
    assert record["detail_row"]["sale_id"] == 9
    assert record["sku"] == "SKU-9"
    assert record["currency"] == "CNY"
    assert record["dimensions"] == "[11, 10, 17]"
    assert record["category_site"] == "CBT"
    assert record["category_external_id"] == 430974
