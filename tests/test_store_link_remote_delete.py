from unittest.mock import Mock

import pytest

from bit import bit_store_link_remote_update as remote
from erp import mercadolibre_store_link_store as store


@pytest.mark.parametrize("final", [
    {"status": "closed", "sub_status": ["deleted"]},
    {"status": "deleted"},
    {"status": "closed", "deleted": True},
])
def test_delete_pauses_verifies_then_marks_local(monkeypatch, final):
    client = Mock()
    client.get_marketplace_item.side_effect = [
        {"id": "MLM123", "status": "active"},
        {"id": "MLM123", "status": "paused"},
        {"id": "MLM123", **final},
    ]
    monkeypatch.setattr(remote, "MercadoLibreClient", lambda _: client)
    mark = Mock()
    monkeypatch.setattr(remote, "mark_store_links_deleted", mark)
    result = remote._delete_one_link({"id": 1, "item_id": "MLM123"}, {"access_token": "fake"})
    assert result["status"] == "success"
    assert [call.args for call in client.update_global_item.call_args_list] == [
        ("MLM123", {"status": "paused"}), ("MLM123", {"deleted": True}),
    ]
    mark.assert_called_once_with([1])


def test_delete_waits_through_active_before_pause_confirmation(monkeypatch):
    monkeypatch.setattr(remote, "REMOTE_DELETE_VERIFY_DELAYS", (0, 0, 0))
    client = Mock()
    client.get_marketplace_item.side_effect = [
        {"id": "MLM123", "status": "active"},
        {"id": "MLM123", "status": "active"},
        {"id": "MLM123", "status": "paused"},
        {"id": "MLM123", "status": "deleted"},
    ]
    monkeypatch.setattr(remote, "MercadoLibreClient", lambda _: client)
    mark = Mock()
    monkeypatch.setattr(remote, "mark_store_links_deleted", mark)

    result = remote._delete_one_link({"id": 1, "item_id": "MLM123"}, {})

    assert result["status"] == "success"
    assert [call.args for call in client.update_global_item.call_args_list] == [
        ("MLM123", {"status": "paused"}),
        ("MLM123", {"deleted": True}),
    ]
    mark.assert_called_once_with([1])


def test_delete_waits_through_paused_before_deleted_confirmation(monkeypatch):
    monkeypatch.setattr(remote, "REMOTE_DELETE_VERIFY_DELAYS", (0, 0))
    client = Mock()
    client.get_marketplace_item.side_effect = [
        {"id": "MLM123", "status": "paused"},
        {"id": "MLM123", "status": "paused"},
        {"id": "MLM123", "status": "deleted"},
    ]
    monkeypatch.setattr(remote, "MercadoLibreClient", lambda _: client)
    mark = Mock()
    monkeypatch.setattr(remote, "mark_store_links_deleted", mark)

    result = remote._delete_one_link({"id": 1, "item_id": "MLM123"}, {})

    assert result["status"] == "success"
    client.update_global_item.assert_called_once_with("MLM123", {"deleted": True})
    mark.assert_called_once_with([1])


@pytest.mark.parametrize("status", ["under_review", "paused"])
def test_unconfirmed_deletion_never_marks_local(monkeypatch, status):
    client = Mock()
    client.get_marketplace_item.return_value = {"id": "MLM123", "status": status}
    monkeypatch.setattr(remote, "MercadoLibreClient", lambda _: client)
    mark = Mock()
    monkeypatch.setattr(remote, "mark_store_links_deleted", mark)
    result = remote._delete_one_link({"id": 1, "item_id": "MLM123"}, {})
    assert result["status"] == "error"
    mark.assert_not_called()
    if status == "under_review":
        client.update_global_item.assert_not_called()
        assert "审核" in result["errors"][0]
    else:
        assert client.update_global_item.call_args.args[1] == {"deleted": True}


def test_field_not_updatable_has_readable_failure_reason(monkeypatch):
    client = Mock()
    client.get_marketplace_item.return_value = {"id": "MLM123", "status": "active"}
    client.update_global_item.side_effect = RuntimeError(
        "400 validation_error: field_not_updatable: status")
    monkeypatch.setattr(remote, "MercadoLibreClient", lambda _: client)
    mark = Mock()
    monkeypatch.setattr(remote, "mark_store_links_deleted", mark)

    result = remote._delete_one_link({"id": 1, "item_id": "MLM123"}, {})

    assert result["status"] == "error"
    assert "平台当前不允许修改该链接" in result["errors"][0]
    assert "field_not_updatable" in result["errors"][0]
    client.update_global_item.assert_called_once_with("MLM123", {"status": "paused"})
    mark.assert_not_called()


def test_already_deleted_only_repairs_local(monkeypatch):
    client = Mock()
    client.get_marketplace_item.return_value = {"id": "MLM123", "status": "deleted"}
    monkeypatch.setattr(remote, "MercadoLibreClient", lambda _: client)
    mark = Mock()
    monkeypatch.setattr(remote, "mark_store_links_deleted", mark)
    assert remote._delete_one_link({"id": 1, "item_id": "MLM123"}, {})["status"] == "success"
    client.update_global_item.assert_not_called()
    mark.assert_called_once_with([1])


def test_delete_entry_dispatches_platform_job(monkeypatch):
    start = Mock(return_value=(True, {"running": True}))
    monkeypatch.setattr(remote, "start_store_link_remote_delete", start)
    assert store.delete_store_links([1]) == {"started": True, "state": {"running": True}}
    start.assert_called_once_with([1])


def test_delete_batch_reports_platform_rejection_and_success(monkeypatch):
    monkeypatch.setattr(remote.bit_mysql, "get_mercado_store_token", lambda _: {"access_token": "fake"})
    monkeypatch.setattr(remote, "_client_and_token", lambda token: (None, token))
    monkeypatch.setattr(remote, "record", lambda *args, **kwargs: None)
    def delete(row, _token):
        if row["id"] == 1:
            return {"link_id": row["id"], "status": "success", "errors": []}
        if row["id"] == 3:
            return {"link_id": row["id"], "status": "retained", "errors": [],
                    "retention_reason": "CBT 父商品不支持直接删除"}
        return {"link_id": row["id"], "status": "error",
                "errors": ["Listing is not modifiable"]}
    monkeypatch.setattr(remote, "_delete_one_link", delete)
    result = remote.run_store_link_remote_update([
        {"id": 1, "token_id": 1, "item_id": "MLM1"},
        {"id": 2, "token_id": 1, "item_id": "MLM2"},
        {"id": 3, "token_id": 1, "item_id": "CBT3"},
    ], {"status": "deleted"})
    assert result["running"] is False
    assert result["success_count"] == 1
    assert result["retained_count"] == 1
    assert result["failed_count"] == 1
    assert "删除" in result["message"] and "安全保留 1" in result["message"]


def test_delete_and_progress_share_worker():
    from bit.service_split import is_worker_path
    for path in ("/api/store-links/delete", "/api/db/store-links/delete",
                 "/api/store-links/bulk-update/status"):
        assert is_worker_path(path)


def test_failed_local_write_reports_platform_deletion(monkeypatch):
    client = Mock()
    client.get_marketplace_item.return_value = {"id": "MLM123", "status": "deleted"}
    monkeypatch.setattr(remote, "MercadoLibreClient", lambda _: client)
    monkeypatch.setattr(remote, "mark_store_links_deleted", Mock(side_effect=RuntimeError("db offline")))
    result = remote._delete_one_link({"id": 1, "item_id": "MLM123"}, {})
    assert result["status"] == "partial"
    assert "平台已删除" in result["errors"][0]


@pytest.mark.parametrize("site_id", ["MCO", "MPE", "MEC"])
def test_delete_accepts_non_ml_site_listing_ids(monkeypatch, site_id):
    item_id = f"{site_id}123456789"
    client = Mock()
    client.get_marketplace_item.return_value = {"id": item_id, "status": "deleted"}
    monkeypatch.setattr(remote, "MercadoLibreClient", lambda _: client)
    mark = Mock()
    monkeypatch.setattr(remote, "mark_store_links_deleted", mark)

    result = remote._delete_one_link(
        {"id": 7, "item_id": item_id, "site_id": site_id},
        {"access_token": "fake"},
    )

    assert result["status"] == "success"
    client.get_marketplace_item.assert_called_once_with(item_id)
    client.update_global_item.assert_not_called()
    mark.assert_called_once_with([7])


def test_delete_retains_cbt_parent_without_remote_or_local_write(monkeypatch):
    client = Mock(side_effect=AssertionError("CBT 父商品不应创建删除客户端"))
    monkeypatch.setattr(remote, "MercadoLibreClient", client)
    mark = Mock()
    monkeypatch.setattr(remote, "mark_store_links_deleted", mark)

    result = remote._delete_one_link(
        {"id": 8, "item_id": "CBT123456789", "site_id": "CBT"},
        {"access_token": "fake"},
    )

    assert result["status"] == "retained"
    assert "CBT" in result["retention_reason"]
    client.assert_not_called()
    mark.assert_not_called()


@pytest.mark.parametrize("row", [
    {"id": 9, "item_id": "MZZ123456789", "site_id": "MZZ"},
    {"id": 10, "item_id": "MCO123456789", "site_id": "MPE"},
])
def test_delete_rejects_unknown_or_mismatched_site_before_remote_call(monkeypatch, row):
    client = Mock(side_effect=AssertionError("未核验的站点商品不应调用删除接口"))
    monkeypatch.setattr(remote, "MercadoLibreClient", client)
    mark = Mock()
    monkeypatch.setattr(remote, "mark_store_links_deleted", mark)

    result = remote._delete_one_link(row, {"access_token": "fake"})

    assert result["status"] in {"error", "retained"}
    client.assert_not_called()
    mark.assert_not_called()
