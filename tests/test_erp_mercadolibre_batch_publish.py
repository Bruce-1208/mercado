import threading
import time
from unittest.mock import patch

import pytest

from erp import mercadolibre_batch_publish as batch_publish


def _rows():
    return [
        {"id": 11, "source_item_id": "MLM111", "source_url": "https://example/MLM111", "review_status": "approved", "weight_g": 350, "net_proceeds_usd": 10},
        {"id": 12, "source_item_id": "MLM222", "source_url": "https://example/MLM222", "review_status": "approved", "weight_g": 420, "net_proceeds_usd": 20},
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
        assert kwargs["net_proceeds"] in (9.5, 19.0)
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
        return_value={
            "display_name": "测试店铺",
            "access_token": "secret",
            "site_settings": [{"site_id": "MLB", "discount_rate": 95}],
        },
    ), patch.object(batch_publish, "follow_sell", side_effect=fake_follow_sell):
        result = batch_publish.publish_product_batch(
            _rows(),
            token_id=7,
            site_id="MLB",
            quantity=3,
            workers=20,
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
    assert result["discount_rate"] == 95
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
    rows = [{"id": 11, "source_item_id": "MLM111", "review_status": "risk", "weight_g": 100, "net_proceeds_usd": 5}]
    with pytest.raises(ValueError, match="只有审核状态为“通过”"):
        batch_publish.publish_product_batch(
            rows,
            token_id=7,
            update_state=lambda *args, **kwargs: None,
        )


def test_product_publish_issues_reports_all_local_blockers():
    assert batch_publish.product_publish_issues({
        "review_status": "unreviewed",
        "weight_g": None,
        "net_proceeds_usd": 0,
    }) == [
        "审核状态未通过",
        "未填写有效重量",
        "净收益小于等于 0",
    ]
    assert batch_publish.product_publish_issues({
        "review_status": "approved",
        "weight_g": 350,
        "net_proceeds_usd": 8,
    }) == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"weight_g": None}, "未填写有效重量"),
        ({"net_proceeds_usd": None}, "净收益尚未计算"),
        ({"net_proceeds_usd": -0.01}, "净收益小于等于 0"),
    ],
)
def test_batch_publish_rejects_missing_weight_or_nonpositive_net_before_store_call(
    changes, message,
):
    row = {**_rows()[0], **changes}
    with patch.object(batch_publish, "_token_record") as token_record, pytest.raises(
        ValueError, match=message
    ):
        batch_publish.publish_product_batch(
            [row],
            token_id=7,
            update_state=lambda *args, **kwargs: None,
        )
    token_record.assert_not_called()


def test_discounted_net_proceeds_uses_site_percentage_and_rounds_half_up():
    assert batch_publish.discounted_net_proceeds_usd(
        {"net_proceeds_usd": "7.60"}, "95"
    ) == 7.22
    assert batch_publish.site_discount_rate(
        {"site_settings": [{"site_id": "MLB", "discount_rate": None}]}, "MLB"
    ) == 100


def test_batch_publish_rejects_nonpositive_worker_count_before_contacting_store():
    with pytest.raises(ValueError, match="大于 0"):
        batch_publish.publish_product_batch(
            _rows(),
            token_id=7,
            workers=-1,
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
        "title": "Producto editado",
        "description_text": "Descripción editada",
        "price": 1299.9,
        "currency_id": "MXN",
        "category_id": "MLM123",
        "source_snapshot_json": (
            '{"source":{"id":"MLM111","title":"Título anterior",'
            '"price":999,"pictures":[{"source":"https://example/old.webp"}]},'
            '"description":{"plain_text":"Descripción anterior"}}'
        ),
    }

    with patch(
        "erp.mercadolibre_source_store.upsert_source_snapshot"
    ) as upsert_source_snapshot:
        batch_publish._sync_product_source_snapshot(row)

    saved = upsert_source_snapshot.call_args.args[0]
    assert saved["category_id"] == "MLM123"
    assert saved["main_image_url"] == "https://example/product.webp"
    assert saved["source"]["title"] == "Producto editado"
    assert saved["source"]["price"] == 1299.9
    assert saved["source"]["pictures"][0]["source"] == "https://example/product.webp"
    assert saved["description"] == {"plain_text": "Descripción editada"}


def test_embedded_product_snapshot_avoids_publish_write_read_round_trip():
    row = {
        **_rows()[0],
        "title": "Producto editado",
        "description_text": "Descripción editada",
        "price": 88,
        "currency_id": "MXN",
        "category_id": "MLM123",
        "package_length_cm": 20,
        "package_width_cm": 10,
        "package_height_cm": 5,
        "source_snapshot_json": (
            '{"source":{"id":"MLM111","site_id":"MLM",'
            '"title":"Anterior","pictures":[{"source":"https://example/p.jpg"}]},'
            '"description":{"plain_text":"Anterior"}}'
        ),
    }
    captured = {}

    def fake_follow_sell(_client, _source_url, **kwargs):
        captured.update(kwargs)
        return {
            "result": {"id": "CBT999"},
            "timings": {"source": 0.01, "publish": 0.02, "total": 0.03},
        }

    with patch.object(
        batch_publish,
        "_token_record",
        return_value={"display_name": "测试店铺", "access_token": "secret"},
    ), patch.object(
        batch_publish, "_sync_product_source_snapshot"
    ) as sync_snapshot, patch.object(
        batch_publish, "follow_sell", side_effect=fake_follow_sell
    ):
        result = batch_publish.publish_product_batch(
            [row],
            token_id=7,
            update_state=lambda *_args, **_kwargs: None,
            client=object(),
        )

    sync_snapshot.assert_not_called()
    source, description = captured["prepared_listing"]
    assert source["title"] == "Producto editado"
    assert source["price"] == 88
    assert description == {"plain_text": "Descripción editada"}
    assert any(
        attribute["id"] == "PACKAGE_WEIGHT"
        for attribute in source["attributes"]
    )
    assert result["average_stage_seconds"]["publish"] == 0.02
    assert result["elapsed_seconds"] >= 0


def test_successful_upload_is_not_reported_failed_when_latest_state_save_fails():
    row = _rows()[0]
    record_updates = []

    def update_state(_product_id, **changes):
        if changes.get("status") == "published":
            raise RuntimeError("database temporarily unavailable")

    with patch.object(
        batch_publish,
        "_token_record",
        return_value={
            "display_name": "测试店铺",
            "access_token": "secret",
            "site_settings": [{"site_id": "MLB", "discount_rate": 95}],
        },
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


def test_500_item_publish_orchestration_has_low_local_overhead():
    snapshot = (
        '{"source":{"site_id":"MLM","title":"Producto",'
        '"pictures":[{"source":"https://example/p.jpg"}]},'
        '"description":{}}'
    )
    rows = [
        {
            "id": index,
            "source_item_id": f"MLM{100000 + index}",
            "source_url": f"https://example/MLM{100000 + index}",
            "review_status": "approved",
            "weight_g": 300,
            "net_proceeds_usd": 10,
            "source_snapshot_json": snapshot,
        }
        for index in range(1, 501)
    ]

    def fake_follow_sell(_client, source_url, **_kwargs):
        return {
            "result": {"id": source_url.rsplit("/", 1)[-1]},
            "timings": {"publish": 0.001, "total": 0.001},
        }

    started = time.perf_counter()
    with patch.object(
        batch_publish,
        "_token_record",
        return_value={"display_name": "测试店铺", "access_token": "secret"},
    ), patch.object(batch_publish, "follow_sell", side_effect=fake_follow_sell):
        result = batch_publish.publish_product_batch(
            rows,
            token_id=7,
            workers=10,
            update_state=lambda *_args, **_kwargs: None,
            client=object(),
        )
    local_elapsed = time.perf_counter() - started

    assert result["published_count"] == 500
    assert result["failed_count"] == 0
    assert result["worker_count"] == 10
    # This guards only local scheduling/snapshot overhead; network API latency
    # is surfaced separately by the production stage timings.
    assert local_elapsed < 5
