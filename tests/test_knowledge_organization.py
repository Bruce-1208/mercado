"""Execute tenant boundaries against a relational store, including ID guessing."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from bit import bit_mysql as db
from bit.knowledge_organization import bind, organization, scope


class Cursor:
    def __init__(self, connection):
        self.raw = connection.raw.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.raw.close()

    def execute(self, sql, params=()):
        assert sql.count('%s') == len(params)
        sql = sql.replace('%s', '?').replace(' FOR UPDATE', '').replace('INSERT IGNORE', 'INSERT OR IGNORE')
        self.raw.execute(sql, params)

    @property
    def lastrowid(self):
        return self.raw.lastrowid

    @property
    def rowcount(self):
        return self.raw.rowcount

    def fetchone(self):
        row = self.raw.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(row) for row in self.raw.fetchall()]


class Connection:
    def __init__(self, path):
        self.raw = sqlite3.connect(path)
        self.raw.row_factory = sqlite3.Row

    def cursor(self):
        return Cursor(self)

    def begin(self):
        self.raw.execute('BEGIN')

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / 'knowledge.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE infringement_knowledge (
              id INTEGER PRIMARY KEY AUTOINCREMENT, organization_key TEXT NOT NULL,
              brand_name TEXT NOT NULL, list_type TEXT, notes TEXT, source_type TEXT DEFAULT 'manual',
              evidence_count INTEGER DEFAULT 0, source_detail TEXT, created_at TEXT, updated_at TEXT,
              deleted_at TEXT, aliases_json TEXT, scope_json TEXT, valid_until TEXT,
              rule_version INTEGER DEFAULT 1, original_source_type TEXT, reviewed_by TEXT, reviewed_at TEXT,
              UNIQUE(organization_key, brand_name));
            CREATE TABLE infringement_knowledge_history (
              id INTEGER PRIMARY KEY, rule_id INTEGER, rule_version INTEGER, action TEXT,
              actor TEXT, before_json TEXT, after_json TEXT, created_at TEXT);
            CREATE TABLE infringement_knowledge_evidence (
              id INTEGER PRIMARY KEY, rule_id INTEGER, source_type TEXT, source_detail TEXT,
              payload_json TEXT, fingerprint TEXT, created_at TEXT, UNIQUE(rule_id, fingerprint));
        ''')
    monkeypatch.setattr(db.pymysql, 'connect', lambda **kwargs: Connection(path))
    monkeypatch.setattr(db, '_ensure_infringement_knowledge_table', lambda cursor: None)
    return path


def record(name='Same Brand', **kwargs):
    return {'brand_name': name, 'list_type': 'pending', 'notes': 'evidence', **kwargs}


def test_same_brand_shared_within_enterprise_but_independent_between_enterprises(database):
    with scope('enterprise-a'):
        a = db.create_infringement_knowledge(record())['id']
        with pytest.raises(ValueError, match='已存在'):
            db.create_infringement_knowledge(record())
    with scope('enterprise-b'):
        b = db.create_infringement_knowledge(record())['id']
        assert a != b
        listing = db.list_infringement_knowledge(page=1, page_size=1)
        assert listing['summary']['total'] == listing['filtered_total'] == 1
        assert [row['id'] for row in listing['rows']] == [b]
        assert [row['id'] for row in db.list_infringement_knowledge(after_id=0)['rows']] == [b]
        for operation in (
            lambda: db.get_infringement_knowledge_detail(a),
            lambda: db.update_infringement_knowledge(a, record('Hijacked')),
            lambda: db.delete_infringement_knowledge(a),
        ):
            with pytest.raises(ValueError, match='不存在'):
                operation()
        db.update_infringement_knowledge(b, record('B reviewed', list_type='blacklist', scope={'mode': 'global'}))
        assert db.get_infringement_knowledge_detail(b)['record']['list_type'] == 'blacklist'
        db.delete_infringement_knowledge(b)
        assert db.list_infringement_knowledge()['summary']['total'] == 0
    with scope('enterprise-a'):
        detail = db.get_infringement_knowledge_detail(a)
        assert detail['record']['brand_name'] == 'Same Brand'
        assert len(detail['history']) == 1


def test_bulk_candidates_and_evidence_do_not_reuse_other_enterprise_rule(database):
    with scope('enterprise-a'):
        db.bulk_create_infringement_knowledge([record(), record('Second')])
        a = db.list_infringement_knowledge()['rows'][0]['id']
    with scope('enterprise-b'):
        result = db.bulk_create_infringement_knowledge([record(), record('Second')])
        assert result['inserted'] == 2 and result['skipped'] == 0
        b = db.submit_infringement_knowledge_candidate(record(source_detail='B private evidence'))['id']
        detail = db.get_infringement_knowledge_detail(b)
        assert detail['evidence'][0]['source_detail'] == 'B private evidence'
    with scope('enterprise-a'):
        assert db.get_infringement_knowledge_detail(a)['evidence'] == []


def test_background_capture_preserves_company_and_restores_thread_context():
    with scope('enterprise-a'):
        callback = bind(organization)
    with scope('enterprise-b'):
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(callback).result() == 'enterprise-a'
        assert organization() == 'enterprise-b'


def test_public_identity_ignores_spoofed_company_and_shares_between_users(monkeypatch):
    from bit import bit_interface as app
    user = {'id': 1, 'organization_key': 'enterprise-a', 'role_key': 'employee'}
    monkeypatch.setattr(app, 'get_current_workbench_user', lambda: user)
    with app.app.test_request_context('/api/infringement-knowledge?organization_key=enterprise-b',
                                       headers={'X-Knowledge-Organization': 'enterprise-b'}):
        assert organization() == 'enterprise-a'
        user['id'] = 2
        assert organization() == 'enterprise-a'
        user.pop('organization_key')
        with pytest.raises(ValueError, match='企业标识'):
            organization()


def test_internal_agent_scope_wins_over_forwarded_header(monkeypatch):
    from flask import g
    from bit import bit_interface as app
    with app.app.test_request_context('/api/db/infringement-knowledge',
                                       headers={'X-Knowledge-Organization': 'enterprise-b'}):
        assert organization() == 'enterprise-b'
        g.internal_agent_user = {'organization_key': 'enterprise-a'}
        assert organization() == 'enterprise-a'


def test_analysis_state_is_shared_only_inside_company():
    from bit import bit_interface as app
    with scope('enterprise-a'):
        bucket = app._knowledge_analysis_bucket()
        bucket['logs'].clear()
        app._append_infringement_knowledge_analysis_log('private A')
    with scope('enterprise-b'):
        assert app._knowledge_analysis_bucket() is not bucket
        assert not any('private A' in line for line in app._knowledge_analysis_bucket()['logs'])
    with scope('enterprise-a'):
        assert app._knowledge_analysis_bucket() is bucket


def test_wipo_settings_and_writer_are_bound_to_company(tmp_path, monkeypatch):
    from bit.wipo_sync import WipoSyncService
    monkeypatch.setenv('BIT_WIPO_SYNC_PATH', str(tmp_path / 'wipo.sqlite3'))
    with scope('enterprise-a'):
        a = WipoSyncService('test-secret')
    with scope('enterprise-b'):
        b = WipoSyncService('test-secret')
    assert a.path != b.path and a.lock_key != b.lock_key
    with a.database() as conn:
        a._write(conn, 'settings', {'feed_url': 'https://example.com/a'})
    assert b.settings()['feed_url'] != 'https://example.com/a'


def test_schema_migrates_global_unique_constraint_atomically(monkeypatch):
    cursor = MagicMock()
    cursor.fetchall.return_value = [{'Key_name': 'uniq_infringement_knowledge_brand'}]
    monkeypatch.setattr(db, '_ensure_column', lambda *args: None)
    db._ensure_infringement_knowledge_table(cursor)
    alter = [call.args[0] for call in cursor.execute.call_args_list if call.args[0].startswith('ALTER TABLE')]
    assert len(alter) == 1
    assert 'DROP INDEX `uniq_infringement_knowledge_brand`' in alter[0]
    assert '(`organization_key`, `brand_name`)' in alter[0]


def test_analysis_queries_and_totals_filter_company_before_limits(monkeypatch):
    from erp import mercadolibre_store_link_store
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = {"total": 0}
    monkeypatch.setattr(db.pymysql, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(mercadolibre_store_link_store, "ensure_store_link_table", lambda cursor: None)
    with scope("enterprise-b"):
        db.get_infringement_knowledge_analysis_sources(10, 20)
    queries = [call.args for call in cursor.execute.call_args_list]
    assert len(queries) == 4
    for sql, params in queries:
        assert "organization_key" in sql
        assert sql.count("%s") == len(params)
        assert params[0] == "enterprise-b"
    assert "NOT EXISTS" in queries[0][0]  # Ambiguous legacy store names cannot cross companies.
    assert queries[0][1] == ("enterprise-b", "enterprise-b", 10)
    assert queries[1][1] == ("enterprise-b", 20)


def test_http_database_forwarding_carries_bound_company(monkeypatch):
    from bit import bit_db_api
    response = MagicMock(status_code=200)
    response.json.return_value = {"status": "success", "data": {"rows": []}}
    request = MagicMock(return_value=response)
    monkeypatch.setattr(bit_db_api.DB_API_SESSION, "request", request)
    with scope("enterprise-b"):
        bit_db_api._request("GET", "/api/db/infringement-knowledge")
    assert request.call_args.kwargs["headers"]["X-Knowledge-Organization"] == "enterprise-b"


def test_wipo_worker_uses_saved_enterprise_even_when_called_from_another(tmp_path, monkeypatch):
    from bit.wipo_sync import WipoSyncService, DEFAULT_SETTINGS
    seen = []
    migrated = []
    monkeypatch.setattr(db, "migrate_wipo_blacklist", lambda: migrated.append(organization()) or 0)
    monkeypatch.setattr(db, "upsert_wipo_infringement_knowledge",
                        lambda records: seen.append(organization()) or {"inserted": 1})
    with scope("enterprise-a"):
        service = WipoSyncService("test-secret", path=tmp_path / "wipo.sqlite3",
            fetcher=lambda *args: ([record()], {"fetched": 1, "unique": 1}))
    with scope("enterprise-b"):
        service._run({**DEFAULT_SETTINGS, "source_mode": "feed"}, MagicMock())
        assert organization() == "enterprise-b"
    assert seen == ["enterprise-a"]
    assert migrated == ["enterprise-a"]
    assert service.status()["status"] == "success"
