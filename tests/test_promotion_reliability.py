from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import pytest

from bit import mercado_promotions as service
from erp.mercadolibre_promotion_store import PromotionStore
from mercado_api.promotions import MercadoPromotionsClient
from mercado_api.client import MercadoAPIError
from test_mercado_promotions import FakeClient, FakeSession, FakeResponse


@pytest.fixture
def store(tmp_path):
    return PromotionStore(tmp_path / 'promotion.sqlite3')


def setup_job(store, action='enroll', status='candidate'):
    pk = store.upsert_promotion(token_id=1,store_name='test',application_id='app',seller_id='child',site_id='MLM',
        row={'id':'sale','type':'DEAL','status':'started'})
    store.replace_items(pk,[{'id':'MLM1','status':status,'price':15,'original_price':20,'currency_id':'USD','max_price':18}])
    preview=store.create_preview(actor='test',action=action,promotion_fk=pk,payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    job=store.create_job(preview_id=preview,actor='test',action=action,total=1)
    return store.get_promotion(pk),job,dict(item_id='MLM1',deal_price=15,currency_id='USD')


def test_promotion_client_refreshes_persisted_token_after_long_job(monkeypatch):
    client=FakeClient(FakeSession())
    monkeypatch.setattr(service,'_client_and_token',lambda token:(client,token))
    monkeypatch.setattr(service.bit_mysql,'get_mercado_store_token',lambda token:{'access_token':'token-a'})
    refreshed=[]
    monkeypatch.setattr(service,'_refresh_token',lambda token:refreshed.append(token) or {'access_token':'fresh'})
    adapter,_=service._adapter({'id':7,'access_token':'token-a'})
    adapter.client._refresh_access_token()
    assert client.access_token=='fresh' and refreshed==[7]
    monkeypatch.setattr(service.bit_mysql,'get_mercado_store_token',lambda token:{'access_token':'newer'})
    adapter.client._refresh_access_token()
    assert client.access_token=='newer' and refreshed==[7]


@pytest.mark.parametrize('status,error',[(423,'ENTITY_LOCKED'),(429,'RATE_LIMITED'),(409,'internal_capacity_conflict')])
def test_temporary_rejections_retry_sequentially_with_server_delay(monkeypatch,status,error):
    delays=[]
    monkeypatch.setattr('mercado_api.promotions.time.sleep',delays.append)
    first=FakeResponse(status=status,payload={'error':error});first.headers={'Retry-After':'3'}
    session=FakeSession([first,FakeResponse(payload={'price':15})])
    adapter=MercadoPromotionsClient(FakeClient(session))
    assert adapter.enroll_item('child','MLM1',promotion_id='sale',promotion_type='DEAL',deal_price=15)['price']==15
    assert len(session.calls)==2 and delays==[3]


def test_business_error_is_not_replayed():
    session=FakeSession([FakeResponse(status=409,payload={'error':'REPEATED_FAILED_REQUEST'})])
    with pytest.raises(MercadoAPIError):
        MercadoPromotionsClient(FakeClient(session)).enroll_item('child','MLM1',promotion_id='sale',promotion_type='DEAL',deal_price=15)
    assert len(session.calls)==1


def test_ambiguous_enrollment_is_verified_without_posting_again(store,monkeypatch):
    promotion,job,row=setup_job(store)
    calls=[]
    def enroll(*args,**kwargs):
        calls.append('POST');raise MercadoAPIError('500 结果未知')
    adapter=SimpleNamespace(enroll_item=enroll,get_item_promotions=lambda *args:[{
        'id':'sale','type':'DEAL','status':'pending','price':15,'currency_id':'USD'}])
    result=service._execute_job(store,job,adapter,promotion,[row],'enroll')
    assert calls==['POST']
    assert result['results'][0]['status']=='succeeded'
    assert '核实成功' in result['results'][0]['message']
    assert store.list_items_page(promotion['id'],enrolled='1')['total']==1


def test_other_campaign_or_wrong_price_does_not_resolve_unknown(store,monkeypatch):
    promotion,job,row=setup_job(store)
    monkeypatch.setattr(service.time,'sleep',lambda delay:None)
    def enroll(*args,**kwargs):raise MercadoAPIError('500 结果未知')
    adapter=SimpleNamespace(enroll_item=enroll,get_item_promotions=lambda *args:[{
        'id':'sale','type':'DEAL','status':'started','price':16,'currency_id':'USD'}])
    result=service._execute_job(store,job,adapter,promotion,[row],'enroll')
    assert result['results'][0]['status']=='unknown'
    assert store.list_items_page(promotion['id'],enrolled='1')['total']==0


def test_withdrawn_product_can_be_reselected_and_official_eligibility_is_checked(store,monkeypatch):
    promotion,_,_=setup_job(store,status='withdrawn')
    monkeypatch.setattr(service,'enrich_item_products',lambda p,rows:rows)
    item=store.list_items(promotion['id'])[0]
    preview=service.create_preview(promotion_fk=promotion['id'],action='enroll',actor='test',store=store,
        rows=[{'item_local_id':item['id'],'deal_price':15}])
    assert preview['ready_count']==1
    row=preview['rows'][0]
    assert row['requires_eligibility_check']
    job=store.create_job(preview_id=preview['preview_id'],actor='test',action='enroll',total=1)
    calls=[]
    adapter=SimpleNamespace(enroll_item=lambda *a,**kw:calls.append('POST') or {},
        get_item_promotions=lambda *a:[{'id':'sale','type':'DEAL','status':'candidate','max_discounted_price':18}])
    result=service._execute_job(store,job,adapter,promotion,[row],'enroll')
    assert result['results'][0]['status']=='succeeded' and calls==['POST']


def test_stale_preview_does_not_submit_product_already_enrolled(store):
    promotion,job,row=setup_job(store,status='pending_approval')
    adapter=SimpleNamespace(enroll_item=lambda *a,**kw:pytest.fail('must not post again'))
    result=service._execute_job(store,job,adapter,promotion,[row],'enroll')
    assert result['results'][0]['status']=='succeeded'
    assert '跳过重复' in result['results'][0]['message']


def test_complete_official_sync_resolves_unknown_but_never_overwrites_running_work(store):
    promotion,job,row=setup_job(store)
    store.finish_job(job,results=[dict(row,status='unknown',message='500')])
    official=[{'id':'MLM1','status':'pending','price':15,'currency_id':'USD'}]
    assert store.reconcile_completed_jobs(promotion['id'],official)==1
    assert store.latest_job(promotion['id'])['unknown_count']==0
    assert store.latest_job(promotion['id'])['success_count']==1
    promotion,job,row=setup_job(store)
    store.save_job_progress(job,results=[dict(row,status='unknown')])
    assert store.reconcile_completed_jobs(promotion['id'],official)==0
    assert store.latest_job(promotion['id'])['status']=='running'
