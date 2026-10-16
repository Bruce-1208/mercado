import threading
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from bit import mercado_promotions as service
from erp.mercadolibre_promotion_store import PromotionStore


def make_job(tmp_path, rows):
    store=PromotionStore(tmp_path/'promotions.sqlite3')
    pk=store.upsert_promotion(token_id=1,store_name='店铺',application_id='app',seller_id='seller',site_id='MLM',
        row={'id':'sale','type':'DEAL','status':'started'})
    store.replace_items(pk,rows)
    preview=store.create_preview(actor='tester',action='withdraw',promotion_fk=pk,payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    job=store.create_job(preview_id=preview,actor='tester',action='withdraw',total=len(rows))
    return store,job,store.get_promotion(pk),[dict(row,item_id=row['id']) for row in rows]


def test_slow_product_does_not_block_later_items_or_progress_and_pool_is_bounded(tmp_path,monkeypatch):
    monkeypatch.setenv('BIT_PROMOTION_WORKERS','4')
    rows=[dict(id=f'MLM{i}',status='started',offer_id=f'offer{i}') for i in range(70)]
    store,job,promotion,intents=make_job(tmp_path,rows)
    slow_started,release,later_done=threading.Event(),threading.Event(),threading.Event()
    guard=threading.Lock();calls=[];active=0;peak=0
    def withdraw(seller,item,**kwargs):
        nonlocal active,peak
        with guard:
            calls.append(item);active+=1;peak=max(peak,active)
        try:
            if item=='MLM0':
                slow_started.set()
                assert release.wait(5)
            else:
                assert slow_started.wait(3)
                if item=='MLM69':later_done.set()
            return {}
        finally:
            with guard:active-=1
    errors=[]
    def run():
        try:service._execute_job(store,job,SimpleNamespace(withdraw_item=withdraw),promotion,intents,'withdraw')
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=run);thread.start()
    try:
        assert slow_started.wait(3)
        assert later_done.wait(3), 'Later products must finish while the first request remains blocked'
        snapshot=store.latest_job(promotion['id'])
        assert snapshot['status']=='running' and snapshot['success_count']>=64
        assert 2<=peak<=4
    finally:
        release.set();thread.join(5)
    assert not thread.is_alive() and not errors
    assert sorted(calls)==sorted(row['id'] for row in rows)
    assert store.latest_job(promotion['id'])['success_count']==70
    assert store.latest_job(promotion['id'])['unknown_count']==0


def test_multiple_offers_for_same_product_stay_serial_and_one_failure_does_not_stop_queue(tmp_path,monkeypatch):
    monkeypatch.setenv('BIT_PROMOTION_WORKERS','4')
    rows=[dict(id='MLM1',status='started',offer_id='a'),dict(id='MLM1',status='started',offer_id='b'),
          dict(id='MLM2',status='started',offer_id='c')]
    store,job,promotion,intents=make_job(tmp_path,rows)
    calls=[];guard=threading.Lock();active=set()
    def withdraw(seller,item,**kwargs):
        with guard:
            assert item not in active
            active.add(item);calls.append(kwargs['offer_id'])
        try:
            if kwargs['offer_id']=='a':raise service.MercadoAPIError('平台拒绝取消')
            return {}
        finally:
            with guard:active.remove(item)
    outcome=service._execute_job(store,job,SimpleNamespace(withdraw_item=withdraw),promotion,intents,'withdraw')
    assert calls.index('a')<calls.index('b')
    assert [r['status'] for r in outcome['results']]==['failed','succeeded','succeeded']
    assert store.latest_job(promotion['id'])['success_count']==2


def test_real_clients_get_independent_worker_sessions_and_close_them(tmp_path,monkeypatch):
    monkeypatch.setenv('BIT_PROMOTION_WORKERS','2')
    store,job,promotion,intents=make_job(tmp_path,[dict(id=f'MLM{i}',status='started') for i in range(4)])
    clients=[];closed=[];barrier=threading.Barrier(2)
    def factory(token):
        identifier=len(clients);clients.append(identifier)
        adapter=SimpleNamespace(client=SimpleNamespace(session=SimpleNamespace(close=lambda:closed.append(identifier))))
        first=True
        def withdraw(*args,**kwargs):
            nonlocal first
            if first:first=False;barrier.wait(timeout=3)
            return {}
        adapter.withdraw_item=withdraw
        return adapter,token
    monkeypatch.setattr(service.bit_mysql,'get_mercado_store_token',lambda token:{'id':token})
    monkeypatch.setattr(service,'_adapter',factory)
    original=service.MercadoPromotionsClient(SimpleNamespace())
    service._execute_job(store,job,original,promotion,intents,'withdraw')
    assert len(clients)==2 and sorted(closed)==sorted(clients)
    assert store.latest_job(promotion['id'])['success_count']==4


def test_worker_setting_is_capped_and_invalid_values_are_safe(monkeypatch):
    for value,expected in [('0',1),('99',16),('invalid',8),('2',2)]:
        monkeypatch.setenv('BIT_PROMOTION_WORKERS',value)
        assert service._promotion_worker_count(100)==expected
        assert service._promotion_worker_count(1)==1


def test_default_pool_uses_eight_simultaneous_workers(tmp_path,monkeypatch):
    monkeypatch.delenv('BIT_PROMOTION_WORKERS',raising=False)
    assert service._promotion_worker_count(100)==8
    store,job,promotion,intents=make_job(tmp_path,[dict(id=f'MLM{i}',status='started') for i in range(8)])
    barrier=threading.Barrier(8)
    def withdraw(*args,**kw):barrier.wait(timeout=3);return {}
    service._execute_job(store,job,SimpleNamespace(withdraw_item=withdraw),promotion,intents,'withdraw')
    assert store.latest_job(promotion['id'])['success_count']==8
