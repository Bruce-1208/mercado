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
checks=[]
for params in ({},{'page':2},{'include_summary':False},{'sort_by':'cost'},{'sort_by':'profit','sort_dir':'asc'},{'allowed_store_ids':[1,2],'store_ids':[2]},{'allowed_store_ids':[]},{'transfer_warehouse':'yiwu'},{'search':'test00000'}, {'search':'Synthetic','include_summary':False}, {'search':'pack123'}, {'search':'不存在'}, {'summary_only':True,'store_ids':[2]}, {'summary_only':True,'country':'墨西哥','salesperson':'Person 1'},{'origin':'yandex'},{'freight_variance':'actual_higher'},{'freight_difference_min':'-2','freight_difference_max':'2'},{'country':'墨西哥','salesperson':'Person 1'},{'start_date':'2026-01-10','end_date':'2026-01-12','summary_only':True}):
 t=time.monotonic();a=old(**params);before=time.monotonic()-t
 t=time.monotonic();b=db.list_orders(**params);after=time.monotonic()-t
 differences=[k for k in a if a[k]!=b.get(k)]
 row={'params':params,'before_ms':round(before*1000,1),'after_ms':round(after*1000,1),'total':b.get('total'),'different_fields':differences}
 checks.append(row);Path(__file__).with_name('order-equivalence.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2));print(json.dumps(row,ensure_ascii=False),flush=True)
Path(__file__).with_name('order-equivalence.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
assert all(not x['different_fields'] for x in checks)
