"""Short-lived, bounded, scope-keyed order reads; coalesce identical queries."""
from collections import OrderedDict
from copy import deepcopy
import json
import threading
import time

_CACHE = OrderedDict()
_LOCK = threading.Lock()
_FLIGHTS = {}
_GENERATION = 0


def invalidate():
    global _GENERATION
    with _LOCK:
        _GENERATION += 1
        _CACHE.clear()


def read(loader, params, *, scope=None, refresh=False):
    key = json.dumps([params, scope], sort_keys=True, default=str)
    # Each in-flight key owns its own lock. Hash stripes made unrelated users
    # and filters queue behind an expensive query merely due to a collision.
    with _LOCK:
        flight = _FLIGHTS.setdefault(key, [threading.Lock(), 0])
        flight[1] += 1
    try:
        with flight[0]:
            with _LOCK:
                generation = _GENERATION
                cached = _CACHE.get(key)
                hit = not refresh and cached and time.monotonic() - cached[0] < 10
            if hit:
                return deepcopy(cached[1])
            result = loader(**params)
            # Copy outside the global lock so large pages do not serialize
            # cache hits for other accounts. Cached objects are never mutated.
            snapshot = deepcopy(result)
            with _LOCK:
                if generation == _GENERATION:
                    _CACHE[key] = (time.monotonic(), snapshot)
                    _CACHE.move_to_end(key)
                    while len(_CACHE) > 64:
                        _CACHE.popitem(last=False)
            return result
    finally:
        with _LOCK:
            flight[1] -= 1
            if not flight[1]:
                del _FLIGHTS[key]
