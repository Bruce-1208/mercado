"""Backend outages must never be confused with revoked plugin credentials."""
from test_browser_extension_management_categories import workbench, HEADERS
from bit.db_pool import PoolAcquireTimeout


def test_busy_auth_lookup_returns_service_error_then_recovers(workbench, monkeypatch, subscribed_workbench_account):
    client = workbench.app.test_client()
    original = workbench._browser_extension_user_from_token
    def busy(_token):
        raise PoolAcquireTimeout('database busy')
    monkeypatch.setattr(workbench, '_browser_extension_user_from_token', busy)
    response = client.get('/api/browser-extension/session', headers=HEADERS)
    assert response.status_code == 503
    assert '无需重新登录' in response.json['message']
    monkeypatch.setattr(workbench, '_browser_extension_user_from_token', original)
    assert client.get('/api/browser-extension/session', headers=HEADERS).status_code == 200


def test_invalid_token_still_returns_401(workbench, monkeypatch):
    monkeypatch.setattr(workbench, '_browser_extension_user_from_token', lambda token: None)
    response = workbench.app.test_client().get('/api/browser-extension/session', headers=HEADERS)
    assert response.status_code == 401
