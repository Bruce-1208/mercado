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
