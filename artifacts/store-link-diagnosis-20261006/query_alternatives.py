"""Compare equivalent read-only query forms; no index or application changes."""
import json
import time
from pathlib import Path
import pymysql
from bit.db_pool import default_connection_config
config=default_connection_config()
config.update(host='127.0.0.1',port=3306,read_timeout=20)
connection=pymysql.connect(**config)
report=[]
try:
 with connection.cursor() as cursor:
  cursor.execute('SET SESSION MAX_EXECUTION_TIME=15000')
  cursor.execute('SET SESSION TRANSACTION READ ONLY')
  cursor.execute('SELECT DISTINCT token_id FROM erp_mercadolibre_store_links FORCE INDEX (idx_erp_meli_store_link_store) WHERE is_current=1')
  scope=[row['token_id'] for row in cursor.fetchall()]
  placeholders=','.join(['%s']*len(scope))
  statements=[
   ('baseline_count','SELECT COUNT(*) AS n FROM erp_mercadolibre_store_links WHERE is_current=1',()),
   ('scoped_covering_count',f'SELECT COUNT(*) AS n FROM erp_mercadolibre_store_links FORCE INDEX (idx_erp_meli_store_link_store) WHERE is_current=1 AND token_id IN ({placeholders})',tuple(scope)),
   ('baseline_last_sync','SELECT MAX(last_synced_at) AS last_synced_at FROM erp_mercadolibre_store_links WHERE is_current=1',()),
   ('scoped_ordered_last_sync',f'SELECT last_synced_at FROM erp_mercadolibre_store_links FORCE INDEX (idx_erp_meli_link_sort_last_synced_at) WHERE is_current=1 AND token_id IN ({placeholders}) ORDER BY last_synced_at DESC LIMIT 1',tuple(scope)),
  ]
  results={}
  for name,sql,args in statements:
   cursor.execute('EXPLAIN '+sql,args)
   plan=cursor.fetchall()
   started=time.monotonic()
   cursor.execute(sql,args)
   value=cursor.fetchone()
   result={'name':name,'seconds':round(time.monotonic()-started,3),'plan':plan}
   results[name]=value
   report.append(result)
   print(json.dumps(result,default=str),flush=True)
  assert results['baseline_count']==results['scoped_covering_count']
  assert results['baseline_last_sync']==results['scoped_ordered_last_sync']
  report.append({'equivalence_check':'passed in same read-only transaction', 'scope_size':len(scope)})
finally:
 connection.close()
Path('artifacts/store-link-diagnosis-20261006/query-alternatives.json').write_text(json.dumps(report,indent=2,default=str)+'\n')
