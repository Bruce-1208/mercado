import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from erp.mercadolibre_collection_store import get_product_items_by_ids,update_ai_original_product
from erp.ai_original_cost_control import product_generation_lock
for pid in [16764284,17268522]:
 with product_generation_lock(pid):
  r=get_product_items_by_ids([pid])[0];s=r['source_snapshot_json'];s=json.loads(s) if isinstance(s,str) else s;a=s['ai_original']
  if a.get('status')!='completed':continue
  Path(__file__).with_name(f'{pid}-finalize-before.json').write_text(json.dumps(r,ensure_ascii=False,default=str))
  update_ai_original_product(pid,{'title':a.get('title_es') or a.get('title') or r['title'],
      'description_text':a.get('description_es') or a.get('description') or r.get('description_text',''),
      'main_image_url':a['main_image_url'],'expected_ai_original_revision':int(a.get('edit_revision') or 0)})
  print(pid,'main image saved')
