"""Compare actual old/new SQL on 80k generated orders, never production data."""
import ast,json,os,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
os.environ.update(MYSQL_HOST='127.0.0.1',MYSQL_PORT='13317',MYSQL_USER='root',MYSQL_PASSWORD='',MYSQL_DATABASE='stress_test',BIT_BACKGROUND_SERVICES_DISABLED='1')
from bit import bit_mysql as db,yandex_order_sync
from erp import mercadolibre_profitability_cache as rates,mercadolibre_store_link_store as links
for name in ('_ensure_mercado_synced_orders_table','_ensure_mercado_store_tokens_table','_ensure_mercado_store_site_settings_table'):setattr(db,name,lambda c:None)
yandex_order_sync.ensure_tables=lambda c:None
rates.ensure_profitability_cache_tables=lambda c:None
links.ensure_store_link_table=lambda c:None
node=next(n for n in ast.parse(Path(__file__).with_name('bit_mysql.before.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='list_orders')
namespace=dict(db.__dict__);exec(compile(ast.Module(body=[node],type_ignores=[]),'<before>','exec'),namespace)
old=namespace['list_orders']
# Additional currency / missing amount variants, isolated synthetic rows only.
import pymysql
fixture=pymysql.connect(host='127.0.0.1',port=13317,user='root',database='stress_test')
columns=['freight_currency_id','freight','quoted_freight_currency_id','quoted_freight_local','raw_json']
ids=[f'test{i:08}' for i in range(8)]
with fixture.cursor() as cursor:
 cursor.execute('SELECT order_id,'+','.join(columns)+' FROM mercado_synced_orders WHERE order_id IN ('+','.join(['%s']*len(ids))+')',ids)
 originals=cursor.fetchall()
 for i,ident in enumerate(ids):
  cursor.execute('UPDATE mercado_synced_orders SET freight_currency_id=%s,freight=%s,quoted_freight_currency_id=%s,quoted_freight_local=%s WHERE order_id=%s',(['MXN',None,'','usd','EUR','USD','MXN','USD'][i],None if i%3==0 else i,'MXN' if i%2 else 'USD',None if i%4==0 else 4,ident))
 with fixture.cursor() as raw_cursor:
  originals_by_id={row[0]:row for row in originals}
  packs=[None,None,'null','x'*200+'尾部','AbC-PACK-Case',123456789006,'','percent%_back\\slash']
  for i,ident in enumerate(ids):
   raw=json.loads(originals_by_id[ident][-1])
   if i==0:raw.pop('pack_id',None)
   else:raw['pack_id']=packs[i]
   raw_cursor.execute('UPDATE mercado_synced_orders SET raw_json=%s WHERE order_id=%s',(json.dumps(raw,ensure_ascii=False),ident))
fixture.commit()
try:
 checks=[]
 for params in [{'freight_variance': 'different', 'summary_only': False}, {'freight_variance': 'different', 'summary_only': True}, {'freight_variance': 'different_any', 'summary_only': False}, {'freight_variance': 'different_any', 'summary_only': True}, {'freight_variance': 'actual_lower', 'summary_only': False}, {'freight_variance': 'actual_lower', 'summary_only': True}, {'freight_variance': 'pending_actual', 'summary_only': False}, {'freight_variance': 'pending_actual', 'summary_only': True}, {'freight_variance': 'pending_quote', 'summary_only': False}, {'freight_variance': 'pending_quote', 'summary_only': True}, {'freight_variance': 'dimensions_changed', 'summary_only': False}, {'freight_variance': 'dimensions_changed', 'summary_only': True}, {'sort_by': 'amount', 'include_summary': False}, {'sort_by': 'balance', 'include_summary': False}, {'sort_by': 'profit_rate', 'include_summary': False}, {'summary_only': True, 'group_name': 'Group 1'}, {'summary_only': True, 'status': '交付'}, {'summary_only': True, 'origin': 'yandex'}, {'summary_only': True, 'allowed_store_ids': [1, 2], 'store_ids': [3]}, {'summary_only': True, 'search': 'Synthetic', 'store_ids': [2]}, {'search': '尾部', 'include_summary': False}, {'search': 'AbC-PACK', 'include_summary': False}, {'search': 'abc-pack', 'include_summary': False}, {'search': '123456789006', 'include_summary': False}, {'search': '%', 'include_summary': False}, {'search': '_', 'include_summary': False}, {'search': 'notfound-edge', 'include_summary': False}, {'remark_search': 'remark', 'summary_only': True}]:
  t=time.monotonic();a=old(**params);before=time.monotonic()-t
  t=time.monotonic();b=db.list_orders(**params);after=time.monotonic()-t
  differences=[k for k in a.keys() | b.keys() if a.get(k)!=b.get(k)]
  row={'params':params,'before_ms':round(before*1000,1),'after_ms':round(after*1000,1),'total':b.get('total'),'different_fields':differences}
  checks.append(row);Path(__file__).with_name('order-edge-equivalence.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2));print(json.dumps(row,ensure_ascii=False),flush=True)
 Path(__file__).with_name('order-edge-equivalence.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
 assert all(not x['different_fields'] for x in checks)

finally:
 with fixture.cursor() as cursor:
  for original in originals:
   cursor.execute('UPDATE mercado_synced_orders SET '+','.join(c+'=%s' for c in columns)+' WHERE order_id=%s',(*original[1:],original[0]))
 fixture.commit();fixture.close()
