"""Transient read outages must not replay writes or hide the original error."""
import pytest
from bit import bit_db_api
from erp.ai_weight_price.service import Service


class Response:
    headers = {}
    def __init__(self, code):
        self.status_code = code
        self.ok = code == 200
    def close(self): pass
    def json(self):
        assert self.ok, 'gateway HTML must not be parsed as JSON'
        return {'status': 'success', 'data': {'value': 1}}


@pytest.mark.parametrize('operation', ['get', 'state', 'execution_snapshot'])
def test_read_rpc_recovers_from_gateway_error(monkeypatch, operation):
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs)
        return Response(502 if len(calls) < 3 else 200)
    monkeypatch.setattr(bit_db_api.DB_API_SESSION, 'request', request)
    monkeypatch.setattr(bit_db_api.time, 'sleep', lambda seconds: None)
    assert bit_db_api._request('POST', '/api/db/ai-weight-price/store',
        json={'method': operation}) == {'value': 1}
    assert len(calls) == 3


@pytest.mark.parametrize('operation', ['update', 'set_state', 'log', 'record_visual_event'])
def test_uncertain_writes_are_not_replayed(monkeypatch, operation):
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs)
        return Response(502)
    monkeypatch.setattr(bit_db_api.DB_API_SESSION, 'request', request)
    with pytest.raises(RuntimeError, match='HTTP 502'):
        bit_db_api._request('POST', '/api/db/ai-weight-price/store', json={'method': operation})
    assert len(calls) == 1


def test_failed_finalization_preserves_original_error_and_releases_lock(tmp_path, monkeypatch):
    class FailedBrowser:
        def __enter__(self): raise RuntimeError('original gateway failure')
        def __exit__(self, *args): pass
    service = Service(tmp_path, browser_factory=lambda *args: FailedBrowser())
    def fail_state(*args, **kwargs): raise RuntimeError('finalization gateway failure')
    monkeypatch.setattr(service.store, 'set_state', fail_state)
    lock = service.lock()
    assert lock.acquire()
    service.run({}, 'pipeline', None, lock)
    assert 'original gateway failure' in service.last_failure
    assert 'finalization gateway failure' not in service.last_failure
    next_lock = service.lock()
    assert next_lock.acquire()
    next_lock.release()
