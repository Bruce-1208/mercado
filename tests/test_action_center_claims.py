"""Today's claims must reflect conversation state, not historical notifications."""
from copy import deepcopy
from flask import Flask
import pytest
from bit import mercado_action_center as module


def message(sender='complainant', receiver='respondent', date='2026-01-01T00:00:00Z', text='Olá, preciso de ajuda.'):
    return dict(sender_role=sender, receiver_role=receiver, message_date=date, message=text)


def claim(claim_id, messages=None, **fields):
    return dict(claim=dict(id=str(claim_id), status='opened', resource='order', resource_id='200', site_id='MLB',
                           players=[dict(type='seller', role='respondent', available_actions=[{'action':'send_message_to_complainant'}])], **fields),
                messages=messages if messages is not None else [message()])


class Store:
    def __init__(self, bundles=None):
        self.bundles = bundles or {'1': claim('1')}
        self.calls = []
        self.translation = {'translations':['您好，我需要帮助。']}
    def list_mercado_store_tokens(self):
        return {'rows':[dict(id=7, display_name='测试店铺', organization_key='org', salesperson='A'),
                        dict(id=8, organization_key='other', salesperson='A'),
                        dict(id=9, organization_key='org', salesperson='B')]}
    def execute_mercado_store_communication(self, token_id, action, data):
        self.calls.append((token_id, action, deepcopy(data)))
        if action == 'claims-list':
            # Small pages exercise complete paging despite a requested limit of 100.
            rows = [dict(bundle['claim']) for bundle in self.bundles.values()]
            return {'data':rows[data['offset']:data['offset']+2], 'paging':{'total':len(rows)}}
        if action == 'claims-detail':
            assert data['section'] == 'core'
            return deepcopy(self.bundles[data['claim_id']])
        if action == 'claims-translate':
            if isinstance(self.translation, Exception): raise self.translation
            return self.translation
        raise AssertionError('Unexpected communication: '+action)
    def list_mercado_operator_tasks(self, **kwargs):
        return {'rows':[dict(id=i, topic=topic) for i,topic in enumerate(['messages','marketplace messages','claims','marketplace_claims','items'])]}
    def list_mercado_notification_events(self, **kwargs): return {'rows':[]}
    def list_mercado_prohibited_listings(self, **kwargs): return {'rows':[]}
    def list_mercado_today_work_states(self, **kwargs): return []


def client(store, allowed=None, permission=True):
    app = Flask(__name__)
    app.secret_key = 'test'
    app.register_blueprint(module.create_action_center_blueprint(
        login_required=lambda fn:fn, current_user=lambda:{'organization_key':'org'},
        authorized_token_ids=lambda user:allowed if allowed is not None else [7], storage=store,
        has_permission=lambda user,key:permission))
    return app.test_client()


def test_only_unanswered_order_claims_from_buyer_or_mediator_are_shown():
    bundles = {'1':claim('1'),
               '2':claim('2', [message(date='2026-01-03T00:00:00Z',sender='respondent',receiver='complainant'),message()]),
               '3':claim('3', [message(sender='mediator')]),
               '4':claim('4', [message(sender='mediator',receiver='complainant')]),
               '5':claim('5', []), '6':claim('6'), '7':claim('7'), '8':claim('8')}
    bundles['6']['claim']['status']='closed'
    bundles['7']['claim']['resource']='payment'
    bundles['8']['claim']['players'][0]['available_actions']=[]
    store=Store(bundles)
    result=module._unanswered_claims(store,[{'id':7}])
    assert {r['claim_id'] for r in result['rows']} == {'1','3'}
    assert result['errors']==[]
    assert [c[2]['offset'] for c in store.calls if c[1]=='claims-list']==[0,2,4,6]
    assert result['rows'][0]['latest_at'].startswith('2026-01')  # No seven-day cutoff.


@pytest.mark.parametrize('failure', ['time','role','messages','store'])
def test_uncertain_reply_state_reports_error_and_preserves_other_claims(failure):
    bundles={'1':claim('1'),'2':claim('2')}
    if failure=='time': bundles['2']['messages'][0]['message_date']=None
    elif failure=='role': bundles['2']['messages'][0]['sender_role']=None
    elif failure=='messages': bundles['2']['resource_errors']={'messages':'messages unavailable'}
    store=Store(bundles)
    if failure=='store':
        original=store.execute_mercado_store_communication
        def read(token,action,data):
            if action=='claims-list' and data['offset']>0: raise ValueError('partial scan')
            result=original(token,action,data)
            if action=='claims-list': result['paging']['total']=3
            return result
        store.execute_mercado_store_communication=read
    result=module._unanswered_claims(store,[{'id':7}])
    assert '1' in {r['claim_id'] for r in result['rows']}
    assert result['errors']
    if failure!='store': assert len(result['rows'])==1


def test_claim_endpoint_is_scoped_cached_and_rechecks_permission():
    store=Store()
    allowed=[7,8,9]
    api=client(store,allowed)
    data=api.get('/api/mercado/today/claims?salesperson=A').json['data']
    row=data['rows'][0]
    assert row['text']=='Olá, preciso de ajuda.'
    assert row['text_zh']=='您好，我需要帮助。'
    assert row['work_ref'] and row['work_status']=='open'
    assert {c[0] for c in store.calls}=={7}
    api.get('/api/mercado/today/claims?salesperson=A')
    assert len([c for c in store.calls if c[1]=='claims-list'])==1
    allowed.clear()
    assert api.get('/api/mercado/today/claims').json['data']['rows']==[]
    assert client(Store(),permission=False).get('/api/mercado/today/claims').status_code==403


def test_translation_pending_completion_failure_and_retry_preserve_original():
    store=Store()
    api=client(store)
    store.translation={'pending':True}
    data=api.get('/api/mercado/today/claims').json['data']
    assert data['refreshing'] and data['rows'][0]['translation_pending']
    store.translation={'translations':['您好']}
    data=api.get('/api/mercado/today/claims').json['data']
    assert not data['refreshing'] and data['rows'][0]['text_zh']=='您好'
    store.translation=ValueError('offline')
    row=api.get('/api/mercado/today/claims').json['data']['rows'][0]
    assert row['translation_error']=='offline' and row['text']=='Olá, preciso de ajuda.'
    assert 'text_zh' not in row  # Cached source rows were not mutated.
    store.translation={'translations':['您好']}
    api.get('/api/mercado/today/claims?refresh=1')
    assert store.calls[-1][2]['retry'] is True


def test_rich_text_and_attachments_are_readable_and_safe():
    text='<p>Olá, <b>vendedor!</b></p><p>Preciso de ajuda.<br>Obrigado.</p><script>unsafe()</script>'
    store=Store({'1':claim('1',[message(text=text)]),'2':claim('2',[message(text='')])})
    rows=client(store).get('/api/mercado/today/claims').json['data']['rows']
    assert rows[0]['text']=='Olá, vendedor!\nPreciso de ajuda.\nObrigado.'
    assert rows[1]['text']==rows[1]['text_zh']=='（附件消息）'
    assert len([c for c in store.calls if c[1]=='claims-translate'])==1
    assert module._claim_message_text('&lt;p&gt;Olá &amp; bom dia&lt;/p&gt;')=='Olá & bom dia'


def test_stale_claim_and_message_notifications_do_not_reappear_in_tasks(monkeypatch):
    monkeypatch.setattr(module,'_refresh_order_watch_tasks',lambda *args:(set(),set()))
    data=client(Store()).get('/api/mercado/today/tasks?include_closed=1').json['data']
    assert [r['topic'] for r in data['rows']]==['items']
