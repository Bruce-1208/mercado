"""Batch metadata writes use the real transaction and offline SQL adapter."""
import pytest
from bit import product_access
from erp import mercadolibre_collection_store as store
from test_purchase_net_proceeds_persistence import database, _row  # noqa: F401

ADMIN = {'id': 10, 'role_key': 'enterprise_admin', 'organization_key': 'test'}


def seed(database):
    for table in (store.PRODUCT_TABLE, store.COLLECTION_TABLE):
        row = _row(source_type='collected', purchased=False)
        row.update(organization_key='test', owner_user_id=10, owner_name='主管', visibility='enterprise')
        database.insert(table, **row)
    database.db.execute('CREATE TABLE workbench_users (id, display_name, username, organization_key, is_active)')
    database.db.execute("INSERT INTO workbench_users VALUES (20, '小李', 'li', 'test', 1), (30, '外部', 'other', 'other', 1)")
    database.db.execute(f'CREATE TABLE {store.MANAGEMENT_CATEGORY_TABLE} (id, name)')
    database.db.execute(f"INSERT INTO {store.MANAGEMENT_CATEGORY_TABLE} VALUES (5, '家居')")
    database.commit()


def test_batch_metadata_and_dimensions_persist_to_linked_copy(database):
    seed(database)
    with product_access.scope(ADMIN):
        result = store.update_product_items([1], {
            'owner_user_id': 20, 'visibility': 'private', 'management_category_id': 5,
            'review_status': 'risk', 'package_length_cm': 25,
        }, connection_factory=lambda: database)
    assert result['changed'] == 1
    for table in (store.PRODUCT_TABLE, store.COLLECTION_TABLE):
        saved = database.row(table)
        assert (saved['owner_user_id'], saved['owner_name'], saved['visibility'], saved['management_category_id']) == (20, '小李', 'private', 5)
        assert saved['package_length_cm'] == 25
    assert database.row(store.PRODUCT_TABLE)['review_status'] == 'risk'


@pytest.mark.parametrize('changes,error', [
    ({'owner_user_id': 30}, ValueError),
    ({'management_category_id': 99}, ValueError),
    ({'visibility': 'public'}, ValueError),
    ({'review_status': 'bogus'}, ValueError),
])
def test_invalid_batch_rolls_back_content(database, changes, error):
    seed(database)
    before = database.row(store.PRODUCT_TABLE)
    with product_access.scope(ADMIN), pytest.raises(error):
        store.update_product_items([1], {'title': 'Must not save', **changes}, connection_factory=lambda: database)
    assert database.row(store.PRODUCT_TABLE) == before


def test_member_cannot_reassign_ownership(database):
    seed(database)
    with product_access.scope({**ADMIN, 'role_key': 'member'}), pytest.raises(PermissionError):
        store.update_product_items([1], {'owner_user_id': 20}, connection_factory=lambda: database)
    assert database.row(store.PRODUCT_TABLE)['owner_user_id'] == 10


def test_ai_original_cannot_skip_review_in_batch(database):
    seed(database)
    database.db.execute(f"UPDATE {store.PRODUCT_TABLE} SET source_type = 'ai_original'")
    database.commit()
    with product_access.scope(ADMIN), pytest.raises(ValueError, match='逐件审核'):
        store.update_product_items([1], {'review_status': 'approved'}, connection_factory=lambda: database)
