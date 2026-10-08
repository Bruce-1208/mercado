"""Browser session exchange uses the authenticated console identity only."""
import pytest
from flask import session


@pytest.fixture
def bridge(monkeypatch):
    from bit import bit_interface as workbench
    workbench.app.config.update(TESTING=True, SECRET_KEY='console-session-test')
    monkeypatch.setattr(workbench, 'get_current_workbench_user', lambda: session.get('workbench_user'))
    monkeypatch.setattr(workbench, '_workbench_billing_blocked', lambda user: None)
    return workbench, workbench.app.test_client()


def test_console_session_requires_real_login(bridge):
    _, client = bridge
    result = client.post('/api/browser-extension/console-session', json={
        'user': {'id': 1, 'role_key': 'super_admin'}, 'token': 'fake'})
    assert result.status_code == 401
    assert 'token' not in result.get_json()


@pytest.mark.parametrize('role', ['super_admin', 'operator'])
def test_console_session_returns_signed_token_and_actual_role(bridge, role):
    workbench, client = bridge
    user = {'id': 7, 'username': 'collector', 'role_key': role, 'access_version': 1}
    with client.session_transaction() as current:
        current['workbench_user'] = user
    result = client.post('/api/browser-extension/console-session', json={})
    assert result.status_code == 200
    assert result.headers['Cache-Control'] == 'no-store'
    data = result.get_json()['data']
    assert data['user'] == user
    from itsdangerous import URLSafeTimedSerializer
    payload = URLSafeTimedSerializer(workbench.app.secret_key, salt=workbench.BROWSER_EXTENSION_TOKEN_SALT).loads(data['token'])
    assert payload['id'] == 7
    assert data['expires_in'] > 0


def test_console_session_rejects_form_posts(bridge):
    _, client = bridge
    assert client.post('/api/browser-extension/console-session', data={}).status_code == 415


def test_console_session_respects_account_block(bridge, monkeypatch):
    workbench, client = bridge
    with client.session_transaction() as current:
        current['workbench_user'] = {'id': 7, 'username': 'blocked'}
    monkeypatch.setattr(workbench, '_workbench_billing_blocked', lambda user: (workbench.jsonify(message='blocked'), 403))
    assert client.post('/api/browser-extension/console-session', json={}).status_code == 403
