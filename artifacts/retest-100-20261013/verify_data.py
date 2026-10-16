"""Real order route/SQL + lightweight DB probes on an isolated synthetic MySQL.
This does not emulate production product/store-list queries or public networking.
"""
import ast,json,math,os,sys,threading,time,logging
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
os.environ.update(MYSQL_HOST='127.0.0.1',MYSQL_PORT='13327',MYSQL_USER='root',MYSQL_PASSWORD='',MYSQL_DATABASE='stress_test',BIT_BACKGROUND_SERVICES_DISABLED='1',MYSQL_POOL_MAX_CONNECTIONS='6',MYSQL_POOL_ACQUIRE_TIMEOUT='5')
from flask import Flask,jsonify,request,has_request_context,g,session as flask_session
from waitress import create_server
import requests
from bit import bit_mysql as db,yandex_order_sync,order_read_cache
from bit.db_pool import get_db_connection,pool_status,close_all_pools
from bit.database_availability import install_database_error_handlers
from erp import mercadolibre_profitability_cache as rates,mercadolibre_store_link_store as links
for name in ('_ensure_mercado_synced_orders_table','_ensure_mercado_store_tokens_table','_ensure_mercado_store_site_settings_table'):setattr(db,name,lambda c:None)
yandex_order_sync.ensure_tables=lambda c:None
rates.ensure_profitability_cache_tables=lambda c:None
links.ensure_store_link_table=lambda c:None
if os.environ.get('CAPACITY_BASELINE') == '1':
 old_node=next(n for n in ast.parse(Path(__file__).with_name('bit_mysql.before.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='list_orders')
 old_ns=dict(db.__dict__);exec(compile(ast.Module(body=[old_node],type_ignores=[]),'<baseline>','exec'),old_ns)
 db.list_orders=old_ns['list_orders']

checks=[]
conn=db.pymysql.connect(host='127.0.0.1',port=13327,user='root',database='stress_test')
def check(name, params, where='1=1', values=()):
 with conn.cursor() as q:
  q.execute('SELECT COUNT(*) AS total FROM mercado_synced_orders WHERE '+where,values or None); expected=q.fetchone()['total']
 result=db.list_orders(**params)
 ok=result['total']==expected
 if params.get('allowed_store_ids') is not None:
  ok=ok and all(r['store_id'] in params['allowed_store_ids'] for r in result.get('rows',[]))
 checks.append(dict(name=name,params=params,expected=expected,actual=result['total'],ok=ok))
 print(checks[-1],flush=True)
check('all-orders',{})
check('page2',{'page':2})
check('store-access',{'allowed_store_ids':[1,2]},'token_id IN (1,2)')
check('no-access',{'allowed_store_ids':[]},'1=0')
check('foreign-store',{'allowed_store_ids':[1],'store_ids':[2]},'1=0')
check('warehouse',{'transfer_warehouse':'shahu'},"transfer_warehouse='shahu'")
check('child-order-search',{'search':'test00000100'},"order_id LIKE '%test00000100%'")
check('pack-search',{'search':'pack123'},"JSON_UNQUOTE(JSON_EXTRACT(raw_json,'$.pack_id')) LIKE '%pack123%'")
check('product-search',{'search':'product42'},"product_id LIKE '%product42%'")
check('no-results',{'search':'not-found-unique'},'1=0')
check('summary-scope',{'allowed_store_ids':[2],'summary_only':True},'token_id=2')
a=db.list_orders(page=1,include_summary=False);b=db.list_orders(page=2,include_summary=False)
checks.append(dict(name='pagination-no-overlap',ok=len(a['rows'])==50 and len(b['rows'])==50 and not ({x['id'] for x in a['rows']}&{x['id'] for x in b['rows']})))
Path(__file__).with_name('data-checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2));conn.close();close_all_pools()
assert all(x['ok'] for x in checks)
