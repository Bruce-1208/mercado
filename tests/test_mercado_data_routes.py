from test_browser_extension_management_categories import workbench, HEADERS
from bit.mercado_product_data import DataError


def test_product_data_routes_require_own_login_and_hide_upstream_auth(workbench, monkeypatch, subscribed_workbench_account):
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


def test_product_data_uses_web_pool_independent_of_background_jobs():
    from bit.service_split import is_worker_path
    # ProductDataService already coalesces cache misses and session refreshes
    # with an interprocess file lock; reads do not need the task worker.
    assert not is_worker_path('/api/browser-extension/mercado-data/query')
    assert not is_worker_path('/api/browser-extension/mercado-data/status')
    assert is_worker_path('/api/browser-extension/zying/start')


def test_exchange_rate_route_auth_and_snapshot(workbench, monkeypatch, subscribed_workbench_account):
    from bit.mercado_product_data import service
    client = workbench.app.test_client()
    remote = {"base_url":"https://zeshun.example.com", "environ_base":{"REMOTE_ADDR":"192.0.2.25"}}
    url = '/api/browser-extension/mercado-data/exchange-rates'
    assert client.get(url, **remote).status_code == 401
    monkeypatch.setattr(service, 'exchange_rates', lambda:{"rates_to_usd":{"MXN":.05},"fetched_at":123})
    response = client.get(url, headers=HEADERS, **remote)
    assert response.status_code == 200
    assert response.json['data']['rates_to_usd']['MXN'] == .05
    assert response.headers['Cache-Control'] == 'no-store'
