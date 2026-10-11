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
        return {}
    monkeypatch.setattr(service.bit_mysql, 'get_mercado_store_token', lambda token: {})
    monkeypatch.setattr(service, '_adapter', lambda token: (SimpleNamespace(withdraw_item=withdraw), token))
    result = service.stop_campaign(promotion_fk=pk, actor='tester', store=store, background=False)
    assert len(calls) == result['total'] == 125
    assert all(options['promotion_id']=='sale-1-DEAL' and options['offer_id'] for _,options in calls)
    assert store.list_items_page(pk, enrolled='1')['total'] == 1
    assert store.list_items_page(other, enrolled='1')['total'] == 1
    assert store.latest_job(pk)['success_count'] == 124
    assert store.latest_job(pk)['failed_count'] == 1
    assert next(row for row in store.list_items(pk) if row['item_id']=='candidate')['status_raw']=='candidate'


def test_empty_and_unsupported_campaign_do_not_submit(store, monkeypatch):
    monkeypatch.setattr(service, 'execute_preview', lambda *a, **kw: pytest.fail('must not submit'))
    assert service.stop_campaign(promotion_fk=campaign(store), actor='tester', store=store)['status']=='skipped'
    with pytest.raises(ValueError, match='暂不支持'):
        service.stop_campaign(promotion_fk=campaign(store, kind='CLEARANCE'), actor='tester', store=store)


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
