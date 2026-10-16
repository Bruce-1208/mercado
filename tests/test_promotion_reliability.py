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


def test_newer_blocked_preflight_does_not_clear_older_unknown_enrollment_guard(store,monkeypatch):
    promotion,unknown_job,row=setup_job(store)
    store.finish_job(unknown_job,results=[dict(row,status='unknown',message='网络中断，结果未知')])

    expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
    blocked_preview=store.create_preview(actor='test',action='enroll',promotion_fk=promotion['id'],payload={},expires_at=expires_at)
    blocked_job=store.create_job(preview_id=blocked_preview,actor='test',action='enroll',total=1)
    store.finish_job(blocked_job,results=[dict(row,status='blocked',message='本地预检不符合报名条件')])
    assert store.latest_job(promotion['id'])['blocked_count']==1

    retry_preview=store.create_preview(actor='test',action='enroll',promotion_fk=promotion['id'],payload={},expires_at=expires_at)
    retry_job=store.create_job(preview_id=retry_preview,actor='test',action='enroll',total=1)
    monkeypatch.setattr(service.time,'sleep',lambda delay:None)
    posts=[]
    reads=[]
    adapter=SimpleNamespace(
        enroll_item=lambda *args,**kwargs:posts.append((args,kwargs)) or {},
        get_item_promotions=lambda *args:reads.append(args) or [],
    )

    result=service._execute_job(store,retry_job,adapter,promotion,[row],'enroll')

    assert posts==[]
    assert len(reads)==3
    assert result['results'][0]['status']=='unknown'
    assert '上次报名结果未知' in result['results'][0]['message']
    assert '未重复提交' in result['results'][0]['message']
    assert store.latest_job(promotion['id'])['unknown_count']==1


def test_successful_history_with_stale_candidate_is_read_verified_before_any_post(store, monkeypatch):
    promotion, succeeded_job, row = setup_job(store)
    store.finish_job(succeeded_job, results=[dict(
        row, status='succeeded', local_sync_failed=True,
        message='平台操作成功，但本地状态写回失败',
    )])
    preview = store.create_preview(
        actor='test', action='enroll', promotion_fk=promotion['id'], payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
    )
    retry_job = store.create_job(preview_id=preview, actor='test', action='enroll', total=1)
    reads, posts = [], []
    adapter = SimpleNamespace(
        get_item_promotions=lambda *args: reads.append(args) or [{
            'id': 'sale', 'type': 'DEAL', 'status': 'started',
            'price': 15, 'currency_id': 'USD',
        }],
        enroll_item=lambda *args, **kwargs: posts.append((args, kwargs)) or {},
    )

    result = service._execute_job(store, retry_job, adapter, promotion, [row], 'enroll')

    assert posts == []
    assert len(reads) == 1
    assert result['results'][0]['status'] == 'succeeded'
    assert '跳过重复报名' in result['results'][0]['message']


def test_successful_history_with_unverified_candidate_never_posts(store, monkeypatch):
    promotion, succeeded_job, row = setup_job(store)
    store.finish_job(succeeded_job, results=[dict(row, status='succeeded', local_sync_failed=True)])
    preview = store.create_preview(
        actor='test', action='enroll', promotion_fk=promotion['id'], payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
    )
    retry_job = store.create_job(preview_id=preview, actor='test', action='enroll', total=1)
    monkeypatch.setattr(service.time, 'sleep', lambda _delay: None)
    reads, posts = [], []
    adapter = SimpleNamespace(
        get_item_promotions=lambda *args: reads.append(args) or [],
        enroll_item=lambda *args, **kwargs: posts.append((args, kwargs)) or {},
    )

    result = service._execute_job(store, retry_job, adapter, promotion, [row], 'enroll')

    assert posts == []
    assert len(reads) == 3
    assert result['results'][0]['status'] == 'unknown'
    assert '未重复提交' in result['results'][0]['message']


def test_successful_history_read_only_candidate_requires_fresh_sync_before_post(store):
    promotion, succeeded_job, row = setup_job(store)
    store.finish_job(succeeded_job, results=[dict(row, status='succeeded', local_sync_failed=True)])
    preview = store.create_preview(
        actor='test', action='enroll', promotion_fk=promotion['id'], payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
    )
    retry_job = store.create_job(preview_id=preview, actor='test', action='enroll', total=1)
    reads, posts = [], []
    adapter = SimpleNamespace(
        get_item_promotions=lambda *args: reads.append(args) or [{
            'id':'sale', 'type':'DEAL', 'status':'candidate',
            'price':15, 'currency_id':'USD',
        }],
        enroll_item=lambda *args, **kwargs: posts.append((args, kwargs)) or {},
    )

    result = service._execute_job(store, retry_job, adapter, promotion, [row], 'enroll')

    assert posts == []
    assert len(reads) == 1
    assert result['results'][0]['status'] == 'unknown'
    assert '只读核实为候选' in result['results'][0]['message']


def test_fresh_official_candidate_snapshot_after_success_allows_reenrollment(store):
    promotion, succeeded_job, row = setup_job(store)
    store.finish_job(succeeded_job, results=[dict(row, status='succeeded')])
    finished_at = store.latest_job(promotion['id'])['finished_at']
    fresh_at = (datetime.fromisoformat(finished_at) + timedelta(seconds=2)).isoformat()
    store.replace_items(promotion['id'], [{
        'id': 'MLM1', 'status': 'candidate', 'price': 15,
        'original_price': 20, 'currency_id': 'USD', 'max_price': 18,
    }], snapshot_started_at=fresh_at)
    refreshed = store.list_items(promotion['id'])[0]
    preview = store.create_preview(
        actor='test', action='enroll', promotion_fk=promotion['id'], payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
    )
    retry_job = store.create_job(preview_id=preview, actor='test', action='enroll', total=1)
    posts = []
    adapter = SimpleNamespace(
        enroll_item=lambda *args, **kwargs: posts.append((args, kwargs)) or {},
    )

    result = service._execute_job(store, retry_job, adapter, promotion, [dict(
        row, item_local_id=refreshed['id'], last_synced_at=refreshed['last_synced_at'],
        source_status='candidate',
    )], 'enroll')

    assert len(posts) == 1
    assert result['results'][0]['status'] == 'succeeded'


def test_same_second_prefinish_snapshot_cannot_clear_success_guard_or_post(store):
    promotion, succeeded_job, row = setup_job(store)
    store.finish_job(succeeded_job, results=[dict(row, status='succeeded', local_sync_failed=True)])
    finished_at = store.latest_job(promotion['id'])['finished_at']
    ambiguous_same_second = datetime.fromisoformat(finished_at).replace(microsecond=500000).isoformat()
    store.replace_items(promotion['id'], [{
        'id':'MLM1', 'status':'candidate', 'price':15,
        'original_price':20, 'currency_id':'USD', 'max_price':18,
    }], snapshot_started_at=ambiguous_same_second)
    refreshed = store.list_items(promotion['id'])[0]
    preview = store.create_preview(
        actor='test', action='enroll', promotion_fk=promotion['id'], payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
    )
    retry_job = store.create_job(preview_id=preview, actor='test', action='enroll', total=1)
    posts=[]
    adapter=SimpleNamespace(
        enroll_item=lambda *args, **kwargs: posts.append((args, kwargs)) or {},
    )

    result=service._execute_job(store,retry_job,adapter,promotion,[dict(
        row, item_local_id=refreshed['id'], last_synced_at=refreshed['last_synced_at'],
        source_status='candidate',
    )],'enroll')

    assert posts == []
    assert result['results'][0]['status'] == 'unknown'
    assert '未重复提交' in result['results'][0]['message']


def test_withdrawn_product_can_be_reselected_and_official_eligibility_is_checked(store,monkeypatch):
    promotion, previous_job, previous_row = setup_job(store,status='withdrawn')
    store.finish_job(previous_job, results=[dict(previous_row, status='succeeded')])
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


def test_offer_scoped_guards_do_not_cross_contaminate_other_offer(store, monkeypatch):
    pk = store.upsert_promotion(
        token_id=1, store_name='test', application_id='app', seller_id='child', site_id='MLM',
        row={'id':'smart-sale','type':'SMART','status':'started'},
    )
    store.replace_items(pk, [
        {'id':'MLM1','status':'started','offer_id':'offer-a'},
        {'id':'MLM1','status':'candidate','offer_id':'offer-b'},
    ])
    promotion = store.get_promotion(pk)
    preview = store.create_preview(actor='test', action='enroll', promotion_fk=pk, payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    previous_job = store.create_job(preview_id=preview, actor='test', action='enroll', total=1)
    store.finish_job(previous_job, results=[{
        'item_id':'MLM1','offer_id':'offer-a','status':'succeeded',
    }])
    retry_preview = store.create_preview(actor='test', action='enroll', promotion_fk=pk, payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    retry_job = store.create_job(preview_id=retry_preview, actor='test', action='enroll', total=1)
    posts=[]
    adapter=SimpleNamespace(enroll_item=lambda *a,**kw:posts.append(kw['offer_id']) or {})

    result=service._execute_job(store,retry_job,adapter,promotion,[{
        'item_id':'MLM1','offer_id':'offer-b',
    }],'enroll')

    assert posts == ['offer-b']
    assert result['results'][0]['status'] == 'succeeded'


def test_offer_verification_never_uses_other_offer_as_success(store, monkeypatch):
    pk = store.upsert_promotion(
        token_id=1, store_name='test', application_id='app', seller_id='child', site_id='MLM',
        row={'id':'smart-sale','type':'SMART','status':'started'},
    )
    store.replace_items(pk, [
        {'id':'MLM1','status':'started','offer_id':'offer-a'},
        {'id':'MLM1','status':'candidate','offer_id':'offer-b'},
    ])
    promotion = store.get_promotion(pk)
    previous_preview = store.create_preview(actor='test', action='enroll', promotion_fk=pk, payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    previous_job = store.create_job(preview_id=previous_preview, actor='test', action='enroll', total=1)
    store.finish_job(previous_job, results=[{
        'item_id':'MLM1','offer_id':'offer-b','status':'unknown',
    }])
    retry_preview = store.create_preview(actor='test', action='enroll', promotion_fk=pk, payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    retry_job = store.create_job(preview_id=retry_preview, actor='test', action='enroll', total=1)
    monkeypatch.setattr(service.time, 'sleep', lambda _delay: None)
    posts=[]
    adapter=SimpleNamespace(
        enroll_item=lambda *a,**kw:posts.append(kw['offer_id']) or {},
        get_item_promotions=lambda *a:[{
            'id':'smart-sale','type':'SMART','status':'started','offer_id':'offer-a',
        }],
    )

    result=service._execute_job(store,retry_job,adapter,promotion,[{
        'item_id':'MLM1','offer_id':'offer-b',
    }],'enroll')

    assert posts == []
    assert result['results'][0]['status'] == 'unknown'


def test_unknown_first_offer_blocks_later_offers_for_same_item(store, monkeypatch):
    pk = store.upsert_promotion(
        token_id=1, store_name='test', application_id='app', seller_id='child', site_id='MLM',
        row={'id':'smart-sale','type':'SMART','status':'started'},
    )
    store.replace_items(pk, [
        {'id':'MLM1','status':'candidate','offer_id':'offer-a'},
        {'id':'MLM1','status':'candidate','offer_id':'offer-b'},
    ])
    promotion = store.get_promotion(pk)
    preview = store.create_preview(actor='test', action='enroll', promotion_fk=pk, payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    job = store.create_job(preview_id=preview, actor='test', action='enroll', total=2)
    monkeypatch.setattr(service.time, 'sleep', lambda _delay: None)
    posts=[]
    def enroll(*_args, **kwargs):
        posts.append(kwargs['offer_id'])
        raise MercadoAPIError('500 结果未知')
    adapter=SimpleNamespace(enroll_item=enroll, get_item_promotions=lambda *a:[])

    result=service._execute_job(store,job,adapter,promotion,[
        {'item_id':'MLM1','offer_id':'offer-a'},
        {'item_id':'MLM1','offer_id':'offer-b'},
    ],'enroll')

    assert posts == ['offer-a']
    assert [entry['status'] for entry in result['results']] == ['unknown', 'blocked']


def test_terminal_job_missing_results_is_treated_as_unknown_guard(store, monkeypatch):
    promotion, _, row = setup_job(store)
    ready = dict(row, status='ready', item_local_id=store.list_items(promotion['id'])[0]['id'])
    preview = store.create_preview(actor='test', action='enroll', promotion_fk=promotion['id'],
        payload={'rows':[ready]}, expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    incomplete = store.create_job(preview_id=preview, actor='test', action='enroll', total=1)
    store.finish_job(incomplete, results=[])
    # Legacy workers could mark a job terminal without accounting for every
    # preview row; retain that historical shape for the upgrade guard.
    with store._connect() as db:
        db.execute("UPDATE promotion_jobs SET total_count=1 WHERE job_id=?", (incomplete,))
    retry_preview = store.create_preview(actor='test', action='enroll', promotion_fk=promotion['id'], payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    retry_job = store.create_job(preview_id=retry_preview, actor='test', action='enroll', total=1)
    monkeypatch.setattr(service.time, 'sleep', lambda _delay: None)
    posts=[]
    adapter=SimpleNamespace(
        enroll_item=lambda *a,**kw:posts.append((a,kw)) or {},
        get_item_promotions=lambda *a:[],
    )

    result=service._execute_job(store,retry_job,adapter,promotion,[row],'enroll')

    assert posts == []
    assert result['results'][0]['status'] == 'unknown'
    assert '未重复提交' in result['results'][0]['message']


def test_execute_preview_skips_terminal_migration_but_missing_selected_row_stays_guarded(store, monkeypatch):
    promotion, orphan_job, row = setup_job(store)
    store.finish_job(orphan_job, results=[])
    item = store.list_items(promotion['id'])[0]
    historical_preview = store.create_preview(
        actor='test', action='enroll', promotion_fk=promotion['id'],
        payload={'promotion': promotion, 'rows': [dict(
            row, status='ready', item_local_id=item['id'],
        )]},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
    )
    historical_job = store.create_job(
        preview_id=historical_preview, actor='test', action='enroll', total=1,
    )
    store.finish_job(historical_job, results=[])
    # Counter sum already equals total; only the valid JSON result length
    # exposes this legacy incomplete job.
    with store._connect() as db:
        db.execute(
            "UPDATE promotion_jobs SET total_count=1, success_count=1 WHERE job_id=?",
            (historical_job,),
        )
    preview = service.create_preview(
        promotion_fk=promotion['id'], action='enroll', actor='test', store=store,
        rows=[{'item_local_id': item['id'], 'deal_price': 15}],
    )
    monkeypatch.setattr(service.bit_mysql, 'get_mercado_store_token', lambda _token: {})
    monkeypatch.setattr(service.time, 'sleep', lambda _delay: None)
    posts = []
    adapter = SimpleNamespace(
        enroll_item=lambda *args, **kwargs: posts.append((args, kwargs)) or {},
        get_item_promotions=lambda *args: [],
    )
    monkeypatch.setattr(service, '_adapter', lambda token: (adapter, token))

    submitted = service.execute_preview(
        preview['preview_id'], actor='test', store=store, background=False,
    )

    assert posts == []
    assert submitted['results'][0]['status'] == 'unknown'
    assert '未重复提交' in submitted['results'][0]['message']
    untouched = store.list_jobs(job_ids=[historical_job])[0]
    assert untouched['total_count'] == untouched['success_count'] == 1
    assert untouched['results'] == []


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


def test_reconcile_does_not_trust_same_second_snapshot_for_second_precision_job(store):
    promotion, job, row = setup_job(store)
    store.finish_job(job, results=[dict(row, status='unknown', message='500')])
    finished_at = store.latest_job(promotion['id'])['finished_at']
    same_second = datetime.fromisoformat(finished_at).replace(microsecond=900000).isoformat()
    official = [{'id':'MLM1','status':'pending','price':15,'currency_id':'USD'}]

    assert store.reconcile_completed_jobs(
        promotion['id'], official, snapshot_started_at=same_second,
    ) == 0
    assert store.latest_job(promotion['id'])['unknown_count'] == 1
    assert store.reconcile_completed_jobs(
        promotion['id'], official,
        snapshot_started_at=(datetime.fromisoformat(finished_at)+timedelta(seconds=2)).isoformat(),
    ) == 1


def test_complete_official_sync_reconciles_unknown_by_exact_offer(store):
    pk = store.upsert_promotion(
        token_id=1, store_name='test', application_id='app', seller_id='child', site_id='MLM',
        row={'id':'smart-sale','type':'SMART','status':'started'},
    )
    preview = store.create_preview(actor='test', action='enroll', promotion_fk=pk, payload={},
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
    job = store.create_job(preview_id=preview, actor='test', action='enroll', total=1)
    store.finish_job(job, results=[{
        'item_id':'MLM1','offer_id':'offer-b','status':'unknown',
    }])

    assert store.reconcile_completed_jobs(pk, [{
        'id':'MLM1','offer_id':'offer-a','status':'started',
    }]) == 0
    assert store.latest_job(pk)['unknown_count'] == 1
    assert store.reconcile_completed_jobs(pk, [{
        'id':'MLM1','offer_id':'offer-b','status':'started',
    }]) == 1
    assert store.latest_job(pk)['success_count'] == 1


def test_item_snapshot_does_not_resolve_campaign_level_withdraw_unknown(store):
    promotion, job, _ = setup_job(store, action='withdraw', status='started')
    store.finish_job(job, results=[{
        'status': 'unknown', 'message': '活动未核实完成：官方读取失败',
    }])

    assert store.reconcile_completed_jobs(promotion['id'], [{
        'id': 'MLM1', 'status': 'started', 'price': 15,
    }]) == 0
    latest = store.latest_job(promotion['id'])
    assert latest['status'] == 'partial'
    assert latest['unknown_count'] == 1
    assert latest['success_count'] == 0


def test_running_job_recovery_respects_live_campaign_lock_then_finishes_orphan(
    store, tmp_path, monkeypatch,
):
    from bit import bit_runtime_lock

    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path / "runtime-locks")
    promotion, job_id, _ = setup_job(store)
    lease = service.InterProcessLock(
        f"promotion_campaign_{promotion['id']}", owner="live-test-worker",
    )
    assert lease.acquire(timeout=0)
    try:
        assert service.recover_stale_running_jobs(store=store, token_ids=[1]) == 0
        assert store.list_jobs(job_ids=[job_id])[0]["status"] == "running"
    finally:
        lease.release()

    assert service.recover_stale_running_jobs(store=store, token_ids=[1]) == 1
    recovered = store.list_jobs(job_ids=[job_id])[0]
    assert recovered["status"] == "partial"
    assert recovered["total_count"] == recovered["unknown_count"] == 1
    assert recovered["results"][0]["status"] == "unknown"
    assert "未自动重提报名" in recovered["results"][0]["message"]
    assert recovered["finished_at"]
    assert store.get_preview(recovered["preview_id"])["payload"]["compacted"] is True


@pytest.mark.parametrize("action", ["enroll", "withdraw"])
def test_terminal_job_recovery_fills_missing_occurrences_without_replaying(
    store, tmp_path, monkeypatch, action,
):
    from bit import bit_runtime_lock

    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path / "runtime-locks")
    promotion_id = store.upsert_promotion(
        token_id=1, store_name="test", application_id="app", seller_id="child", site_id="MLM",
        row={"id": "terminal-repair", "type": "DEAL", "status": "started"},
    )
    promotion = store.get_promotion(promotion_id)
    rows = [
        {"item_local_id": 1, "item_id": "MLM1", "offer_id": "", "status": "ready", "deal_price": 15},
        {"item_local_id": 2, "item_id": "MLM2", "offer_id": "", "status": "ready", "deal_price": 14},
    ]
    preview_id = store.create_preview(
        actor="test", action=action, promotion_fk=promotion["id"],
        payload={"promotion": promotion, "rows": rows},
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    job_id = store.create_job(
        preview_id=preview_id, actor="test", action=action, total=2,
    )
    store.save_job_progress(job_id, results=[{
        **rows[0], "status": "succeeded", "message": "saved result",
    }])
    original_finished_at = datetime.now(timezone.utc).isoformat()
    with store._connect() as db:
        db.execute(
            "UPDATE promotion_jobs SET status='partial', finished_at=? WHERE job_id=?",
            (original_finished_at, job_id),
        )

    complete_preview_id = store.create_preview(
        actor="test", action=action, promotion_fk=promotion["id"],
        payload={"promotion": promotion, "rows": [rows[0]]},
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    complete_job_id = store.create_job(
        preview_id=complete_preview_id, actor="test", action=action, total=1,
    )
    store.finish_job(complete_job_id, results=[{
        **rows[0], "status": "failed", "message": "complete marker",
    }])

    lease = service.InterProcessLock(
        f"promotion_campaign_{promotion['id']}", owner="live-test-worker",
    )
    assert lease.acquire(timeout=0)
    try:
        assert service.recover_stale_running_jobs(
            store=store, token_ids=[1], promotion_fks=[promotion["id"]],
        ) == 0
        assert len(store.list_jobs(job_ids=[job_id])[0]["results"]) == 1
    finally:
        lease.release()

    assert service.recover_stale_running_jobs(
        store=store, token_ids=[1], promotion_fks=[promotion["id"]],
        include_terminal=False,
    ) == 0
    assert len(store.list_jobs(job_ids=[job_id])[0]["results"]) == 1

    assert service.recover_stale_running_jobs(
        store=store, token_ids=[1], promotion_fks=[promotion["id"]],
    ) == 1
    repaired = store.list_jobs(job_ids=[job_id])[0]
    assert repaired["status"] == "partial"
    assert repaired["total_count"] == 2
    assert repaired["success_count"] == repaired["unknown_count"] == 1
    assert repaired["failed_count"] == repaired["blocked_count"] == 0
    assert repaired["finished_at"] == original_finished_at
    assert [row["item_id"] for row in repaired["results"]] == ["MLM1", "MLM2"]
    assert repaired["results"][1]["status"] == "unknown"
    assert "历史执行日志不完整" in repaired["results"][1]["message"]
    assert store.get_preview(preview_id)["payload"]["compacted"] is True

    complete = store.list_jobs(job_ids=[complete_job_id])[0]
    assert complete["status"] == "failed"
    assert complete["results"][0]["message"] == "complete marker"
    assert store.get_preview(complete_preview_id)["payload"].get("compacted") is not True
    assert service.recover_stale_running_jobs(
        store=store, token_ids=[1], promotion_fks=[promotion["id"]],
    ) == 0
    if action == "enroll":
        unresolved = store.unresolved_enrollment_results(
            promotion["id"], item_ids=["MLM2"],
        )
        assert len(unresolved) == 1
        assert unresolved[0]["job_id"] == job_id


def test_terminal_recovery_drains_more_than_one_bounded_batch():
    jobs = [{
        "job_id": f"job-{index}", "preview_id": f"preview-{index}",
        "status": "partial", "total_count": 1, "results": [],
    } for index in range(501)]

    class BatchedStore:
        def __init__(self):
            self.batches = [jobs[:500], jobs[500:], []]
            self.finished = []

        def list_jobs(self, **kwargs):
            if kwargs.get("status") == "running":
                return []
            assert kwargs.get("repairable_only") is True
            return self.batches.pop(0)

        def get_preview(self, preview_id):
            item_id = preview_id.replace("preview-", "MLM-")
            return {"payload": {"rows": [{"item_id": item_id, "status": "ready"}]}}

        def finish_job(self, job_id, *, results, preserve_finished_at=False):
            assert preserve_finished_at is True
            assert len(results) == 1 and results[0]["status"] == "unknown"
            self.finished.append(job_id)

        def compact_preview(self, _preview_id):
            pass

    store = BatchedStore()
    assert service._recover_jobs_for_campaign(store, 1) == 501
    assert store.finished == [job["job_id"] for job in jobs]
    assert store.batches == []


def test_live_listing_withdrawal_holds_campaign_lock_against_recovery(
    store, tmp_path, monkeypatch,
):
    import threading
    from bit import bit_runtime_lock

    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path / "runtime-locks")
    promotion, _, _ = setup_job(store, status="started")
    # setup_job creates an unrelated running fixture job; close it before the
    # withdrawal so the production path can recover it under its campaign lock.
    stale = store.latest_job(promotion["id"])
    store.finish_job(stale["job_id"], results=[{
        "item_id": "MLM1", "status": "succeeded", "message": "fixture complete",
    }])
    entered, release = threading.Event(), threading.Event()
    errors = []

    def withdraw(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return {}

    def run():
        try:
            service.withdraw_listing_promotions(
                token_id=1,
                site_id="MLM",
                item_id="MLM1",
                actor="test",
                store=store,
                adapter=SimpleNamespace(withdraw_item=withdraw),
            )
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(3)
        running = store.latest_job(promotion["id"])
        assert running["status"] == "running"
        assert service.recover_stale_running_jobs(
            store=store, promotion_fks=[promotion["id"]], token_ids=[1],
        ) == 0
        assert store.latest_job(promotion["id"])["status"] == "running"
    finally:
        release.set()
        worker.join(5)

    assert not worker.is_alive()
    assert errors == []
    assert store.latest_job(promotion["id"])["status"] == "succeeded"
