from io import BytesIO
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
import requests
from flask import Flask
from werkzeug.datastructures import FileStorage

from bit import mercado_rights_reply as reply
from bit import mercado_action_center as center

ROW = {'token_id': 7, 'infraction_id': '123', 'item_id': 'MLM100', 'site_id': 'MLM'}


class Session:
    def __init__(self):
        self.posts = []
        self.puts = []
        self.status = 200
        self.error = None

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status, ok=200 <= self.status < 300)

    def put(self, url, **kwargs):
        self.puts.append((url, kwargs))
        return SimpleNamespace(ok=True, json=lambda: {'file_name': '123.pdf'})

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(reply.bit_runtime_lock, 'RUNTIME_LOCK_DIR', tmp_path)
    detail = {'case_id': 123, 'item_info': {'item_id': 'MLM100'},
              'current_status': 'WAITING_DOCUMENTATION', 'reason_id': 'PPPI1',
              'due_date': (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}
    client = SimpleNamespace(session=Session(), access_token='test', BASE_URL='https://api.mercadolibre.com',
                             request=lambda *a, **kw: detail.copy())
    client.detail = detail
    monkeypatch.setattr(reply, 'store_client', lambda token_id: (client, {'id': token_id}))
    monkeypatch.setattr(reply, '_save_current_case', lambda *args: None)
    return client


def test_api_reply_sends_once_and_blocks_replay(client):
    assert reply.reply_case(7, ROW, 'Our evidence')['mode'] == 'submitted'
    url, kwargs = client.session.posts[0]
    assert url.endswith('/moderations/pppi/case/123')
    assert kwargs['json'] == {'seller_quittance': 'Our evidence', 'document_name': ' '}
    assert reply.reply_case(7, ROW, 'retry')['mode'] == 'unknown'
    assert len(client.session.posts) == 1


def test_attachment_uploaded_before_reply(client):
    file = FileStorage(stream=BytesIO(b'%PDF-1.7 test'), filename='proof.pdf')
    assert reply.reply_case(7, ROW, 'Evidence attached', file)['mode'] == 'submitted'
    assert client.session.puts[0][1]['params'] == {'case_id': '123', 'name': 'evidence.pdf'}
    assert client.session.posts[0][1]['json']['document_name'] == '123.pdf'


@pytest.mark.parametrize('failure', [requests.Timeout('timeout'), requests.ConnectionError('connection')])
def test_ambiguous_write_never_replayed(client, failure):
    client.session.error = failure
    assert reply.reply_case(7, ROW, 'reply')['mode'] == 'unknown'
    assert reply.reply_case(7, ROW, 'reply')['mode'] == 'unknown'
    assert len(client.session.posts) == 1


@pytest.mark.parametrize('status', [403, 404, 422])
def test_definite_api_rejection_allows_browser_fallback(client, status):
    client.session.status = status
    assert reply.reply_case(7, ROW, 'reply')['mode'] == 'browser'
    assert not (reply.bit_runtime_lock.RUNTIME_LOCK_DIR / 'rights-reply-7-123.json').exists()


def test_server_error_remains_unknown(client):
    client.session.status = 500
    assert reply.reply_case(7, ROW, 'reply')['mode'] == 'unknown'
    assert reply.reply_case(7, ROW, 'reply')['mode'] == 'unknown'
    assert len(client.session.posts) == 1


@pytest.mark.parametrize('change', [
    {'current_status': 'DOCUMENTATION_PRESENTED'},
    {'due_date': '2020-01-01T00:00:00Z'},
])
def test_stale_or_expired_case_not_submitted(client, change):
    client.detail.update(change)
    assert reply.reply_case(7, ROW, 'reply')['mode'] == 'closed'
    assert client.session.posts == []


def test_wrong_case_item_rejected(client):
    client.detail['item_info'] = {'item_id': 'MLB200'}
    with pytest.raises(ValueError, match='不一致'):
        reply.reply_case(7, ROW, 'reply')
    assert client.session.posts == []


def test_image_case_without_proof_submits_text_without_mutating_images(client):
    client.detail['reason_id'] = 'PPPI6'
    client.detail['photos_denounced'] = [{'id': 'reported-picture'}]
    assert reply.reply_case(7, ROW, 'reply')['mode'] == 'submitted'
    assert len(client.session.posts) == 1
    assert client.session.posts[0][1]['json'] == {
        'seller_quittance': 'reply', 'document_name': ' ',
    }
    assert client.session.puts == []


def test_image_text_reply_rejected_by_platform_falls_back(client):
    client.detail['reason_id'] = 'PPPI6'
    client.session.status = 422
    assert reply.reply_case(7, ROW, 'reply')['mode'] == 'browser'
    assert len(client.session.posts) == 1


@pytest.mark.parametrize('name,content', [('x.pdf', b'bad'), ('x.exe', b'%PDF-'), ('x.pdf', b'%PDF-' + b'x' * reply.MAX_DOCUMENT_BYTES)])
def test_invalid_file_never_sent(client, name, content):
    with pytest.raises(ValueError, match='5 MB'):
        reply.reply_case(7, ROW, 'reply', FileStorage(stream=BytesIO(content), filename=name))
    assert not client.session.posts and not client.session.puts


def route_client(monkeypatch, *, allowed=(7,), organization='alpha', execute=True):
    class Store:
        def list_mercado_store_tokens(self):
            return {'rows': [{'id': 7, 'organization_key': 'alpha'}, {'id': 8, 'organization_key': 'beta'}]}
        def list_mercado_prohibited_listings(self, **kwargs):
            assert kwargs['token_ids'] == [7]
            return {'rows': [ROW], 'total': 1}
    app = Flask(__name__)
    app.register_blueprint(center.create_action_center_blueprint(
        login_required=lambda fn: fn, current_user=lambda: {'organization_key': organization},
        authorized_token_ids=lambda user: allowed, storage=Store(),
        has_permission=lambda user, permission: execute,
    ))
    monkeypatch.setattr(reply, 'inspect_case', lambda *args: {'mode': 'api'})
    monkeypatch.setattr(reply, 'reply_case', lambda token, row, text, upload: {'mode': 'submitted', 'text': text})
    return app.test_client()


def test_route_authorized_case_get_and_submit(monkeypatch):
    client = route_client(monkeypatch)
    assert client.get('/api/mercado/today/rights-holder/7/123').json['data']['mode'] == 'api'
    response = client.post('/api/mercado/today/rights-holder/7/123', data={'text': 'my reply'})
    assert response.status_code == 200
    assert response.json['data']['text'] == 'my reply'
    assert response.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('kwargs,path,status', [
    ({'execute': False}, '7/123', 403),
    ({'allowed': ()}, '7/123', 404),
    ({'organization': 'beta'}, '7/123', 404),
    ({}, '7/999', 409),
    ({}, '7/abc', 400),
])
def test_route_rejects_unauthorized_or_wrong_case(monkeypatch, kwargs, path, status):
    client = route_client(monkeypatch, **kwargs)
    assert client.post('/api/mercado/today/rights-holder/' + path, data={'text': 'reply'}).status_code == status


@pytest.mark.usefixtures('isolated_legacy_console_user')
def test_browser_fallback_queues_only_authorized_store(monkeypatch):
    from bit import bit_interface as app_module
    queued = []
    class AgentStore:
        def get_agent(self, agent_id, **kwargs):
            return {'agent_id': agent_id, 'name': 'Office', 'online': True, 'capabilities': ['daily_task']}
        def enqueue_job(self, *args, **kwargs):
            queued.append((args, kwargs))
    monkeypatch.setattr(app_module, 'get_local_agent_store', AgentStore)
    monkeypatch.setattr(app_module, 'current_local_agent_bundle', lambda: {'version': 'test'})
    monkeypatch.setattr(app_module.bit_db_api, 'list_mercado_store_tokens', lambda: {'rows': [{'id': 7, 'display_name': 'Shop A'}]})
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session['workbench_user'] = {'id': 1, 'role_key': 'super_admin', 'is_platform_admin': True}
    response = client.post('/api/mercado/today/rights-holder/7/open-browser', json={'agent_id': 'office', 'target_url': 'https://untrusted.example'})
    assert response.status_code == 202
    assert queued[0][0][2] == 'open_store'
    payload = queued[0][0][3]
    assert payload['destination'] == 'rights_holder'
    assert payload['token_record']['display_name'] == 'Shop A'
    assert 'target_url' not in payload
    assert client.post('/api/mercado/today/rights-holder/8/open-browser', json={'agent_id': 'office'}).status_code == 404
    assert client.post('/api/mercado/today/rights-holder/7/open-browser', json={'execution_target': 'server'}).status_code == 400
    assert len(queued) == 1


def test_agent_rights_fallback_uses_fixed_url_and_store_mapping(monkeypatch):
    import threading
    import local_agent_worker
    from bit import bit_interface
    calls = []
    def open_browser(token_id, **kwargs):
        calls.append((token_id, kwargs))
        return {'shop_name': 'Shop A'}
    monkeypatch.setattr(bit_interface, '_open_mercado_claim_browser', open_browser)
    result = local_agent_worker.run_open_store({
        'token_id': 7, 'token_record': {'id': 7, 'display_name': 'Shop A'},
        'destination': 'rights_holder', 'target_url': 'https://untrusted.example',
    }, threading.Event())
    assert calls[0][1]['target_url'] == reply.RIGHTS_URL
    assert calls[0][1]['token_record']['id'] == 7
    assert '权利人回复' in result['message']


def test_unavailable_api_credentials_fall_back_without_sending(client, monkeypatch):
    def unavailable(*args):
        raise ValueError('missing token')
    monkeypatch.setattr(reply, 'store_client', unavailable)
    assert reply.inspect_case(7, ROW)['mode'] == 'browser'
    assert reply.reply_case(7, ROW, 'reply')['mode'] == 'browser'
    assert client.session.posts == []


def test_timeout_followed_by_platform_confirmation_reports_submitted(client):
    def post(url, **kwargs):
        client.session.posts.append((url, kwargs))
        client.detail['current_status'] = 'DOCUMENTATION_PRESENTED'
        raise requests.Timeout('response lost after save')
    client.session.post = post
    result = reply.reply_case(7, ROW, 'reply')
    assert result['mode'] == 'submitted'
    assert result['current_status'] == 'DOCUMENTATION_PRESENTED'
    assert len(client.session.posts) == 1


def test_server_error_is_recorded_and_status_rechecked(client):
    import json
    client.session.status = 500
    result = reply.reply_case(7, ROW, 'reply')
    assert result['mode'] == 'unknown'
    assert '已移出今日待办' in result['message']
    marker = reply.bit_runtime_lock.RUNTIME_LOCK_DIR / 'rights-reply-7-123.json'
    saved = json.loads(marker.read_text())
    assert saved['http_status'] == 500
    assert saved['platform_status'] == 'WAITING_DOCUMENTATION'
    assert len(client.session.posts) == 1
