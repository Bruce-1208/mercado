import pymysql,json
from collections import Counter
from pathlib import Path
from bit.db_pool import default_connection_config
c=pymysql.connect(**dict(default_connection_config(),read_timeout=15))
counts=Counter(); samples={}
with c.cursor() as q:
 q.execute('SET SESSION max_execution_time=8000')
 q.execute('SELECT id,organization_key,display_name,meli_user_id,nickname,salesperson FROM mercado_store_tokens')
 tokens=q.fetchall()
 q.execute('SELECT token_id,site_id,salesperson FROM mercado_store_site_settings')
 settings=q.fetchall()
 for empty in ["= ''",'IS NULL']:
  last=0
  while True:
   q.execute(f"SELECT id,source_item_id,JSON_EXTRACT(source_snapshot_json,'$.plugin_snapshot') plugin,JSON_EXTRACT(source_snapshot_json,'$.source.seller_id') seller FROM erp_mercadolibre_products FORCE INDEX(idx_product_salesperson) WHERE organization_key='wuhan-zeshun' AND owner_name {empty} AND source_type='pulled' AND id>%s ORDER BY id LIMIT 1000",(last,))
   rows=q.fetchall()
   if not rows:break
   for r in rows:
    p=json.loads(r['plugin'] or '{}'); key=(str(p.get('token_id','')),str(p.get('store_name','')),str(p.get('site_id','')),str(r['seller']))
    counts[key]+=1;samples.setdefault(str(key),r)
   last=rows[-1]['id']
report={'tokens':tokens,'settings':settings,'counts':[dict(token_id=k[0],store=k[1],site=k[2],seller=k[3],count=n) for k,n in counts.most_common()], 'samples':samples}
Path('artifacts/pulled-owner-repair-20261012/diagnosis.json').write_text(json.dumps(report,ensure_ascii=False,default=str,indent=2))
print('total',sum(counts.values()))
print(json.dumps(report['counts'],ensure_ascii=False))
c.close()
