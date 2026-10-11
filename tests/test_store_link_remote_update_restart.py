from copy import deepcopy
from decimal import Decimal
import sqlite3

import pytest

from bit import bit_runtime_lock
from bit import bit_store_link_remote_update as update


def link(link_id):
    return {"id": link_id, "token_id": 74, "item_id": f"MLM{3308393900 + link_id}",
            "store_name": "测试店铺", "status": "active", "price": 5,
            "weight_g": 200, "package_length_cm": 10, "package_width_cm": 10,
            "package_height_cm": 10}


def result(row):
    return {"link_id": row["id"], "item_id": row["item_id"], "status": "success",
            "applied_fields": ["price"], "remote_groups": ["售价"], "errors": []}


@pytest.fixture
def queue(tmp_path, monkeypatch):
    path = tmp_path / "remote-updates.sqlite3"
    monkeypatch.setenv("MERCADO_STORE_LINK_REMOTE_UPDATE_PATH", str(path))
    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path / "locks")
    state = deepcopy(update._update_state)
    state.update(running=False, task_id="", status="idle", results=[], logs=[])
    monkeypatch.setattr(update, "_update_state", state)
    monkeypatch.setattr(update, "start_store_link_remote_update_recovery", lambda: True)
    monkeypatch.setattr(update, "get_store_links_by_ids", lambda ids: [link(value) for value in ids])
    monkeypatch.setattr(update.bit_mysql, "get_mercado_store_token",
                        lambda token_id: {"id": token_id, "access_token": "fresh-secret"})
    monkeypatch.setattr(update, "_client_and_token", lambda token: (None, token))
    return path


def reset_process_state(monkeypatch):
    state = deepcopy(update._update_state)
    state.update(running=False, task_id="", status="idle", results=[], logs=[])
    monkeypatch.setattr(update, "_update_state", state)


def test_submission_is_committed_before_worker_start_without_credentials(queue, monkeypatch):
    def started():
        with sqlite3.connect(queue) as database:
            assert database.execute("SELECT status FROM tasks").fetchone()[0] == "queued"
            assert database.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
        return True

    monkeypatch.setattr(update, "start_store_link_remote_update_recovery", started)
    monkeypatch.setattr(update, "get_store_links_by_ids", lambda ids: [
        {**link(value), "access_token": "must-not-persist", "remote_json": "large unneeded data"}
        for value in ids
    ])
    accepted, state = update.start_store_link_remote_update([18, 19], {"price": Decimal("12.500000001")})
    assert accepted is True
    task = update._load_task(state["task_id"])
    assert task["changes"]["price"] == Decimal("12.500000001")
    assert all("access_token" not in row and "remote_json" not in row for row in task["rows"])


def test_restart_skips_checkpointed_items_and_restores_original_task_progress(queue, monkeypatch):
    accepted, original = update.start_store_link_remote_update([18, 19], {"price": "12.5"})
    assert accepted
    update._checkpoint_item(original["task_id"], result(link(18)))
    update._state_update(status="running")
    update._persist_task_state(original["task_id"])
    reset_process_state(monkeypatch)
    calls = []

    def change(row, token, changes):
        calls.append((row["id"], token["access_token"], changes["price"]))
        return result(row)

    monkeypatch.setattr(update, "_update_one_link", change)
    assert update.resume_pending_store_link_remote_updates() is True
    state = update.store_link_remote_update_status()
    assert calls == [(19, "fresh-secret", Decimal("12.5"))]
    assert state["task_id"] == original["task_id"]
    assert state["status"] == "completed"
    assert state["total_links"] == state["processed_links"] == state["success_count"] == 2
    assert update.resume_pending_store_link_remote_updates() is False


def test_cross_process_submission_reuses_pending_task(queue, monkeypatch):
    accepted, first = update.start_store_link_remote_update([18], {"price": "12.5"})
    assert accepted
    # A second process has independent memory but shares the durable queue.
    reset_process_state(monkeypatch)
    accepted, duplicate = update.start_store_link_remote_update([19], {"price": "20"})
    assert accepted is False
    assert duplicate["task_id"] == first["task_id"]
    with sqlite3.connect(queue) as database:
        assert database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert database.execute("SELECT link_id FROM items").fetchone()[0] == 18


def test_stale_process_memory_does_not_block_submission_after_another_worker_finishes(queue, monkeypatch):
    _, first = update.start_store_link_remote_update([18], {"price": "12.5"})
    stale_state = deepcopy(update._update_state)
    monkeypatch.setattr(update, "_update_one_link", lambda row, *_args: result(row))
    assert update.resume_pending_store_link_remote_updates()
    monkeypatch.setattr(update, "_update_state", stale_state)

    assert update.store_link_remote_update_status()["status"] == "completed"
    accepted, second = update.start_store_link_remote_update([19], {"price": "20"})
    assert accepted
    assert second["task_id"] != first["task_id"]


def test_singleton_drain_does_not_compete_with_a_live_recovery_worker(queue, monkeypatch):
    update.start_store_link_remote_update([18], {"price": "12.5"})
    lease = bit_runtime_lock.InterProcessLock(update.REMOTE_UPDATE_RECOVERY_LOCK_KEY, owner="other-worker")
    assert lease.acquire()
    monkeypatch.setattr(update, "_update_one_link", lambda *_args: pytest.fail("another drain owns recovery"))
    try:
        assert update.resume_pending_store_link_remote_updates() is False
    finally:
        lease.release()


def test_uncheckpointed_remote_write_is_replayed_as_an_absolute_target(queue, monkeypatch):
    remote_price = Decimal("5")
    writes = []
    local = {}

    class Client:
        def __init__(self, _token):
            pass

        def update_global_item(self, item_id, payload):
            nonlocal remote_price
            remote_price = Decimal(str(payload["price"]))
            writes.append((item_id, remote_price))

        def get_marketplace_item(self, item_id, **_kwargs):
            return {"id": item_id, "price": remote_price, "currency_id": "USD", "status": "active"}

    monkeypatch.setattr(update, "MercadoLibreClient", Client)
    monkeypatch.setattr(update, "bulk_update_store_links", lambda ids, changes: local.update(changes))
    monkeypatch.setattr(update, "sync_pulled_product_fields_from_store_links", lambda *_args: None)
    accepted, state = update.start_store_link_remote_update([18], {"price": "9.99"})
    assert accepted
    checkpoint = update._checkpoint_item
    monkeypatch.setattr(update, "_checkpoint_item", lambda *_args: (_ for _ in ()).throw(RuntimeError("crash before checkpoint")))
    assert update.resume_pending_store_link_remote_updates()
    assert remote_price == Decimal("9.99")
    assert update._load_task(state["task_id"])["status"] == "queued"

    monkeypatch.setattr(update, "_checkpoint_item", checkpoint)
    reset_process_state(monkeypatch)
    assert update.resume_pending_store_link_remote_updates()
    assert [value for _item, value in writes] == [Decimal("9.99"), Decimal("9.99")]
    assert remote_price == local["price"] == Decimal("9.99")
    assert update.store_link_remote_update_status()["status"] == "completed"


def test_recovered_delete_verifies_existing_remote_deletion_before_local_write(queue, monkeypatch):
    writes = []
    deleted = []

    class Client:
        def __init__(self, _token):
            pass

        def get_marketplace_item(self, item_id):
            return {"id": item_id, "status": "closed", "deleted": True}

        def update_global_item(self, *_args):
            writes.append(_args)

    monkeypatch.setattr(update, "MercadoLibreClient", Client)
    monkeypatch.setattr(update, "mark_store_links_deleted", lambda ids: deleted.extend(ids))
    accepted, original = update.start_store_link_remote_delete([18])
    assert accepted
    reset_process_state(monkeypatch)
    assert update.resume_pending_store_link_remote_updates()
    assert deleted == [18]
    assert writes == []
    assert update.store_link_remote_update_status()["task_id"] == original["task_id"]
    assert update.store_link_remote_update_status()["status"] == "completed"


def test_result_checkpoint_reconstructs_progress_after_final_state_write_is_interrupted(queue, monkeypatch):
    _, original = update.start_store_link_remote_update([18, 19], {"price": "12.5"})
    update._checkpoint_item(original["task_id"], result(link(18)))
    update._checkpoint_item(original["task_id"], result(link(19)))
    reset_process_state(monkeypatch)
    monkeypatch.setattr(update, "_update_one_link", lambda *_args: pytest.fail("all items were checkpointed"))
    assert update.resume_pending_store_link_remote_updates()
    assert update.store_link_remote_update_status()["success_count"] == 2
    assert update.store_link_remote_update_status()["status"] == "completed"


def test_generic_recovery_failure_is_paced_and_releases_singleton_lease(queue, monkeypatch):
    delays = []
    monkeypatch.setattr(update, "_resume_next_task", lambda: (_ for _ in ()).throw(RuntimeError("database temporarily unavailable")))

    class InterruptedWait:
        def wait(self, seconds):
            delays.append(seconds)
            raise SystemExit("end this test loop")

    monkeypatch.setattr(update, "_recovery_wakeup", InterruptedWait())
    with pytest.raises(SystemExit):
        update._remote_update_recovery_loop()
    assert delays == [30]
    assert not bit_runtime_lock.get_lock_owner(update.REMOTE_UPDATE_RECOVERY_LOCK_KEY)


def test_remote_edit_does_not_wait_for_whole_store_sync(queue, monkeypatch):
    from bit.bit_store_link_sync import STORE_LINK_SYNC_LOCK_KEY
    lease = bit_runtime_lock.InterProcessLock(STORE_LINK_SYNC_LOCK_KEY, owner="full-sync")
    assert lease.acquire()
    monkeypatch.setattr(update, "_update_one_link", lambda row, *_args: result(row))
    try:
        accepted, _ = update.start_store_link_remote_update([18], {"price": "12.5"})
        assert accepted
        assert update.resume_pending_store_link_remote_updates()
        assert update.store_link_remote_update_status()["status"] == "completed"
    finally:
        lease.release()


def test_same_item_waits_until_snapshot_is_written_but_other_items_continue(queue, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from bit.store_link_item_lock import store_link_item_locks

    entered = threading.Event()
    release = threading.Event()
    writes = []

    def sync_snapshot():
        with store_link_item_locks(74, [link(18)["item_id"]], owner="sync"):
            entered.set()
            assert release.wait(5)
            writes.append("old snapshot")

    def edit(row, *_args):
        writes.append(row["id"])
        return result(row)

    monkeypatch.setattr(update, "_update_one_link", edit)
    with ThreadPoolExecutor(max_workers=3) as pool:
        sync = pool.submit(sync_snapshot)
        assert entered.wait(2)
        same = pool.submit(update._update_one_link_locked, link(18), {}, {})
        other = pool.submit(update._update_one_link_locked, link(19), {}, {})
        try:
            assert other.result(timeout=2)["status"] == "success"
            assert not same.done()
        finally:
            release.set()
        sync.result(timeout=2)
        same.result(timeout=2)
    assert writes.index("old snapshot") < writes.index(18)
    assert not list((queue.parent / "locks").glob("mercado_listing_*.lockdir"))
