import sys,json,time,collections
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from erp.mercadolibre_collection_store import _connect_list,list_product_items
from erp.ai_original_images import variation_image_sources
from erp.ai_original_public import compact_product_row
c=_connect_list()
with c.cursor() as q:
 q.execute("SELECT id,source_item_id,source_snapshot_json FROM erp_mercadolibre_products WHERE source_type='ai_original'");rs=q.fetchall()
 keys=[r['source_item_id'] for r in rs]
 q.execute('SELECT id,source_type,source_item_id,source_snapshot_json FROM erp_mercadolibre_products WHERE source_type<>\'ai_original\' AND source_item_id IN ('+','.join(['%s']*len(keys))+')',keys);others=q.fetchall()
print('matching source records',len(others))
found=[]
for r in others:
 s=json.loads(r['source_snapshot_json'] or '{}')
 def walk(x):
  if isinstance(x,dict):
   if (x.get('sku_id') or x.get('id')) and variation_image_sources(x):yield x
   for v in x.values():yield from walk(v)
  elif isinstance(x,list):
   for v in x:yield from walk(v)
 hits=list(walk(s))
 if hits:found.append({'source_item_id':r['source_item_id'],'rows':hits})
print('recoverable source records',len(found))
Path(__file__).with_suffix('.json').write_text(json.dumps(found,ensure_ascii=False,default=str))
r=list_product_items(source_type='ai_original',include_total=False,include_generator_options=False,limit=50)
for row in r['rows']:row.pop('source_snapshot_json',None)
before=len(json.dumps(r,default=str));t=time.monotonic();r['rows']=[compact_product_row(row) for row in r['rows']];after=len(json.dumps(r,default=str))
print('before_bytes',before,'after_bytes',after,'reduction',round(100*(1-after/before),1),'projection_ms',round(1000*(time.monotonic()-t),1))
print('draft largest',sorted([(k,len(json.dumps(v,default=str))) for k,v in r['rows'][0]['ai_original'].items()],key=lambda x:-x[1])[:8])
