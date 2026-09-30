"""Regression coverage for batched progress and audit writes."""
from contextlib import contextmanager

import pytest

from erp.ai_weight_price.store import RemoteStore, Store


def test_visual_progress_uses_one_rpc_and_preserves_history(tmp_path, monkeypatch):
    store = Store(tmp_path / 'server')
    store.set_actor({'id': 7, 'display_name': 'seller'})
    store.add({'erp_goods_id': 'g1', 'title': 'cup', 'main_image_url': 'image'})
    old = {'run_id': 'old', 'message': 'previous batch'}
    store.update('g1', visual_history=[old])
    store.set_state('run', {'run_id': 'new', 'processed_items': 4})
    remote = RemoteStore(tmp_path / 'client')
    remote.set_actor({'id': 7, 'display_name': 'seller'})
    calls = []

    def request(method, path, **kwargs):
        body = kwargs['json']
        calls.append(body['method'])
        with store.actor_scope(body['actor']):
            return getattr(store, body['method'])(*body['args'], **body['kwargs'])

    monkeypatch.setattr('bit.bit_db_api._request', request)
    remote.record_visual_event('g1', {'id': 'event', 'message': 'reading SKU', 'step': 'candidate'})
    assert calls == ['record_visual_event']
    assert store.state('run') == {'run_id': 'new', 'processed_items': 4,
                                  'current_task_id': 'g1', 'message': 'reading SKU'}
    history = store.get('g1')['visual_history']
    assert history[0] == old
    assert history[1]['run_id'] == 'new'
    assert store.state('visual_progress')['steps'] == history[1:]
    assert store.logs()[-1]['message'] == 'reading SKU'
    remote.set_actor({'id': 8})
    with pytest.raises(KeyError):
        remote.record_visual_event('g1', {'message': 'unauthorized'})
    assert store.get('g1')['visual_history'] == history


def test_candidate_logs_use_one_transaction_with_owner_and_order(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.set_actor({'id': 7, 'display_name': 'seller'})
    connect = store.connect
    opened = []

    @contextmanager
    def counted():
        opened.append(True)
        with connect() as db:
            yield db

    monkeypatch.setattr(store, 'connect', counted)
    messages = [f'candidate {i}' for i in range(10)]
    store.log_many(messages, 'g1')
    assert len(opened) == 1
    logs = store.logs()
    assert [row['message'] for row in logs] == messages
    assert all(row['owner_user_id'] == 7 and row['task_id'] == 'g1' for row in logs)
    store.set_actor({'id': 8})
    assert store.logs() == []


def test_execution_snapshot_is_one_rpc_with_fresh_controls(tmp_path, monkeypatch):
    store = Store(tmp_path / "server")
    store.set_actor({"id": 7})
    store.add({"erp_goods_id": "g1", "title": "cup"})
    remote = RemoteStore(tmp_path / "client")
    remote.set_actor({"id": 7})
    calls = []

    def request(method, path, **kwargs):
        body = kwargs["json"]
        calls.append(body["method"])
        with store.actor_scope(body["actor"]):
            return getattr(store, body["method"])(*body["args"], **body["kwargs"])

    monkeypatch.setattr("bit.bit_db_api._request", request)
    assert remote.execution_snapshot("g1")["task"]["erp_goods_id"] == "g1"
    assert calls == ["execution_snapshot"]
    store.set_state("stop_requested", True)
    assert remote.execution_snapshot("g1") == {
        "stop_requested": True, "circuit": None, "task": None}
    store.set_state("stop_requested", False)
    store.set_state("circuit", "login required")
    assert remote.execution_snapshot("g1")["circuit"] == "login required"
    remote.set_actor({"id": 8})
    with pytest.raises(KeyError):
        remote.execution_snapshot("g1")
