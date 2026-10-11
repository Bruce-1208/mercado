"""Serialize snapshot reads/writes and remote edits only for the same listings."""
from contextlib import contextmanager

from bit.bit_runtime_lock import InterProcessLock


@contextmanager
def store_link_item_locks(token_id, item_ids, *, owner):
    # Stable ordering prevents deadlocks for overlapping snapshot batches.
    keys = sorted({f"mercado_listing_{int(token_id)}_{str(item).strip().upper()}"
                   for item in item_ids})
    held = []
    try:
        for key in keys:
            lease = InterProcessLock(key, owner=owner)
            if not lease.acquire(timeout=120):
                raise TimeoutError("该商品正在同步或修改，等待超过 120 秒，请稍后重试")
            held.append(lease)
        yield
    finally:
        for lease in reversed(held):
            lease.release()
