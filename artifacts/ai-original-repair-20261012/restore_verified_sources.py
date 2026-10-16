"""Sources visually verified from captured galleries; do not infer other SKU photos."""
import sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from erp.mercadolibre_collection_store import get_product_items_by_ids,update_ai_original_product
from erp.ai_original_cost_control import product_generation_lock
from erp.ai_original_images import merge_recollected_sku_images,prepare_variation_white_images
OUT=Path(__file__).resolve().parent
# 17318193 gallery labels explicitly say X53 and X53D. 17318196 labels
# explicitly say white, black, translucent black, translucent blue, 1 filter.
MAPPING={17318193:{'6274655887476':5,'6274655887477':6},
         17318196:{'6122378417825':5,'6122378417826':6,'6122378417827':7,'6122378417828':8}}
results=[]
for pid,mapping in MAPPING.items():
 with product_generation_lock(pid) as check:
  row=get_product_items_by_ids([pid])[0]
  s=row['source_snapshot_json'];s=json.loads(s) if isinstance(s,str) else s
  (OUT/f'{pid}-verified-before.json').write_text(json.dumps(row,ensure_ascii=False,default=str))
  revision=int(s['ai_original'].get('edit_revision') or 0)
  gallery=s['original_1688']['images']
  s=merge_recollected_sku_images(s,{'variations':[{'sku_id':sku,'image_url':gallery[index]} for sku,index in mapping.items()]})
  # Keep the real metadata error and accounting; restoring images does not
  # claim that the provider has granted missing model permissions.
  state=s['ai_original'];state['input_blockers']=[]
  def save(rows,cache):
   check();state.update(variations=rows,sku_image_cache=cache)
   s.setdefault('marketplace_listings',{})['mercadolibre']=dict(state)
   update_ai_original_product(pid,{'source_snapshot_json':s,'expected_ai_original_revision':revision})
  save(state.get('variations') or [],state.get('sku_image_cache') or {})
  variations,cache=prepare_variation_white_images(state.get('variations') or s['original_1688']['variations'],
    item_id=s['original_1688']['source_1688_item_id'],cache=state.get('sku_image_cache'),on_checkpoint=save)
  state.setdefault('preparation',{})['sku_images_ready']=True
  save(variations,cache)
  results.append({'product_id':pid,'restored_skus':len(mapping),'remaining_error':state.get('error')})
  (OUT/f'{pid}-verified-after.json').write_text(json.dumps(get_product_items_by_ids([pid])[0],ensure_ascii=False,default=str))
(OUT/'verified-image-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps(results,ensure_ascii=False),flush=True)
