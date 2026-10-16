import sys,json,time,collections
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from erp.mercadolibre_collection_store import _connect_list,list_product_items
c=_connect_list()
with c.cursor() as q:
 q.execute("SELECT id,source_snapshot_json FROM erp_mercadolibre_products WHERE id IN (16943431,17422911,17318200,17318199)");rs=q.fetchall()
for r in rs:
 s=json.loads(r['source_snapshot_json'] or '{}');print('ID',r['id'],'keys',list(s))
 def walk(v,p=''):
  if isinstance(v,dict):
   for k,x in v.items():yield from walk(x,p+'.'+k)
  elif isinstance(v,list):
   for i,x in enumerate(v):yield from walk(x,p+f'[{i}]')
  elif isinstance(v,str) and ('alicdn' in v or ('image' in p.lower() and v)):yield p,v[:160]
 hits=list(walk(s));print('images',len(hits),hits[:22])
 print('original keys',list((s.get('original_1688') or {})))
for count in [1,50]:
 t=time.monotonic();r=list_product_items(source_type='ai_original',include_total=False,include_generator_options=False,limit=count)
 print('list',count,'seconds',round(time.monotonic()-t,3),'jsonMB',round(len(json.dumps(r,default=str))/1e6,2))
 for row in r.get('rows',[]):row.pop('source_snapshot_json',None)
 print('without snapshot MB',round(len(json.dumps(r,default=str))/1e6,2))
 if count==1:
  print('row field bytes',sorted([(k,len(json.dumps(v,default=str))) for k,v in r['rows'][0].items()],key=lambda x:-x[1])[:9])
