import sys,json,time,collections
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from erp.mercadolibre_collection_store import _connect_list
start=time.monotonic();c=_connect_list()
with c.cursor() as q:
 q.execute("SELECT id,category_id,weight_g,package_length_cm,package_width_cm,package_height_cm,net_proceeds_usd,review_status,source_snapshot_json FROM erp_mercadolibre_products WHERE source_type='ai_original'");rows=q.fetchall()
c.close();print('read_seconds',round(time.monotonic()-start,3));counts=collections.Counter();details=[]
from erp.ai_original_images import variation_image_sources,variation_white_image_ready,restore_variation_image_sources
for r in rows:
 s=json.loads(r.pop('source_snapshot_json') or '{}');a=s.get('ai_original') or {};o=s.get('original_1688') or {};counts[a.get('status')]+=1
 vs=restore_variation_image_sources(a.get('variations') or o.get('variations') or [],o.get('variations') or [])
 details.append({**r,'status':a.get('status'),'step':a.get('failed_step'),'error':a.get('error'),'title':o.get('title'),'sku_count':len(vs),'missing_sources':sum(not variation_white_image_ready(v) and not variation_image_sources(v) for v in vs),'missing_white':sum(not variation_white_image_ready(v) for v in vs),'missing_attributes':a.get('missing_required_attributes') or []})
print(json.dumps({'counts':counts,'missing_sku_sources':sum(x['missing_sources']>0 for x in details),'missing_attributes':sum(bool(x['missing_attributes']) for x in details),'missing_categories':sum(not x['category_id'] for x in details),'failed_non_image':[{'id':x['id'],'error':str(x['error'] or '')[:160]} for x in details if x['status']=='failed' and x['step']!='sku_images']},ensure_ascii=False,default=str))
Path(__file__).with_name('final-audit.json').write_text(json.dumps(details,ensure_ascii=False,indent=2,default=str))
