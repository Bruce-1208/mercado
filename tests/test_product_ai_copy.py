import json
from unittest.mock import MagicMock

import pytest
from erp import mercadolibre_collection_store as db


@pytest.mark.parametrize('changed', [False, True])
def test_product_copy_preserves_variants_and_rejects_concurrent_edits(monkeypatch, changed):
    snapshot = {'source': {'title': 'Old', 'variations': [{'id': 1, 'price': 7}]},
                'description': {'plain_text': 'Old description'}, 'plugin_snapshot': {'id': 9},
                'marketplace_listings': {'amazon': {'review_status': 'approved'},
                                         'yandex': {'review_status': 'approved'}}}
    row = {'id': 12, 'title': 'Old', 'description_text': 'Old description',
           'source_type': 'ai_original', 'review_status': 'approved',
           'source_snapshot_json': json.dumps(snapshot)}
    expected = db.product_ai_copy_row(row)
    current = dict(row)
    if changed:
        current['source_snapshot_json'] = json.dumps({**snapshot, 'source': {'title': 'Manual edit'}})
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = current
    monkeypatch.setattr(db, 'ensure_collection_tables', lambda _: None)
    generated = {'title_es': 'a' * 55, 'title_pt': 'b' * 55,
                 'description_es': 'New ES', 'description_pt': 'New PT'}
    result = db.save_collection_item_ai_copy(12, expected, generated, connection_factory=lambda: connection)
    updates = [call.args for call in cursor.execute.call_args_list if call.args[0].startswith('UPDATE')]
    if changed:
        assert result['status'] == 'conflict'
        assert not updates
        assert connection.rollback.called
    else:
        assert result['saved']
        assert len(updates) == 1
        assert db.PRODUCT_TABLE in updates[0][0]
        saved = json.loads(updates[0][1][2])
        assert saved['source']['variations'] == snapshot['source']['variations']
        assert saved['plugin_snapshot'] == snapshot['plugin_snapshot']
        assert saved['source']['title'] == generated['title_es']
        assert saved['description']['plain_text'] == 'New ES'
        assert saved['ai_copy']['title_pt'] == generated['title_pt']
        assert saved['marketplace_listings']['amazon']['review_status'] == 'unreviewed'
        assert saved['marketplace_listings']['yandex']['review_status'] == 'unreviewed'
        assert "`review_status`='unreviewed'" in updates[0][0]
