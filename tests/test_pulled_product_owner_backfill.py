from scripts.backfill_pulled_product_snapshot_owners import resolve


def test_store_identity_is_scoped_by_enterprise_and_site():
    identities = {('a', 7, 'MLM'): '甲', ('a', 7, 'MLB'): '乙', ('b', 7, 'MLM'): '丙'}
    assert resolve({'token_id': 7, 'site_id': 'mlm'}, 'a', identities, {}) == ('甲', 'matched')
    assert resolve({'token_id': 7, 'site_id': 'MLB'}, 'a', identities, {}) == ('乙', 'matched')
    assert resolve({'token_id': 7, 'site_id': 'MLM'}, 'b', identities, {}) == ('丙', 'matched')


def test_historical_name_fallback_requires_unique_current_assignment():
    names = {('a', 'Store', 'MLM'): {'甲'}, ('a', 'Store', 'MLB'): {'甲', '乙'}}
    assert resolve({'store_name': 'Store', 'site_id': 'MLM'}, 'a', {}, names) == ('甲', 'matched')
    assert resolve({'store_name': 'Store', 'site_id': 'MLB'}, 'a', {}, names) == (None, 'ambiguous_store')
    assert resolve({'store_name': 'Store', 'site_id': 'MLM'}, 'b', {}, names) == (None, 'unmatched_store')
    # A removed explicit token must not be replaced with another same-named store.
    assert resolve({'token_id': 8, 'store_name': 'Store', 'site_id': 'MLM'}, 'a', {}, names) == (None, 'store_unassigned')
