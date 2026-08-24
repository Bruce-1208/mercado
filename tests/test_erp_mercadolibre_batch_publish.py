import threading
from unittest.mock import patch

import pytest

from erp import mercadolibre_batch_publish as batch_publish


def _rows():
    return [
        {"id": 11, "source_item_id": "MLM111", "source_url": "https://example/MLM111"},
        {"id": 12, "source_item_id": "MLM222", "source_url": "https://example/MLM222"},
    ]


def test_batch_publish_continues_after_an_item_failure_and_records_each_state():
    state_calls = []
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


def test_batch_publish_validates_quantity_before_contacting_store():
    try:
        batch_publish.publish_product_batch(
            _rows(), token_id=7, quantity=0, update_state=lambda *args, **kwargs: None
        )
    except ValueError as exc:
        assert "1-9999" in str(exc)
    else:
        raise AssertionError("quantity validation was not applied")


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
