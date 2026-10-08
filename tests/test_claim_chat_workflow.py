"""Claim progressive reads and Chinese-only reply workflow; no live sends."""
import threading
import time

import pytest

from bit.mercado_communications import execute_store_communication
from mercado_api.communications import MercadoCommunicationsClient
from test_mercado_communications import Response, Session, _workbench_user
from bit import bit_interface


def execute(action, payload, session, translator=None, **kwargs):
    return execute_store_communication(7, action, payload,
        get_token=lambda _: {'access_token': 'secret', 'meli_user_id': '123', 'site_id': 'CBT'},
        refresh_token=lambda _: None, http=session, translator=translator, **kwargs)


def test_core_reads_only_claim_and_chat():
    session = Session([Response(payload={'id': 100, 'reason_id': 'PDD1'}),
                       Response(payload=[{'message': 'Hola'}])])
    bundle = execute('claims-detail', {'claim_id': '100', 'section': 'core'}, session)
    assert len(session.calls) == 2
    assert session.calls[-1][1].endswith('/100/messages')
    assert bundle['messages'][0]['message'] == 'Hola'
    assert bundle['extras_pending'] is True
    assert all(call[2]['timeout'] == 8 for call in session.calls)


def test_extras_read_concurrently_with_independent_sessions_and_one_refresh(monkeypatch):
    initial = Session([Response(payload={'id': 100, 'reason_id': 'PDD1'})])
    barrier = threading.Barrier(4)
    refreshes = []
    sessions = []
    class WorkerSession:
        trust_env = False
        def __init__(self):
            sessions.append(self)
            self.calls = 0
            self.closed = False
        def request(self, method, url, **kwargs):
            self.calls += 1
            if self.calls == 1:
                barrier.wait(timeout=2)
                return Response(401, {'message': 'expired'})
            assert kwargs['headers']['Authorization'] == 'Bearer refreshed'
            if url.endswith('/affects-reputation'):
                return Response(403, {'message': 'unavailable'})
            return Response(payload=[] if url.endswith('/expected-resolutions') else {})
        def close(self): self.closed = True
    monkeypatch.setattr('mercado_api.communications.requests.Session', WorkerSession)
    client = MercadoCommunicationsClient('secret', session=initial,
        refresh_access_token=lambda: refreshes.append(True) or 'refreshed')
    result = client.get_claim_bundle('100', section='extras')
    assert len(sessions) == 4 and all(session.closed for session in sessions)
    assert refreshes == [True]
    assert set(result['resource_errors']) == {'affects_reputation'}
    assert 'messages' not in result


@pytest.mark.parametrize('site,language,label', [('MLB', 'pt-BR', 'Português'), ('MLM', 'es', 'Español'), ('MLA', 'es', 'Español')])
def test_chinese_reply_sends_one_english_and_local_message(site, language, label):
    session = Session([Response(payload={'id': 100, 'site_id': site}), Response(201, {'id': 'sent'})])
    calls = []
    def translator(texts, source, target):
        calls.append((texts, source, target))
        return ['We will help.' if target == 'en' else 'LOCAL TRANSLATION']
    result = execute('claims-send', {'claim_id': '100', 'receiver_role': 'mediator',
        'message': '我们会帮助您。', 'auto_translate': True, 'site_id': 'MLB'}, session, translator)
    assert calls == [(['我们会帮助您。'], 'zh-CN', 'en'), (['We will help.'], 'en', language)]
    assert session.calls[-1][2]['json'] == {'receiver_role': 'mediator',
        'message': f'English:\nWe will help.\n\n{label}:\nLOCAL TRANSLATION', 'attachments': []}
    assert result['translation']['site_id'] == site
    assert sum(call[0] == 'POST' for call in session.calls) == 1


def test_order_context_resolves_cbt_language():
    session = Session([Response(payload={'id': 100, 'resource_id': 200}), Response(201, {})])
    result = execute('claims-send', {'claim_id': '100', 'receiver_role': 'complainant',
        'message': '您好', 'auto_translate': True}, session,
        lambda texts, source, target: ['Hello'],
        get_order_contexts=lambda token, ids: [{'site_id': 'MLB'}])
    assert result['translation']['target_language'] == 'pt-BR'


@pytest.mark.parametrize('failure', ['language', 'translation'])
def test_reply_does_not_send_if_language_or_translation_fails(failure):
    session = Session([Response(payload={'id': 100, 'site_id': 'UNKNOWN' if failure == 'language' else 'MLM'})])
    def fail_translation(*args): raise RuntimeError('translation unavailable')
    with pytest.raises((ValueError, RuntimeError)):
        execute('claims-send', {'claim_id': '100', 'receiver_role': 'complainant',
            'message': '您好', 'auto_translate': True}, session, fail_translation)
    assert all(call[0] == 'GET' for call in session.calls)


def test_claim_translation_view_permission(monkeypatch):
    monkeypatch.setattr(bit_interface, 'get_current_workbench_user', lambda: _workbench_user('customer_service.view'))
    calls = []
    monkeypatch.setattr(bit_interface.bit_db_api, 'execute_mercado_store_communication',
        lambda token, action, payload: calls.append((token, action)) or {'translations': ['您好']})
    response = bit_interface.app.test_client().post('/api/mercado-communications/7/claims-translate', json={'texts': ['Hola']})
    assert response.status_code == 200
    assert calls == [(7, 'claims-translate')]


def test_short_portuguese_chat_uses_claim_site_to_disambiguate(monkeypatch):
    from erp import mercadolibre_translation as translation
    import langid
    translation.detect_text_language.cache_clear()
    monkeypatch.setattr(langid, 'classify', lambda _: ('es', 1))
    session = Session([Response(payload={'id': 100, 'site_id': 'MLB'})])
    calls = []
    result = execute('claims-translate', {'claim_id': '100', 'texts': ['Olá, preciso de ajuda.']},
        session, lambda texts, source, target: calls.append((source, target)) or ['您好，我需要帮助。'])
    assert calls == [('pt-BR', 'zh-CN')]
    assert result['translations'] == ['您好，我需要帮助。']
    translation.detect_text_language.cache_clear()


def test_auto_translation_handles_mixed_incoming_languages(monkeypatch):
    from erp import mercadolibre_translation as translation
    import langid
    translation._TRANSLATION_CACHE.clear()
    translation.detect_text_language.cache_clear()
    monkeypatch.setattr(langid, 'classify', lambda text: ('zh' if text == '您好' else 'en' if text == 'Hello' else 'pt', 1))
    calls = []
    monkeypatch.setattr(translation, '_argos_batch_translate', lambda texts, source, target: calls.append((texts, source, target)) or ['中文'])
    assert translation.translate_texts(['Hello', 'Olá', '您好'], 'auto', 'zh-CN') == ['中文', '中文', '您好']
    assert calls == [(['Hello'], 'en', 'zh-CN'), (['Olá'], 'pt-BR', 'zh-CN')]


def test_background_translation_polls_one_job_without_blocking(monkeypatch):
    from bit import communication_preload, mercado_communications
    from bit.communication_preload import CommunicationPreload
    queue = CommunicationPreload(workers=1)
    monkeypatch.setattr(communication_preload, 'claim_translation_preload', queue)
    monkeypatch.setattr(mercado_communications, '_claim_language', lambda *args: ('MLB', 'pt-BR'))
    monkeypatch.setattr('erp.mercadolibre_translation.detect_text_language', lambda *args: 'pt-BR')
    release = threading.Event()
    calls = []
    def translator(*args):
        calls.append(True)
        release.wait(2)
        return ['您好']
    payload = {'claim_id': '100', 'texts': ['Olá'], 'background': True}
    try:
        assert execute('claims-translate', payload, Session([]), translator) == {'pending': True}
        assert execute('claims-translate', payload, Session([]), translator) == {'pending': True}
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = execute('claims-translate', payload, Session([]), translator)
            if not result.get('pending'): break
            time.sleep(.01)
        assert result == {'translations': ['您好']}
        assert calls == [True]
    finally:
        release.set()
        queue.executor.shutdown(wait=True)


def test_claim_attachment_binary_read_and_old_claim_fallback():
    import base64
    image = Response()
    image.content = b'\xff\xd8\xffbuyer-photo'
    image.headers['Content-Type'] = 'image/jpeg'
    session = Session([Response(400, {'message': 'Available only for claims 1.0'}), image])
    result = execute('claims-attachment', {'claim_id': '100', 'attachment_id': 'buyer.jpg'}, session)
    assert base64.b64decode(result['content_base64']) == image.content
    assert result['content_type'] == 'image/jpeg'
    assert session.calls[-1][1].endswith('/post-purchase/v1/claims/100/attachments/buyer.jpg/download')


@pytest.mark.parametrize('filename', ['../secret', 'https://evil.test/x.jpg', 'buyer.jpg?token=x', 'x\r\nHeader'])
def test_attachment_rejects_paths_and_urls(filename):
    session = Session([])
    with pytest.raises(ValueError):
        execute('claims-attachment', {'claim_id': '100', 'attachment_id': filename}, session)
    assert not session.calls


def test_attachment_view_route_returns_private_image(monkeypatch):
    import base64
    monkeypatch.setattr(bit_interface, 'get_current_workbench_user', lambda: _workbench_user('customer_service.view'))
    calls = []
    def attachment(token, action, payload):
        calls.append((token, action, payload))
        return {'filename': 'buyer.jpg', 'content_type': 'image/jpeg', 'content_base64': base64.b64encode(b'photo').decode()}
    monkeypatch.setattr(bit_interface.bit_db_api, 'execute_mercado_store_communication', attachment)
    response = bit_interface.app.test_client().get('/api/mercado-communications/7/claims-attachment?claim_id=100&attachment_id=buyer.jpg')
    assert response.status_code == 200
    assert response.data == b'photo' and response.mimetype == 'image/jpeg'
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert calls == [(7, 'claims-attachment', {'claim_id': '100', 'attachment_id': 'buyer.jpg'})]


def test_post_sale_attachment_resolves_cbt_order_site():
    image = Response()
    image.headers['Content-Type'] = 'image/png'
    session = Session([image])
    execute('post-sale-attachment', {'pack_id': '200', 'site_id': 'CBT', 'attachment_id': 'buyer.png'}, session,
            get_order_contexts=lambda token, ids: [{'site_id': 'MLB'}])
    assert session.calls[0][2]['params'] == {'tag': 'post_sale', 'site_id': 'MLB'}
