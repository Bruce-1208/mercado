from erp.mercadolibre_promotion_store import PromotionStore


def test_enrolled_activity_prices_are_scoped_and_exclude_candidates(tmp_path):
    store = PromotionStore(tmp_path / 'promotions.sqlite3')
    for token, site, name, status, price, currency in [
        (1, 'MLM', '正在参加', 'started', 12, 'USD'),
        (1, 'MLM', '即将开始', 'pending', 200, 'MXN'),
        (1, 'MLM', '候选活动', 'candidate', 9, 'USD'),
        (1, 'MLM', '结束活动', 'finished', 8, 'USD'),
        (2, 'MLM', '其他店铺', 'started', 7, 'USD'),
        (1, 'MLB', '其他站点', 'started', 6, 'USD'),
    ]:
        pk = store.upsert_promotion(
            token_id=token, store_name='店铺', application_id='app',
            seller_id=str(token), site_id=site,
            row={'id': name, 'name': name, 'type': 'DEAL', 'status': 'started'},
        )
        store.replace_items(pk, [{'id': 'MLM1', 'status': status, 'price': price,
                                  'currency_id': currency}])
    key = (1, 'MLM', 'MLM1')
    rows = store.applied_item_details([key])[key]
    assert [(r['name'], r['price'], r['currency_id']) for r in rows] == [
        ('正在参加', 12, 'USD'), ('即将开始', 200, 'MXN')]
    assert store.applied_item_keys([key]) == {key}
    assert store.applied_item_details([]) == {}
    # Max-size listing pages must not exceed SQLite's expression depth.
    keys = [(1, 'MLM', f'MLM{i}') for i in range(1000)]
    assert store.applied_item_details(keys) == {key: rows}


def test_clearance_is_scoped_requires_complete_sync_and_excludes_candidates(tmp_path):
    store = PromotionStore(tmp_path / 'promotions.sqlite3')
    for token, site, kind, item, status, parent in [
        (1, 'MLM', 'CLEARANCE', 'MLM1', 'started', 'started'),
        (1, 'MLM', 'CLEARANCE', 'MLM2', 'candidate', 'started'),
        (1, 'MLM', 'CLEARANCE', 'MLM3', 'finished', 'started'),
        (1, 'MLM', 'CLEARANCE', 'MLM4', 'started', 'finished'),
        (1, 'MLM', 'DEAL', 'MLM5', 'started', 'started'),
        (2, 'MLM', 'CLEARANCE', 'MLM5', 'started', 'started'),
        (1, 'MLB', 'CLEARANCE', 'MLM5', 'started', 'started'),
        (1, 'MLM', 'CLEARANCE', 'MLM6', 'pending', 'started'),
    ]:
        pk = store.upsert_promotion(token_id=token, store_name='半托管', application_id='app',
            seller_id=str(token), site_id=site, row={'id':item, 'type':kind, 'status':parent})
        store.replace_items(pk, [{'id':item, 'status':status}])
    keys = [(1, 'MLM', f'MLM{i}') for i in range(1, 8)]
    assert all(v['status'] == 'unknown' for v in store.clearance_item_states(keys).values())
    store.record_scope_sync(1, 'MLM', 'succeeded', [f'MLM{i}' for i in range(1, 8)])
    rows = store.clearance_item_states(keys)
    assert rows[keys[0]]['status'] == 'enrolled'
    assert rows[keys[5]]['status'] == 'enrolled'
    assert all(rows[keys[i]]['status'] == 'not_enrolled' for i in [1,2,3,4,6])
    store.record_scope_sync(1, 'MLM', 'running')
    assert store.clearance_item_states(keys)[keys[0]]['status'] == 'unknown'
    # A complete zero-activity response expires previous enrollments.
    store.record_scope_sync(1, 'MLM', 'succeeded', [])
    assert all(v['status'] == 'not_enrolled' for v in store.clearance_item_states(keys).values())
    assert store.clearance_item_states([(2,'MLM','MLM5')])[(2,'MLM','MLM5')]['status'] == 'unknown'


def test_expired_or_stale_clearance_is_not_shown_as_current(tmp_path):
    store = PromotionStore(tmp_path / 'promotions.sqlite3')
    pk = store.upsert_promotion(token_id=1, store_name='半托管', application_id='app', seller_id='1',
        site_id='MLM', row={'id':'clearance', 'type':'CLEARANCE', 'status':'started',
                           'finish_date':'2020-01-01T00:00:00Z'})
    store.replace_items(pk, [{'id':'MLM1', 'status':'started'}])
    store.record_scope_sync(1, 'MLM', 'succeeded', ['clearance'])
    key = (1,'MLM','MLM1')
    assert store.clearance_item_states([key])[key]['status'] == 'not_enrolled'
    with store._connect() as db:
        db.execute("UPDATE promotion_sync_scopes SET checked_at='2020-01-01T00:00:00+00:00'")
    assert store.clearance_item_states([key])[key]['status'] == 'unknown'


def test_sync_only_marks_clearance_snapshot_complete_after_all_pages(monkeypatch, tmp_path):
    from bit import mercado_promotions as sync
    from types import SimpleNamespace
    store = PromotionStore(tmp_path / 'promotions.sqlite3')
    monkeypatch.setattr(sync, '_token_summaries', lambda: [{'id':1, 'enabled':True}])
    monkeypatch.setattr(sync.bit_mysql, 'get_mercado_store_token', lambda token: {'meli_user_id':'1'})
    monkeypatch.setattr(sync, '_marketplace_accounts', lambda *a: [{'user_id':'1','site_id':'MLM'}])
    adapter = SimpleNamespace(client=None,
        iter_promotions=lambda u: iter([{'id':'c1','type':'CLEARANCE','status':'started'}]),
        iter_items=lambda *a: iter([{'id':'MLM1','status':'started'}]))
    monkeypatch.setattr(sync, '_adapter', lambda t: (adapter,t))
    assert sync.sync_promotions([1],store=store)['succeeded'] == 1
    key=(1,'MLM','MLM1')
    assert store.clearance_item_states([key])[key]['status'] == 'enrolled'
    def partial(*a):
        yield {'id':'MLM2','status':'started'}
        raise RuntimeError('second page failed')
    adapter.iter_items=partial
    assert sync.sync_promotions([1],store=store)['failed'] == 1
    assert store.clearance_item_states([key])[key]['status'] == 'unknown'
    # Preserve previous item data after a partial page, but do not assert currency.
    assert store.applied_item_details([key])[key][0]['promotion_type'] == 'CLEARANCE'


def test_available_activities_are_candidates_and_scoped(tmp_path):
    store = PromotionStore(tmp_path / 'promotions.sqlite3')
    for token, site, status, parent, end in [
        (1, 'MLM', 'candidate', 'started', ''),
        (1, 'MLM', 'started', 'started', ''),
        (2, 'MLM', 'candidate', 'started', ''),
        (1, 'MLB', 'candidate', 'started', ''),
        (1, 'MLM', 'candidate', 'finished', ''),
        (1, 'MLM', 'candidate', 'started', '2020-01-01T00:00:00Z'),
    ]:
        name = f'{token}-{site}-{status}-{parent}-{end}'
        pk = store.upsert_promotion(token_id=token, store_name='Store', application_id='app',
            seller_id=str(token), site_id=site,
            row={'id': name, 'name': name, 'type': 'DEAL', 'status': parent, 'finish_date': end})
        store.replace_items(pk, [{'id': 'MLM1', 'status': status, 'price': 10}])
    key = (1, 'MLM', 'MLM1')
    result = store.available_item_details([key])
    assert len(result[key]) == 1
    assert result[key][0]['status_raw'] == 'candidate'
    assert store.applied_item_details([key])[key][0]['status_raw'] == 'started'
    assert store.available_item_details([]) == {}
    assert store.available_item_details([(1, 'MLM', f'MLM{i}') for i in range(1000)]) == result
