"""Bounded read-only EXPLAIN and execution of authorization-scope metadata."""
import json
import time
from pathlib import Path
import pymysql
from bit.db_pool import default_connection_config

output = Path('artifacts/store-link-diagnosis-20261006/scoped.json')
config=default_connection_config()
config.update(host='127.0.0.1',port=3306,read_timeout=20)
connection=pymysql.connect(**config)
report={}
try:
 with connection.cursor() as cursor:
  cursor.execute('SET SESSION MAX_EXECUTION_TIME=15000')
  cursor.execute('SET SESSION TRANSACTION READ ONLY')
  cursor.execute('SELECT token_id, COUNT(*) AS n FROM erp_mercadolibre_store_links FORCE INDEX (idx_erp_meli_store_link_store) WHERE is_current=1 GROUP BY token_id ORDER BY n DESC')
  distribution=cursor.fetchall()
  report['store_counts']=distribution
  report['plans']=[]
  scope_sets={'all_stores':[r['token_id'] for r in distribution], 'largest_3':[r['token_id'] for r in distribution[:3]]}
  for name,scope in scope_sets.items():
   placeholders=','.join(['%s']*len(scope))
   for kind,sql in [
    ('sites',f'SELECT links.site_id, COUNT(*) AS link_count FROM erp_mercadolibre_store_links AS links WHERE links.is_current=1 AND links.site_id IS NOT NULL AND links.site_id<>\'\' AND links.token_id IN ({placeholders}) GROUP BY links.site_id ORDER BY links.site_id'),
    ('current_count',f'SELECT COUNT(*) AS current_count FROM erp_mercadolibre_store_links WHERE is_current=1 AND token_id IN ({placeholders})'),
    ('last_sync',f'SELECT MAX(last_synced_at) AS last_synced_at FROM erp_mercadolibre_store_links WHERE is_current=1 AND token_id IN ({placeholders})'),
   ]:
    cursor.execute('EXPLAIN '+sql,tuple(scope))
    plan=cursor.fetchall()
    case={'scope':name,'scope_size':len(scope),'kind':kind,'plan':plan}
    started=time.monotonic()
    try:
     cursor.execute(sql,tuple(scope))
     cursor.fetchall()
    except Exception as exc:
     case['error_code']=exc.args[0] if exc.args else type(exc).__name__
    case['seconds']=round(time.monotonic()-started,3)
    report['plans'].append(case)
    output.write_text(json.dumps(report,indent=2,default=str)+'\n')
    print(json.dumps(case,default=str),flush=True)
  cursor.execute("SELECT LEFT(DIGEST_TEXT,600) AS digest, COUNT_STAR AS calls, FIRST_SEEN AS first_seen, LAST_SEEN AS last_seen, ROUND(MAX_TIMER_WAIT/1000000000000,3) AS max_seconds FROM performance_schema.events_statements_summary_by_digest WHERE SCHEMA_NAME=DATABASE() AND DIGEST_TEXT LIKE '%erp_mercadolibre_store_links%' AND DIGEST_TEXT LIKE '%IN (...)%' AND DIGEST_TEXT NOT LIKE 'INSERT%' AND DIGEST_TEXT NOT LIKE 'UPDATE%' ORDER BY MAX_TIMER_WAIT DESC LIMIT 8")
  report['historical_times']=cursor.fetchall()
  output.write_text(json.dumps(report,indent=2,default=str)+'\n')
finally:
 connection.close()
