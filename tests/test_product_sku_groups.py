import copy
import json
import sqlite3
import pytest
from erp import product_sku_groups as groups


def product(identifier=1, color='Red', size='XL', **fields):
    return {'id': identifier, 'source_type': 'pulled', 'organization_key': 'org', 'category_id': 'MLM1',
            'title': f'Long product title {color} {size}', 'management_category_id': None,
            'source_snapshot_json': {'source': {'attributes': [
                {'id': 'COLOR', 'value_name': color}, {'id': 'SIZE', 'value_name': size},
                {'id': 'SELLER_SKU', 'value_name': f'123456-{color}-{size}'}]},
                'plugin_snapshot': {'token_id': 7, 'site_id': 'MLM'}}, **fields}


def test_verified_variant_suffixes_group_but_other_products_do_not():
    first = groups.identity(product())
    assert first == (groups.identity(product(2, 'Blue', 'S'))[0], 'Long product title', 'Red / XL')
    for field, value in [('source_type', 'collected'), ('organization_key', 'other'), ('category_id', 'MLM2')]:
        other = groups.identity(product(**{field: value}))
        assert other is None or other[0] != first[0]
    for key, value in [('token_id', 8), ('site_id', 'MLB')]:
        row = product(); row['source_snapshot_json']['plugin_snapshot'][key] = value
        assert groups.identity(row)[0] != first[0]
    row = product(); row['source_snapshot_json']['source']['attributes'][-1]['value_name'] = 'different-Blue-S'
    assert groups.identity(row) is None
    assert groups.identity(product(title='Long product title')) is None
    assert groups.identity(product(source_snapshot_json='invalid')) is None


def test_explicit_family_can_join_different_titles_but_no_scope_does_not():
    rows = [product(), product(2, 'Blue', 'S', title='A different title')]
    for row in rows:
        row['source_snapshot_json']['source']['family_id'] = 'F123'
    assert groups.identity(rows[0])[0] == groups.identity(rows[1])[0]
    rows[1]['source_snapshot_json']['plugin_snapshot'] = {}
    assert groups.identity(rows[1]) is None


class Cursor:
    def __init__(self, db): self.db = db
    def execute(self, sql, params): self.cursor = self.db.execute(sql.replace('%s', '?'), params)
    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None
    def fetchall(self): return [dict(row) for row in self.cursor.fetchall()]


@pytest.mark.parametrize('include_total', [True, False])
def test_pagination_groups_before_limit_and_keeps_filtered_scope(include_total, monkeypatch):
    db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
    db.create_function('CONCAT', -1, lambda *values: ''.join(map(str, values)))
    db.executescript(f'''CREATE TABLE {groups.PRODUCTS} (id INTEGER PRIMARY KEY,source_type TEXT,
        organization_key TEXT, category_id TEXT, title TEXT,management_category_id INTEGER,source_snapshot_json TEXT);
        CREATE TABLE {groups.TABLE} (product_id INTEGER PRIMARY KEY,group_key TEXT,group_title TEXT,sku_label TEXT);
        CREATE TABLE erp_mercadolibre_management_categories (id INTEGER,name TEXT,listing_ratio_percent INTEGER);''')
    for row in [product(9), product(1, 'Blue', 'S'), product(8, organization_key='private'), product(5, source_type='collected')]:
        group = groups.identity(row)
        db.execute(f'INSERT INTO {groups.PRODUCTS} VALUES (?,?,?,?,?,?,?)', tuple(json.dumps(v) if isinstance(v, dict) else v for v in row.values()))
        if group: db.execute(f'INSERT INTO {groups.TABLE} VALUES (?,?,?,?)', (row['id'], *group))
    def page(offset, where="WHERE organization_key='org'"):
        return groups.grouped_page(Cursor(db), where, [], limit=1, offset=offset, include_total=include_total, decode=lambda row: row)
    first = page(0)
    assert [row['id'] for row in first['rows']] == [9, 1]
    assert first['group_count'] == 1 and first['has_more']
    assert first['total'] == (2 if include_total else None)
    assert [row['id'] for row in page(1)['rows']] == [5]
    assert page(2)['rows'] == []
    assert page(2)['total'] == (2 if include_total else None)
    assert [row['id'] for row in page(0, "WHERE organization_key='org' AND id > 1")['rows']] == [9]
    from erp import sku_matrix
    monkeypatch.setattr(sku_matrix, 'sku_count_sql', lambda *args: '1')
    data = groups.grouped_page(Cursor(db), "WHERE organization_key='org'", [], limit=1, offset=0,
                              include_total=include_total, decode=lambda row: row, sku_bounds=(2, 3))
    assert [r['id'] for r in data['rows']] == [9, 1]
    assert data['total'] == (1 if include_total else None)
    # A newer sibling outside the authorized scope cannot suppress the visible group.
    key = groups.identity(product())[0]
    db.execute(f'UPDATE {groups.TABLE} SET group_key=? WHERE product_id=8', (key,))
    data = page(0, "WHERE `erp_mercadolibre_products`.id IN (SELECT id FROM `erp_mercadolibre_products` WHERE organization_key='org')")
    assert [r['id'] for r in data['rows']] == [9, 1]
    data = page(0, "WHERE `erp_mercadolibre_products`.id IN (SELECT id FROM `erp_mercadolibre_products` WHERE id=1)")
    assert [r['id'] for r in data['rows']] == [1]
    db.close()


def test_risk_pagination_uses_same_groups():
    data = groups.paginate_rows([product(9), product(8, source_type='collected'), product(1, 'Blue', 'S')], limit=1, offset=0)
    assert data['total'] == 2
    assert [r['id'] for r in data['rows']] == [9, 1]


@pytest.mark.parametrize('title,attributes,sku,expected', [
    ('Long product title NO.99 Shidou Ryusei Xxl', {'COLOR':'NO.99 Shidou Ryusei','SIZE':'XXL'}, '502521331-NO.99SHIDOURYUSEI-XXL', 'Long product title'),
    ('Long product title 140 26275+', {'COLOR':'140码','SIZE':'26275款裙子+包包'}, '1043712449058-140码-26275款裙子+包包', 'Long product title'),
    ('Long product title 27t613', {'CAP_SIZE':'27T613'}, '765088540580-27T613', 'Long product title'),
    ('Long product title Preto Singlepackage', {'COLOR':'Preto'}, '939593333770-BLACK-SINGLEPACKAGE', 'Long product title'),
    ('Long product title 3.0 Cinza', {'COLOR':'Cinza'}, '337698615-3.0-GRAY', 'Long product title'),
    ('Long product title Five-piececareset', {'CAP_SIZE':'Five-piececareset'}, '919224084-FIVE-PIECECARESET', 'Long product title'),
])
def test_full_catalog_title_normalization(title, attributes, sku, expected):
    row = product(title=title)
    row['source_snapshot_json']['source']['attributes'] = [
        {'id':k,'value_name':v} for k,v in {**attributes, 'SELLER_SKU':sku}.items()]
    family = groups.identity(row)
    assert family and family[1] == expected
    public = groups.public_titles([dict(row, sku_group_key=family[0])])[0]
    assert public['display_title'] == expected
    assert public['title'] == title  # Remote titles are not edited by a display change.


def test_supplier_family_handles_translated_skus_and_shared_title():
    rows = [product(1,title='Long product title 2.5 Dourado'), product(2,title='Long product title 3.0 Cinza')]
    for row, color, sku in zip(rows, ['Dourado','Cinza'], ['337698615-2.5-GOLDEN','337698615-3.0-GRAY']):
        row['source_snapshot_json']['source']['attributes'] = [
            {'id':'COLOR','value_name':color}, {'id':'SELLER_SKU','value_name':sku}]
    assert groups.identity(rows[0])[0] == groups.identity(rows[1])[0]
    data = groups.paginate_rows(rows, limit=10, offset=0)
    assert data['total'] == 1
    assert {r['display_title'] for r in data['rows']} == {'Long product title'}
