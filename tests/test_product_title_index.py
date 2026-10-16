from unittest.mock import MagicMock

from erp import product_title_index as index


def test_online_product_migration_is_idempotent():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.side_effect = [
        [{'Field': name} for name in index.COLUMNS],
        [{'Field': name, 'Extra': 'VIRTUAL GENERATED INVISIBLE'} for name in index.COLUMNS],
        [{'Key_name': name} for name in index.INDEXES],
    ]
    index.install(connection)
    assert not any(call.args[0].startswith('ALTER') for call in cursor.execute.call_args_list)
    connection.commit.assert_called_once()


def test_product_migration_builds_all_translations_online():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.side_effect = [[], [], []]
    index.install(connection)
    alterations = [call.args[0] for call in cursor.execute.call_args_list if call.args[0].startswith('ALTER')]
    assert len(alterations) == 2
    assert all('ALGORITHM=INPLACE, LOCK=NONE' in sql for sql in alterations)
    assert 'JSON_VALID' in alterations[0]
    assert alterations[0].count('VIRTUAL INVISIBLE') == 3
    assert 'title_es' in alterations[0] and 'title_pt' in alterations[0]
    assert alterations[1].count('(`organization_key`,') == 3


def test_link_migration_reuses_existing_index():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{'Field': 'rights_title_hash', 'Extra': 'VIRTUAL GENERATED INVISIBLE'}]
    cursor.fetchone.return_value = {'n': 1}
    index.install_links(connection)
    assert not any(call.args[0].startswith('ALTER') for call in cursor.execute.call_args_list)
