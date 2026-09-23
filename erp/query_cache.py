"""Small bounded cache for non-authoritative, scoped query totals."""
from collections import OrderedDict
from copy import deepcopy
import threading
import time


class QueryCache:
    def __init__(self, ttl=15, max_entries=512):
        self.ttl, self.max_entries = ttl, max_entries
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.generation = 0

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.generation += 1

    def get_or_load(self, key, load):
        with self.lock:
            entry = self.entries.get(key)
            if entry is not None and entry[0] > time.monotonic():
                self.entries.move_to_end(key)
                return deepcopy(entry[1])
            generation = self.generation
        # Do not serialize unrelated users, nor hold a cache lock during SQL.
        value = load()
        with self.lock:
            if generation == self.generation:
                self.entries[key] = (time.monotonic() + self.ttl, deepcopy(value))
                self.entries.move_to_end(key)
                while len(self.entries) > self.max_entries:
                    self.entries.popitem(last=False)
        return value
