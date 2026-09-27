import json
from unittest.mock import MagicMock

import pytest
from bit.ai_weight_price_client import start, detail, search, dispatch, stop
from erp.ai_weight_price.models import Models
from erp.ai_weight_price.service import Service
from erp import collection_ai_check as repository


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    service = Service(tmp_path)
    service.bind_actor({'id': 7, 'username': 'tester'})
    service.exchange_rate = lambda _: {'cny_per_usd': '7', 'date': '2026-09-27', 'source': 'test'}
    saved = []
    def save(task, status, reason, changes=None, evidence=None):
        saved.append((task, status, changes, evidence))
        return {'status': status, 'reason': reason}
    monkeypatch.setattr(repository, 'save_result', save)
    row = {'erp_goods_id': 'collection:1', 'collection_item_id': 1, 'title': 'test',
           'main_image_url': 'https://http2.mlstatic.com/test.jpg', 'weight_g': 10,
           'collection_before': {'weight_g': 10}}
    result = start(service, {'max_items': 1}, collection_products=[row])
    service.store.update(result["task"]["erp_goods_id"], best_match_approved=True)
    return service, result['task']['erp_goods_id'], saved


def test_collection_direct_search_and_server_write_without_zying_login(workflow):
    service, key, saved = workflow
    assert key.startswith('collection:1:')
    result = detail(service, {'task_id': key, 'detail': {
        'url': 'https://detail.1688.com/offer/1.html', 'weight_g': 80,
        'package_length_cm': 10, 'package_width_cm': 5, 'package_height_cm': 2,
        'skus': [{'id': 'x', 'price': 7, 'label': 'test'}],
    }})
    assert result['action'] == 'done'
    assert saved[-1][1] == 'checked'
    assert saved[-1][2] == {'weight_g': 80, 'package_length_cm': 10, 'package_width_cm': 5, 'package_height_cm': 2}
    assert saved[-1][3]['cost_price_cny'] == 7
    assert saved[-1][0]['collection_before']['weight_g'] == 10


def test_missing_measurements_preserve_original(workflow):
    service, key, saved = workflow
    detail(service, {'task_id': key, 'detail': {'skus': [{'id': 'x', 'price': 7}]}})
    assert saved[-1][1] == 'missing_evidence'
    assert saved[-1][2] == {}


def test_unmatched_does_not_write_measurements(workflow, monkeypatch):
    service, key, saved = workflow
    monkeypatch.setattr(Models, 'match_images', lambda *_: ([], []))
    search(service, {'task_id': key, 'candidates': [{'url': 'https://detail.1688.com/offer/2.html'}]})
    assert saved[-1][1] == 'unmatched'
    assert saved[-1][2] is None


def test_stale_callback_and_stopped_callback_rejected(workflow):
    service, key, saved = workflow
    with pytest.raises(ValueError, match='不一致'):
        dispatch(service, 'detail', {'task_id': 'another'})
    stop(service)
    assert saved[-1][1] == 'stopped'
    with pytest.raises(ValueError, match='停止'):
        dispatch(service, 'detail', {'task_id': key})


@pytest.mark.parametrize('value', [None, 0, -1, 'NaN', 'Infinity', 'unknown'])
def test_invalid_evidence_is_not_written(value):
    assert repository.measurements({'weight_g': value}, {}) == {}


@pytest.mark.parametrize('current,expected_status,changed', [(10, 'checked', True), (99, 'conflict', False), (20, 'checked', False)])
def test_atomic_audit_and_concurrent_edit_protection(monkeypatch, current, expected_status, changed):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {'id': 1, 'weight_g': current}
    monkeypatch.setattr(repository.db, '_connect', lambda: connection)
    monkeypatch.setattr(repository.db, 'ensure_collection_tables', lambda _: None)
    result = repository.save_result({'collection_item_id': 1, 'erp_goods_id': 'collection:1:run',
                                     'collection_before': {'weight_g': 10}}, 'checked', 'ok', {'weight_g': 20})
    assert result['status'] == expected_status
    assert result['modified'] is changed
    assert connection.commit.called
    updates = [c.args for c in cursor.execute.call_args_list if c.args[0].startswith('UPDATE')]
    assert len(updates) == 2
    assert ('`weight_g` = %s' in updates[0][0]) is changed
    assert json.loads(updates[0][1][-2])['status'] == expected_status


def test_write_failure_rolls_back_audit_and_measurements(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {'id': 1, 'weight_g': 10}
    cursor.execute.side_effect = [None, None, RuntimeError('mirror failed')]
    monkeypatch.setattr(repository.db, '_connect', lambda: connection)
    monkeypatch.setattr(repository.db, 'ensure_collection_tables', lambda _: None)
    with pytest.raises(RuntimeError):
        repository.save_result({'collection_item_id': 1, 'erp_goods_id': 'x', 'weight_g': 10}, 'checked', 'ok', {'weight_g': 20})
    assert connection.rollback.called
    assert not connection.commit.called


def test_queue_is_per_actor_and_only_claimed_once(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from bit import collection_ai_workflow as queue
    service = Service(tmp_path)
    service.bind_actor({'id': 7, 'username': 'tester'})
    monkeypatch.setattr(queue, 'queue_lock', lambda _: nullcontext())
    monkeypatch.setattr(repository, 'products', lambda ids: [{
        'collection_item_id': ids[0], 'erp_goods_id': 'collection:1',
        'title': 'test', 'main_image_url': 'https://http2.mlstatic.com/test.jpg',
    }])
    monkeypatch.setattr(repository, 'save_result', lambda *a, **kw: {})
    assert queue.enqueue(service, [1])['count'] == 1
    with pytest.raises(ValueError, match='已有'):
        queue.enqueue(service, [1])
    with service.store.actor_scope({'id': 8, 'username': 'other'}):
        assert queue.claim(service, {}) == {'action': 'done'}
    assert queue.claim(service, {})['action'] == 'search'
    assert queue.claim(service, {}) == {'action': 'done'}


def test_collection_cannot_write_before_image_approval(workflow):
    service, key, _ = workflow
    service.store.update(key, best_match_approved=False)
    with pytest.raises(ValueError, match='同款匹配'):
        detail(service, {'task_id': key, 'detail': {'skus': [{'price': 7}]}})
