from copy import deepcopy
from decimal import Decimal
import json
import pytest
from erp import mercadolibre_collection_store as store
from erp.ai_original_attribute_labels import localize_attribute_labels
from test_ai_original_category_lock import SnapshotDB


def test_legacy_package_values_follow_columns_and_preserve_source():
    snapshot = {'ai_original': {'attributes': [
        {'id': 'PACKAGE_LENGTH', 'value_name': '999 cm', 'value_id': 'old'},
        {'id': 'SELLER_PACKAGE_WEIGHT', 'value_name': '2 kg', 'value_struct': {'number': 2, 'unit': 'kg'}},
        {'id': 'MATERIAL', 'value_name': 'Cotton'}]}, 'original_1688': {'weight_g': 1}}
    row = {'source_type': 'ai_original', 'source_snapshot_json': json.dumps(snapshot),
           'package_length_cm': 12, 'weight_g': 350, 'net_proceeds_usd': 8}
    output = store._mirror_zying_snapshot_fields(row)
    attrs = {a['id']: a for a in output['ai_original']['attributes']}
    assert attrs['PACKAGE_LENGTH'] == {'id': 'PACKAGE_LENGTH', 'value_name': '12 cm'}
    assert attrs['SELLER_PACKAGE_WEIGHT'] == {'id': 'SELLER_PACKAGE_WEIGHT', 'value_name': '350 g'}
    assert attrs['MATERIAL']['value_name'] == 'Cotton'
    assert output['ai_original']['net_proceeds_usd'] == 8
    assert output['original_1688']['weight_g'] == 1
    assert json.loads(row['source_snapshot_json']) == snapshot


class ManualDB(SnapshotDB):
    def execute(self, query, params=None):
        if query.startswith('UPDATE') and '`weight_basis`' in query:
            fields = ['package_length_cm', 'package_width_cm', 'package_height_cm']
            for field, value in zip(['weight_g', 'net_proceeds_usd', *fields], params):
                self.row[field] = value
            self.row['weight_basis'] = 'ai_original_manual'
            self.row['volumetric_weight_kg'] = self.row[fields[0]] * self.row[fields[1]] * self.row[fields[2]] / 6000
        else:
            super().execute(query, params)


def test_listing_saves_manual_metrics_and_package_attributes_atomically(monkeypatch):
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    db = ManualDB({'ai_original': {'attributes': [{'id': 'PACKAGE_LENGTH', 'value_name': '999 cm'}]},
                   'marketplace_listings': {'amazon': {'status': 'completed', 'review_status': 'approved'}}})
    output = store.update_ai_original_listing(1, {'weight_g': 350, 'net_proceeds_usd': 12,
        'package_length_cm': 20, 'package_width_cm': 10, 'package_height_cm': 5}, connection_factory=lambda: db)
    assert db.committed
    assert output['weight_g'] == 350
    assert output['net_proceeds_usd'] == 12
    assert output['review_status'] == 'unreviewed'
    saved = json.loads(db.row['source_snapshot_json'])
    for name in ['ai_original', 'source']:
        assert {a['id']: a['value_name'] for a in saved[name]['attributes']}['PACKAGE_LENGTH'] == '20 cm'
    assert Decimal(saved['marketplace_listings']['mercadolibre']['net_proceeds_usd']) == 12
    assert saved['marketplace_listings']['amazon']['review_status'] == 'unreviewed'


def test_mercado_editor_revalidates_the_saved_draft(monkeypatch):
    from erp import ai_original_platforms

    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    calls = []
    def validate(draft, platform):
        calls.append((draft.get('title_es'), draft.get('description_es'), platform))
        return {'errors': ['标题语言需修正'], 'warnings': []}

    monkeypatch.setattr(ai_original_platforms, 'validate_platform_listing', validate)
    snapshot = {'ai_original': {
        'title_es': 'Título anterior', 'title_pt': 'Título anterior',
        'description_es': 'Descripción', 'description_pt': 'Descrição',
        'main_image_url': '/product-ai-white.jpg',
    }}

    db = SnapshotDB(snapshot)
    store.update_ai_original_listing(
        1, {'title_es': 'English title entered manually'}, connection_factory=lambda: db,
    )
    saved = json.loads(db.row['source_snapshot_json'])

    assert db.committed
    assert calls == [('English title entered manually', 'Descripción', 'mercadolibre')]
    assert saved['ai_original']['validation'] == {
        'errors': ['标题语言需修正'], 'warnings': [],
    }
    assert saved['marketplace_listings']['mercadolibre']['validation'] == saved['ai_original']['validation']


def test_labels_preserve_platform_names_values_and_translate_unknown_fields():
    rows = [{'id': 'PACKAGE_LENGTH', 'name': 'Package length'},
            {'id': 'CUSTOM', 'name': 'Water resistance', 'values': [{'id': '1', 'name': 'Yes'}]}]
    localize_attribute_labels(rows, translator=lambda texts, *_: [{'Water resistance': '防水性', 'Yes': '是'}[s] for s in texts])
    assert rows[0]['name_zh'] == '包装长度'
    assert rows[1]['name_zh'] == '防水性'
    assert rows[1]['values'][0] == {'id': '1', 'name': 'Yes', 'name_zh': '是'}
    assert rows[1]['name'] == 'Water resistance'


def test_short_english_attribute_does_not_cancel_all_chinese_labels():
    calls = []
    translations = {'Water resistance': '防水性'}

    def translate(texts, source, target):
        calls.append((list(texts), source, target))
        return [translations[text] for text in texts]

    rows = [
        {'id': 'CHARACTER', 'name': 'Character',
         'values': [{'id': '3040149', 'name': 'Black panther'}]},
        {'id': 'GENDER', 'name': 'Gender',
         'values': [{'id': '339668', 'name': 'Girls'}]},
        {'id': 'CUSTOM', 'name': 'Water resistance'},
    ]

    localize_attribute_labels(rows, translator=translate)

    assert [row['name_zh'] for row in rows] == ['角色', '性别', '防水性']
    assert rows[0]['values'][0]['name_zh'] == '黑豹'
    assert rows[1]['values'][0]['name_zh'] == '女孩'
    assert all(source == 'en' and target == 'zh-CN' for _, source, target in calls)


def test_one_translation_failure_only_leaves_that_label_unavailable():
    def translate(texts, *_):
        if len(texts) > 1 or texts == ['Unsupported label']:
            raise RuntimeError('translation unavailable')
        return ['防水性']

    rows = [
        {'id': 'CUSTOM_A', 'name': 'Water resistance'},
        {'id': 'CUSTOM_B', 'name': 'Unsupported label'},
    ]

    localize_attribute_labels(rows, translator=translate)

    assert rows[0]['name_zh'] == '防水性'
    assert rows[1]['name_zh'] == ''
