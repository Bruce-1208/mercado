import threading
from unittest.mock import patch

import pytest

from erp import mercadolibre_batch_publish as batch_publish


def _rows():
    return [
        {"id": 11, "source_item_id": "MLM111", "source_url": "https://example/MLM111", "review_status": "approved"},
        {"id": 12, "source_item_id": "MLM222", "source_url": "https://example/MLM222", "review_status": "approved"},
    ]


def test_batch_publish_continues_after_an_item_failure_and_records_each_state():
    state_calls = []
    record_create_calls = []
    record_update_calls = []
    simultaneous = threading.Barrier(2)
    thread_ids = set()

    def fake_follow_sell(client, source_url, **kwargs):
        assert kwargs["publish"] is True
        assert kwargs["source_from_database"] is True
        assert kwargs["quantity"] == 3
        assert kwargs["destination_site_id"] == "MLB"
        thread_ids.add(threading.get_ident())
        simultaneous.wait(timeout=2)
        if "MLM222" in source_url:
            raise RuntimeError("category rejected")
        return {"result": {"id": "CBT999"}}

    def create_records(rows, **metadata):
        record_create_calls.append((list(rows), metadata))
        return {11: 101, 12: 102}

    with patch.object(
        batch_publish,
        "_token_record",
        return_value={"display_name": "测试店铺", "access_token": "secret"},
    ), patch.object(batch_publish, "follow_sell", side_effect=fake_follow_sell):
        result = batch_publish.publish_product_batch(
            _rows(),
            token_id=7,
            site_id="MLB",
            quantity=3,
            workers=2,
            update_state=lambda product_id, **changes: state_calls.append((product_id, changes)),
            client=object(),
            batch_id="batch-001",
            created_by="测试用户",
            create_records=create_records,
            update_record=lambda record_id, **changes: record_update_calls.append((record_id, changes)),
        )

    assert result["published_count"] == 1
    assert result["failed_count"] == 1
    assert result["site_id"] == "MLB"
    assert result["site_name"] == "巴西"
    assert result["worker_count"] == 2
    assert len(thread_ids) == 2
    assert result["results"][0]["published_item_id"] == "CBT999"
    assert result["results"][1]["status"] == "failed"
    assert any(call[1]["status"] == "published" for call in state_calls)
    assert any(call[1]["status"] == "failed" for call in state_calls)
    assert result["batch_id"] == "batch-001"
    assert record_create_calls[0][1]["created_by"] == "测试用户"
    assert record_create_calls[0][1]["site_id"] == "MLB"
    assert any(record_id == 101 and changes["status"] == "published" for record_id, changes in record_update_calls)
    assert any(
        record_id == 102
        and changes["status"] == "failed"
        and "category rejected" in changes["failure_reason"]
        for record_id, changes in record_update_calls
    )


def test_batch_publish_validates_quantity_before_contacting_store():
    try:
        batch_publish.publish_product_batch(
            _rows(), token_id=7, quantity=0, update_state=lambda *args, **kwargs: None
        )
    except ValueError as exc:
        assert "1-9999" in str(exc)
    else:
        raise AssertionError("quantity validation was not applied")


def test_batch_publish_rejects_products_that_are_not_approved():
    rows = [{"id": 11, "source_item_id": "MLM111", "review_status": "risk"}]
    with pytest.raises(ValueError, match="只有审核状态为“通过”"):
        batch_publish.publish_product_batch(
            rows,
            token_id=7,
            update_state=lambda *args, **kwargs: None,
        )


def test_batch_publish_validates_worker_count_before_contacting_store():
    with pytest.raises(ValueError, match="1-8"):
        batch_publish.publish_product_batch(
            _rows(),
            token_id=7,
            workers=9,
            update_state=lambda *args, **kwargs: None,
        )


def test_local_store_cannot_publish_to_another_site():
    with patch.object(
        batch_publish,
        "_token_record",
        return_value={"site_id": "MLM", "access_token": "secret"},
    ), pytest.raises(ValueError, match="Global Selling"):
        batch_publish.publish_product_batch(
            _rows(),
            token_id=7,
            site_id="MLB",
            update_state=lambda *args, **kwargs: None,
            client=object(),
        )


def test_product_snapshot_is_refreshed_before_publish():
    row = {
        "source_item_id": "MLM111",
        "source_url": "https://example/MLM111",
        "main_image_url": "https://example/product.webp",
        "title": "Producto",
        "price": 10,
        "currency_id": "MXN",
        "category_id": "MLM123",
        "source_snapshot_json": '{"source":{"id":"MLM111"},"description":{}}',
    }

    with patch(
        "erp.mercadolibre_source_store.upsert_source_snapshot"
    ) as upsert_source_snapshot:
        batch_publish._sync_product_source_snapshot(row)

    saved = upsert_source_snapshot.call_args.args[0]
    assert saved["category_id"] == "MLM123"
    assert saved["main_image_url"] == "https://example/product.webp"


def test_successful_upload_is_not_reported_failed_when_latest_state_save_fails():
    row = _rows()[0]
    record_updates = []

    def update_state(_product_id, **changes):
        if changes.get("status") == "published":
            raise RuntimeError("database temporarily unavailable")

    with patch.object(
        batch_publish,
        "_token_record",
        return_value={"display_name": "测试店铺", "access_token": "secret"},
    ), patch.object(
        batch_publish,
        "follow_sell",
        return_value={"result": {"id": "CBT999"}},
    ):
        result = batch_publish.publish_product_batch(
            [row],
            token_id=7,
            update_state=update_state,
            client=object(),
            batch_id="batch-002",
            create_records=lambda rows, **metadata: {11: 103},
            update_record=lambda record_id, **changes: record_updates.append(
                (record_id, changes)
            ),
        )

    assert result["published_count"] == 1
    assert result["failed_count"] == 0
    assert "保存产品最新状态时出错" in result["results"][0]["message"]
    assert any(
        record_id == 103 and changes["status"] == "published"
        for record_id, changes in record_updates
    )
