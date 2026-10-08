"""Read-only benchmark of the corrected list query on the existing local DB.

Schema initialization is bypassed; the connection enforces READ ONLY and every
statement is checked before execution. No identity, session, or data is changed.
"""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from erp import mercadolibre_collection_store as store
from bit.db_pool import default_connection_config
import pymysql

counts = []

class ReadCursor:
    def __init__(self, cursor):
        self.cursor = cursor
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.cursor.close()
    def execute(self, sql, params=()):
        if sql.lstrip().split(None, 1)[0].upper() not in {"SELECT", "WITH", "SHOW"}:
            raise RuntimeError("Read-only benchmark rejected a mutating statement")
        counts.append(sql.lstrip().split(None, 1)[0])
        return self.cursor.execute(sql, params)
    def __getattr__(self, name):
        return getattr(self.cursor, name)

class ReadConnection:
    def __init__(self):
        self.connection = pymysql.connect(**default_connection_config())
        with self.connection.cursor() as cursor:
            cursor.execute('SET SESSION TRANSACTION READ ONLY')
            cursor.execute('SET SESSION max_execution_time=3000')
    def cursor(self):
        return ReadCursor(self.connection.cursor())
    def commit(self):
        self.connection.rollback()
    def close(self):
        self.connection.close()

def main():
    original = store.ensure_collection_tables
    store.ensure_collection_tables = lambda cursor: None
    samples = []
    try:
        for search in ('FunctionalAuditNoMatch20261008', '__audit_no_match_20261008__'):
            for visit in range(1):
                counts.clear()
                start = time.perf_counter()
                sample = {'search_kind': 'plain' if search[0] != '_' else 'underscores',
                          'visit': visit + 1}
                try:
                    result = store.list_product_items(search=search, limit=100,
                                                      connection_factory=ReadConnection)
                    sample.update(rows=len(result['rows']), total=result['total'])
                    assert result == {'total': 0, 'rows': []}
                except Exception as error:
                    sample.update(error_type=type(error).__name__,
                                  error_code=error.args[0] if error.args else None)
                sample.update(elapsed_ms=round((time.perf_counter()-start)*1000, 2),
                              statements=len(counts))
                samples.append(sample)
    finally:
        store.ensure_collection_tables = original
    path = Path(__file__).with_name('empty-search-benchmark.json')
    path.write_text(json.dumps(samples, indent=2))
    print(json.dumps(samples))

if __name__ == '__main__':
    main()
