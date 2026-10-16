"""Execute list/facet predicates on offline rows, including historical statuses."""
import re
import sqlite3

import pytest

from erp import mercadolibre_store_link_store as store


@pytest.fixture
def listings(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE erp_mercadolibre_store_links (
            id INTEGER, token_id INTEGER, site_id TEXT, status TEXT, is_current INTEGER,
            item_id TEXT, category_id TEXT, sold_quantity INTEGER DEFAULT 0, last_synced_at TEXT);
        INSERT INTO erp_mercadolibre_store_links VALUES
            (1,1,'MLM','active',1,'MLM1','C1',0,'2026-10-10'),
            (2,1,'MLM','paused',1,'MLM2','C1',0,'2026-10-11'),
            (3,1,'MLM','closed',0,'MLM3','C2',0,'2026-10-09'),
            (4,1,'MLB','active',1,'MLB4','C2',0,'2026-10-12'),
            (5,2,'MLM','active',1,'MLM5','C2',0,'2026-10-13'),
            (6,2,'MLM','closed',1,'MLM6','C1',0,'2026-10-14'),
            (7,3,'MLM','active',1,'MLM7','C1',0,'2026-10-15');
    ''')
    queries = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def execute(self, sql, params=()):
            queries.append((sql, params))
            if 'AS `table_exists`' in sql:
                self.rows = [{'table_exists': 0}]
                return
            if 'AS page_ids' in sql:
                # Execute the exact production page-id query; decoration joins
                # are unrelated to filter semantics and need no remote tables.
                sql = sql.split('INNER JOIN (', 1)[1].split(') AS page_ids', 1)[0]
                sql = re.sub(r'FORCE INDEX \(`[^`]+`\)', '', sql)
                ids = [row['id'] for row in db.execute(sql.replace('%s', '?'), params)]
                self.rows = [dict(db.execute('SELECT * FROM erp_mercadolibre_store_links WHERE id=?', (i,)).fetchone()) for i in ids]
            else:
                self.rows = [dict(row) for row in db.execute(sql.replace('%s', '?'), params)]
        def fetchone(self): return self.rows[0] if self.rows else None
        def fetchall(self): return self.rows

    class Connection:
        def cursor(self): return Cursor()
        def close(self): pass

    def metadata(*args, token_ids=None, **kwargs):
        permitted = [dict(r) for r in db.execute('SELECT * FROM erp_mercadolibre_store_links')
                     if token_ids is None or r['token_id'] in token_ids]
        current = [r for r in permitted if r['is_current']]
        return {
            'group_map': {(1, 'MLM'): 'A', (1, 'MLB'): 'B', (2, 'MLM'): 'A'},
            'salesperson_map': {(1, 'MLM'): '张三', (1, 'MLB'): '李四', (2, 'MLM'): '张三'},
            'salespeople': ['张三', '李四'], 'stores': [], 'groups': [], 'store_groups': {},
            'sites': [{'site_id': site, 'link_count': sum(r['site_id'] == site for r in current)} for site in ['MLM', 'MLB']],
            'mercado_categories': [],
            'summary': {'all_count': len(permitted), 'current_count': len(current)},
        }

    from erp import mercadolibre_collection_store
    monkeypatch.setattr(mercadolibre_collection_store, 'ensure_collection_tables', lambda _: None)
    monkeypatch.setattr(store, 'ensure_store_link_table', lambda _: None)
    monkeypatch.setattr(store, '_store_link_metadata', metadata)
    monkeypatch.setattr(store, '_decorate_store_link_markers', lambda _: None)
    monkeypatch.setattr(store, '_marker_filter_keys', lambda *args, **kwargs: {(1, 'MLM', 'MLM1'), (1, 'MLB', 'MLB4')})

    def load(**kwargs):
        return store.list_store_links(connection_factory=Connection, **kwargs)
    yield load, queries
    db.close()


def test_store_site_status_totals_share_scope(listings):
    load, _ = listings
    all_states = load(token_id=1, site_id='MLM')
    active = load(token_id=1, site_id='MLM', status='active')
    paused = load(token_id=1, site_id='MLM', status='paused')
    assert (all_states['total'], active['total'], paused['total']) == (2, 1, 1)
    assert all_states['summary']['all_count'] == 3
    assert all_states['summary']['current_count'] == 2
    assert all_states['summary']['store_count'] == 1
    assert all_states['summary']['last_synced_at'] == '2026-10-11'
    assert all_states['status_counts'] == {'': 2, 'active': 1, 'paused': 1}
    assert {s['site_id']: s['link_count'] for s in active['sites']} == {'MLM': 1, 'MLB': 1}
    assert {s['site_id']: s['link_count'] for s in paused['sites']} == {'MLM': 1, 'MLB': 0}
    for result in (all_states, active, paused):
        assert result['total'] == len(result['rows']) == result['summary']['current_count']


@pytest.mark.parametrize('filters,ids', [
    ({'filter_token_ids': [1, 2], 'token_ids': [1, 2], 'site_id': 'MLM'}, {1, 2, 5, 6}),
    ({'filter_token_ids': [1, 3], 'token_ids': [1, 2], 'site_id': 'MLM'}, {1, 2}),
    ({'token_id': 3, 'token_ids': [1, 2]}, set()),
    ({'token_id': 1, 'token_ids': []}, set()),
    ({'token_id': 1, 'group_name': 'A'}, {1, 2}),
    ({'token_id': 1, 'salesperson': '李四'}, {4}),
    ({'token_id': 1, 'mercado_category': 'C1', 'status': 'paused'}, {2}),
    ({'token_id': 1, 'site_id': 'MLM', 'promotion_applied': True}, {1}),
    ({'token_id': 1, 'site_id': 'MLM', 'promotion_applied': False}, {2}),
    ({'token_id': 1, 'site_id': 'MLM', 'status': 'closed'}, set()),
    ({'token_id': 1, 'site_id': 'MLM', 'status': 'closed', 'current_only': False}, {3}),
])
def test_facets_keep_all_filters_and_permissions(listings, filters, ids):
    load, _ = listings
    result = load(**filters)
    assert {row['id'] for row in result['rows']} == ids
    assert result['total'] == len(ids)
    if filters.get('current_only', True):
        assert result['summary']['current_count'] == len(ids)
    if filters.get('site_id'):
        assert next(s['link_count'] for s in result['sites'] if s['site_id'] == filters['site_id']) == len(ids)
    if filters.get('promotion_applied'):
        assert next(s['link_count'] for s in result['sites'] if s['site_id'] == 'MLB') == 1


def test_pagination_does_not_change_counts_and_zero_site_is_kept(listings):
    load, _ = listings
    first = load(token_id=1, site_id='MLM', page_size=1)
    second = load(token_id=1, site_id='MLM', page_size=1, page=2)
    assert first['total'] == second['total'] == 2
    assert first['summary'] == second['summary']
    assert first['sites'] == second['sites']
    assert first['rows'][0]['id'] != second['rows'][0]['id']
    empty = load(token_id=1, site_id='MLC')
    assert empty['total'] == 0
    assert any(site['site_id'] == 'MLC' and site['link_count'] == 0 for site in empty['sites'])


def test_statistics_cache_separates_permission_scopes_and_invalidates(monkeypatch):
    from erp.query_cache import QueryCache
    monkeypatch.setattr(store, '_filtered_count_cache', QueryCache())
    calls = []

    class Cursor:
        def execute(self, sql, values):
            calls.append((sql, values))
            self.token_id = values[0]
        def fetchall(self):
            return [dict(token_id=self.token_id, site_id='MLM', status='active', is_current=1,
                         total=self.token_id, last_synced_at=None)]

    def load(token, status=''):
        return store._filtered_store_link_statistics(
            Cursor(), ['links.`token_id` IN (%s)'], [token],
            site_id='MLM', status=status, current_only=True, known_sites=[], use_cache=True)

    assert load(1)['total'] == 1
    assert load(1, 'paused')['total'] == 0
    assert len(calls) == 1
    assert load(2)['total'] == 2
    assert len(calls) == 2
    store.invalidate_store_link_metadata_cache()
    assert load(1)['total'] == 1
    assert len(calls) == 3
