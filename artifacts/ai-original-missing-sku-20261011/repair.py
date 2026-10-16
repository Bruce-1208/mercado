"""Restore visually verified gallery sources by SKU identity, then resume only images."""
import json,sys
from copy import deepcopy
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from erp.mercadolibre_collection_store import get_product_items_by_ids,update_ai_original_product
from erp.ai_original_cost_control import product_generation_lock
from erp.ai_original_images import merge_recollected_sku_images,variation_white_image_ready
from erp.ai_original_products import prepare_ai_original_product
OUT=Path(__file__).resolve().parent
# Gallery images inspected individually: black=4, red=2, blue=1; C33=4.
MAPPING={16764290:{'6273818499756':4,'6273818499757':2,'6273818499758':1},16764291:{'6036291395426':4}}
def no_model(*args,**kwargs):
 raise AssertionError('Image-only recovery must not call a language model')
for pid,mapping in MAPPING.items():
 with product_generation_lock(pid) as check:
  row=get_product_items_by_ids([pid])[0]
  snapshot=row['source_snapshot_json'];snapshot=json.loads(snapshot) if isinstance(snapshot,str) else snapshot
  before=deepcopy(snapshot['ai_original'])
  assert before['preparation']['sku_images_only']
  gallery=snapshot['original_1688']['images']
  sources=[{'sku_id':sku,'image_url':gallery[index]} for sku,index in mapping.items()]
  snapshot=merge_recollected_sku_images(snapshot,{'variations':sources})
  row['source_snapshot_json']=snapshot
  result=prepare_ai_original_product(row,chat=no_model,template_id=before.get('template_id') or 'standard')
  after=json.loads(result['source_snapshot_json'])['ai_original']
  assert after['status']=='completed' and not after['error']
  assert all(variation_white_image_ready(v) for v in after['variations'])
  for key in ['generation_budget','title_es','description_es','category_id','attributes']:
   assert before.get(key)==after.get(key),key
  check()
  update_ai_original_product(pid,{'source_snapshot_json':result['source_snapshot_json'],'expected_ai_original_revision':int(before.get('edit_revision') or 0)})
  fresh=get_product_items_by_ids([pid])[0]
  (OUT/f'{pid}-after.json').write_text(json.dumps(fresh,ensure_ascii=False,default=str))
  saved=fresh['source_snapshot_json'];saved=json.loads(saved) if isinstance(saved,str) else saved
  assert saved['ai_original']['status']=='completed'
  assert all(variation_white_image_ready(v) for v in saved['ai_original']['variations'])
  print(json.dumps({'id':pid,'status':saved['ai_original']['status'],'sku_count':len(after['variations']),'model_cost_added':0},ensure_ascii=False),flush=True)
