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


def read_authorized(loader, params, *, user, refresh=False):
    """Share identical resolved order queries within an organization.

    Call only after authentication and after store, salesperson and warehouse
    restrictions have been resolved into params. list_orders is a pure read of
    those params: account identity does not change its returned fields. The
    complete params remain in the cache key; a permission change therefore uses
    a different entry on the next request. Unknown tenant contexts retain the
    original per-user isolation.
    """
    organization = (user or {}).get("organization_key")
    scope = ({"organization_key": organization, "resolved_orders": 1}
             if organization and (user or {}).get("id") else user)
    return read(loader, params, scope=scope, refresh=refresh)


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
        flight = _FLIGHTS.setdefault(key, [threading.Lock(), 0, 0])
        observed_completion = flight[2]
        flight[1] += 1
    try:
        with flight[0]:
            with _LOCK:
                generation = _GENERATION
                cached = _CACHE.get(key)
                # A refresh bypasses a result that already existed on arrival,
                # but can share a fresh result completed while it was waiting.
                # Invalidation clears the cache, so queued readers cannot reuse
                # a snapshot from before a write, even if a flight completed.
                hit = (cached and time.monotonic() - cached[0] < 10
                       and (not refresh or flight[2] > observed_completion))
            if hit:
                return deepcopy(cached[1])
            result = loader(**params)
            # Copy outside the global lock so large pages do not serialize
            # cache hits for other accounts. Cached objects are never mutated.
            snapshot = deepcopy(result)
            with _LOCK:
                if generation == _GENERATION:
                    _CACHE[key] = (time.monotonic(), snapshot)
                    flight[2] += 1
                    _CACHE.move_to_end(key)
                    while len(_CACHE) > 64:
                        _CACHE.popitem(last=False)
            return result
    finally:
        with _LOCK:
            flight[1] -= 1
            if not flight[1]:
                del _FLIGHTS[key]
