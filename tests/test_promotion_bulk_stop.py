from types import SimpleNamespace

from flask import Flask
import pytest

from bit import mercado_promotions as service
from bit import mercado_promotion_routes as routes
from erp.mercadolibre_promotion_store import PromotionStore


@pytest.fixture
def store(tmp_path):
    return PromotionStore(tmp_path / 'promotions.sqlite3')


def campaign(store, token=1, kind='DEAL'):
    return store.upsert_promotion(token_id=token, store_name='店铺', application_id='app',
        seller_id=str(token), site_id='MLM', row={'id':f'sale-{token}-{kind}', 'name':'活动', 'type':kind, 'status':'started'})


def test_stop_covers_all_pages_and_keeps_failed_offers_enrolled(store, monkeypatch):
    pk = campaign(store)
    other = campaign(store, token=2)
    rows = [{'id':f'MLM{i}', 'status':'pending_approval', 'offer_id':f'offer-{i}'} for i in range(125)]
    store.replace_items(pk, rows + [{'id':'candidate', 'status':'candidate'}])
    store.replace_items(other, rows[:1])
    calls = []
    def withdraw(seller, item, **kwargs):
        calls.append((item, kwargs))
        if item == 'MLM3':
            raise service.MercadoAPIError('拒绝取消')
        rows[:] = [r for r in rows if r['id'] != item]
        return {}
    monkeypatch.setattr(service.bit_mysql, 'get_mercado_store_token', lambda token: {})
    monkeypatch.setattr(service, '_adapter', lambda token: (SimpleNamespace(withdraw_item=withdraw, iter_items=lambda *a, **kw: list(rows)), token))
    result = service.stop_campaign(promotion_fk=pk, actor='tester', store=store, background=False)
    assert len(calls) == result['total'] == 125
    assert all(options['promotion_id']=='sale-1-DEAL' and options['offer_id'] for _,options in calls)
    assert store.list_items_page(pk, enrolled='1')['total'] == 1
    assert store.list_items_page(other, enrolled='1')['total'] == 1
    assert store.latest_job(pk)['success_count'] == 124
    assert store.latest_job(pk)['failed_count'] == 1
    assert not any(row['item_id']=='candidate' for row in store.list_items(pk))  # official snapshot replaces stale cache


def test_empty_and_unsupported_campaign_do_not_submit(store, monkeypatch):
    monkeypatch.setattr(service.bit_mysql, 'get_mercado_store_token', lambda token: {})
    monkeypatch.setattr(service, '_adapter', lambda token: (SimpleNamespace(iter_items=lambda *a, **kw: []), token))
    pk = campaign(store)
    service.stop_campaign(promotion_fk=pk, actor='tester', store=store, background=False)
    assert store.latest_job(pk)['status'] == 'succeeded'
    with pytest.raises(ValueError, match='禁止取消'):
        service.stop_campaign(promotion_fk=campaign(store, kind='CLEARANCE'), actor='tester', store=store)


def test_empty_official_snapshot_replaces_stale_local_stop_total(store, monkeypatch):
    pk = campaign(store)
    store.replace_items(pk, [
        {'id': 'MLM1', 'status': 'started', 'offer_id': 'offer-1'},
        {'id': 'MLM2', 'status': 'pending_approval', 'offer_id': 'offer-2'},
    ])
    monkeypatch.setattr(service.bit_mysql, 'get_mercado_store_token', lambda token: {})
    monkeypatch.setattr(service, '_adapter', lambda token: (
        SimpleNamespace(iter_items=lambda *args, **kwargs: []), token,
    ))

    result = service.stop_campaign(promotion_fk=pk, actor='tester', store=store, background=False)

    job = store.latest_job(pk)
    assert result['total'] == 0
    assert job['status'] == 'succeeded'
    assert job['results'] == []
    assert job['total_count'] == 0
    assert sum((job['success_count'], job['failed_count'], job['unknown_count'], job['blocked_count'])) == job['total_count']
    assert store.list_items(pk) == []


def test_stop_route_authorizes_entire_selection_and_requires_confirmation(store, monkeypatch):
    one, other = campaign(store), campaign(store, token=2)
    calls=[]
    monkeypatch.setattr(routes, 'PromotionStore', lambda: store)
    monkeypatch.setattr(routes, 'stop_campaign', lambda **kwargs: calls.append(kwargs['promotion_fk']) or {'job_id':'job','status':'running'})
    permission={'allowed':True}
    app=Flask(__name__)
    app.register_blueprint(routes.create_promotions_blueprint(login_required=lambda fn:fn,
        current_user=lambda:{'username':'tester'}, authorized_token_ids=lambda user:[1],
        has_permission=lambda *args:permission['allowed']))
    client=app.test_client()
    assert client.post('/api/promotions/stop',json={'promotion_ids':[one,other],'confirmed':True}).status_code==403
    assert not calls
    assert client.post('/api/promotions/stop',json={'promotion_ids':[one]}).status_code==400
    permission['allowed']=False
    assert client.post('/api/promotions/stop',json={'promotion_ids':[one],'confirmed':True}).status_code==403
    assert not calls
    permission['allowed']=True
    result=client.post('/api/promotions/stop',json={'promotion_ids':[one,one],'confirmed':True})
    assert result.status_code==200
    assert calls==[one]
    assert result.get_json()['data']['results'][0]['status']=='running'


def test_job_summary_queries_specific_jobs_without_returning_history_and_keeps_permissions(store, monkeypatch):
    from datetime import datetime, timedelta, timezone
    jobs=[]
    for token in (1,2):
        pk=campaign(store, token=token)
        preview=store.create_preview(actor='tester',action='withdraw',promotion_fk=pk,payload={},
            expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
        job=store.create_job(preview_id=preview,actor='tester',action='withdraw',total=1000)
        store.save_job_progress(job,results=[{'item_id':f'MLM{i}','status':'succeeded'} for i in range(500)])
        jobs.append(job)
    monkeypatch.setattr(routes,'PromotionStore',lambda:store)
    app=Flask(__name__)
    app.register_blueprint(routes.create_promotions_blueprint(login_required=lambda fn:fn,
        current_user=lambda:{'username':'tester'},authorized_token_ids=lambda user:[1]))
    client=app.test_client()
    response=client.get('/api/promotions/jobs',query_string=[('summary','1'),('job_id',jobs[0]),('job_id',jobs[1])])
    rows=response.get_json()['data']['rows']
    assert len(rows)==1 and rows[0]['job_id']==jobs[0]
    assert rows[0]['success_count']==500 and rows[0]['total_count']==1000
    assert 'results' not in rows[0] and 'results_json' not in rows[0]
    assert len(response.data)<1000
    assert response.headers['Cache-Control']=='no-store'
    assert store.list_jobs(job_ids=[])==[]
    assert client.get('/api/promotions/jobs?summary=1&action=enroll&status=running').get_json()['data']['rows']==[]


def stop_adapter(monkeypatch, live, withdraw):
    adapter = SimpleNamespace(iter_items=lambda *a, **kw: list(live), withdraw_item=withdraw)
    monkeypatch.setattr(service.bit_mysql, 'get_mercado_store_token', lambda token: {})
    monkeypatch.setattr(service, '_adapter', lambda token: (adapter, token))
    return adapter


def test_stop_finds_official_items_missing_from_local_snapshot(store, monkeypatch):
    pk = campaign(store, kind='SMART')
    live = [{'id': f'MLM{i}', 'offer_id': f'o{i}', 'status': 'started'} for i in range(130)]
    calls=[]
    def withdraw(seller, item, **kw):
        calls.append((item,kw))
        live[:] = [row for row in live if row['id'] != item]
        return {}
    stop_adapter(monkeypatch, live, withdraw)
    service.stop_campaign(promotion_fk=pk, actor='tester', store=store, background=False)
    assert len(calls) == 130 and all(kw['promotion_type']=='SMART' for _,kw in calls)
    assert store.latest_job(pk)['status']=='succeeded'
    assert store.latest_job(pk)['total_count']==130
    assert store.list_promotions()[0]['participated']


def test_acknowledged_but_still_active_never_reports_complete_and_retry_is_bounded(store, monkeypatch):
    pk=campaign(store)
    live=[{'id':'MLM1', 'status':'started', 'offer_id':'o1'}]
    calls=[]
    stop_adapter(monkeypatch, live, lambda *a, **kw: calls.append(kw) or {})
    service.stop_campaign(promotion_fk=pk, actor='tester', store=store, background=False)
    job=store.latest_job(pk)
    assert len(calls)==2
    assert job['status']=='partial' and job['unknown_count']==1 and job['success_count']==0
    assert store.list_items_page(pk,enrolled='1')['total']==1
    assert store.list_promotions()[0]['stop_summary']['unknown_count']==1


def test_stop_second_pass_covers_newly_discovered_offer(store, monkeypatch):
    pk=campaign(store)
    live=[{'id':'MLM1', 'status':'started', 'offer_id':'o1'}]
    calls=[]
    def withdraw(seller,item,**kw):
        calls.append(item)
        live.clear()
        if item=='MLM1':live.append({'id':'MLM2','status':'started','offer_id':'o2'})
        return {}
    stop_adapter(monkeypatch,live,withdraw)
    service.stop_campaign(promotion_fk=pk,actor='tester',store=store,background=False)
    assert calls==['MLM1','MLM2']
    assert store.latest_job(pk)['success_count']==2 and store.latest_job(pk)['status']=='succeeded'


def test_failed_final_read_keeps_job_unknown_even_after_delete_success(store, monkeypatch):
    pk=campaign(store)
    live=[{'id':'MLM1','status':'started','offer_id':'o1'}]
    adapter=stop_adapter(monkeypatch,live,lambda *a,**kw: {})
    reads=0
    def snapshot(*a,**kw):
        nonlocal reads
        reads+=1
        if reads>1:raise service.MercadoAPIError('读取超时')
        return live
    adapter.iter_items=snapshot
    service.stop_campaign(promotion_fk=pk,actor='tester',store=store,background=False)
    job=store.latest_job(pk)
    assert job['status']=='partial' and job['success_count']==0
    assert any('读取超时' in r['message'] for r in job['results'])


def test_stop_campaigns_run_independently_and_block_duplicate_campaign(store, monkeypatch):
    import threading
    first,other=campaign(store),campaign(store,token=2)
    entered,release,done=threading.Event(),threading.Event(),threading.Event()
    def snapshots(seller,*args,**kwargs):
        if seller=='1':
            entered.set()
            assert release.wait(5)
        return []
    monkeypatch.setattr(service.bit_mysql,'get_mercado_store_token',lambda token:{})
    monkeypatch.setattr(service,'_adapter',lambda token:(SimpleNamespace(iter_items=snapshots),token))
    try:
        result=service.stop_campaign(promotion_fk=first,actor='tester',store=store)
        assert entered.wait(2)
        with pytest.raises(ValueError,match='已提交'):
            service.stop_campaign(promotion_fk=first,actor='tester',store=store)
        service.stop_campaign(promotion_fk=other,actor='tester',store=store,background=False)
        assert store.latest_job(other)['status']=='succeeded'
        assert store.latest_job(first)['status']=='running'
    finally:
        release.set()
        for t in threading.enumerate():
            if t.name==f"promotion-{result['job_id'][:10]}":t.join(3)
    assert store.latest_job(first)['status']=='succeeded'


def test_overlapping_campaigns_wait_for_same_product_instead_of_skipping(store,monkeypatch):
    import threading
    first=campaign(store)
    other=campaign(store,kind='DOD')
    live={f'sale-1-{kind}':[{'id':'MLM1','status':'started','offer_id':kind}] for kind in ['DEAL','DOD']}
    entered,release=threading.Event(),threading.Event()
    calls=[]
    def withdraw(seller,item,**kw):
        calls.append(kw['promotion_type'])
        if kw['promotion_type']=='DEAL':
            entered.set();assert release.wait(5)
        live[kw['promotion_id']].clear()
        return {}
    adapter=SimpleNamespace(withdraw_item=withdraw,iter_items=lambda seller,pid,**kw:list(live[pid]))
    monkeypatch.setattr(service.bit_mysql,'get_mercado_store_token',lambda token:{})
    monkeypatch.setattr(service,'_adapter',lambda token:(adapter,token))
    job1=service.stop_campaign(promotion_fk=first,actor='tester',store=store)
    assert entered.wait(2)
    job2=service.stop_campaign(promotion_fk=other,actor='tester',store=store)
    release.set()
    for t in threading.enumerate():
        if t.name in [f"promotion-{j['job_id'][:10]}" for j in [job1,job2]]:t.join(4)
    assert calls==['DEAL','DOD']
    assert all(store.latest_job(pk)['status']=='succeeded' for pk in [first,other])


def test_synthetic_price_discount_is_verified_by_item_endpoint(store,monkeypatch):
    pk=store.upsert_promotion(token_id=1,store_name='shop',application_id='app',seller_id='seller',site_id='MLM',
        row={'id':'ITEM-PRICE_DISCOUNT-MLM1','type':'PRICE_DISCOUNT','status':'started'})
    live=[{'type':'PRICE_DISCOUNT','status':'started'}]
    calls=[]
    def withdraw(*args,**kwargs):calls.append(kwargs);live.clear();return {}
    adapter=SimpleNamespace(get_item_promotions=lambda *a:list(live),withdraw_item=withdraw)
    monkeypatch.setattr(service.bit_mysql,'get_mercado_store_token',lambda token:{})
    monkeypatch.setattr(service,'_adapter',lambda token:(adapter,token))
    service.stop_campaign(promotion_fk=pk,actor='tester',store=store,background=False)
    assert calls[0]['promotion_id']==''
    assert store.latest_job(pk)['status']=='succeeded'


def test_smart_preview_uses_trusted_candidate_and_submits_batch(store,monkeypatch):
    pk=campaign(store,kind='SMART')
    store.replace_items(pk,[{'id':'MLB1','status':'candidate','offer_id':'CANDIDATE-1','price':15},
        {'id':'MLB2','status':'candidate','price':16}])
    monkeypatch.setattr(service,'enrich_item_products',lambda promotion,rows:rows)
    local=store.list_items(pk)
    preview=service.create_preview(promotion_fk=pk,actor='tester',action='enroll',store=store,
        rows=[{'item_local_id':r['id'],'deal_price':1,'offer_id':'untrusted'} for r in local])
    assert preview['ready_count']==1 and preview['blocked_count']==1
    assert preview['rows'][0]['offer_id']=='CANDIDATE-1' and preview['rows'][0]['deal_price'] is None
    calls=[]
    def enroll(*a,**kw):calls.append(kw);return {'offer_id':'OFFER-1','price':15}
    monkeypatch.setattr(service.bit_mysql,'get_mercado_store_token',lambda token:{})
    monkeypatch.setattr(service,'_adapter',lambda token:(SimpleNamespace(enroll_item=enroll),token))
    result=service.execute_preview(preview['preview_id'],actor='tester',store=store)
    assert [row['status'] for row in result['results']]==['succeeded','blocked']
    assert store.latest_job(pk)['total_count']==2
    assert calls[0]['offer_id']=='CANDIDATE-1' and calls[0]['deal_price'] is None


def test_smart_reenrollment_refreshes_withdrawn_offer_identifier(store,monkeypatch):
    pk=campaign(store,kind='SMART')
    store.replace_items(pk,[{'id':'MLB1','status':'withdrawn','offer_id':'OFFER-old','price':15}])
    monkeypatch.setattr(service,'enrich_item_products',lambda promotion,rows:rows)
    preview=service.create_preview(promotion_fk=pk,actor='tester',action='enroll',store=store,
        rows=[{'item_local_id':store.list_items(pk)[0]['id']}])
    calls=[]
    adapter=SimpleNamespace(
        get_item_promotions=lambda *a:[{'id':'sale-1-SMART','type':'SMART','status':'candidate','offer_id':'CANDIDATE-new'}],
        enroll_item=lambda *a,**kw:calls.append(kw) or {'price':15,'offer_id':'OFFER-new'})
    monkeypatch.setattr(service.bit_mysql,'get_mercado_store_token',lambda token:{})
    monkeypatch.setattr(service,'_adapter',lambda token:(adapter,token))
    outcome=service.execute_preview(preview['preview_id'],actor='tester',store=store)
    assert outcome['results'][0]['status']=='succeeded'
    assert calls[0]['offer_id']=='CANDIDATE-new'
