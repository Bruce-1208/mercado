"""Concurrent refreshes share work, without crossing writes or permission scopes."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import Mock

from bit import order_read_cache as cache


def wait_for_readers(count):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with cache._LOCK:
            if sum(flight[1] for flight in cache._FLIGHTS.values()) == count:
                return
        time.sleep(.005)
    raise AssertionError('refresh readers did not overlap')


def test_overlapping_refreshes_replace_warm_cache_once_and_copy_results():
    cache.invalidate()
    cache.read(lambda: {'rows': [{'id': 'old'}]}, {}, scope={'id': 1})
    entered, release = threading.Event(), threading.Event()
    def load():
        entered.set()
        assert release.wait(3)
        return {'rows': [{'id': 'fresh'}]}
    loader = Mock(side_effect=load)
    with ThreadPoolExecutor(12) as pool:
        pending = [pool.submit(cache.read, loader, {}, scope={'id': 1}, refresh=True)
                   for _ in range(12)]
        try:
            assert entered.wait(2)
            wait_for_readers(12)
        finally:
            release.set()
        values = [future.result(timeout=3) for future in pending]
    assert loader.call_count == 1
    assert all(value == {'rows': [{'id': 'fresh'}]} for value in values)
    values[0]['rows'].clear()
    assert all(value['rows'] for value in values[1:])
    assert cache.read(lambda: None, {}, scope={'id': 1})['rows']
    assert not cache._FLIGHTS


def test_sequential_refreshes_still_bypass_preexisting_results():
    cache.invalidate()
    loader = Mock(side_effect=[{'revision': 1}, {'revision': 2}, {'revision': 3}])
    assert cache.read(loader, {}) == {'revision': 1}
    assert cache.read(loader, {}, refresh=True) == {'revision': 2}
    assert cache.read(loader, {}, refresh=True) == {'revision': 3}
    assert loader.call_count == 3


def test_write_during_refresh_prevents_waiters_from_reusing_old_snapshot():
    cache.invalidate()
    entered, release = threading.Event(), threading.Event()
    def before_write():
        entered.set()
        assert release.wait(3)
        return {'revision': 'old'}
    loader = Mock(side_effect=[{'revision': 'after-write'}])
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(cache.read, before_write, {}, refresh=True)
        assert entered.wait(2)
        second = pool.submit(cache.read, loader, {}, refresh=True)
        try:
            wait_for_readers(2)
            cache.invalidate()
        finally:
            release.set()
        assert first.result(timeout=3) == {'revision': 'old'}
        assert second.result(timeout=3) == {'revision': 'after-write'}
    assert cache.read(lambda: None, {}) == {'revision': 'after-write'}
    assert loader.call_count == 1


def test_refresh_never_shares_across_permission_versions():
    cache.invalidate()
    first = Mock(return_value={'rows': [1, 2]})
    second = Mock(return_value={'rows': [2]})
    assert cache.read(first, {}, scope={'id': 1, 'access_version': 1}, refresh=True)['rows'] == [1, 2]
    assert cache.read(second, {}, scope={'id': 1, 'access_version': 2}, refresh=True)['rows'] == [2]
    assert first.call_count == second.call_count == 1
