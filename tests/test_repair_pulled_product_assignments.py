from scripts.repair_pulled_product_assignments import plan


def product(id, name, *, token=None, org='wuhan-zeshun', seller='123', site='MLM'):
    return dict(id=id, organization_key=org, plugin=dict(store_name=name, token_id=token, site_id=site), seller=seller)


def test_restore_missing_directory_and_same_seller_historical_alias():
    tokens = [dict(id=7, organization_key='wuhan-zeshun', display_name='Store')]
    browsers = [dict(shop_name='Store', salesperson='甲')]
    restore, changes, counts = plan(tokens, [], browsers, [
        product(1, 'Store', token=7), product(2, 'Store'), product(3, 'Old name')])
    assert len(restore) == 6
    assert [(r['id'], r['salesperson'], r['reason']) for r in changes] == [
        (1, '甲', 'explicit_store'), (2, '甲', 'current_name'), (3, '甲', 'seller_identity')]
    assert counts['seller_identity'] == 1


def test_preserve_site_settings_enterprise_and_explicit_token_boundaries():
    tokens = [dict(id=7, organization_key='wuhan-zeshun', display_name='Store'),
              dict(id=8, organization_key='other', display_name='Store')]
    settings = [dict(token_id=7, site_id='MLM', salesperson='乙')]
    restore, changes, counts = plan(tokens, settings, [dict(shop_name='Store', salesperson='甲')], [
        product(1, 'Store', token=7), product(2, 'Store', token=7, site='MLB'),
        product(3, 'Store', token=8, org='other'), product(4, 'Store', token=99),
        product(5, 'Old', org='other'), product(6, 'Old', site='MLB')])
    assert restore == []
    assert [(r['id'], r['salesperson']) for r in changes] == [(1, '乙')]
    assert counts['unresolved'] == 5


def test_ambiguous_seller_and_excluded_store_stay_unassigned():
    browsers = [dict(shop_name='A', salesperson='甲'), dict(shop_name='B', salesperson='乙')]
    restore, changes, counts = plan([], [], browsers, [
        product(1, 'A'), product(2, 'B'), product(3, 'Old'), product(4, 'Deferred')],
        excluded_stores=['Deferred'])
    assert [r['id'] for r in changes] == [1, 2]
    assert counts['unresolved'] == 2
