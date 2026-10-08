"""Enterprise product sharing and private-product access through real queries."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from bit import product_access as access
from erp import mercadolibre_collection_store as store


def user(role='member', id=7, organization='acme'):
    return {'id': id, 'username': 'collector', 'display_name': '采集员',
            'role_key': role, 'access_version': 1, 'organization_key': organization}


class Connection:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.queries = []
        self.db.executescript('''
            CREATE TABLE erp_mercadolibre_products (
                id INTEGER PRIMARY KEY, source_item_id TEXT, source_type TEXT,
                collection_item_id INTEGER, management_category_id INTEGER,
                organization_key TEXT, owner_user_id INTEGER, owner_name TEXT, visibility TEXT,
                last_publish_token_id INTEGER);
            CREATE TABLE erp_mercadolibre_collection_items (
                id INTEGER PRIMARY KEY, source_item_id TEXT, task_id INTEGER,
                management_category_id INTEGER, added_to_products INTEGER,
                organization_key TEXT, owner_user_id INTEGER, owner_name TEXT, visibility TEXT);
            CREATE TABLE erp_mercadolibre_collection_tasks (id INTEGER, created_by TEXT);
            CREATE TABLE erp_mercadolibre_management_categories (id INTEGER, name TEXT, listing_ratio_percent INTEGER);
            INSERT INTO erp_mercadolibre_products VALUES
                (1,'own-private','zying',NULL,NULL,'acme',7,'采集员','private',NULL),
                (2,'other-private','zying',NULL,NULL,'acme',8,'同事','private',NULL),
                (3,'shared-unpublished','zying',NULL,NULL,'acme',8,'同事','enterprise',NULL),
                (4,'foreign','zying',NULL,NULL,'other',7,'采集员','enterprise',NULL),
                (5,'legacy-private','zying',NULL,NULL,'acme',NULL,'collector','private',NULL),
                (6,'same-name-private','zying',NULL,NULL,'acme',99,'采集员','private',NULL);
            INSERT INTO erp_mercadolibre_collection_items VALUES
                (11,'own-private',1,NULL,0,'acme',7,'采集员','private'),
                (12,'other-private',1,NULL,0,'acme',8,'同事','private'),
                (13,'shared-unpublished',1,NULL,0,'acme',8,'同事','enterprise'),
                (14,'foreign',1,NULL,0,'other',7,'采集员','enterprise');
        ''')

    def cursor(self):
        parent = self
        class Cursor:
            def __enter__(self):
                self.cursor = parent.db.cursor()
                return self
            def __exit__(self, *args): self.cursor.close()
            def execute(self, sql, params=()):
                parent.queries.append((sql, params))
                self.cursor.execute(sql.replace('%s', '?').replace(' FOR UPDATE', '').replace('<=>', 'IS'), params)
                self.rowcount = self.cursor.rowcount
            def fetchone(self):
                row = self.cursor.fetchone()
                return dict(row) if row else None
            def fetchall(self): return [dict(row) for row in self.cursor.fetchall()]
        return Cursor()
    def commit(self): self.db.commit()
    def rollback(self): self.db.rollback()
    def close(self): pass


@pytest.fixture
def database(monkeypatch):
    conn = Connection()
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    monkeypatch.setattr(store, '_connect_list', lambda: conn)
    monkeypatch.setattr(store, '_connect', lambda: conn)
    return conn


@pytest.mark.parametrize('role,expected', [
    ('member', [5,3,1]), ('operator', [5,3,1]), ('viewer', [5,3,1]),
    ('enterprise_admin', [6,5,3,2,1]), ('super_admin', [6,5,3,2,1]),
])
def test_shared_and_private_products_are_scoped_before_count_and_pagination(database, role, expected):
    with access.scope(user(role)):
        data = store.list_product_items(limit=2, offset=0, token_ids=[])
        assert data['total'] == len(expected)
        assert [row['id'] for row in data['rows']] == expected[:2]
        page = store.list_product_items(limit=2, offset=2, include_total=False)
        assert [row['id'] for row in page['rows']] == expected[2:4]
        assert page['has_more'] is (len(expected) > 4)
        assert data['rows'][0]['can_manage_visibility'] is True


@pytest.mark.parametrize('role,expected', [('member', [13,11]), ('enterprise_admin',[13,12,11])])
def test_collection_has_the_same_enterprise_visibility(database, role, expected):
    with access.scope(user(role)):
        data = store.list_collection_items()
        assert data['total'] == len(expected)
        assert [row['id'] for row in data['rows']] == expected


@pytest.mark.parametrize('role,id,allowed', [
    ('member',1,True), ('member',2,False), ('member',3,True), ('member',4,False),
    ('operator',2,False), ('enterprise_admin',2,True), ('super_admin',2,True),
    ('enterprise_admin',4,False), ('super_admin',4,False), ('member',6,False),
])
def test_direct_ids_obey_privacy_and_company_boundary(database, role, id, allowed):
    with access.scope(user(role)):
        if allowed: store.check_catalog_access('products', [id])
        else:
            with pytest.raises(PermissionError): store.check_catalog_access('products', [id])


def test_collector_can_restore_sharing_and_cannot_hide_coworkers_product(database):
    with access.scope(user()):
        assert store.set_catalog_visibility('products', [1], 'enterprise')['count'] == 1
        assert database.db.execute('SELECT visibility FROM erp_mercadolibre_collection_items WHERE id=11').fetchone()[0] == 'enterprise'
        store.set_catalog_visibility('products', [1], 'private')
        with pytest.raises(PermissionError): store.set_catalog_visibility('products', [3], 'private')
        with pytest.raises(PermissionError): store.set_catalog_visibility('products', [1,3], 'private')
    assert database.db.execute('SELECT visibility FROM erp_mercadolibre_products WHERE id=3').fetchone()[0] == 'enterprise'


def test_admin_can_manage_private_products_for_multiple_collectors(database):
    with access.scope(user('enterprise_admin')):
        store.set_catalog_visibility('products', [1,2,3], 'private')
    assert [row[0] for row in database.db.execute('SELECT visibility FROM erp_mercadolibre_collection_items WHERE id IN (11,12,13)')] == ['private']*3


def test_hidden_ids_cannot_be_read_or_deleted_through_store_functions(database):
    with access.scope(user()):
        with pytest.raises(PermissionError): store.get_product_items_by_ids([2])
        with pytest.raises(PermissionError): store.delete_product_items([1,2])
        with pytest.raises(PermissionError): store.get_collection_items_by_ids([12])
    assert database.db.execute('SELECT COUNT(*) FROM erp_mercadolibre_products').fetchone()[0] == 6


def test_ingestion_rejects_recollection_of_coworkers_private_product(database):
    with access.scope(user()), database.cursor() as cursor:
        with pytest.raises(PermissionError):
            store._execute_access_insert(cursor,
                'INSERT INTO `erp_mercadolibre_products` (`source_item_id`, `source_type`) VALUES (%s,%s)',
                ('other-private', 'zying'))
        store._execute_access_insert(cursor,
            'INSERT INTO `erp_mercadolibre_products` (`source_item_id`, `source_type`) VALUES (%s,%s)',
            ('new', 'zying'))
    row = dict(database.db.execute("SELECT * FROM erp_mercadolibre_products WHERE source_item_id='new'").fetchone())
    assert row['organization_key'] == 'acme'
    assert row['owner_user_id'] == 7
    assert row['visibility'] == 'enterprise'


def test_background_actor_survives_thread_boundary():
    with access.scope(user()):
        task = access.bind(lambda: access.actor())
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(task).result() == user()
    assert access.actor() is None
