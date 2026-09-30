import pytest
from bit import ad_profit
from bit import bit_ad_analysis as analysis


@pytest.fixture(autouse=True)
def isolated_profit(tmp_path, monkeypatch):
    monkeypatch.setenv('BIT_AD_PROFIT_PATH', str(tmp_path / 'profit.sqlite3'))


def product(**metrics):
    return dict(direct_units_available=True, token_id=7, site_id='MLM', item_id='MLM123', currency_id='MXN', profit_currency_id='MXN',
                metrics={'cost': 100, 'clicks': 40, 'direct_units_quantity': 5, **metrics})


@pytest.mark.parametrize('profit, metrics, action', [
    (None, {}, 'missing'), (0, {}, 'pause'), (-10, {}, 'pause'),
    (30, {}, 'increase'), (20, {}, 'continue'), (18, {}, 'decrease'),
    (10, {}, 'pause'), (30, {'direct_units_quantity': 0}, 'pause'),
    (30, {'clicks': 10, 'direct_units_quantity': 1}, 'observe'),
    (30, {'cost': 0}, 'observe'),
])
def test_recommendations(profit, metrics, action):
    assert ad_profit.recommendation(product(**metrics), profit)['action'] == action


def test_indirect_units_do_not_inflate_profit():
    result = ad_profit.recommendation(product(units_quantity=100, indirect_units_quantity=95), 20)
    assert result['net_profit'] == 0
    assert result['break_even_cpc'] == 2.5


def test_missing_direct_metrics_are_not_treated_as_zero_sales():
    row = product()
    row['direct_units_available'] = False
    assert ad_profit.recommendation(row, 30)['action'] == 'observe'
    assert ad_profit.recommendation(row, 30)['net_profit'] is None


def test_persistence_clear_and_scope():
    row = product()
    ad_profit.save_profit(row, 30)
    snapshot = {'links': [row, {**row, 'token_id': 8}, {**row, 'profit_currency_id': 'USD'}]}
    ad_profit.enrich(snapshot)
    assert [item['unit_profit'] for item in snapshot['links']] == [30, None, None]
    assert row['recommendation']['action'] == 'increase'
    ad_profit.save_profit(row, None)
    assert ad_profit.enrich({'links': [row]})['links'][0]['unit_profit'] is None


@pytest.mark.parametrize('value', ['NaN', 'Infinity', 'bad', True, 1e13])
def test_invalid_profit_rejected(value):
    with pytest.raises(ValueError):
        ad_profit.save_profit(product(), value)


def test_image_batch_enrichment_and_safe_url():
    class Client:
        def get_listings(self, ids):
            assert ids == ['MLM123']
            return [{'id': 'MLM123', 'pictures': [{'secure_url': 'https://example.com/product.jpg'}]}]
    links = [product(), product()]
    analysis._enrich_images(Client(), links)
    assert all(row['thumbnail_url'] == 'https://example.com/product.jpg' for row in links)
    assert analysis._product_image({'thumbnail': 'javascript:bad'}) == ''


def test_picture_failure_keeps_ad_data():
    class Client:
        def get_listings(self, ids):
            raise RuntimeError('unavailable')
    rows = [product()]
    analysis._enrich_images(Client(), rows)
    assert rows[0]['metrics']['cost'] == 100


def test_profit_route_requires_execute_and_store_scope(monkeypatch):
    from bit import bit_interface
    client = bit_interface.app.test_client()
    user = {'id': 9, 'username': 'profit-tester', 'role_key': 'member',
            'permissions': ['ad_analysis.view'], 'access_version': 1}
    monkeypatch.setattr(bit_interface, 'get_current_workbench_user', lambda: user)
    response = client.post('/api/ad-analysis/profit', json={'row': product(), 'unit_profit': 30})
    assert response.status_code == 403
    user['permissions'] = ['ad_analysis.view', 'ad_analysis.execute']
    monkeypatch.setattr(bit_interface, '_authorized_token_ids_for_user', lambda: {8})
    assert client.post('/api/ad-analysis/profit', json={'row': product(), 'unit_profit': 30}).status_code == 403
    monkeypatch.setattr(bit_interface, '_authorized_token_ids_for_user', lambda: {7})
    monkeypatch.setattr(bit_interface.bit_db_api, 'save_mercado_ad_profit', ad_profit.save_profit)
    response = client.post('/api/ad-analysis/profit', json={'row': product(), 'unit_profit': 30})
    assert response.status_code == 200
    assert response.get_json()['data']['recommendation']['action'] == 'increase'
    assert client.post('/api/ad-analysis/profit', json={'row': product()}).status_code == 400


def test_remote_profit_save_routes_to_database_service(monkeypatch):
    from bit import bit_db_api
    calls = []
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    monkeypatch.setattr(bit_db_api, '_request', lambda *args, **kwargs: calls.append((args, kwargs)) or {})
    bit_db_api.save_mercado_ad_profit(product(), 25)
    assert calls[0][0] == ('POST', '/api/db/ad-analysis/profit')
    assert calls[0][1]['json']['unit_profit'] == 25


@pytest.mark.parametrize('currency', ['', 'MXN', 'CNY'])
def test_default_profit_is_cny_and_survives_ad_currency_changes(currency):
    row = product()
    row.pop('profit_currency_id')
    row['currency_id'] = currency
    result = ad_profit.save_profit(row, 30)
    assert result['profit_currency_id'] == 'CNY'
    assert result['recommendation']['net_profit'] == (50 if currency == 'CNY' else None)
    refreshed = {**row, 'currency_id': 'USD'}
    ad_profit.enrich({'links': [refreshed]})
    assert refreshed['unit_profit'] == 30
    assert refreshed['profit_currency_id'] == 'CNY'
    ad_profit.save_profit(refreshed, None)
    assert ad_profit.enrich({'links': [row]})['links'][0]['unit_profit'] is None


@pytest.mark.parametrize('field', ['token_id', 'site_id', 'item_id'])
def test_missing_product_identity_still_rejected(field):
    row = product()
    row.pop(field)
    with pytest.raises(ValueError, match='商品、店铺和站点不能为空'):
        ad_profit.save_profit(row, 30)
