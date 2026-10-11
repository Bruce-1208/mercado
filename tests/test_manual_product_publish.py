"""Human approval controls entry; individual publication still validates data."""
import json
from unittest.mock import patch

import pytest

from erp import mercadolibre_batch_publish as publisher


def imported_row(source, **changes):
    return {
        "id": 1, "source_item_id": "MLM123", "source_url": "https://example/MLM123",
        "source_type": source, "review_status": "approved", "weight_g": None,
        "net_proceeds_usd": 8,
        "source_snapshot_json": json.dumps({
            "source": {"title": "Reviewed product", "attributes": []},
            "page_snapshot": {"detail_supplement": "timeout", "detail_error": "旧采集超时"},
        }),
        **changes,
    }


@pytest.mark.parametrize("source", ["zying", "collected", "pulled"])
def test_approved_import_publishes_with_historical_collection_diagnostics(source):
    row = imported_row(source)
    states = []
    assert publisher.product_publish_issues(row) == []
    with (
        patch.object(publisher, "_token_record", return_value={"access_token": "test"}),
        patch.object(publisher, "_sync_product_source_snapshot"),
        patch.object(publisher, "follow_sell", return_value={"result": {"id": "CBT123"}}) as publish,
    ):
        result = publisher.publish_product_batch(
            [row], token_id=1, client=object(),
            update_state=lambda *args, **kw: states.append(kw),
        )
    assert result["published_count"] == 1
    assert publish.call_args.kwargs["net_proceeds"] == 8
    assert publish.call_args.kwargs["prepared_listing"][0]["title"] == "Reviewed product"
    assert states[-1]["status"] == "published"


@pytest.mark.parametrize("source", ["zying", "collected", "pulled"])
@pytest.mark.parametrize("status", ["unreviewed", "risk", "failed"])
def test_import_still_requires_human_approval(source, status):
    row = imported_row(source, review_status=status)
    assert publisher.product_publish_issues(row) == ["审核状态未通过"]
    with patch.object(publisher, "_token_record") as token, pytest.raises(ValueError, match="只有审核状态"):
        publisher.publish_product_batch([row], token_id=1, update_state=lambda *a, **k: None)
    token.assert_not_called()


@pytest.mark.parametrize("net", [None, 0, -1])
def test_bad_price_is_recorded_per_item_and_does_not_block_other_approved_imports(net):
    rows = [imported_row("zying", net_proceeds_usd=net), imported_row("pulled", id=2)]
    states, records = [], []
    with (
        patch.object(publisher, "_token_record", return_value={"access_token": "test"}),
        patch.object(publisher, "_sync_product_source_snapshot"),
        patch.object(publisher, "follow_sell", return_value={"result": {"id": "CBT123"}}) as publish,
    ):
        result = publisher.publish_product_batch(
            rows, token_id=1, client=object(),
            create_records=lambda rows, **kw: {1: 101, 2: 102},
            update_record=lambda record, **kw: records.append((record, kw)),
            update_state=lambda product, **kw: states.append((product, kw)),
        )
    assert result["failed_count"] == result["published_count"] == 1
    publish.assert_called_once()
    assert any(pid == 1 and state["status"] == "failed" and "净收益必须大于 0" in state["error_message"] for pid, state in states)
    assert any(rid == 101 and state["status"] == "failed" for rid, state in records)


def test_human_approval_keeps_infringement_gate():
    row = imported_row("collected", infringement_check_json=None)
    assert publisher.product_publish_issues(row) == ["侵权尚未检测"]
    assert publisher.product_publish_issues(row, defer_infringement=True) == []


def test_ai_original_keeps_its_preparation_requirements():
    issues = publisher.product_publish_issues(imported_row("ai_original"))
    assert "AI 原创任务尚未完成" in issues
    assert "未填写有效重量" in issues
