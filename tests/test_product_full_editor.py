"""Full product edits execute real persistence SQL against the offline adapter."""
import json
from decimal import Decimal

import pytest

from erp import mercadolibre_collection_store as store
from test_purchase_net_proceeds_persistence import database, _row  # noqa: F401


@pytest.mark.parametrize('source_type', ['collected', 'zying', 'ai_original', 'pulled'])
def test_full_edit_round_trip_and_manual_net_survives_refresh(database, source_type):
    database.db.create_function('NOW', 0, lambda: '2026-10-13 10:00:00')
    for table in (store.PRODUCT_TABLE, store.COLLECTION_TABLE):
        row = _row(source_type=source_type, purchased=False)
        if source_type == 'ai_original':
            snapshot = json.loads(row['source_snapshot_json'])
            snapshot['marketplace_listings'] = {
                'amazon': {'status': 'completed', 'review_status': 'approved'},
                'yandex': {'status': 'completed', 'review_status': 'approved'},
            }
            row['source_snapshot_json'] = json.dumps(snapshot)
            row['review_status'] = 'approved'
        row['organization_key'] = 'test'
        database.insert(table, **row)
    changes = {
        'title': 'Edited title', 'price': 130, 'weight_g': 420,
        'package_length_cm': 30, 'package_width_cm': 20, 'package_height_cm': 10,
        'description_text': 'Full description', 'net_proceeds_usd': '18.1250',
        'title_pt': 'Titulo portugues', 'description_pt': 'Descricao completa',
        'pictures': [{'id': 'IMG1', 'source': 'https://example.test/1.jpg'}, {'source': 'https://example.test/2.jpg'}],
        'attributes': [{'id': 'MATERIAL', 'value_name': 'Steel', 'value_id': '42'}],
        'variations': [{'seller_sku': 'BLUE', 'available_quantity': 9, 'picture_ids': ['IMG1']}],
        'variation_dimensions': [{'id': 'COLOR', 'name': 'Color', 'values': ['Blue']}],
        'sale_terms': [{'id': 'WARRANTY_TIME', 'value_name': '12 months'}],
        'available_quantity': 0, 'condition': 'new',
    }
    result = store.update_product_item(1, changes, connection_factory=lambda: database)
    assert not result['profitability_refresh_pending']
    saved = database.row(store.PRODUCT_TABLE)
    snapshot = json.loads(saved['source_snapshot_json'])
    assert saved['title'] == changes['title']
    assert saved['weight_g'] == 420
    assert saved['net_proceeds_usd'] == 18.125
    assert saved['profitability_source'] == 'manual_product_edit'
    for key in ('pictures', 'attributes', 'variations', 'variation_dimensions', 'sale_terms', 'available_quantity', 'condition'):
        assert snapshot['source'][key] == changes[key]
        assert json.loads(database.row(store.COLLECTION_TABLE)['source_json'])[key] == changes[key]
    assert snapshot['source']['title'] == changes['title']
    if source_type == 'zying':
        assert Decimal(str(snapshot['zying_net_proceeds_usd'])) == Decimal('18.125')
    if source_type == 'ai_original':
        assert Decimal(str(snapshot['source_1688_overrides']['net_proceeds_usd'])) == Decimal('18.125')
        prepared = snapshot['ai_original']
        assert prepared['title_es'] == changes['title']
        assert prepared['description_es'] == changes['description_text']
        assert prepared['title_pt'] == changes['title_pt']
        assert prepared['attributes'] == changes['attributes']
        assert prepared['variations'] == changes['variations']
        assert prepared['edit_revision'] == 1
        assert snapshot['marketplace_listings']['mercadolibre'] == prepared
        assert saved['review_status'] == 'unreviewed'
        assert {snapshot['marketplace_listings'][name]['review_status'] for name in ('amazon', 'yandex')} == {'unreviewed'}
    # A previously queued estimator must not overwrite a freshly saved target.
    store.update_item_profitability('MLM1', {'id': 1, 'source_type': 'collected', 'net_proceeds_usd': 99}, connection_factory=lambda: database)
    assert database.row(store.PRODUCT_TABLE)['net_proceeds_usd'] == 18.125
    store.update_product_item(1, {'weight_g': 450}, connection_factory=lambda: database)
    assert database.row(store.PRODUCT_TABLE)['net_proceeds_usd'] == 18.125
    store.update_product_item(1, {'attributes': [], 'variations': [], 'sale_terms': []}, connection_factory=lambda: database)
    source = json.loads(database.row(store.PRODUCT_TABLE)['source_snapshot_json'])['source']
    assert source['attributes'] == source['variations'] == source['sale_terms'] == []
    assert source['pictures'] == changes['pictures']


def test_bulk_product_measurement_edit_returns_ai_original_to_review(database):
    for table in (store.PRODUCT_TABLE, store.COLLECTION_TABLE):
        row = _row(source_type='ai_original', purchased=False)
        snapshot = json.loads(row['source_snapshot_json'])
        snapshot['marketplace_listings'] = {
            'amazon': {'status': 'completed', 'review_status': 'approved'},
        }
        row.update(source_snapshot_json=json.dumps(snapshot), review_status='approved',
                   organization_key='test')
        database.insert(table, **row)

    result = store.update_product_items(
        [1], {'weight_g': 560}, connection_factory=lambda: database,
    )

    saved = database.row(store.PRODUCT_TABLE)
    snapshot = json.loads(saved['source_snapshot_json'])
    assert result['changed'] == 1
    assert saved['weight_g'] == 560
    assert saved['weight_basis'] == 'ai_original_manual'
    assert saved['review_status'] == 'unreviewed'
    assert snapshot['marketplace_listings']['amazon']['review_status'] == 'unreviewed'


def test_single_ai_copy_edit_refreshes_mercado_validation(database):
    for table in (store.PRODUCT_TABLE, store.COLLECTION_TABLE):
        row = _row(source_type='ai_original', purchased=False)
        snapshot = json.loads(row['source_snapshot_json'])
        prepared = {
            'status': 'completed', 'title_es': 'Tren de madera para niños',
            'title_pt': 'Trem de madeira para crianças',
            'description_es': 'Descripcion completa', 'description_pt': 'Descricao completa',
            'main_image_url': 'https://example.test/product-ai-white.jpg',
            'validation': {'errors': [], 'warnings': []},
            'review_status': 'approved',
        }
        snapshot['ai_original'] = prepared
        snapshot['marketplace_listings'] = {
            'mercadolibre': dict(prepared),
            'amazon': {'status': 'completed', 'review_status': 'approved'},
        }
        row.update(
            source_snapshot_json=json.dumps(snapshot), review_status='approved',
            organization_key='test', title=prepared['title_es'],
            main_image_url=prepared['main_image_url'],
        )
        database.insert(table, **row)

    store.update_product_item(
        1, {'title': 'Beautiful Wooden Toy Train For Small Children'},
        connection_factory=lambda: database,
    )

    saved = database.row(store.PRODUCT_TABLE)
    snapshot = json.loads(saved['source_snapshot_json'])
    assert saved['review_status'] == 'unreviewed'
    assert any(
        '疑似整段英文' in issue
        for issue in snapshot['ai_original']['validation']['errors']
    )
    assert snapshot['marketplace_listings']['mercadolibre']['review_status'] == 'unreviewed'
    assert snapshot['marketplace_listings']['amazon']['review_status'] == 'approved'


def test_bulk_ai_category_and_copy_edit_invalidates_generated_category_work(database):
    for table in (store.PRODUCT_TABLE, store.COLLECTION_TABLE):
        row = _row(source_type='ai_original', purchased=False)
        snapshot = json.loads(row['source_snapshot_json'])
        prepared = {
            'status': 'completed', 'category_id': 'CBT123',
            'title_es': 'Titulo anterior', 'description_es': 'Descripcion anterior',
            'generated_copy': {
                'title_es': 'Titulo anterior', 'title': 'Titulo anterior',
                'description_es': 'Descripcion anterior', 'description': 'Descripcion anterior',
            },
            'preparation': {
                'category_ready': True, 'variations_ready': True, 'required_ready': True,
            },
            'review_status': 'approved',
        }
        snapshot['ai_original'] = prepared
        snapshot['marketplace_listings'] = {
            'mercadolibre': dict(prepared),
            'amazon': {'status': 'completed', 'review_status': 'approved'},
            'yandex': {'status': 'completed', 'review_status': 'approved'},
        }
        row.update(
            source_snapshot_json=json.dumps(snapshot), review_status='approved',
            organization_key='test', category_id='CBT123', title='Titulo anterior',
            description_text='Descripcion anterior',
        )
        database.insert(table, **row)

    result = store.update_product_items(
        [1], {
            'category_id': 'CBT456', 'title': 'Beautiful Wooden Toy Train For Small Children',
            'description_text': 'Descripcion nueva',
        }, connection_factory=lambda: database,
    )

    saved = database.row(store.PRODUCT_TABLE)
    snapshot = json.loads(saved['source_snapshot_json'])
    prepared = snapshot['ai_original']
    assert result['changed'] == 1
    assert saved['category_id'] == 'CBT456'
    assert saved['review_status'] == 'unreviewed'
    assert prepared['category_id'] == 'CBT456'
    assert prepared['status'] == 'failed'
    assert prepared['failed_step'] == 'category'
    assert prepared['workflow_status'] == 'needs_category'
    assert prepared['title_es'] == 'Beautiful Wooden Toy Train For Small Children'
    assert prepared['description_es'] == 'Descripcion nueva'
    assert prepared['generated_copy']['title'] == 'Beautiful Wooden Toy Train For Small Children'
    assert prepared['generated_copy']['description'] == 'Descripcion nueva'
    assert any('疑似整段英文' in issue for issue in prepared['validation']['errors'])
    assert prepared['preparation'] == {
        'category_ready': False, 'variations_ready': False, 'required_ready': False,
    }
    assert snapshot['marketplace_listings']['mercadolibre'] == prepared
    assert snapshot['marketplace_listings']['mercadolibre']['review_status'] == 'unreviewed'
    assert snapshot['marketplace_listings']['amazon']['review_status'] == 'approved'
    assert snapshot['marketplace_listings']['yandex']['review_status'] == 'approved'
    with pytest.raises(ValueError, match='原创任务完成'):
        store.update_ai_original_product(
            1, {'review_status': 'approved'}, connection_factory=lambda: database,
        )


def test_ai_batch_dimensions_reset_independent_platform_reviews(database):
    for table in (store.PRODUCT_TABLE, store.COLLECTION_TABLE):
        row = _row(source_type='ai_original', purchased=False)
        snapshot = json.loads(row['source_snapshot_json'])
        snapshot['marketplace_listings'] = {
            'amazon': {'status': 'completed', 'review_status': 'approved'},
            'yandex': {'status': 'completed', 'review_status': 'approved'},
        }
        row.update(source_snapshot_json=json.dumps(snapshot), review_status='approved',
                   organization_key='test', package_length_cm=None,
                   package_width_cm=None, package_height_cm=None)
        database.insert(table, **row)

    result = store.update_ai_original_product_items(
        [1], {'package_length_cm': 30, 'package_width_cm': 20, 'package_height_cm': 10},
        connection_factory=lambda: database,
    )

    saved = database.row(store.PRODUCT_TABLE)
    snapshot = json.loads(saved['source_snapshot_json'])
    assert result['changed'] == 1
    assert (saved['package_length_cm'], saved['package_width_cm'], saved['package_height_cm']) == (30, 20, 10)
    assert saved['review_status'] == 'unreviewed'
    assert {snapshot['marketplace_listings'][name]['review_status'] for name in ('amazon', 'yandex')} == {'unreviewed'}


@pytest.mark.parametrize('changes', [
    {'net_proceeds_usd': 'NaN'}, {'net_proceeds_usd': None}, {'net_proceeds_usd': '1e20'},
    {'pictures': [{'source': 'javascript:alert(1)'}]}, {'attributes': {}}, {'attributes': [{}]},
    {'available_quantity': -1}, {'available_quantity': 1.5}, {'condition': 'bad'},
])
def test_invalid_full_edit_does_not_open_database(changes):
    def unexpected_connection():
        pytest.fail('Validation must precede writes')
    with pytest.raises(ValueError):
        store.update_product_item(1, changes, connection_factory=unexpected_connection)
