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
    assert client.update_global_item.call_args.args[1] == (
        {"status": "paused"} if status == "under_review" else {"deleted": True}
    )


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
    monkeypatch.setattr(remote, "_delete_one_link", lambda row, token: {
        "link_id": row["id"], "status": "success" if row["id"] == 1 else "error",
        "errors": [] if row["id"] == 1 else ["Listing is not modifiable"],
    })
    result = remote.run_store_link_remote_update([
        {"id": 1, "token_id": 1, "item_id": "MLM1"},
        {"id": 2, "token_id": 1, "item_id": "MLM2"},
    ], {"status": "deleted"})
    assert result["running"] is False
    assert result["success_count"] == 1
    assert result["failed_count"] == 1
    assert "删除" in result["message"]


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
