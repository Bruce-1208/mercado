"""Read-only, bounded SQL diagnostics; omit application data and credentials."""
import json
import re
import time
from pathlib import Path
import pymysql
from bit.db_pool import default_connection_config
from erp import mercadolibre_store_link_store as store

OUTPUT = Path('artifacts/store-link-diagnosis-20261006')
report = {'note': 'SELECT-only database diagnostics, no schema changes; workflow marker decoration omitted from listing timings.', 'snapshot': {}, 'cases': []}
config = default_connection_config()
config.update(host='127.0.0.1', port=3306, read_timeout=20, write_timeout=5)

def connect():
    connection = pymysql.connect(**config)
    with connection.cursor() as cursor:
        cursor.execute('SET SESSION MAX_EXECUTION_TIME=15000')
        cursor.execute('SET SESSION TRANSACTION READ ONLY')
    return connection

connection = connect()
try:
    with connection.cursor() as cursor:
        cursor.execute("SELECT TABLE_NAME, TABLE_ROWS, ROUND((DATA_LENGTH+INDEX_LENGTH)/1024/1024) AS size_mb FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ('erp_mercadolibre_store_links','mercado_synced_orders','erp_mercadolibre_products')")
        report['snapshot']['tables'] = cursor.fetchall()
        for table in ('erp_mercadolibre_store_links','mercado_synced_orders','erp_mercadolibre_products'):
            cursor.execute(f'SHOW INDEX FROM `{table}`')
            report['snapshot'][table + '_indexes'] = [{k: row.get(k) for k in ('Key_name','Seq_in_index','Column_name','Cardinality','Collation')} for row in cursor.fetchall()]
        cursor.execute("SELECT ID, TIME, STATE, INFO FROM information_schema.PROCESSLIST WHERE COMMAND='Query' AND ID<>CONNECTION_ID() ORDER BY TIME DESC")
        report['snapshot']['active_queries'] = []
        for row in cursor.fetchall():
            sql = str(row.get('INFO') or '')
            report['snapshot']['active_queries'].append({'seconds': row['TIME'], 'state': row['STATE'], 'tables': sorted(set(re.findall(r'(?:FROM|JOIN|UPDATE|INTO)\s+`?([a-zA-Z_][a-zA-Z0-9_]*)', sql, re.I))), 'operation': sql.lstrip().split(None,1)[0] if sql.strip() else ''})
        try:
            cursor.execute("SELECT LEFT(DIGEST_TEXT,1200) AS digest, COUNT_STAR AS calls, ROUND(SUM_TIMER_WAIT/1000000000000,3) AS total_seconds, ROUND(AVG_TIMER_WAIT/1000000000000,3) AS avg_seconds, ROUND(MAX_TIMER_WAIT/1000000000000,3) AS max_seconds, SUM_ROWS_EXAMINED AS rows_examined, SUM_SORT_ROWS AS sort_rows FROM performance_schema.events_statements_summary_by_digest WHERE SCHEMA_NAME=DATABASE() AND DIGEST_TEXT LIKE '%erp_mercadolibre_store_links%' ORDER BY SUM_TIMER_WAIT DESC LIMIT 12")
            report['snapshot']['query_digests'] = cursor.fetchall()
        except Exception as exc:
            report['snapshot']['digest_error'] = type(exc).__name__
        cursor.execute('SELECT id FROM mercado_store_tokens ORDER BY id LIMIT 1')
        token_id = (cursor.fetchone() or {}).get('id')
finally:
    connection.close()

class TracedCursor:
    def __init__(self, inner, trace): self.inner, self.trace = inner, trace
    def __enter__(self): self.inner.__enter__(); return self
    def __exit__(self, *args): return self.inner.__exit__(*args)
    def __getattr__(self, name): return getattr(self.inner, name)
    def execute(self, sql, args=None):
        if not re.match(r'^\s*(SELECT|SHOW|EXPLAIN)\b', sql, re.I):
            raise RuntimeError('Non-read-only SQL blocked')
        normalized = re.sub(r'\s+', ' ', sql).strip()
        label = ('recent_sales' if 'JSON_TABLE' in sql else 'page' if 'AS page_ids' in sql else 'metadata' if 'site_settings' in sql else 'count' if 'COUNT(' in sql else 'other')
        entry = {'kind':label, 'sql': normalized, 'args_count':len(args or ())}
        self.trace.append(entry)
        started = time.monotonic()
        try: return self.inner.execute(sql, args)
        finally: entry['seconds'] = round(time.monotonic()-started, 3)

class TracedConnection:
    def __init__(self, inner, trace): self.inner, self.trace = inner, trace
    def cursor(self): return TracedCursor(self.inner.cursor(), self.trace)
    def close(self): return self.inner.close()

store._store_link_schema_ready = True
store._decorate_store_link_markers = lambda rows: None
cases = [
    ('default_cold_50', dict(page_size=50)),
    ('default_warm_50', dict(page_size=50)),
    ('default_warm_200', dict(page_size=200)),
    ('status_active', dict(page_size=50, status='active')),
    ('site_MLM', dict(page_size=50, site_id='MLM')),
    ('price_unfiltered', dict(page_size=50, sort_by='price')),
]
if token_id:
    cases.extend([
        ('one_store', dict(page_size=50, token_id=token_id)),
        ('one_store_scoped', dict(page_size=50, token_ids=[token_id])),
    ])
for name, kwargs in cases:
    trace = []
    store._connect = lambda: TracedConnection(connect(), trace)
    started = time.monotonic()
    case = {'name':name, 'query_trace':trace}
    try:
        data = store.list_store_links(include_categories=False, **kwargs)
        case.update(rows=len(data['rows']), total=data['total'])
    except Exception as exc:
        case.update(error=type(exc).__name__, error_code=exc.args[0] if exc.args and isinstance(exc.args[0],int) else None)
    case['seconds'] = round(time.monotonic()-started,3)
    report['cases'].append(case)
    print(json.dumps({k:v for k,v in case.items() if k!='query_trace'}), flush=True)
    (OUTPUT/'measurements.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n')
print(json.dumps(report['snapshot'].get('active_queries')),flush=True)
