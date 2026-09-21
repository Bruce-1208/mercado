import pytest

from bit import bit_zying_infringement as review


class FakeZyingApi:
    def __init__(self, rows, change_before_write=None):
        self.rows = {str(row["id"]): dict(row) for row in rows}
        self.change_before_write = str(change_before_write or "")
        self.rechecked = set()
        self.updates = []

    def __call__(self, command, payload):
        if command == "sale.update":
            product_id = str(payload["sale_id"])
            assert self.rows[product_id]["sale_stat"] == review.PENDING_STATUS
            self.rows[product_id]["sale_stat"] = payload["sale_stat"]
            self.updates.append((product_id, payload["sale_stat"]))
            return {}

        assert command == "sale.stat"
        wanted_status = int(payload["stat"])
        ids = {str(value) for value in payload.get("ids") or []}
        if ids:
            product_id = next(iter(ids))
            if product_id == self.change_before_write and product_id not in self.rechecked:
                self.rows[product_id]["sale_stat"] = review.APPROVED_STATUS
                self.rechecked.add(product_id)
            rows = [self.rows[value] for value in ids if value in self.rows]
        else:
            rows = list(self.rows.values()) if payload["page"] == 1 else []
        return {"list": {"data": [row for row in rows if row["sale_stat"] == wanted_status]}}


def test_reviews_fixed_batches_and_only_updates_rows_still_pending():
    rows = [
        {
            "id": index,
            "title": f"<b>Producto {index}</b>",
            "thumb": f"https://img.test/{index}.jpg",
            "sale_stat": review.PENDING_STATUS,
        }
        for index in range(1, 46)
    ]
    rows.append({"id": 99, "title": "Already approved", "thumb": "x", "sale_stat": 1000})
    api = FakeZyingApi(rows, change_before_write=2)
    batches = []

    def classifier(records):
        batches.append([dict(record) for record in records])
        return [
            {
                "row_id": record["row_id"],
                "record_key": record["record_key"],
                "risk_level": 1 if int(record["product_id"]) % 3 == 0 else 0,
                "keywords": ["Brand"] if int(record["product_id"]) % 3 == 0 else [],
            }
            for record in records
        ]

    result = review.review_pending_products(
        auth_token="test-token",
        start_page=1,
        end_page=2,
        api_call=api,
        classifier=classifier,
    )

    assert [len(batch) for batch in batches] == [20, 20, 5]
    assert batches[0][0] == {
        "row_id": "1",
        "record_key": "1",
        "product_id": "1",
        "title": "Producto 1",
        "main_image_url": "https://img.test/1.jpg",
        "page_number": 1,
    }
    assert result["pending_count"] == 45
    assert result["checked_count"] == 44
    assert result["skipped_changed_count"] == 1
    assert result["approved_count"] == 29
    assert result["suspected_count"] == 15
    assert "2" not in {product_id for product_id, _status in api.updates}
    assert "99" not in {product_id for product_id, _status in api.updates}
    assert api.rows["3"]["sale_stat"] == review.SUSPECTED_STATUS
    assert api.rows["4"]["sale_stat"] == review.APPROVED_STATUS


def test_incomplete_deepseek_batch_never_writes_status():
    api = FakeZyingApi([
        {"id": 7, "title": "One", "thumb": "one.jpg", "sale_stat": review.PENDING_STATUS},
        {"id": 8, "title": "Two", "thumb": "two.jpg", "sale_stat": review.PENDING_STATUS},
    ])

    def incomplete(records):
        return [{"row_id": records[0]["row_id"], "risk_level": 0}]

    try:
        review.review_pending_products(
            auth_token="test-token",
            api_call=api,
            classifier=incomplete,
        )
    except ValueError as exc:
        assert "未完整返回" in str(exc)
    else:
        raise AssertionError("DeepSeek 结果不完整时应停止")

    assert api.updates == []


def test_product_cursor_is_inclusive_and_limits_review_count():
    rows = [
        {"id": index, "title": f"Product {index}", "thumb": "x.jpg",
         "sale_stat": review.PENDING_STATUS}
        for index in range(1, 11)
    ]
    api = FakeZyingApi(rows)
    classified = []

    def classifier(records):
        classified.extend(row["product_id"] for row in records)
        return [
            {"row_id": row["row_id"], "record_key": row["record_key"], "risk_level": 0}
            for row in records
        ]

    result = review.review_pending_products(
        auth_token="test-token",
        start_page=1,
        end_page=10,
        start_product_id="4",
        max_items=3,
        api_call=api,
        classifier=classifier,
    )

    assert classified == ["4", "5", "6"]
    assert [product_id for product_id, _ in api.updates] == ["4", "5", "6"]
    assert result["pending_count"] == 3
    assert result["checked_count"] == 3


def test_missing_product_cursor_does_not_write_any_status():
    api = FakeZyingApi([
        {"id": 1, "title": "One", "thumb": "x.jpg", "sale_stat": review.PENDING_STATUS},
    ])
    with pytest.raises(ValueError, match="未找到起始产品编号"):
        review.review_pending_products(
            auth_token="test-token", start_page=1, end_page=2,
            start_product_id="999", max_items=1, api_call=api,
            classifier=lambda rows: [],
        )
    assert api.updates == []


def test_list_payload_always_filters_pending_status():
    payload = review._list_payload(
        3,
        category="202170568",
        product_developer_id="17",
        ids=["123"],
    )
    assert payload["stat"] == review.PENDING_STATUS
    assert payload["localid"] == "202170568"
    assert payload["loginid"] == 17
    assert payload["ids"] == [123]
