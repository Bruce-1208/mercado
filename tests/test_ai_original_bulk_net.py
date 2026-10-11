"""Batch economics use current locked values and commit the whole selection."""
from copy import deepcopy
from decimal import Decimal

import pytest

from bit import bit_db_api, product_access
from erp import mercadolibre_collection_store as store
from test_ai_original_budget_routes import isolated_function
from flask import Flask


class BatchDB:
    def __init__(self, amounts, stale=False):
        self.rows = {i: {"id": i, "net_proceeds_usd": value} for i, value in amounts.items()}
        self.queries = []
        self.committed = self.rolled_back = self.closed = False
        self.stale = stale
        self.rowcount = 1

    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params):
        self.queries.append((sql, params))
        if sql.startswith('UPDATE'):
            amount, row_id = params
            if not self.stale: self.rows[row_id]['net_proceeds_usd'] = amount
    def fetchall(self): return deepcopy(list(self.rows.values()))
    def fetchone(self): return deepcopy(self.rows[self.queries[-1][1][0]])
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True
    def close(self): self.closed = True


@pytest.fixture(autouse=True)
def no_ddl(monkeypatch):
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)


def update(db, changes, ids=(1, 2)):
    return store.update_ai_original_product_items(ids, changes, connection_factory=lambda: db)


def test_ratio_multiplies_each_current_net_and_preserves_unselected():
    db = BatchDB({1: Decimal('10'), 2: Decimal('20.1234')})
    result = update(db, {'net_proceeds_ratio': 150}, ids=(2, 1, 1))
    assert db.rows[1]['net_proceeds_usd'] == Decimal('15.0000')
    assert db.rows[2]['net_proceeds_usd'] == Decimal('30.1851')
    assert result == {'requested': 2, 'changed': 2, 'skipped': 0, 'updated_fields': ['net_proceeds_usd']}
    assert 'FOR UPDATE' in db.queries[0][0]
    assert db.committed and db.closed and not db.rolled_back


def test_fixed_net_overwrites_existing_and_fills_missing():
    db = BatchDB({1: Decimal('10'), 2: None})
    assert update(db, {'net_proceeds_usd': '12.34565'})['changed'] == 2
    assert [row['net_proceeds_usd'] for row in db.rows.values()] == [Decimal('12.3457')] * 2


def test_ratio_skips_missing_net_without_using_formula_default():
    db = BatchDB({1: Decimal('10'), 2: None})
    result = update(db, {'net_proceeds_ratio': 50})
    assert db.rows[1]['net_proceeds_usd'] == 5
    assert db.rows[2]['net_proceeds_usd'] is None
    assert result['changed'] == result['skipped'] == 1


@pytest.mark.parametrize('changes', [
    {'net_proceeds_usd': 0}, {'net_proceeds_usd': -1}, {'net_proceeds_usd': 'NaN'},
    {'net_proceeds_usd': True}, {'net_proceeds_usd': '1USD'},
    {'net_proceeds_ratio': 'Infinity'}, {'net_proceeds_ratio': 10001},
    {'net_proceeds_usd': 10, 'net_proceeds_ratio': 150},
    {'net_proceeds_ratio': 150, 'package_length_cm': 10},
    {'net_proceeds_usd': '10000000000000000'},
])
def test_invalid_request_never_writes(changes):
    db = BatchDB({1: 10, 2: 20})
    with pytest.raises(ValueError): update(db, changes)
    assert not any(sql.startswith('UPDATE') for sql, _ in db.queries)
    assert not db.committed


@pytest.mark.parametrize('amount', [Decimal('9999999999999999'), Decimal('0.0001')])
def test_invalid_result_rolls_back_before_any_writes(amount):
    db = BatchDB({1: 10, 2: amount})
    ratio = 150 if amount > 1 else '0.0001'
    with pytest.raises(ValueError): update(db, {'net_proceeds_ratio': ratio})
    assert db.rolled_back and db.closed and not db.committed
    assert not any(sql.startswith('UPDATE') for sql, _ in db.queries)


def test_missing_product_rejects_entire_batch():
    db = BatchDB({1: 10})
    with pytest.raises(KeyError): update(db, {'net_proceeds_usd': 15})
    assert db.rolled_back and not db.committed


def test_cross_organization_selection_is_rejected():
    db = BatchDB({1: 10, 2: 20})
    with product_access.scope({'access_version': 1, 'role_key': 'super_admin', 'organization_key': 'other'}):
        with pytest.raises(PermissionError): update(db, {'net_proceeds_ratio': 150})
    assert db.closed and not any(sql.startswith('UPDATE') for sql, _ in db.queries)


def test_wrong_readback_cannot_report_success():
    db = BatchDB({1: 10, 2: 20}, stale=True)
    with pytest.raises(ValueError, match='回读'): update(db, {'net_proceeds_usd': 15})
    assert db.rolled_back and not db.committed


def test_remote_transport_uses_net_route(monkeypatch):
    requests = []
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    monkeypatch.setattr(bit_db_api, '_request', lambda *a, **kw: requests.append((a, kw)) or {'changed': 2})
    assert bit_db_api.update_ai_original_product_items([1, 2], {'net_proceeds_ratio': 150}) == {'changed': 2}
    assert requests[0][0] == ('PATCH', '/api/db/ai-original-products/bulk-net-proceeds')
    assert requests[0][1]['json']['changes'] == {'net_proceeds_ratio': 150}


@pytest.mark.parametrize('name', ['api_update_ai_original_product_items', 'api_db_update_ai_original_product_items'])
def test_routes_return_batch_results_and_permission_errors(name):
    calls = []
    route = isolated_function(name, reject_db_api_client_mode=lambda: None,
        db_update_ai_original_product_items=lambda ids, changes: calls.append((ids, changes)) or {'changed': 2})
    app = Flask(__name__)
    with app.test_request_context(json={'product_item_ids': [1, 2], 'changes': {'net_proceeds_ratio': 150}}):
        response = route()
        assert response.get_json()['data']['changed'] == 2
    assert calls == [([1, 2], {'net_proceeds_ratio': 150})]
    def deny(*args): raise PermissionError('无权访问')
    route = isolated_function(name, reject_db_api_client_mode=lambda: None, db_update_ai_original_product_items=deny)
    with app.test_request_context(json={'product_item_ids': [1], 'changes': {'net_proceeds_usd': 10}}):
        response, code = route()
        assert code == 403
