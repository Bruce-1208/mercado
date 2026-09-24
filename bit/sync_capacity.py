"""Shared admission budget for store synchronizations in the scheduler process.

Only store coordinators take slots. Their detail workers never take a second
store slot, avoiding nested-pool deadlocks. Existing per-module durable due
states and inter-process task locks remain responsible for recovery/deduplication.
"""
from contextlib import contextmanager
import os
import threading
import time


def _integer(name, default, minimum=1, maximum=64):
    try:
        return max(minimum, min(maximum, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


class SyncBudget:
    def __init__(self, limit=8, order_reserve=2):
        self.limit = max(1, int(limit))
        self.reserve = min(max(0, int(order_reserve)), self.limit - 1)
        self.condition = threading.Condition()
        self.active = 0
        self.background_active = 0
        self.keys = set()
        self.waiters = []
        self.completed = 0
        self.wait_seconds = 0.0

    @contextmanager
    def slot(self, kind, token_id):
        key = (str(kind), str(token_id))
        urgent = kind == "orders"
        ticket = object()
        started = time.monotonic()
        with self.condition:
            self.waiters.append((ticket, key, urgent))
            try:
                while True:
                    eligible = [w for w in self.waiters if w[1] not in self.keys and
                                (w[2] or self.background_active < self.limit - self.reserve)]
                    # FIFO within each class; reserved slots keep orders moving
                    # even when large catalog batches fill background capacity.
                    first = next((w for w in eligible if w[2]), eligible[0] if eligible else None)
                    if self.active < self.limit and first and first[0] is ticket:
                        self.waiters.remove((ticket, key, urgent))
                        self.keys.add(key)
                        self.active += 1
                        self.background_active += not urgent
                        self.wait_seconds += time.monotonic() - started
                        self.condition.notify_all()
                        break
                    self.condition.wait()
            except BaseException:
                self.waiters.remove((ticket, key, urgent))
                self.condition.notify_all()
                raise
        try:
            yield
        finally:
            with self.condition:
                self.keys.remove(key)
                self.active -= 1
                self.background_active -= not urgent
                self.completed += 1
                self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return dict(limit=self.limit, order_reserve=self.reserve, active=self.active,
                        waiting=len(self.waiters), completed=self.completed,
                        wait_seconds=round(self.wait_seconds, 3))


_budget = SyncBudget(_integer("MERCADO_SYNC_STORE_BUDGET", 8),
                     _integer("MERCADO_SYNC_ORDER_RESERVE", 2, 0))


def run_store(kind, token_id, call, *args, **kwargs):
    with _budget.slot(kind, token_id):
        return call(*args, **kwargs)


def snapshot():
    return _budget.snapshot()


def due_batch(token_ids):
    """Bound each automatic dispatch; remaining due rows persist for next tick."""
    return list(token_ids)[:_integer("MERCADO_SYNC_AUTO_BATCH_STORES", 8, maximum=100)]
