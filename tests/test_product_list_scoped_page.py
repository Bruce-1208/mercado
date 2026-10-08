"""Execute scoped pagination SQL against overlapping ownership data."""
import sqlite3

import pytest

from erp import mercadolibre_collection_store as store


class Cursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.cursor = self.connection.db.cursor()
        return self

    def __exit__(self, *args):
        self.cursor.close()

    def execute(self, sql, params=()):
        self.connection.queries.append(sql)
        self.cursor.execute(sql.replace("%s", "?").replace("STRAIGHT_JOIN", "INNER JOIN"), params)

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]


class Connection:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.queries = []
        self.closed = False
        self.db.executescript("""
            CREATE TABLE erp_mercadolibre_products (
                id INTEGER PRIMARY KEY, source_item_id TEXT,
                last_publish_token_id INTEGER, source_type TEXT,
                collection_item_id INTEGER, management_category_id INTEGER);
            CREATE TABLE erp_mercadolibre_publish_records (product_item_id INTEGER, token_id INTEGER);
            CREATE TABLE erp_mercadolibre_store_links (item_id TEXT, token_id INTEGER);
            CREATE TABLE erp_mercadolibre_management_categories (id INTEGER, name TEXT, listing_ratio_percent INTEGER);
            CREATE TABLE erp_mercadolibre_collection_items (id INTEGER, task_id INTEGER);
            CREATE TABLE erp_mercadolibre_collection_tasks (id INTEGER, created_by TEXT);
            INSERT INTO erp_mercadolibre_products VALUES
                (1,'A',7,'zying',1,1),(2,'B',NULL,'zying',NULL,NULL),
                (3,'C',NULL,'pulled',NULL,NULL),(4,'D',8,'zying',NULL,NULL),
                (5,'E',NULL,'zying',NULL,NULL);
            INSERT INTO erp_mercadolibre_publish_records VALUES (1,7),(2,7),(2,7),(99,7);
            INSERT INTO erp_mercadolibre_store_links VALUES ('A',7),('C',7),('C',7),('D',8),('missing',7);
            INSERT INTO erp_mercadolibre_management_categories VALUES (1,'Accessories',120);
            INSERT INTO erp_mercadolibre_collection_items VALUES (1,1);
            INSERT INTO erp_mercadolibre_collection_tasks VALUES (1,'浏览器插件：开发员');
        """)

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.db.commit()

    def close(self):
        self.closed = True
        self.db.close()


@pytest.mark.parametrize("tokens,offset,expected_ids,expected_total", [
    ([7], 0, [3, 2], 3), ([7], 2, [1], 3), ([7], 3, [], 3),
    ([7, 8, 7], 1, [3, 2], 4), ([8], 0, [4], 1),
    ([999], 0, [], 0), ([], 0, [], 0),
])
def test_exact_total_and_page_preserve_scope_and_empty_offsets(monkeypatch, tokens, offset, expected_ids, expected_total):
    connection = Connection()
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    data = store.list_product_items(token_ids=tokens, limit=2, offset=offset,
                                    connection_factory=lambda: connection)
    assert data["total"] == expected_total
    assert [row["id"] for row in data["rows"]] == expected_ids
    assert all("__list_total" not in row for row in data["rows"])
    assert len(connection.queries) == (1 if tokens else 2)
    assert connection.closed
    if expected_ids == [1]:
        assert data["rows"][0]["management_category_name"] == "Accessories"
        assert data["rows"][0]["salesperson"] == "开发员"


def test_additional_filter_keeps_total_and_page_consistent(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    data = store.list_product_items(token_ids=[7], source_type="zying", limit=1, offset=1,
                                    connection_factory=lambda: connection)
    assert data["total"] == 2
    assert [row["id"] for row in data["rows"]] == [1]


@pytest.mark.parametrize("tokens,offset,expected_ids,has_more", [
    ([7], 0, [3, 2], True), ([7], 2, [1], False),
    ([7], 3, [], False), ([7, 8, 7], 1, [3, 2], True),
    ([8], 0, [4], False), ([999], 0, [], False),
    ([], 0, [], False), (None, 0, [5, 4], True),
])
def test_single_page_skips_count_and_preserves_permissions(monkeypatch, tokens, offset, expected_ids, has_more):
    connection = Connection()
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    data = store.list_product_items(token_ids=tokens, limit=2, offset=offset,
                                    include_total=False, connection_factory=lambda: connection)
    assert [row["id"] for row in data["rows"]] == expected_ids
    assert data["total"] is None
    assert data["total_exact"] is False
    assert data["has_more"] is has_more
    assert len(connection.queries) == 1
    assert "COUNT(" not in connection.queries[0]
    assert connection.closed


def test_single_page_with_filter_keeps_scope(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)
    data = store.list_product_items(token_ids=[7], source_type="zying", limit=1,
                                    include_total=False, connection_factory=lambda: connection)
    assert [row["id"] for row in data["rows"]] == [2]
    assert data["has_more"] is True
    assert len(connection.queries) == 1


def test_absent_keyword_returns_exact_empty_result_without_second_catalog_scan(monkeypatch):
    connection = Connection()
    connection.db.execute('ALTER TABLE erp_mercadolibre_products ADD COLUMN title TEXT')
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    data = store.list_product_items(token_ids=[7], search='FunctionalAuditNoMatch20261008',
                                    connection_factory=lambda: connection)
    assert data == {'total': 0, 'rows': []}
    assert len(connection.queries) == 1
    assert 'COUNT(*)' in connection.queries[0]
    assert connection.closed


def test_matching_keyword_still_loads_authorized_product_page(monkeypatch):
    connection = Connection()
    connection.db.execute('ALTER TABLE erp_mercadolibre_products ADD COLUMN title TEXT')
    connection.db.execute("UPDATE erp_mercadolibre_products SET title='Matching title' WHERE id IN (1,4)")
    monkeypatch.setattr(store, 'ensure_collection_tables', lambda cursor: None)
    data = store.list_product_items(token_ids=[7], search='Matching',
                                    connection_factory=lambda: connection)
    assert data['total'] == 1
    assert [row['id'] for row in data['rows']] == [1]
    assert len(connection.queries) == 2
    assert connection.closed


@pytest.mark.parametrize('include_total',[True,False])
def test_keyword_pagination_preserves_enterprise_and_private_visibility(monkeypatch,include_total):
    from bit import product_access
    connection=Connection()
    for column,definition in [('title',"TEXT DEFAULT 'needle'"),('organization_key',"TEXT DEFAULT 'wuhan-zeshun'"),
                              ('visibility',"TEXT DEFAULT 'private'"),('owner_user_id','INTEGER'),('owner_name','TEXT')]:
        connection.db.execute(f'ALTER TABLE erp_mercadolibre_products ADD COLUMN {column} {definition}')
    connection.db.execute("UPDATE erp_mercadolibre_products SET owner_user_id=5 WHERE id=1")
    connection.db.execute("UPDATE erp_mercadolibre_products SET visibility='enterprise' WHERE id=2")
    connection.db.execute("UPDATE erp_mercadolibre_products SET owner_user_id=9 WHERE id=3")
    connection.db.execute("UPDATE erp_mercadolibre_products SET organization_key='another-company',visibility='enterprise' WHERE id=4")
    connection.db.execute("UPDATE erp_mercadolibre_products SET owner_name='Tester' WHERE id=5")
    monkeypatch.setattr(store,'ensure_collection_tables',lambda cursor:None)
    with product_access.scope({'id':5,'role_key':'employee','organization_key':'wuhan-zeshun','username':'tester'}):
        result=store.list_product_items(search='needle',offset=1,limit=2,include_total=include_total,
                                       connection_factory=lambda:connection)
    assert [row['id'] for row in result['rows']]==[2,1]
    assert result['total']==(3 if include_total else None)
    if not include_total:assert result['has_more'] is False
