import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from erp.mercadolibre_collection_store import _connect_list
from erp.ai_original_images import restore_product_variation_image_sources,variation_image_sources,variation_white_image_ready
ids=[17318201,17318203,17318202,17318205,17318206,17318208,17318209,17318207,17318211,17318213,17318212]
c=_connect_list()
with c.cursor() as q:
 q.execute('SELECT id,source_item_id,source_snapshot_json FROM erp_mercadolibre_products WHERE id IN ('+','.join(['%s']*len(ids))+')',ids)
 rows=q.fetchall()
c.close()
Path(__file__).with_name('source-snapshots.json').write_text(json.dumps(rows,ensure_ascii=False,default=str))
for r in rows:
 s=json.loads(r['source_snapshot_json'] or '{}');o=s.get('original_1688') or {};a=s.get('ai_original') or {};vs=restore_product_variation_image_sources(a.get('variations') or [],o,a.get('source_variations') or [])
 print(json.dumps({'id':r['id'],'source':r['source_item_id'],'title':o.get('title'),'status':a.get('status'),'step':a.get('failed_step'),'original_keys':list(o),'main':o.get('main_image_url'),'gallery':len(o.get('images') or []),'sku':len(vs),'missing':sum(not variation_image_sources(v) and not variation_white_image_ready(v) for v in vs),'samples':[{k:v.get(k) for k in ['sku_id','label','image_url','attribute_combinations']} for v in (o.get('variations') or [])[:3]]},ensure_ascii=False))
