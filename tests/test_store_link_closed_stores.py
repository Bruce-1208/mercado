import pytest

from bit import bit_store_link_sync


@pytest.mark.parametrize('selected', [[1, 7, 123, 125, 126], []])
def test_token_records_skips_closed_stores(monkeypatch, selected):
    closed = [7, 123, 125, 126]
    monkeypatch.setattr(bit_store_link_sync.bit_mysql, 'list_mercado_store_tokens', lambda: {
        'rows': [{'id': 1, 'enabled': True}] + [{'id': value, 'enabled': False} for value in closed],
    })
    fetched = []
    monkeypatch.setattr(bit_store_link_sync.bit_mysql, 'get_mercado_store_token',
                        lambda value: fetched.append(value) or {'id': value})
    monkeypatch.setattr(bit_store_link_sync, 'order_store_link_token_ids_for_full_sync', list)
    assert bit_store_link_sync._token_records(selected) == [{'id': 1}]
    assert fetched == [1]


def test_closed_selection_does_not_fall_back_to_all_stores(monkeypatch):
    monkeypatch.setattr(bit_store_link_sync.bit_mysql, 'list_mercado_store_tokens', lambda: {
        'rows': [{'id': 1, 'enabled': True}, {'id': 7, 'enabled': False}],
    })
    monkeypatch.setattr(bit_store_link_sync.bit_mysql, 'get_mercado_store_token',
                        lambda value: pytest.fail('Should not fetch an unselected store'))
    with pytest.raises(ValueError, match='所选店铺均已关闭'):
        bit_store_link_sync.start_store_link_sync([7])
    with pytest.raises(ValueError, match='授权不存在：999'):
        bit_store_link_sync._token_records([7, 999])


def test_start_sync_only_queues_enabled_selection(monkeypatch):
    monkeypatch.setattr(bit_store_link_sync, '_sync_state', {**bit_store_link_sync._sync_state, 'running': False})
    monkeypatch.setattr(bit_store_link_sync, '_token_records', lambda ids: [{'id': 1}])
    queued = []
    monkeypatch.setattr(bit_store_link_sync, 'request_store_link_sync', queued.append)
    threads = []
    class Thread:
        def __init__(self, **kwargs):
            threads.append(kwargs)
        def start(self):
            pass
    monkeypatch.setattr(bit_store_link_sync.threading, 'Thread', Thread)
    started, _ = bit_store_link_sync.start_store_link_sync([1, 7, 123, 125, 126])
    assert started
    assert queued == [[1]]
    assert threads[0]['args'] == ([1],)
