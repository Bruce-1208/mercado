from decimal import Decimal

import pytest
from bit import bit_store_link_remote_update as update
from test_store_link_remote_update_restart import queue, link


@pytest.mark.parametrize('base,ratio,target', [('10', '110', '11.00'), ('20', '90', '18.00'), ('1.005', '100', '1.01')])
def test_ratio_uses_each_link_and_rounds_half_up(base, ratio, target):
    changes = update._normalize_changes({'net_proceeds_ratio': ratio})
    assert update._resolve_net_ratio({'net_proceeds_usd': base}, changes) == {'net_proceeds_usd': Decimal(target)}
    assert changes == {'net_proceeds_ratio': Decimal(ratio)}


@pytest.mark.parametrize('ratio', ['NaN', 'Infinity', '0', '-1', 'invalid'])
def test_invalid_ratio_rejected(ratio):
    with pytest.raises(ValueError):
        update._normalize_changes({'net_proceeds_ratio': ratio})


@pytest.mark.parametrize('field', ['price', 'net_proceeds_usd'])
def test_conflicting_price_fields_rejected(field):
    with pytest.raises(ValueError, match='不能'):
        update._normalize_changes({'net_proceeds_ratio': 110, field: 10})


@pytest.mark.parametrize('base', [None, '', 'NaN', '-2', '0', '0.001'])
def test_invalid_base_has_no_remote_side_effects(monkeypatch, base):
    class Client:
        def __init__(self, token):
            pass
        def update_global_item(self, *args):
            pytest.fail('invalid net must never be submitted')
    monkeypatch.setattr(update, 'MercadoLibreClient', Client)
    result = update._update_one_link({**link(1), 'net_proceeds_usd': base}, {}, {'net_proceeds_ratio': Decimal('100')})
    assert result['status'] == 'error'
    assert result['applied_fields'] == []


def test_ratio_snapshot_survives_restart_and_refresh(queue, monkeypatch):
    monkeypatch.setattr(update, 'get_store_links_by_ids', lambda ids: [{**link(i), 'net_proceeds_usd': i * 10} for i in ids])
    _, state = update.start_store_link_remote_update([1, 2], {'net_proceeds_ratio': '110'})
    task = update._load_task(state['task_id'])
    assert task['changes']['net_proceeds_ratio'] == Decimal('110')
    # A preceding job, a sync, or an interrupted remote write may change the current value.
    for row, target in zip(task['rows'], ['11.00', '22.00']):
        row['net_proceeds_usd'] = 999
        assert update._resolve_net_ratio(row, task['changes'])['net_proceeds_usd'] == Decimal(target)


def test_ratio_uses_existing_withdraw_write_and_verified_local_flow(monkeypatch):
    from bit import mercado_promotions
    calls = []
    class Client:
        def __init__(self, token):
            pass
        def update_global_item(self, item, payload):
            calls.append(('write', payload))
        def get_marketplace_item(self, *args, **kwargs):
            return {'id': 'MLM3308393901', 'price': 15, 'currency_id': 'USD',
                    'net_proceeds': {'amount': 11, 'currency_id': 'USD'}}
    monkeypatch.setattr(update, 'MercadoLibreClient', Client)
    monkeypatch.setattr(mercado_promotions, 'withdraw_listing_promotions', lambda **kw: calls.append(('withdraw', kw['item_id'])) or {'cancelled_count': 1})
    monkeypatch.setattr(update, 'bulk_update_store_links', lambda ids, changes: calls.append(('local', changes)) or {})
    monkeypatch.setattr(update, 'sync_pulled_product_fields_from_store_links', lambda *args: 0)
    result = update._update_one_link({**link(1), 'net_proceeds_usd': 10}, {'id': 74}, {'net_proceeds_ratio': Decimal('110')})
    assert result['status'] == 'success'
    assert calls[0][0] == 'withdraw'
    assert calls[1] == ('write', {'net_proceeds': 11})
    assert calls[2][1]['net_proceeds_usd'] == Decimal('11')
    assert calls[2][1]['price'] == Decimal('15')
