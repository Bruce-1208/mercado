from bit import bit_runtime_lock
from bit import bit_store_link_sync as sync
from bit import store_link_item_lock as locking
import pytest


def test_sync_holds_item_locks_from_detail_read_through_both_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(sync, "STORE_LINK_WRITE_BATCH_SIZE", 200)
    batches = []

    def assert_locked(item_id):
        assert bit_runtime_lock.get_lock_owner(f"mercado_listing_74_{item_id}")

    class Client:
        def request(self, method, path):
            if path == "/marketplace/users/seller":
                return {"marketplaces": [{"user_id": "child", "site_id": "MLM"}]}
            return {}

        def iter_listing_ids(self, seller, status):
            return [f"MLM{i}" for i in range(10)] if status == "active" else []

        def get_marketplace_item(self, item_id, **kwargs):
            assert_locked(item_id)
            return {"id": item_id, "status": "active", "price": 10}

    def replace(record, items, **kwargs):
        for item in items:
            assert_locked(item["id"])
        batches.append(len(items))
        return {"total": len(items), "updated": len(items)}

    def products(record, items):
        for item in items:
            assert_locked(item["id"])
        return {"count": len(items)}

    monkeypatch.setattr(sync, "_client_and_token", lambda row: (Client(), row))
    monkeypatch.setattr(sync, "replace_store_snapshot", replace)
    monkeypatch.setattr(sync, "upsert_pulled_store_links_to_products", products)
    monkeypatch.setattr(sync, "finalize_store_snapshot", lambda *args: None)
    result = sync._sync_store({"id": 74, "meli_user_id": "seller"})
    assert result["stored"] == 10
    assert batches == [8, 2]
    assert not list((tmp_path / "locks").glob("*.lockdir"))


def test_partial_lock_acquisition_failure_releases_previous_items(monkeypatch):
    released = []

    class Lease:
        def __init__(self, key, **kwargs): self.key = key
        def acquire(self, **kwargs): return self.key.endswith("MLM1")
        def release(self): released.append(self.key)

    monkeypatch.setattr(locking, "InterProcessLock", Lease)
    with pytest.raises(TimeoutError):
        with locking.store_link_item_locks(74, ["MLM2", "MLM1"], owner="test"):
            pytest.fail("must not enter without all locks")
    assert released == ["mercado_listing_74_MLM1"]
