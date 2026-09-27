import json
from pathlib import Path
from bit import browser_extension_models
from erp.mercadolibre_collection_store import _connect
from bit.bit_mysql import list_mercado_store_tokens
from erp.mercadolibre_batch_publish import DatabaseMercadoLibreClient, _prepared_listing_from_product_row, _complete_ai_publication_attributes
from erp.ai_original_products import complete_marketplace_category
c=_connect()
with c.cursor() as q:
 q.execute("SELECT * FROM erp_mercadolibre_products WHERE source_type='ai_original'")
 rows=q.fetchall()
 q.execute("SELECT id FROM workbench_users WHERE display_name=%s", ('管理员',))
 owner=q.fetchone()['id']
c.close()
token=next(r for r in list_mercado_store_tokens()['rows'] if r.get('enabled') and r.get('site_id') in ('CBT','',None))
api_key=browser_extension_models.get_api_key(owner,'deepseek',Path('bit/runtime_locks/workbench_secret.key').read_text().strip())
client=DatabaseMercadoLibreClient(token['id'])
for row in rows:
 if row['id'] not in (11271695,11373944): continue
 snapshot=json.loads(row['source_snapshot_json']); ai=snapshot['ai_original']
 try:
  if row['id']==11373942:
   listing=_complete_ai_publication_attributes(row,_prepared_listing_from_product_row(row,'MLM'),client,api_key=api_key)
   result={'attributes':listing[0]['attributes']}
  else:
   result=complete_marketplace_category(snapshot['original_1688'],ai,client,api_key=api_key)
  with open(f"artifacts/ai-listing-fix-20260927/{row['id']}.json",'w') as f: json.dump(result,f,ensure_ascii=False,indent=2)
  print(row['id'],'OK',result.get('category_id'),len(result.get('variations',[])),result.get('missing_required_attributes'),flush=True)
 except Exception as exc:
  print(row['id'],type(exc).__name__,str(exc),flush=True)
