"""Current participation, listing cancellation and net-price mutation ordering."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import Flask

from erp.mercadolibre_promotion_store import PromotionStore
from bit import mercado_promotions as service
from bit import bit_store_link_remote_update as remote
from bit import mercado_promotion_routes as routes


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('BIT_PROMOTION_DB_PATH', str(tmp_path / 'promotions.sqlite3'))
    return PromotionStore()


def campaign(store, *, token=1, site='MLM', status='started', kind='DEAL', name='sale'):
    return store.upsert_promotion(token_id=token, store_name='店铺', application_id='app',
        seller_id=str(token), site_id=site, row={'id':name, 'type':kind, 'status':status})


def test_filter_current_participation_across_pages_and_after_withdrawal(store):
    pk = campaign(store)
    store.replace_items(pk, [dict(id=f'MLM{i}', status='candidate' if i%2 else 'pending_approval',
        price=80, original_price=100, currency_id='USD') for i in range(120)])
    enrolled = store.list_items_page(pk, enrolled='1', page=2, page_size=20)
    assert enrolled['total'] == 60 and enrolled['pages'] == 3
    assert len(enrolled['rows']) == 20
    assert all(row['enrolled'] and row['current_discount_percent'] == 20 for row in enrolled['rows'])
    target = store.list_items_page(pk, enrolled='1', page_size=20, target_item_id='MLM98')
    assert any(row['item_id'] == 'MLM98' for row in target['rows'])
    store.set_item_status(pk, 'MLM98', 'withdrawn')
    assert store.list_items_page(pk, enrolled='1')['total'] == 59
    assert store.list_items_page(pk, enrolled='0')['total'] == 61
    assert all(not row['enrolled'] and row['current_discount_percent'] is None
               for row in store.list_items_page(pk, enrolled='0')['rows'])
    campaign(store, status='finished')
    assert store.list_items_page(pk, enrolled='1')['total'] == 0
    assert store.all_applied_item_keys() == set()


def test_withdrawal_clears_both_views_retains_history_and_does_not_cross_scope(store):
    pk = campaign(store)
    other = campaign(store, token=2)
    for activity in (pk, other):
        store.replace_items(activity, [{'id':'MLM1','status':'pending_approval','offer_id':'offer',
                                       'price':70,'original_price':100}])
    calls = []
    adapter = SimpleNamespace(withdraw_item=lambda *a, **kw: calls.append((a, kw)) or {})
    result = service.withdraw_listing_promotions(token_id=1, site_id='MLM', item_id='MLM1',
        actor='tester', store=store, adapter=adapter)
    assert result['cancelled_count'] == 1
    assert calls[0] == (('1','MLM1'), {'promotion_id':'sale','promotion_type':'DEAL','offer_id':'offer'})
    assert store.applied_item_details([(1,'MLM','MLM1')]) == {}
    assert store.list_items_page(pk)['rows'][0]['enrolled'] is False
    assert store.latest_job(pk)['action'] == 'withdraw'
    assert store.latest_job(pk)['status'] == 'succeeded'
    assert store.applied_item_details([(2,'MLM','MLM1')])


@pytest.mark.parametrize('failure', ['平台拒绝取消', '结果未知；请先同步官方状态'])
def test_failed_cancellation_keeps_current_enrollment(store, failure):
    pk = campaign(store)
    store.replace_items(pk, [{'id':'MLM1','status':'started'}])
    def withdraw(*a, **kw):
        raise service.MercadoAPIError(failure)
    with pytest.raises(ValueError, match='取消活动未完成'):
        service.withdraw_listing_promotions(token_id=1, site_id='MLM', item_id='MLM1',
            actor='tester', store=store, adapter=SimpleNamespace(withdraw_item=withdraw))
    assert store.list_items_page(pk)['rows'][0]['enrolled']
    assert store.latest_job(pk)['results'][0]['status'] == ('unknown' if '未知' in failure else 'failed')


def test_unsupported_activity_prevents_partial_cancellation(store):
    for kind in ('DEAL','CLEARANCE'):
        pk = campaign(store, kind=kind, name=kind)
        store.replace_items(pk, [{'id':'MLM1','status':'started'}])
    with pytest.raises(ValueError, match='暂不支持自动取消'):
        service.withdraw_listing_promotions(token_id=1,site_id='MLM',item_id='MLM1',actor='tester',store=store,
            adapter=SimpleNamespace(withdraw_item=lambda *a, **kw: pytest.fail('must not mutate')))


@pytest.mark.parametrize('failed', [False, True])
def test_net_update_cancels_before_any_price_write_and_stops_on_failure(store, monkeypatch, failed):
    calls=[]
    def cancel(**kwargs):
        calls.append('cancel')
        assert kwargs['token_id']==1 and kwargs['site_id']=='MLM' and kwargs['item_id']=='MLM1'
        assert kwargs['verify_live'] is True
        if failed: raise ValueError('结果未知')
        return {'cancelled_count':1}
    monkeypatch.setattr(service,'withdraw_listing_promotions',cancel)
    class Client:
        def __init__(self, token): pass
        def update_global_item(self,item,payload): calls.append(payload)
        def get_marketplace_item(self,*a,**kw):
            calls.append('readback')
            return {'id':'MLM1','price':25,'currency_id':'USD','net_proceeds':{'amount':20,'currency_id':'USD'}}
    monkeypatch.setattr(remote,'MercadoLibreClient',Client)
    monkeypatch.setattr(remote,'bulk_update_store_links',lambda ids,changes:calls.append(dict(changes)))
    monkeypatch.setattr(remote,'sync_pulled_product_fields_from_store_links',lambda *a:None)
    result=remote._update_one_link({'id':7,'token_id':1,'site_id':'MLM','item_id':'MLM1'},
        {'id':1,'access_token':'test'}, {'net_proceeds_usd':Decimal('20')})
    if failed:
        assert calls == ['cancel'] and result['status']=='error'
        assert '未修改净收益' in result['errors'][0]
    else:
        assert calls[:3] == ['cancel',{'net_proceeds':20},'readback']
        assert calls[3]['net_proceeds_usd']==20 and calls[3]['price']==25
        assert result['status']=='success'


def test_cancel_route_requires_authorization_and_filters_exact_item(store, monkeypatch):
    pk=campaign(store)
    store.replace_items(pk,[{'id':'MLM1','status':'started'}])
    monkeypatch.setattr(service,'_adapter',lambda token:(SimpleNamespace(withdraw_item=lambda *a,**kw:{}),token))
    monkeypatch.setattr(service.bit_mysql,'get_mercado_store_token',lambda token:{})
    permission={'allowed':False,'tokens':[1]}
    app=Flask(__name__)
    app.register_blueprint(routes.create_promotions_blueprint(login_required=lambda f:f,
        current_user=lambda:{'username':'tester'},authorized_token_ids=lambda u:permission['tokens'],
        has_permission=lambda *a:permission['allowed']))
    client=app.test_client()
    path=f'/api/promotions/{pk}/withdraw'
    assert client.post(path,json={'item_id':'MLM1'}).status_code==403
    permission['allowed']=True;permission['tokens']=[2]
    assert client.post(path,json={'item_id':'MLM1'}).status_code==403
    permission['tokens']=[1]
    assert client.post(path,json={'item_id':'MLM123'}).status_code==400
    assert client.post(path,json={'item_id':'MLM1'}).get_json()['data']['cancelled_count']==1
    assert client.get(f'/api/promotions/{pk}/items?enrolled=1').get_json()['data']['total']==0


def test_partial_offer_cancellation_keeps_failed_offer_enrolled(store):
    pk = campaign(store)
    store.replace_items(pk, [{'id':'MLM1','status':'started','offer_id':offer} for offer in ('a','b')])
    def cancel(*a, **kwargs):
        if kwargs['offer_id']=='b': raise service.MercadoAPIError('取消失败')
        return {}
    with pytest.raises(ValueError, match='取消活动未完成'):
        service.withdraw_listing_promotions(token_id=1, site_id='MLM', item_id='MLM1', actor='tester',
            store=store, adapter=SimpleNamespace(withdraw_item=cancel))
    active = store.applied_item_details([(1,'MLM','MLM1')])[(1,'MLM','MLM1')]
    assert [row['offer_id'] for row in active] == ['b']
    assert store.list_items_page(pk, enrolled='1')['total'] == 1


def test_successful_items_are_visible_and_filterable_before_batch_finishes(store):
    from datetime import datetime, timedelta, timezone
    from erp.mercadolibre_store_link_store import _marker_filter_keys

    pk = campaign(store)
    store.replace_items(pk, [dict(id=item, status='candidate', price=90, original_price=100, currency_id='USD')
                             for item in ('MLM1', 'MLM2')])
    preview = store.create_preview(actor='tester', action='enroll', promotion_fk=pk,
        payload={}, expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
    job = store.create_job(preview_id=preview, actor='tester', action='enroll', total=2)
    keys = [(1, 'MLM', 'MLM1'), (1, 'MLM', 'MLM2')]

    checked = []

    def enroll(seller, item, **kwargs):
        if item == 'MLM2':
            # The first success is already visible while this batch is still running.
            assert store.list_jobs()[0]['status'] == 'running'
            assert _marker_filter_keys('promotion_applied', token_ids=[1], site_id='MLM') == {keys[0]}
            detail = store.applied_item_details(keys)[keys[0]][0]
            assert detail['status_raw'] == 'pending_approval'
            assert detail['price'] == 80
            checked.append(item)
            raise service.MercadoAPIError('平台拒绝报名')
        return {}

    service._execute_job(store, job, SimpleNamespace(enroll_item=enroll), store.get_promotion(pk),
        [dict(item_id=item, deal_price=80, currency_id='USD') for item in ('MLM1', 'MLM2')], 'enroll')
    assert _marker_filter_keys('promotion_applied', token_ids=[1], site_id='MLM') == {keys[0]}
    assert checked == ['MLM2']
    assert store.list_jobs()[0]['failed_count'] == 1
    assert store.applied_item_details([(2, 'MLM', 'MLM1')]) == {}


def test_net_update_withdraws_all_enrollments_and_clears_listing_filter(store, monkeypatch):
    from erp.mercadolibre_store_link_store import _marker_filter_keys

    for name in ('sale-a', 'sale-b'):
        pk = campaign(store, name=name)
        store.replace_items(pk, [{'id': 'MLM1', 'status': 'pending_approval',
                                  'price': 80, 'original_price': 100}])
    other = campaign(store, token=2, name='other-store')
    store.replace_items(other, [{'id': 'MLM1', 'status': 'started'}])
    calls = []
    monkeypatch.setattr(service, '_adapter', lambda token: (
        SimpleNamespace(
            client=SimpleNamespace(get_marketplace_item=lambda _: {'id':'MLM1','seller_id':'1'}),
            get_item_promotions=lambda *args: [dict(id=name,type='DEAL',status='pending_approval')
                                              for name in ('sale-a','sale-b') if name not in calls],
            withdraw_item=lambda *args, **kwargs: calls.append(kwargs['promotion_id']) or {}), token))
    monkeypatch.setattr(service.bit_mysql, 'get_mercado_store_token', lambda token: {'client_id':'app'})

    class Client:
        def __init__(self, token): pass
        def update_global_item(self, item_id, payload):
            assert calls == ['sale-a', 'sale-b']
            assert _marker_filter_keys('promotion_applied', token_ids=[1]) == set()
            assert store.applied_item_details([(1, 'MLM', item_id)]) == {}
            calls.append(payload)
        def get_marketplace_item(self, *args, **kwargs):
            return {'id': 'MLM1', 'price': 25, 'currency_id': 'USD',
                    'net_proceeds': {'amount': 20, 'currency_id': 'USD'}}

    monkeypatch.setattr(remote, 'MercadoLibreClient', Client)
    monkeypatch.setattr(remote, 'bulk_update_store_links', lambda *args: None)
    monkeypatch.setattr(remote, 'sync_pulled_product_fields_from_store_links', lambda *args: None)
    result = remote._update_one_link({'id': 7, 'token_id': 1, 'site_id': 'MLM', 'item_id': 'MLM1'},
        {'id': 1, 'access_token': 'test'}, {'net_proceeds_usd': Decimal('20')})
    assert result['status'] == 'success'
    assert calls == ['sale-a', 'sale-b', {'net_proceeds': 20}]
    assert '取消活动 2 项' in result['remote_groups']
    assert _marker_filter_keys('promotion_applied', token_ids=[2]) == {(2, 'MLM', 'MLM1')}


@pytest.mark.parametrize('still_active', [False, True])
def test_live_cancellation_finds_activity_missing_from_local_cache(store, monkeypatch, still_active):
    monkeypatch.setattr(service.time, 'sleep', lambda _: None)
    monkeypatch.setattr(service.bit_mysql, 'get_mercado_store_token', lambda _: {'client_id': 'app'})
    calls = []
    activity = {'id': 'remote-sale', 'type': 'DEAL', 'status': 'started', 'name': 'Live sale'}
    class Adapter:
        client = SimpleNamespace(get_marketplace_item=lambda _: {'id': 'MLM1', 'seller_id': 'child'})
        def get_item_promotions(self, seller, item):
            assert (seller, item) == ('child', 'MLM1')
            return [activity] if not calls or still_active else [dict(activity, status='candidate')]
        def withdraw_item(self, seller, item, **kw):
            calls.append((seller, item, kw)); return {}
    arguments = dict(token_id=1, site_id='MLM', item_id='MLM1', actor='test',
                     store=store, adapter=Adapter(), verify_live=True)
    if still_active:
        with pytest.raises(ValueError, match='取消尚未确认'):
            service.withdraw_listing_promotions(**arguments)
        assert store.applied_item_details([(1,'MLM','MLM1')])
    else:
        result = service.withdraw_listing_promotions(**arguments)
        assert result['cancelled_count'] == 1
        assert not store.applied_item_details([(1,'MLM','MLM1')])
    assert len(calls) == 1
    assert calls[0][2]['promotion_id'] == 'remote-sale'


def test_targeted_promotion_refresh_preserves_other_items(store):
    pk = campaign(store)
    store.replace_items(pk, [{'id':'MLM1','status':'started'}, {'id':'MLM2','status':'started'}])
    store.replace_items(pk, [{'id':'MLM1','status':'withdrawn'}], item_id='MLM1')
    assert {r['item_id']:r['status_raw'] for r in store.list_items(pk)} == {'MLM1':'withdrawn','MLM2':'started'}
    with pytest.raises(ValueError):
        store.replace_items(pk, [{'id':'MLM2','status':'withdrawn'}], item_id='MLM1')
    assert len(store.list_items(pk)) == 2


def test_live_lookup_failure_cannot_be_treated_as_no_promotions(store, monkeypatch):
    monkeypatch.setattr(service.bit_mysql, 'get_mercado_store_token', lambda _: {})
    def failed(*args): raise service.MercadoAPIError('read failed')
    adapter=SimpleNamespace(client=SimpleNamespace(get_marketplace_item=lambda _: {'id':'MLM1','seller_id':'child'}),
                            get_item_promotions=failed,
                            withdraw_item=lambda *a,**kw: pytest.fail('must not mutate'))
    with pytest.raises(service.MercadoAPIError, match='read failed'):
        service.withdraw_listing_promotions(token_id=1, site_id='MLM', item_id='MLM1', actor='test',
            store=store, adapter=adapter, verify_live=True)


def test_live_withdrawal_waits_for_propagation_without_replaying_delete(store, monkeypatch):
    monkeypatch.setattr(service.bit_mysql, 'get_mercado_store_token', lambda _: {'client_id':'app'})
    waits=[]
    monkeypatch.setattr(service.time, 'sleep', waits.append)
    activity={'id':'sale','type':'DEAL','status':'started'}
    reads=iter([[activity],[activity],[activity],[dict(activity,status='candidate')]])
    writes=[]
    adapter=SimpleNamespace(
        client=SimpleNamespace(get_marketplace_item=lambda _: {'id':'MLM1','seller_id':'1'}),
        get_item_promotions=lambda *args: next(reads),
        withdraw_item=lambda *args,**kwargs: writes.append(kwargs) or {})
    result=service.withdraw_listing_promotions(token_id=1,site_id='MLM',item_id='MLM1',actor='test',
        store=store,adapter=adapter,verify_live=True)
    assert result['cancelled_count']==1 and len(writes)==1
    assert waits==[1,2]
