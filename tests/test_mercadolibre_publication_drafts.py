import sqlite3

import pytest

from erp.mercadolibre_publication_drafts import draft_storage


class Connection:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, args=()):
        if sql.lstrip().startswith('CREATE TABLE'):
            sql = '''CREATE TABLE IF NOT EXISTS erp_meli_attribute_drafts
                (revision TEXT PRIMARY KEY, product_id INTEGER, token_id INTEGER,
                 site_id TEXT, owner_id INTEGER, draft_json TEXT)'''
        self.result = self.db.execute(sql.replace('%s', '?'), args)

    def fetchone(self):
        return self.result.fetchone()

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        self.db.close()


@pytest.fixture
def storage(tmp_path):
    path = tmp_path / 'drafts.sqlite3'
    def call(action, **kwargs):
        return draft_storage(action, connection_factory=lambda: Connection(path), **kwargs)
    return call


def test_drafts_survive_connections_and_cannot_be_read_by_other_targets(storage):
    key = dict(product_id=1, token_id=7, site_id='MLM', owner_id=9)
    draft = {'revision': 'revision-1', 'source': {'attributes': [{'id':'BRAND','value_name':'真实品牌'}]}}
    storage('save', draft=draft, **key)
    assert storage('get', revision='revision-1', **key) == draft
    for field, value in [('product_id', 2), ('token_id', 8), ('site_id', 'MLB'), ('owner_id', 10)]:
        with pytest.raises(ValueError, match='不属于当前用户'):
            storage('get', revision='revision-1', **{**key, field:value})


def test_saved_revision_is_append_only_and_failed_replace_keeps_original(storage):
    key = dict(product_id=1, token_id=7, site_id='MLM', owner_id=9)
    storage('save', draft={'revision':'original','payload':{'title':'Original'}}, **key)
    with pytest.raises(sqlite3.IntegrityError):
        storage('save', draft={'revision':'original','payload':{'title':'Modified'}}, **key)
    assert storage('get', revision='original', **key)['payload']['title'] == 'Original'
