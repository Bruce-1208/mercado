import json
import os
import time

from bit import bit_runtime_lock


def test_pid_is_running_for_current_process():
    assert bit_runtime_lock._pid_is_running(os.getpid()) is True


def test_stale_lock_from_dead_process_is_reclaimed(tmp_path, monkeypatch):
    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path)
    lock = bit_runtime_lock.InterProcessLock("stale-test")
    lock.lock_path.mkdir()
    lock.owner_path.write_text(
        json.dumps({"pid": 4_294_967_295, "token": "old"}),
        encoding="utf-8",
    )
    old_timestamp = time.time() - 10
    os.utime(lock.lock_path, (old_timestamp, old_timestamp))

    assert lock.acquire(timeout=0) is True
    assert lock.read_owner()["pid"] == os.getpid()
    lock.release()
    assert not lock.lock_path.exists()
