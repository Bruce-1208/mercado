"""Bounded read-only product-list benchmark; never prints product records."""
import sys, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bit import product_access
from bit.db_pool import default_connection_config
from erp import mercadolibre_collection_store as store
import pymysql

def connect():
    c=pymysql.connect(**dict(default_connection_config(),read_timeout=12))
    with c.cursor() as q:
        q.execute('SET SESSION TRANSACTION READ ONLY')
        q.execute('SET SESSION max_execution_time=8000')
    return c
store.ensure_collection_tables=lambda q:None
results=[]
with product_access.scope({'access_version':1,'role_key':'enterprise_admin','organization_key':'wuhan-zeshun'}):
    for name,fn in [('directory',lambda:store.list_product_salespeople(connection_factory=connect)),
                    ('first_page',lambda:store.list_product_items(limit=100,connection_factory=connect)),
                    ('person_filter',lambda:store.list_product_items(salesperson='李壹畅',limit=100,connection_factory=connect))]:
        start=time.monotonic()
        try:
            data=fn()
            result={'case':name,'seconds':round(time.monotonic()-start,3),'rows':len(data.get('rows',data.get('salespeople',[]))),'total':data.get('total')}
        except Exception as exc:result={'case':name,'seconds':round(time.monotonic()-start,3),'error':str(exc)}
        results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
Path(__file__).with_name('benchmark.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
