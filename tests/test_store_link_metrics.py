from types import SimpleNamespace
from contextlib import nullcontext
import pytest
from bit import store_link_metrics as metrics


def test_quality_uses_user_product_score_and_preserves_zero():
    calls=[]
    client=SimpleNamespace(request=lambda method,path: calls.append(path) or {'score':0})
    assert metrics.quality(client,{'id':'MLM1','user_product_id':'MLMU123'})['quality_score']==0
    assert calls==['/user-product/MLMU123/performance']


def test_permission_denial_happens_before_cache_or_remote_reads(monkeypatch):
    monkeypatch.setattr(metrics,'get_store_links_by_ids',lambda ids:[{'id':1,'token_id':87,'item_id':'MLM1'}])
    monkeypatch.setattr(metrics,'collect',lambda row:pytest.fail('unauthorized remote read'))
    with pytest.raises(PermissionError): metrics.refresh([1],{88})


@pytest.mark.parametrize('ids',[[],list(range(1,10)),[-1]])
def test_request_size_is_bounded(ids):
    with pytest.raises(ValueError): metrics.refresh(ids)


def test_partial_failure_does_not_hide_sales_or_promotions(monkeypatch):
    monkeypatch.setattr(metrics,'store_link_item_locks',lambda *a,**k:nullcontext())
    monkeypatch.setattr(metrics.bit_mysql,'get_mercado_store_token',lambda _: {})
    client=SimpleNamespace(get_marketplace_item=lambda _: {'id':'MLM1','sold_quantity':0})
    monkeypatch.setattr(metrics,'_adapter',lambda _: (SimpleNamespace(client=client),{}))
    monkeypatch.setattr(metrics,'fetch_total_visits',lambda *a:{'total_visits':None,'visits_error':'403'})
    monkeypatch.setattr(metrics,'quality',lambda *a:{'quality_score':None,'quality_error':'未提供'})
    monkeypatch.setattr(metrics,'activities',lambda *a:{'enrolled_promotions':[],'available_promotions':[{'name':'sale'}]})
    result=metrics.collect({'id':1,'item_id':'MLM1','token_id':87})
    assert result['sold_quantity']==0 and result['visits_error']=='403'
    assert result['available_promotions'][0]['name']=='sale'


def test_candidate_and_enrolled_are_independent_of_whole_store_sync(tmp_path,monkeypatch):
    from erp.mercadolibre_promotion_store import PromotionStore
    store=PromotionStore(tmp_path/'promos.sqlite3')
    monkeypatch.setattr(metrics,'PromotionStore',lambda:store)
    adapter=SimpleNamespace(get_item_promotions=lambda *a:[
        {'id':'sale-a','type':'DEAL','status':'candidate'},
        {'id':'sale-b','type':'DEAL','status':'started'}])
    result=metrics.activities(adapter,{'client_id':'app'},
        {'id':1,'token_id':87,'item_id':'MLM1','site_id':'MLM'}, {'seller_id':99})
    assert [r['promotion_id'] for r in result['available_promotions']]==['sale-a']
    assert [r['promotion_id'] for r in result['enrolled_promotions']]==['sale-b']
    assert result['promotion_applied']


def test_item_discovery_does_not_erase_offer_price(tmp_path):
    from erp.mercadolibre_promotion_store import PromotionStore
    store=PromotionStore(tmp_path/'promos.sqlite3')
    pk=store.upsert_promotion(token_id=1,store_name='test',application_id='app',seller_id='1',site_id='MLM',row={'id':'P1','type':'DEAL','status':'started'})
    store.replace_items(pk,[{'id':'MLM1','offer_id':'offer','status':'started','price':42,'original_price':50}])
    store.refresh_item_activity(pk,'MLM1',{'status':'candidate'})
    row=store.list_items(pk)[0]
    assert row['price']==42 and row['original_price']==50 and row['offer_id']=='offer'
    assert row['status_raw']=='candidate'


def test_item_discovery_does_not_revert_status_changed_after_remote_read(tmp_path, monkeypatch):
    from erp import mercadolibre_promotion_store as store_module
    from erp.mercadolibre_promotion_store import PromotionStore

    store=PromotionStore(tmp_path/'promos.sqlite3')
    pk=store.upsert_promotion(token_id=1,store_name='test',application_id='app',seller_id='1',site_id='MLM',row={'id':'P1','type':'DEAL','status':'started'})
    store.replace_items(
        pk, [{'id':'MLM1','offer_id':'offer','status':'candidate','price':42}],
        snapshot_started_at='2026-10-14T01:00:00+00:00',
    )
    monkeypatch.setattr(store_module, '_precise_now', lambda: '2026-10-14T01:00:03+00:00')
    assert store.set_item_status(pk, 'MLM1', 'pending_approval') == 1

    store.refresh_item_activity(
        pk, 'MLM1', {'status':'candidate'},
        snapshot_started_at='2026-10-14T01:00:02.500000+00:00',
    )
    row=store.list_items(pk)[0]
    assert row['status_raw']=='pending_approval'
    assert row['price']==42 and row['offer_id']=='offer'

    store.refresh_item_activity(
        pk, 'MLM1', {'status':'candidate'},
        snapshot_started_at='2026-10-14T01:00:05+00:00',
    )
    assert store.list_items(pk)[0]['status_raw']=='candidate'
