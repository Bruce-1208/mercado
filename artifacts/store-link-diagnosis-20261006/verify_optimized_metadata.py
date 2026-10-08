"""SELECT-only verification of the changed metadata code, before index creation."""
import json
import re
import time
from pathlib import Path
import pymysql
from bit.db_pool import default_connection_config
from erp import mercadolibre_store_link_store as store

config = default_connection_config()
config.update(host='127.0.0.1', port=3306, read_timeout=20)
trace = []
class Cursor:
    def __init__(self, cursor): self.inner = cursor
    def __getattr__(self, name): return getattr(self.inner, name)
    def __enter__(self): self.inner.__enter__(); return self
    def __exit__(self, *args): return self.inner.__exit__(*args)
    def execute(self, sql, args=None):
        if not re.match(r'^\s*(SELECT|SHOW|EXPLAIN)\b', sql, re.I):
            raise AssertionError('Read-only verification rejected non-SELECT SQL')
        entry = {'kind':'sites' if 'GROUP BY links.`site_id`' in sql else 'summary' if 'AS `all_count`' in sql else 'last_sync' if 'ORDER BY `last_synced_at`' in sql else 'other'}
        trace.append(entry)
        started=time.monotonic()
        try: return self.inner.execute(sql,args)
        finally: entry['seconds']=round(time.monotonic()-started,3)
class Connection:
    def __init__(self, inner): self.inner=inner
    def cursor(self): return Cursor(self.inner.cursor())
    def close(self): self.inner.close()
def connect():
    connection=pymysql.connect(**config)
    with connection.cursor() as cursor:
        cursor.execute('SET SESSION MAX_EXECUTION_TIME=15000')
        cursor.execute('SET SESSION TRANSACTION READ ONLY')
    return Connection(connection)
store._connect=connect
store._store_link_schema_ready=True
store.invalidate_store_link_metadata_cache()
results=[]
for temperature in ('cold','warm'):
    trace.clear()
    started=time.monotonic()
    result={'case':temperature, 'new_index_created':False}
    try:
        data=store._store_link_metadata(None,use_cache=True,token_ids=[119,116,64],include_categories=False)
        result.update(current_count=data['summary']['current_count'], sites=len(data['sites']))
    except Exception as exc:
        result['error_code']=exc.args[0] if exc.args and isinstance(exc.args[0],int) else type(exc).__name__
    result.update(seconds=round(time.monotonic()-started,3), query_trace=list(trace))
    results.append(result)
    print(json.dumps(result),flush=True)
Path('artifacts/store-link-diagnosis-20261006/optimized-metadata.json').write_text(json.dumps(results,indent=2)+'\n')
