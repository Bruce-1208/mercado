import concurrent.futures
import json

from bit.mercado_product_data import ProductDataService


def test_periodic_refresh_coalesces_between_process_instances(tmp_path):
    calls=[]
    def provider(message):
        calls.append(message)
        return {"renewed":True}, None
    services=[ProductDataService(tmp_path, provider) for _ in range(4)]
    with concurrent.futures.ThreadPoolExecutor(4) as executor:
        results=list(executor.map(lambda s:s.renew_session(),services))
    assert len(calls)==1
    assert all(r['state']=='healthy' for r in results)
    assert 'session_stamp' not in results[0]
    state=services[0].status()
    assert state['ready'] and state['renewal']['last_success']
    with services[0].database() as db:
        saved=services[0]._renewal_state(db)
        saved['next_attempt']=0
        services[0]._save_renewal(db,saved)
    services[0].renew_session()
    assert len(calls)==2


def test_query_401_refreshes_and_replays_once(tmp_path):
    calls=[]
    def provider(message):
        calls.append(message['action'])
        if message['action']=='refresh':return {'renewed':True},None
        if calls.count('products')==1:return None,401
        return [{'Sku':'MLM6226808220'}],None
    service=ProductDataService(tmp_path,provider)
    with service.database() as db:
        rows,error=service._authenticated_call(db,{'action':'products'})
    assert error is None and rows[0]['Sku']=='MLM6226808220'
    assert calls==['products','refresh','products']


def test_revoked_session_stops_until_administrator_replaces_file(tmp_path, monkeypatch):
    filename=tmp_path/'private.json'
    filename.write_text(json.dumps({'token':'old-test-token'}))
    monkeypatch.setenv('ZESHUN_MERCADO_SESSION_FILE',str(filename))
    calls=[]
    def provider(message):
        calls.append(message)
        return None,401
    service=ProductDataService(tmp_path/'cache',provider)
    assert service.renew_session()['state']=='requires_login'
    assert service.renew_session()['state']=='requires_login'
    with service.database() as db:
        assert service._authenticated_call(db,{'action':'products'})==(None,401)
    assert len(calls)==1
    assert not service.status()['ready']
    replacement=tmp_path/'new.json'
    replacement.write_text(json.dumps({'token':'new-test-token'}))
    replacement.replace(filename)
    service.provider=lambda message:({'renewed':True},None)
    assert service.renew_session()['state']=='healthy'
    assert service.status()['ready']


def test_no_endless_retry_when_refreshed_token_is_rejected(tmp_path):
    calls=[]
    def provider(message):
        calls.append(message['action'])
        return ({'renewed':True},None) if message['action']=='refresh' else (None,401)
    service=ProductDataService(tmp_path,provider)
    with service.database() as db:
        assert service._authenticated_call(db,{'action':'products'})==(None,401)
        assert service._authenticated_call(db,{'action':'products'})==(None,401)
    assert calls==['products','refresh','products']
    assert not service.status()['ready']


def test_throttle_and_transient_errors_back_off(tmp_path):
    for code in (429,502):
        calls=[]
        def provider(message):
            calls.append(message)
            return None,code
        service=ProductDataService(tmp_path/str(code),provider)
        result=service.renew_session()
        assert result['state']=='retry_wait'
        assert result['next_attempt']-result['last_attempt'] >= (900 if code==429 else 60)
        with service.database() as db:
            service._renew_session(db,force=True)
        assert len(calls)==1


def test_missing_token_is_not_reported_as_success(tmp_path):
    service=ProductDataService(tmp_path,lambda msg:({},None))
    assert service.renew_session()['state']=='retry_wait'


def test_disabled_or_unconfigured_does_not_call_network(tmp_path,monkeypatch):
    monkeypatch.delenv('ZESHUN_MERCADO_SESSION_FILE',raising=False)
    service=ProductDataService(tmp_path)
    assert service.renew_session()=={'state':'not_configured'}


def test_scheduler_starts_once_and_stops(tmp_path):
    import threading
    called=threading.Event()
    calls=[]
    def provider(message):
        calls.append(message)
        called.set()
        return {"renewed":True},None
    service=ProductDataService(tmp_path,provider)
    service.start_renewal_scheduler()
    thread=service.renewal_thread
    service.start_renewal_scheduler()
    assert service.renewal_thread is thread
    assert called.wait(2)
    service.close()
    thread.join(2)
    assert not thread.is_alive() and len(calls)==1
