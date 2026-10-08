"""Bounded, process-local snapshots for expensive daily inbox calculations."""

import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor


class SnapshotCache:
    def __init__(self, ttl=60, capacity=128, workers=2):
        self.ttl = ttl
        self.capacity = capacity
        self._entries = OrderedDict()
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="today-preload")

    def read(self, key, loader, *, background=False, force=False):
        """Return (value, refreshing); share work across concurrent readers."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) >= self.capacity:
                    victim = next((k for k, e in self._entries.items() if e['future'] is None), None)
                    if victim is None and background:
                        # Backpressure: retry on the next poll without failing the page.
                        return None, True
                    if victim is None:
                        raise RuntimeError("今日必办正在预加载，请稍后重试")
                    del self._entries[victim]
                entry = {'value': None, 'at': 0, 'future': None, 'error': None}
                self._entries[key] = entry
            self._entries.move_to_end(key)
            if (entry['error'] is not None and not force and entry['future'] is None
                    and time.monotonic() - entry['at'] < self.ttl):
                raise entry['error']
            fresh = entry['value'] is not None and time.monotonic() - entry['at'] < self.ttl
            if fresh and not force and entry['future'] is None:
                return entry['value'], False
            future = entry['future']
            leader = future is None
            if leader:
                future = entry['future'] = Future()
            value = entry['value']

        def load():
            try:
                result = loader()
            except Exception as exc:
                with self._lock:
                    entry.update(error=exc, at=time.monotonic(), future=None)
                future.set_exception(exc)
            else:
                with self._lock:
                    entry.update(value=result, at=time.monotonic(), future=None, error=None)
                future.set_result(result)

        if leader:
            if background:
                self._executor.submit(load)
            else:
                load()
        if background:
            if future.done():
                return future.result(), False
            return value, True
        return future.result(), False
