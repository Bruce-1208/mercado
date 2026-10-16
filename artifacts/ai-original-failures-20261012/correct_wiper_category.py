import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from erp.mercadolibre_collection_store import get_product_items_by_ids,update_ai_original_product
from erp.ai_original_cost_control import product_generation_lock
pid=16763285
with product_generation_lock(pid):
 r=get_product_items_by_ids([pid])[0];s=r['source_snapshot_json'];s=json.loads(s) if isinstance(s,str) else s
 Path(__file__).with_suffix('.before.json').write_text(json.dumps(r,ensure_ascii=False,default=str))
 a=s['ai_original'];assert a.get('category_id')=='CBT457092'
 a.update(category_id='CBT5751',category_name='Wipers',category_locked=True,category_correction={'previous':'CBT457092','reason':'丰田乘用车雨刷，农业机械雨刷类目要求 Agricultural，与源商品用途不符','source':'verified_live_category_paths'})
 s.setdefault('marketplace_listings',{})['mercadolibre']=a.copy()
 update_ai_original_product(pid,{'category_id':'CBT5751','category_name':'Wipers','source_snapshot_json':s,'expected_ai_original_revision':int(a.get('edit_revision') or 0)})
