from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
import requests
from bit import model_balance as balance


def payload(amount, available=True, currency='CNY'):
    return {'is_available': available, 'balance_infos': [{'currency': currency, 'total_balance': amount}]}


@pytest.mark.parametrize('amount,available,currency,status', [
    ('10', True, 'CNY', 'ok'), ('9.99', True, 'CNY', 'low'),
    ('-0.26', False, 'CNY', 'overdue'), ('0', False, 'CNY', 'overdue'),
    ('0.99', True, 'USD', 'low'), ('1', True, 'USD', 'ok'),
])
def test_thresholds(amount, available, currency, status):
    assert balance.summarize_balance(payload(amount, available, currency))['status'] == status


def test_cache_account_isolation_rotation_refresh_and_recovery():
    balance._CACHE.clear()
    calls = []
    def get(*args, **kwargs):
        calls.append(kwargs['headers']['Authorization'])
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
            json=lambda: payload('-0.26', False) if len(calls) == 1 else payload('30'))
    def check(uid=1, key='secret', **kw):
        return balance.get_balance_alert(uid, key, http_get=get, **kw)
    assert check(now=0)['status'] == 'overdue'
    assert check(now=10, force=True)['status'] == 'overdue'
    assert check(now=16, force=True)['status'] == 'ok'
    check(uid=2, now=17)
    check(key='rotated', now=18)
    assert len(calls) == 4
    assert 'secret' not in repr(balance._CACHE)
    check(now=400)
    assert len(calls) == 5


@pytest.mark.parametrize('kind', ['timeout', 'malformed', 'nan', 'auth'])
def test_unavailable_balance_never_claims_healthy(kind):
    balance._CACHE.clear()
    def get(*a, **kw):
        if kind == 'timeout': raise requests.Timeout('secret')
        return SimpleNamespace(status_code=401 if kind == 'auth' else 200,
            raise_for_status=lambda: None,
            json=lambda: [] if kind == 'malformed' else payload('NaN'))
    result = balance.get_balance_alert(1, 'secret', http_get=get)
    assert result['status'] in ('unknown', 'error')
    assert 'secret' not in repr(result)


def test_no_credentials_no_network():
    assert balance.get_balance_alert(1, '', http_get=lambda *a, **k: pytest.fail())['status'] == 'unconfigured'


def test_slow_query_does_not_block_another_accounts_cached_balance():
    balance._CACHE.clear()
    started = threading.Event()
    release = threading.Event()
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload('20'))
    balance.get_balance_alert(2, 'cached-key', now=0, http_get=lambda *args, **kwargs: response)

    def slow_get(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return response

    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(balance.get_balance_alert, 1, 'slow-key', now=1, http_get=slow_get)
        try:
            assert started.wait(1)
            cached = pool.submit(balance.get_balance_alert, 2, 'cached-key', now=2,
                                 http_get=lambda *args, **kwargs: pytest.fail('Cache must be used'))
            assert cached.result(timeout=1)['status'] == 'ok'
        finally:
            release.set()
        assert slow.result(timeout=1)['status'] == 'ok'


def test_concurrent_queries_for_same_credential_are_collapsed():
    balance._CACHE.clear()
    started = threading.Event()
    release = threading.Event()
    calls = []

    def get(*args, **kwargs):
        calls.append(args[0])
        started.set()
        assert release.wait(3)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload('20'))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(balance.get_balance_alert, 1, 'same-key', now=0, force=True, http_get=get)
        try:
            assert started.wait(1)
            second = pool.submit(balance.get_balance_alert, 1, 'same-key', now=0, force=True, http_get=get)
        finally:
            release.set()
        assert first.result(timeout=1)['status'] == 'ok'
        assert second.result(timeout=1)['status'] == 'ok'
    assert len(calls) == 1
    assert balance._INFLIGHT == {}


def test_overview_lists_every_saved_provider_without_requesting_billing_with_model_keys(monkeypatch):
    from bit.browser_extension_models import PROVIDERS

    assert tuple(balance.PROVIDER_BALANCE_INFO) == PROVIDERS
    calls = []

    def query(user_id, api_key, *, force):
        calls.append((user_id, api_key, force))
        return {**balance.summarize_balance(payload('20.50')), 'checked_at': '2026-10-01T01:00:00+00:00'}

    monkeypatch.setattr(balance, 'get_balance_alert', query)
    keys_read = []

    def key(provider):
        keys_read.append(provider)
        return 'account-private-token'

    result = balance.get_account_balance_overview(
        31, {f'{provider}_configured': True for provider in PROVIDERS}, key, force=True,
    )
    assert [item['provider'] for item in result['providers']] == list(PROVIDERS)
    assert keys_read == ['deepseek']
    assert calls == [(31, 'account-private-token', True)]
    assert result['providers'][0]['balances'][0]['amount'] == '20.50'
    assert result['status'] == 'unsupported'
    for item in result['providers'][1:]:
        assert item['status'] == 'unsupported'
        assert item['balances'] == []
        assert 'checked_at' not in item
        assert item['console_url'].startswith('https://')
    assert 'account-private-token' not in repr(result)


def test_overview_omits_unconfigured_providers_and_never_reads_shared_keys(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'shared-secret')
    no_key = lambda provider: pytest.fail('Unconfigured keys must not be read')
    empty = balance.get_account_balance_overview(31, {}, no_key)
    assert empty['providers'] == []
    assert empty['status'] == 'unconfigured'
    other = balance.get_account_balance_overview(31, {'openai_configured': True}, no_key)
    assert [item['provider'] for item in other['providers']] == ['openai']
    assert other['status'] == 'unsupported'
    assert 'shared-secret' not in repr(other)


@pytest.mark.parametrize('missing', [False, True])
def test_overview_keeps_other_providers_when_deepseek_key_cannot_be_read(missing):
    def key(provider):
        if missing:
            return ''
        raise ValueError('do-not-expose-storage-detail')

    result = balance.get_account_balance_overview(
        31, {'deepseek_configured': True, 'dashscope_configured': True}, key,
    )
    assert result['status'] == 'error'
    assert [item['status'] for item in result['providers']] == ['error', 'unsupported']
    assert 'do-not-expose-storage-detail' not in repr(result)


def test_balances_api_only_uses_logged_in_accounts_saved_tokens(tmp_path, monkeypatch, isolated_legacy_console_user):
    from bit import bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY='balance-secret')
    monkeypatch.setenv('BIT_BROWSER_EXTENSION_MODEL_CONFIG', str(tmp_path / 'tokens.json'))
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'shared-secret')
    workbench.browser_extension_models.save_settings(
        31, {'deepseek_api_key': 'account-31-secret', 'openai_api_key': 'account-openai-secret'}, 'balance-secret',
    )
    workbench.browser_extension_models.save_settings(
        32, {'wan_api_key': 'account-32-secret'}, 'balance-secret',
    )
    calls = []

    def query(user_id, api_key, *, force):
        calls.append((user_id, api_key, force))
        return balance.summarize_balance(payload('4.99'))

    monkeypatch.setattr(balance, 'get_balance_alert', query)
    monkeypatch.setattr(workbench, '_resolve_ai_original_text_api_key', lambda *args: pytest.fail('No shared fallback'))
    for user_id, names in [(31, ['deepseek', 'openai']), (32, ['wan']), (33, [])]:
        client = workbench.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session['workbench_user'] = {'id': user_id, 'username': 'tester', 'access_version': 0}
        response = client.get('/api/account-integrations/balances?refresh=1')
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['Pragma'] == 'no-cache'
        data = response.get_json()['data']
        assert [item['provider'] for item in data['providers']] == names
        if user_id == 31:
            assert data['status'] == 'low'
        assert 'secret' not in response.get_data(as_text=True)
    assert calls == [(31, 'account-31-secret', True)]


def test_balances_api_requires_login():
    from bit import bit_interface as workbench

    response = workbench.app.test_client().get('/api/account-integrations/balances')
    assert response.status_code == 401
