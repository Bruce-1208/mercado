import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask

from bit import action_center_cache
from bit import mercado_action_center as module


def test_cold_background_read_returns_without_waiting_and_shares_work():
    cache = action_center_cache.SnapshotCache()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def loader():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {'rows': [7]}

    try:
        assert cache.read('store', loader, background=True) == (None, True)
        assert entered.wait(2)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: cache.read('store', loader, background=True), range(20)))
        assert results == [(None, True)] * 20
        assert len(calls) == 1
    finally:
        release.set()
    assert cache.read('store', loader) == ({'rows': [7]}, False)
    assert len(calls) == 1


def test_expired_snapshot_stays_visible_and_force_bypasses_ttl(monkeypatch):
    now = [100]
    monkeypatch.setattr(action_center_cache.time, 'monotonic', lambda: now[0])
    cache = action_center_cache.SnapshotCache(ttl=60)
    assert cache.read(1, lambda: 'old') == ('old', False)
    assert cache.read(1, lambda: 'ignored') == ('old', False)
    now[0] += 61
    release = threading.Event()
    try:
        assert cache.read(1, lambda: release.wait(5) and 'new', background=True) == ('old', True)
    finally:
        release.set()
    assert cache.read(1, lambda: 'ignored') == ('new', False)
    assert cache.read(1, lambda: 'forced', force=True) == ('forced', False)


def test_failed_work_reports_error_and_can_retry():
    cache = action_center_cache.SnapshotCache()
    def fail():
        raise ValueError('offline')
    with pytest.raises(ValueError, match='offline'):
        cache.read(1, fail)
    with pytest.raises(ValueError, match='offline'):
        cache.read(1, lambda: 'must not hide error', background=True)
    assert cache.read(1, lambda: 'recovered', force=True) == ('recovered', False)


def test_cache_capacity_evicts_completed_entries():
    cache = action_center_cache.SnapshotCache(capacity=2)
    for key in range(3):
        cache.read(key, lambda key=key: key)
    assert len(cache._entries) == 2
    assert 0 not in cache._entries


def make_app(store, allowed):
    app = Flask(__name__)
    app.register_blueprint(module.create_action_center_blueprint(
        login_required=lambda fn: fn, current_user=lambda: {'organization_key': 'org'},
        authorized_token_ids=lambda user: allowed, storage=store))
    return app


class Store:
    def list_mercado_store_tokens(self):
        return {'rows': [dict(id=i, organization_key='org', salesperson=str(i)) for i in (1, 2)]}
    def list_mercado_operator_tasks(self, **kwargs):
        return {'rows': []}
    def list_mercado_notification_events(self, **kwargs):
        return {'rows': []}
    def list_mercado_prohibited_listings(self, **kwargs):
        return {'rows': []}


def test_order_preload_is_reused_by_filters_and_manual_refresh_recomputes(monkeypatch):
    calls = []
    monkeypatch.setattr(module, '_refresh_order_watch_tasks', lambda store, ids, org: calls.extend(ids))
    client = make_app(Store(), [1, 2]).test_client()
    assert client.get('/api/mercado/today/tasks').status_code == 200
    assert calls == [1, 2]
    client.get('/api/mercado/today/tasks?salesperson=1&include_closed=1')
    assert calls == [1, 2]
    client.get('/api/mercado/today/tasks?salesperson=1&refresh=1')
    assert calls == [1, 2, 1]


def test_cached_post_sale_rechecks_permissions_on_every_read(monkeypatch):
    calls, allowed = [], [1, 2]
    def load(store, tokens):
        calls.extend(t['id'] for t in tokens)
        return {'rows': [{'token_id': t['id']} for t in tokens], 'errors': []}
    monkeypatch.setattr(module, '_unanswered_post_sale', load)
    client = make_app(Store(), allowed).test_client()
    client.get('/api/mercado/today/post-sale')
    allowed[:] = [2]
    data = client.get('/api/mercado/today/post-sale').json['data']
    assert data['rows'] == [{'token_id': 2}]
    assert calls == [1, 2]
    assert client.get('/api/mercado/today/post-sale?salesperson=1').json['data']['rows'] == []


@pytest.mark.parametrize('endpoint,method', [('tasks', '_refresh_order_watch_tasks'), ('post-sale', '_unanswered_post_sale'), ('claims', '_unanswered_claims')])
def test_cold_endpoint_reports_pending_then_returns_completed_snapshot(monkeypatch, endpoint, method):
    release = threading.Event()
    def load(*args):
        assert release.wait(5)
        return (set(), set()) if endpoint == 'tasks' else {'rows': [], 'errors': []}
    monkeypatch.setattr(module, method, load)
    client = make_app(Store(), [1]).test_client()
    try:
        response = client.get(f'/api/mercado/today/{endpoint}?background=1')
        assert response.status_code == 200
        assert response.json['data']['pending'] is True
        assert response.json['data']['refreshing'] is True
        assert response.headers['Cache-Control'] == 'no-store'
    finally:
        release.set()
    result = client.get(f'/api/mercado/today/{endpoint}').json['data']
    assert result['pending'] is False
    assert result['refreshing'] is False


def test_saturated_background_cache_defers_instead_of_failing():
    cache = action_center_cache.SnapshotCache(capacity=1)
    release = threading.Event()
    try:
        assert cache.read(1, lambda: release.wait(5), background=True) == (None, True)
        assert cache.read(2, lambda: 'second', background=True) == (None, True)
        assert len(cache._entries) == 1
    finally:
        release.set()
    cache.read(1, lambda: None)
    assert cache.read(2, lambda: 'second') == ('second', False)


@pytest.mark.parametrize('endpoint', ['tasks', 'post-sale', 'claims'])
def test_admin_can_page_every_store_beyond_cache_capacity(monkeypatch, endpoint):
    class ManyStores(Store):
        def list_mercado_store_tokens(self):
            return {'rows': [dict(id=i, organization_key='org') for i in range(1, 146)]}
    calls = []
    monkeypatch.setattr(module, '_refresh_order_watch_tasks', lambda store, ids, org: calls.extend(ids))
    def messages(store, tokens):
        calls.extend(t['id'] for t in tokens)
        return {'rows': [], 'errors': []}
    monkeypatch.setattr(module, '_unanswered_post_sale', messages)
    monkeypatch.setattr(module, '_unanswered_claims', messages)
    client = make_app(ManyStores(), None).test_client()
    for page in range(1, 9):
        response = client.get(f'/api/mercado/today/{endpoint}?store_page={page}')
        assert response.status_code == 200
        pagination = response.json['data']['store_pagination']
        assert pagination == dict(page=page, page_size=20, pages=8, total=145)
    assert calls == list(range(1, 146))


def test_one_store_failure_keeps_other_sections_and_event_paging(monkeypatch):
    class PartialStore(Store):
        def list_mercado_operator_tasks(self, **kwargs):
            return {'rows': [{'id': 1, 'topic': 'items', 'status': 'open'}]}
        def list_mercado_notification_events(self, **kwargs):
            assert kwargs['offset'] == 100
            assert kwargs['limit'] == 50
            return {'rows': [{'id': 101}], 'total': 151}
    def fail(*args):
        raise ValueError('unavailable')
    monkeypatch.setattr(module, '_refresh_order_watch_tasks', fail)
    response = make_app(PartialStore(), [1]).test_client().get('/api/mercado/today/tasks?event_page=3')
    assert response.status_code == 200
    data = response.json['data']
    assert data['rows'][0]['id'] == 1
    assert data['events'][0]['id'] == 101
    assert data['event_pagination'] == dict(page=3, pages=4, total=151)
    assert 'unavailable' in data['errors'][0]


def test_independent_sections_are_read_concurrently(monkeypatch):
    # All three reads must start before any finishes; sequential loading deadlocks the barrier.
    barrier = threading.Barrier(3, timeout=2)
    class ParallelStore(Store):
        def list_mercado_operator_tasks(self, **kwargs):
            barrier.wait()
            return {'rows': []}
        def list_mercado_notification_events(self, **kwargs):
            barrier.wait()
            return {'rows': []}
        def list_mercado_prohibited_listings(self, **kwargs):
            barrier.wait()
            return {'rows': []}
        def list_mercado_today_work_states(self, **kwargs):
            return []
    monkeypatch.setattr(module, '_refresh_order_watch_tasks', lambda *args: (set(), set()))
    data = make_app(ParallelStore(), [1]).test_client().get('/api/mercado/today/tasks').json['data']
    assert not data['errors']
