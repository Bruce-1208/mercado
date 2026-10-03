from test_browser_extension_management_categories import workbench, HEADERS
from bit.mercado_product_data import DataError


def test_product_data_routes_require_own_login_and_hide_upstream_auth(workbench, monkeypatch):
    from bit.mercado_product_data import service
    client = workbench.app.test_client()
    remote = {"base_url":"https://zeshun.example.com", "environ_base":{"REMOTE_ADDR":"192.0.2.25"}}
    unauthorized = client.post('/api/browser-extension/mercado-data/query', json={"items":[]}, **remote)
    assert unauthorized.status_code == 401
    monkeypatch.setattr(service, 'query', lambda items:{"items":[{"sku":"MLM6226808220","product":{"weight_g":1300}}]})
    response = client.post('/api/browser-extension/mercado-data/query', json={"items":[{"sku":"MLM6226808220"}]}, headers=HEADERS, **remote)
    assert response.status_code == 200
    assert response.json['data']['items'][0]['product']['weight_g'] == 1300
    assert response.headers['Cache-Control'] == 'no-store'
    def expired(items):
        raise DataError('数据源会话已过期，请联系泽顺管理员更新')
    monkeypatch.setattr(service, 'query', expired)
    failed = client.post('/api/browser-extension/mercado-data/query', json={"items":[{"sku":"MLM6226808220"}]}, headers=HEADERS, **remote)
    assert failed.status_code == 503  # Must not log the customer out of Zeshun.
    assert 'token' not in failed.text


def test_product_data_is_routed_to_single_worker():
    from bit.service_split import is_worker_path
    assert is_worker_path('/api/browser-extension/mercado-data/query')
    assert is_worker_path('/api/browser-extension/mercado-data/status')
