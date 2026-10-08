"""Bounded, shared background reads so large store histories do not time out HTTP."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
import time


class CommunicationPreload:
    def __init__(self, workers=8, ttl=3600, capacity=512):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="communication-preload")
        self.ttl = ttl
        self.capacity = capacity
        self.lock = Lock()
        self.entries = {}

    @staticmethod
    def _query(query):
        try:
            result = {"data": query()}
        except Exception as exc:
            result = {"error": str(exc)}
        return {**result, "finished": time.monotonic(),
                "updated_at": datetime.now(timezone.utc).isoformat()}

    def read(self, key, query, *, refresh=False):
        with self.lock:
            now = time.monotonic()
            entry = self.entries.get(key)
            completed = entry["future"].result() if entry and entry["future"].done() else None
            expired = completed and now - completed["finished"] >= self.ttl
            if entry is None or ((refresh or expired) and entry["future"].done()):
                # Never evict running work: repeated polls must join the same read.
                if entry is None and len(self.entries) >= self.capacity:
                    for old_key, old in list(self.entries.items()):
                        if old["future"].done():
                            del self.entries[old_key]
                            break
                    else:
                        return {"pending": True}
                entry = {"future": self.executor.submit(self._query, query)}
                self.entries[key] = entry
            future = entry["future"]
            if not future.done():
                return {"pending": True}
            return {key: value for key, value in future.result().items() if key != "finished"}


customer_service_preload = CommunicationPreload()

# Long message-history scans must not occupy the claims queue.
customer_service_claims_preload = CommunicationPreload()

# Local translation can take longer than a web request. Poll the same bounded
# job instead of tying up web/DB request threads or restarting work on retry.
claim_translation_preload = CommunicationPreload(workers=2, capacity=256)
