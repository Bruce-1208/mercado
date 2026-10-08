from concurrent.futures import Future
from bit.communication_preload import CommunicationPreload


class Executor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, query):
        future = Future()
        self.jobs.append((future, fn, query))
        return future

    def finish(self, index=0):
        future, fn, query = self.jobs[index]
        future.set_result(fn(query))


def cache(capacity=512):
    reader = CommunicationPreload(capacity=capacity)
    reader.executor.shutdown()
    reader.executor = Executor()
    return reader


def test_pending_polls_share_work_and_completed_reads_expire_hourly(monkeypatch):
    now = [100]
    monkeypatch.setattr('bit.communication_preload.time.monotonic', lambda: now[0])
    reader = cache()
    query = lambda: {'conversations': [{'pack_id': 'read'}]}
    assert reader.read((7, 'week'), query) == {'pending': True}
    assert reader.read((7, 'week'), query, refresh=True) == {'pending': True}
    assert len(reader.executor.jobs) == 1
    reader.executor.finish()
    now[0] += 3599
    assert reader.read((7, 'week'), query)['data']['conversations'][0]['pack_id'] == 'read'
    now[0] += 1
    assert reader.read((7, 'week'), query) == {'pending': True}
    assert len(reader.executor.jobs) == 2


def test_manual_refresh_retries_failures_and_capacity_does_not_duplicate_running_jobs():
    reader = cache(capacity=1)
    def broken():
        raise ValueError('offline')
    reader.read('a', broken)
    assert reader.read('b', lambda: {}) == {'pending': True}
    assert len(reader.executor.jobs) == 1
    reader.executor.finish()
    assert reader.read('a', broken)['error'] == 'offline'
    assert reader.read('a', lambda: {}, refresh=True) == {'pending': True}
    reader.executor.finish(1)
    assert reader.read('a', lambda: {})['data'] == {}
    assert reader.read('b', lambda: {}) == {'pending': True}
    assert len(reader.entries) == 1


def test_claims_queue_is_independent_of_message_history_scans():
    from threading import Event
    from bit.communication_preload import customer_service_claims_preload, customer_service_preload

    assert customer_service_claims_preload.executor is not customer_service_preload.executor
    messages = CommunicationPreload(workers=1)
    claims = CommunicationPreload(workers=1)
    release = Event()
    try:
        messages.read('history', lambda: release.wait(5))
        claims.read('claims', lambda: {'rows': [{'id': 1}]})
        result = claims.entries['claims']['future'].result(timeout=2)
        assert result['data']['rows'] == [{'id': 1}]
        assert not messages.entries['history']['future'].done()
    finally:
        release.set()
        messages.executor.shutdown()
        claims.executor.shutdown()
