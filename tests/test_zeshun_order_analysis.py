from bit import order_analysis as analysis
from bit import bit_interface
import pytest


def order(id, title='同名商品', **values):
    return dict(id=str(id), store_id=1, title=title,
                **{'data_origin': 'token', 'quantity': 1, 'income': 100, 'profit': 20,
                   'actual_freight_usd': 12, 'quoted_freight_usd': 10, **values})


@pytest.fixture(autouse=True)
def empty_cache():
    analysis._CACHE.clear()
    yield
    analysis._CACHE.clear()


def test_aggregation_merges_names_not_ids_and_keeps_losses_and_missing_costs():
    rows = analysis._aggregate([
        order(1, product_id='A', status='取消-发货后', quantity=2),
        order(2, ' 同名商品 ', product_id='B', profit=-5, actual_freight_usd=None),
        order(2, ' 同名商品 ', product_id='B', profit=-5),
        order(3, data_origin='yandex', profit=999),
        order(4, title='', profit=999),
        order(5, '不同名字', product_id='A'),
    ])
    assert len(rows) == 2
    product = rows[0]
    assert product['total_quantity'] == 3
    assert product['total_profit'] == 15
    assert product['after_sale_rate'] == 66.67
    assert product['comparable_orders'] == 1
    assert 'product_id' not in product


def test_all_pages_and_rank_by_absolute_change_without_cancelling_directions(monkeypatch):
    calls = []
    def fetch(**params):
        calls.append(params)
        return {'pages': 2, 'rows': [order(1, '下降', actual_freight_usd=0)] if params['page'] == 1
                else [order(2, '上涨', actual_freight_usd=17), order(3, '缺失', actual_freight_usd=None)]}
    monkeypatch.setattr(analysis.bit_db_api, 'list_orders', fetch)
    result = analysis.analyze('freight', allowed_store_ids=[1], limit=1)
    assert result['summary']['products'] == 2
    assert result['rows'][0]['title'] == '下降'
    assert result['rows'][0]['freight_change'] == -10
    assert [call['page'] for call in calls] == [1, 2]
    assert all(call['origin'] == 'token' and call['allowed_store_ids'] == [1] for call in calls)
    # A second ranking shares the same full data, not the first ranking's limit.
    assert len(analysis.analyze('profits', allowed_store_ids=[1])['rows']) == 3
    assert len(calls) == 2


def test_empty_scope_never_reads_orders(monkeypatch):
    monkeypatch.setattr(analysis.bit_db_api, 'list_orders', lambda **kw: pytest.fail('unscoped query'))
    assert analysis.analyze('profits', allowed_store_ids=[])['rows'] == []


def test_scope_cache_does_not_cross_users(monkeypatch):
    monkeypatch.setattr(analysis.bit_db_api, 'list_orders', lambda **kw: {
        'rows': [order(kw['allowed_store_ids'][0], str(kw['allowed_store_ids'][0]))], 'pages': 1})
    assert analysis.analyze('profits', allowed_store_ids=[1])['rows'][0]['title'] == '1'
    assert analysis.analyze('profits', allowed_store_ids=[2])['rows'][0]['title'] == '2'


def test_route_intersects_zeshun_and_authorized_stores(monkeypatch):
    monkeypatch.setattr(bit_interface, '_authorized_token_ids_for_user', lambda: {1, 3})
    monkeypatch.setattr(bit_interface.bit_db_api, 'list_mercado_store_tokens', lambda: {'rows': [
        {'id': 1, 'organization_key': 'wuhan-zeshun'},
        {'id': 2, 'organization_key': 'wuhan-zeshun'},
        {'id': 3, 'organization_key': 'other'},
    ]})
    calls = []
    monkeypatch.setattr(analysis, 'analyze', lambda kind, **kw: calls.append((kind, kw)) or {})
    bit_interface._analyze_zeshun_orders('profits', {'search': '商品'})
    assert calls == [('profits', {'allowed_store_ids': [1], 'search': '商品'})]


def test_invalid_dates_and_sort():
    with pytest.raises(ValueError):
        analysis.analyze('profits', allowed_store_ids=[], date_from='2026-10-10', date_to='2026-10-01')
    with pytest.raises(ValueError):
        analysis.analyze('freight', allowed_store_ids=[], sort_by='product_id')


def test_freight_endpoint_passes_filters(monkeypatch):
    monkeypatch.setattr(bit_interface, 'get_current_workbench_user', lambda: {
        'id': 1, 'username': 'viewer', 'permissions': ['order_analysis.view'], 'access_version': 1})
    monkeypatch.setattr(bit_interface, '_authorized_token_ids_for_user', lambda user=None: None)
    calls = []
    monkeypatch.setattr(bit_interface, '_analyze_zeshun_orders',
                        lambda kind, params: calls.append((kind, params)) or {'rows': []})
    response = bit_interface.app.test_client().get(
        '/api/order-analysis/freight-changes?search=盒&date_from=2026-10-01&limit=10')
    assert response.status_code == 200
    assert calls == [('freight', dict(search='盒', date_from='2026-10-01', date_to='', limit=10,
                                      sort_by='max_freight_change', sort_dir='desc'))]
