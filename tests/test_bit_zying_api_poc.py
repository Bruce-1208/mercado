from bit import bit_zying_api_poc


def _row(product_id):
    return {
        "id": product_id,
        "title": f"<b>Product {product_id}</b>",
        "thumb": f"https://example.com/{product_id}.jpg",
        "cur": "USD",
        "cost": 12.5,
    }


def test_extract_list_response_reads_rows_and_total():
    rows, total = bit_zying_api_poc._extract_list_response(
        {"data": {"list": {"data": [_row(1)], "maxcount": "403210"}}}
    )

    assert rows[0]["id"] == 1
    assert total == 403210


def test_fetch_product_index_stops_at_requested_unique_limit():
    calls = []

    def api_call(command, payload):
        calls.append((command, payload))
        page = payload["page"]
        start = (page - 1) * 60 + 1
        rows = [_row(product_id) for product_id in range(start, start + 60)]
        return {"data": {"list": {"data": rows, "maxcount": 400000}}}

    records, total = bit_zying_api_poc.fetch_product_index(
        api_call,
        limit=100,
        page_size=60,
    )

    assert len(records) == 100
    assert len({record["product_id"] for record in records}) == 100
    assert records[0]["title"] == "Product 1"
    assert records[-1]["product_id"] == "100"
    assert total == 400000
    assert [payload["page"] for _, payload in calls] == [1, 2]
    assert all(command == "sale.stat" for command, _ in calls)
    assert all(payload["from"] == 8 for _, payload in calls)


def test_enrich_product_details_keeps_failure_and_caches_category():
    calls = []
    records = [
        {
            **bit_zying_api_poc._normalize_list_row(_row(1), 1, "2026-08-11 12:00:00"),
        },
        {
            **bit_zying_api_poc._normalize_list_row(_row(2), 1, "2026-08-11 12:00:00"),
        },
    ]

    def api_call(command, payload):
        calls.append((command, payload))
        if command == "sale.detail" and str(payload["id"]) == "2":
            raise RuntimeError("temporary failure")
        if command == "sale.detail":
            return {
                "data": {
                    "root": [
                        {
                            "sale_id": payload["id"],
                            "sale_cur": "USD",
                            "sale_cost": 12.5,
                            "sale_netproceed": 5,
                            "sale_weight": 100,
                            "sale_size": [1, 2, 3],
                            "sale_stat": 1000,
                            "sale_pic": ["https://example.com/main.jpg"],
                            "sale_siteid": 1,
                            "sale_area": "CBT",
                            "sale_attrs": '{"1":{"site":"CBT","kindid":430974}}',
                        }
                    ]
                }
            }
        if command == "meli_category.detail":
            return {
                "data": {
                    "root": [
                        {
                            "cate_cateid": "CBT430974",
                            "cate_fullname": "Home / Test",
                            "cate_fullzh": "家居 / 测试",
                        }
                    ]
                }
            }
        raise AssertionError(command)

    result = bit_zying_api_poc.enrich_product_details(api_call, records)

    assert result[0]["detail_ok"] is True
    assert result[0]["product_category_id"] == "CBT430974"
    assert result[1]["detail_ok"] is False
    assert "temporary failure" in result[1]["error_message"]
    assert sum(command == "meli_category.detail" for command, _ in calls) == 1


def _selection_row(product_id):
    return {
        "Sku": product_id,
        "Title": f"Selection {product_id}",
        "Thumb": f"https://example.com/{product_id}.webp",
        "Url": f"https://example.com/items/{product_id}",
        "Price": 418,
        "Netproceed": 348,
        "Orders": 750000,
        "Order_7": 7,
        "Order_14": 14,
        "Order_30": 30,
        "Brand": "Brand",
        "Sellerid": 123,
        "Sellername": "Seller",
        "Comment": 99,
        "Rate": 4.8,
        "Stock": 5,
        "Status": 1,
        "Storagetype": 2,
        "Cateid": "MLM167994",
        "CateFullName": "Sports / Supplements",
        "Uptime": "2022-04-28T19:19:13",
    }


def test_fetch_selection_index_collects_100_from_two_pages():
    calls = []

    def api_call(method, payload):
        calls.append((method, payload))
        start = (payload["page"] - 1) * 60 + 1
        rows = [_selection_row(f"MLM{item}") for item in range(start, start + 60)]
        return {"code": 200, "data": {"Datas": rows, "Total": 2000}}

    records, total = bit_zying_api_poc.fetch_selection_index(
        api_call,
        limit=100,
        page_size=60,
        site_id="1",
    )

    assert len(records) == 100
    assert len({(row["site_id"], row["product_id"]) for row in records}) == 100
    assert records[0]["price"] == 418
    assert records[0]["category_id"] == "MLM167994"
    assert records[-1]["product_id"] == "MLM100"
    assert total == 2000
    assert [payload["page"] for _, payload in calls] == [1, 2]
    assert all(method == "getMeliItemsByPage" for method, _ in calls)


def test_enrich_selection_details_retains_list_fields():
    record = bit_zying_api_poc._normalize_selection_row(
        _selection_row("MLM1"),
        page_number=1,
        site_id="1",
        collected_at="2026-08-11 12:00:00",
    )

    def api_call(method, payload):
        assert method == "getMeliItemDetail"
        assert payload == {"sku": "MLM1"}
        return {
            "code": 200,
            "data": {
                "Sku": "MLM1",
                "Title": "Detailed title",
                "Price": 420,
                "Size": [11, 10, 17],
                "Weight": 540,
                "CatePremium": 20,
                "Official": False,
            },
        }

    bit_zying_api_poc.enrich_selection_details(api_call, [record])

    assert record["detail_ok"] is True
    assert record["title"] == "Detailed title"
    assert record["price"] == 420
    assert record["net_income"] == 348
    assert record["size"] == "11 x 10 x 17"
    assert record["weight"] == 540
