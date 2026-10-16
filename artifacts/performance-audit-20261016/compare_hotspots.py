"""A/B only in disposable MySQL. No application/production changes."""
import json,time,statistics
from pathlib import Path
import bench_modules as b
import pymysql
OUT=Path(__file__).with_name('hotspot-comparison.json')
report={'scope':__doc__,'runs':[],'migrations':[]}
def measure(label,name,repeats=7):
 rows=[b.one(name) for _ in range(repeats)]
 result={'label':label,'case':name,**b.summarize(rows),'median_ms':statistics.median(x['ms'] for x in rows),'samples':rows}
 report['runs'].append(result);OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str));print(label,name,result['median_ms'],result['failed'],flush=True)
for name in ('products.ai_filter','products.ai_fast','products.ai_count','products.generator_options','products.search','orders.search','inventory.stock','inventory.shelves','after_sale.order_candidates'):
 measure('baseline',name)
# Isolate repeated schema inspection overhead while leaving identical queries.
original=b.inventory.ensure_inventory_tables
b.inventory.ensure_inventory_tables=lambda cursor:None
try:
 for name in ('inventory.stock','inventory.shelves'):measure('schema_prepared_once_experiment',name)
finally:b.inventory.ensure_inventory_tables=original
# Existing, shipped optional migrations, applied only to the disposable fixture.
from scripts import optimize_order_search_projection,optimize_order_lookup_indexes,optimize_product_search_index
c=pymysql.connect(host='127.0.0.1',port=13326,user='root',database='stress_test',charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor,autocommit=True,read_timeout=600)
try:
 report['migrations'].append(optimize_product_search_index.optimize(c,apply=True))
 report['migrations'].append(optimize_order_search_projection.optimize(c,database='stress_test',apply=True))
 report['migrations'].append(optimize_order_lookup_indexes.optimize(c,apply=True))
finally:c.close()
from bit import order_search_projection
order_search_projection._CACHE.clear()
for name in ('products.search','orders.search','after_sale.order_candidates'):measure('existing_optional_migrations_applied',name)
report['blocked_network_attempts']=b.blocked
OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str));b.close_all_pools()
