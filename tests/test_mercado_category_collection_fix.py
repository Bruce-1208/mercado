import json
from pathlib import Path

import pytest
from erp.mercadolibre_collection_attributes import collection_category_id, check_required_attributes
from test_mercado_page_facts import browser, URL  # noqa: F401


@pytest.mark.parametrize('state,expected', [
    ({'item': {'category_id': 'MLM123'}}, 'MLM123'),
    ({'page': {'tracking': {'categoryId': 'MLB456'}}}, 'MLB456'),
    ({'item': {'category': {'id': 'MCO789', 'name': 'Accesorios'}}}, 'MCO789'),
    ({'recommendations': {'category_id': 'MLM999'}, 'item': {'category_id': 'MLM123'}}, 'MLM123'),
    ({'recommendations': {'category_id': 'MLM999'}}, ''),
    ({'category_id': 'Shoes'}, ''),
])
def test_extension_collects_platform_category_without_using_recommendations(browser, state, expected):
    page = browser.new_page()
    try:
        page.set_content('<h1>商品</h1><script>window.__PRELOADED_STATE__ = ' + json.dumps(state) + ';</script>')
        page.add_script_tag(path=str(Path(__file__).resolve().parents[1] / 'browser_extension/zeshun_collector/collector-core.js'))
        row = page.evaluate('(url) => ZeshunCollectorCore.extractProduct(document, url)', URL)
        assert row['category_id'] == row['source']['category_id'] == row['page_snapshot']['category_id'] == expected
        if expected:
            assert check_required_attributes(row, [])['status'] == 'complete'
    finally:
        page.close()


@pytest.mark.parametrize('field', ['source_json', 'page_snapshot_json', 'plugin_snapshot_json'])
def test_legacy_snapshot_category_recovered(field):
    assert collection_category_id({field: json.dumps({'categoryId': 'mlm123'})}) == 'MLM123'
    assert collection_category_id({'category_id': 'CBT456', field: {'category_id': 'MLM123'}}) == 'CBT456'


def test_shared_catalog_check_uses_recovered_category():
    from erp.catalog_attributes import facts, inspect
    row = {'page_snapshot_json': {'category_id': 'MLM123'}}
    assert facts(row, 'collection')['category_id'] == 'MLM123'
    assert inspect(row, 'collection', [])['status'] == 'complete'


def test_ingestion_persists_category_from_source_and_preserves_missing_as_null():
    from erp import mercadolibre_collection_store as store
    from test_erp_mercadolibre_collection_store import _FakeConnection
    for source, expected in [({'category_id': 'MLM123'}, 'MLM123'), ({}, None)]:
        connection = _FakeConnection()
        store.upsert_collection_items(1, [{'source_item_id': 'MLM123456', 'source': source}], connection_factory=lambda: connection)
        sql, params = next((sql, params) for sql, params in connection.fake_cursor.queries if sql.startswith(f'INSERT INTO `{store.COLLECTION_TABLE}`'))
        columns = sql.split('(', 1)[1].split(')', 1)[0].replace('`', '').split(',')
        assert params[[c.strip() for c in columns].index('category_id')] == expected
