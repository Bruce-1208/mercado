import copy
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from erp import mercadolibre_virtual_ean as ean


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    path = tmp_path / 'codes.sqlite3'
    monkeypatch.setenv('MERCADO_VIRTUAL_EAN_DB', str(path))
    return path


def code(record):
    return next(a['value_name'] for a in record['attributes'] if a['id'] == 'GTIN')


def test_checksum_known_vector_and_reject_invalid_input():
    assert ean.ean13('400638133393') == '4006381333931'
    for value in ('123', 'a' * 12, '２' * 12):
        with pytest.raises(ValueError):
            ean.ean13(value)


def test_assignments_are_durable_unique_and_concurrent(registry):
    identities = [str(i % 20) for i in range(100)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(ean.allocate, identities))
    assert len(set(codes)) == 20
    assert codes[:20] == codes[20:40]
    for value in codes:
        assert len(value) == 13 and value.isascii() and value.isdigit()
        assert sum(int(v) * (1 if i % 2 == 0 else 3) for i, v in enumerate(value)) % 10 == 0
    with sqlite3.connect(registry) as db:
        assert db.execute('SELECT COUNT(*) FROM assignments').fetchone()[0] == 20
    assert ean.allocate('0') == codes[0]


@pytest.mark.parametrize('record', [
    {'attributes': [{'id': 'GTIN', 'value_name': '4006381333931'}]},
    {'attributes': [{'id': 'EMPTY_GTIN_REASON', 'value_name': 'Not registered'}]},
    {'ean': '4006381333931'}, {'upc': '036000291452'},
])
def test_preserves_existing_identifiers_and_exemptions_without_allocation(record, registry):
    original = copy.deepcopy(record)
    result = ean.ensure_virtual_eans(record, 'product')
    assert '_virtual_eans' not in result
    assert not registry.exists()
    assert record == original
    if 'attributes' in original:
        assert result['attributes'] == original['attributes']
    else:
        assert code(result) in original.values()


def test_empty_gtin_replaced_once_without_mutating_source():
    source = {'attributes': [{'id': 'GTIN', 'value_id': '-1'}, {'id': 'BRAND', 'value_name': 'Generic'}]}
    before = copy.deepcopy(source)
    result = ean.ensure_virtual_eans(source, 'product')
    assert len(result['attributes']) == 2
    assert source == before
    assert ean.ensure_virtual_eans(source, 'product') == result
    assert ean.ensure_virtual_eans(result, 'product') == result
    assert code(ean.ensure_virtual_eans(source, 'different-product')) != code(result)


def test_existing_ean_alias_and_scalar_replace_empty_gtin_without_duplicates(registry):
    for record in ({'ean': '4006381333931', 'attributes': [{'id': 'GTIN', 'value_name': ''}]},
                   {'attributes': [{'id': 'EAN', 'value_name': '4006381333931'}]}):
        result = ean.ensure_virtual_eans(record, 'product')
        assert result['attributes'] == [{'id': 'GTIN', 'value_name': '4006381333931'}]
        assert not registry.exists()


def test_skus_keep_codes_after_reordering_and_quantity_price_changes():
    source = {'variations': [
        {'seller_sku': 'blue', 'price': 10, 'available_quantity': 2},
        {'seller_sku': 'red'},
        {'attributes': [{'id': 'GTIN', 'value_name': '4006381333931'}]},
    ]}
    first = ean.ensure_virtual_eans(source, 'product')
    source['variations'].reverse()
    source['variations'][2].update(price=20, available_quantity=5)
    second = ean.ensure_virtual_eans(source, 'product')
    assert [code(v) for v in first['variations']] == [code(v) for v in reversed(second['variations'])]
    assert len({code(v) for v in first['variations']}) == 3
    assert 'attributes' not in first


def test_sku_option_identity_works_without_ids_and_ambiguous_skus_fail():
    source = {'variations': [{'attribute_combinations': [{'id': 'COLOR', 'value_name': color}]}
                             for color in ['Blue', 'Red']]}
    result = ean.ensure_virtual_eans(source, 'product')
    assert code(result['variations'][0]) != code(result['variations'][1])
    for variants in ([{}, {}], [{'id': 'same'}, {'id': 'same'}]):
        with pytest.raises(ValueError, match='唯一标识'):
            ean.ensure_virtual_eans({'variations': variants}, 'product')


@pytest.mark.parametrize('source_type', ['zying', 'ai_original'])
def test_product_and_original_listing_preparation_share_codes(source_type):
    from erp.mercadolibre_batch_publish import _prepared_listing_from_product_row
    row = {'id': 101, 'source_type': source_type, 'source_item_id': 'MLM123',
           'source_snapshot_json': {'source': {'id': 'MLM123'}, 'ai_original': {
               'title_es': 'Producto', 'title_pt': 'Produto',
               'description_es': 'Descripcion', 'description_pt': 'Descricao', 'attributes': [],
           }}}
    before = copy.deepcopy(row)
    mexico, _ = _prepared_listing_from_product_row(row, 'MLM')
    brazil, _ = _prepared_listing_from_product_row(row, 'MLB')
    assert code(mexico) == code(brazil)
    assert mexico['_virtual_eans'][0]['value'] == code(mexico)
    assert row == before


def test_preview_submit_and_retry_share_code_and_visible_warning():
    from test_mercadolibre_publication_preflight import Client, call, source
    client = Client([{'id': 'BRAND'}, {'id': 'GTIN', 'tags': {'required': True}}])
    item = source()
    preview = call(client, item, virtual_ean_identity='product')
    warnings = [i for i in preview['preflight']['issues'] if i['code'] == 'virtual_ean']
    assert len(warnings) == 1 and warnings[0]['severity'] == 'warning'
    assert not client.posts
    checkpoints = []
    call(client, item, virtual_ean_identity='product', prepared_draft=preview['draft'],
         publish=True, on_prepared=lambda draft: checkpoints.append(draft))
    assert client.posts == [preview['payload']]
    assert code(checkpoints[0]['source']) == code(preview['payload'])
    assert code(call(client, item, virtual_ean_identity='product')['payload']) == code(preview['payload'])


def test_legacy_database_listing_gets_code_before_validation(monkeypatch):
    from test_mercadolibre_publication_preflight import Client, source
    from erp.mercadolibre_follow_sell import follow_sell
    from erp import mercadolibre_source_store
    monkeypatch.setattr(mercadolibre_source_store, 'load_listing_for_publish', lambda _: (source(), {}))
    client = Client([{'id': 'BRAND'}, {'id': 'GTIN', 'tags': {'required': True}}])
    preview = follow_sell(client, 'MLM123', source_from_database=True, virtual_ean_identity='product')
    assert code(preview['payload'])
    assert not client.posts


def test_user_product_family_preserves_existing_code_and_fills_only_missing_sku():
    from test_erp_mercadolibre_follow_sell import FamilyClient, family_source
    from erp.mercadolibre_follow_sell import follow_sell
    source = family_source()
    result = follow_sell(FamilyClient(), source['id'], prepared_listing=(source, {}),
                         net_proceeds=20, virtual_ean_identity='family')
    assert code(result['payload'][0]) == '1234567890123'
    assert code(result['payload'][1]).startswith('20')
    assert len([i for i in result['preflight']['issues'] if i['code'] == 'virtual_ean']) == 1
    assert all('_virtual_eans' not in child for child in result['payload'])


def test_shared_registry_reuses_assignments_and_resolves_collision(tmp_path, monkeypatch):
    from test_mercadolibre_publication_drafts import Connection
    class RegistryConnection(Connection):
        def execute(self, sql, args=()):
            if sql.lstrip().startswith('CREATE TABLE'):
                sql = 'CREATE TABLE IF NOT EXISTS erp_meli_virtual_eans (identity_hash TEXT PRIMARY KEY, ean TEXT UNIQUE)'
            sql = sql.replace('INSERT INTO', 'INSERT OR IGNORE INTO').replace(' FOR UPDATE', '')
            sql = sql.replace(' ON DUPLICATE KEY UPDATE identity_hash = identity_hash', '')
            self.result = self.db.execute(sql.replace('%s', '?'), args)
    original_candidate = ean._candidate
    monkeypatch.setattr(ean, '_candidate', lambda identity, attempt:
                        original_candidate('collision', 0) if attempt == 0 else original_candidate(identity, attempt))
    factory = lambda: RegistryConnection(tmp_path / 'shared.sqlite3')
    first = ean.allocate_shared('a', connection_factory=factory)
    second = ean.allocate_shared('b', connection_factory=factory)
    assert first != second
    assert ean.allocate_shared('a', connection_factory=factory) == first
    assert ean.allocate_shared('b', connection_factory=factory) == second


def test_client_allocates_through_shared_authenticated_database_api(monkeypatch):
    from bit import bit_db_api
    monkeypatch.delenv('MERCADO_VIRTUAL_EAN_DB')
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    calls = []
    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return {'ean': '2000000000015'}
    monkeypatch.setattr(bit_db_api, '_request', request)
    assert ean.allocate('product') == '2000000000015'
    assert calls == [(('POST', '/api/db/mercado-products/virtual-ean'), {'json': {'identity': 'product'}})]
