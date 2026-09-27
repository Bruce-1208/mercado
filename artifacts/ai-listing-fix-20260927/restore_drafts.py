import json
from pathlib import Path
from copy import deepcopy
from erp.mercadolibre_collection_store import _connect
from erp.mercadolibre_batch_publish import DatabaseMercadoLibreClient, _prepared_listing_from_product_row
from erp.mercadolibre_follow_sell import build_user_product_family_payload, build_user_product_payload
from bit.bit_mysql import list_mercado_store_tokens
root=Path('artifacts/ai-listing-fix-20260927')
conn=_connect()
with conn.cursor() as q:
 q.execute('SELECT * FROM erp_mercadolibre_products WHERE id IN (11271695,11271696,11373942,11373944)')
 rows=q.fetchall()
conn.close()
token=next(r for r in list_mercado_store_tokens()['rows'] if r.get('enabled') and r.get('site_id') in ('CBT','',None))
client=DatabaseMercadoLibreClient(token['id'])
plans=[]
for row in rows:
 result=json.loads((root/f"{row['id']}.json").read_text())
 snapshot=json.loads(row['source_snapshot_json']); before=deepcopy(snapshot)
 ai=snapshot['ai_original']; source=snapshot['source']
 if row['id']==11373942:
  gender=next(a for a in result['attributes'] if a['id']=='GENDER')
  for part in (ai,source):
   part['attributes']=[a for a in part.get('attributes',[]) if a.get('id')!='GENDER']+[gender]
  ai['missing_required_attributes']=[a for a in ai.get('missing_required_attributes',[]) if a['id']!='GENDER']
 else:
  assert ai['status']=='failed'
  assert len(result['variations'])==len(snapshot['original_1688']['variations'])
  for old,new in zip(snapshot['original_1688']['variations'],result['variations']):
   assert {k:v for k,v in old.items() if k!='attribute_combinations'}=={k:v for k,v in new.items() if k!='attribute_combinations'}
  ai.update(result,status='completed',error='')
  source.update({k:result[k] for k in ('category_id','category_name','attributes','variations')})
 updated={**row,'source_snapshot_json':snapshot,'category_id':source['category_id'],'category_name':source.get('category_name','')}
 listing=_prepared_listing_from_product_row(updated,'MLM')
 builder=build_user_product_family_payload if listing[0].get('variations') else build_user_product_payload
 payload=builder(client,*listing,site_id='MLM',quantity=1,net_proceeds=float(row['net_proceeds_usd'] or 10))
 print(row['id'],'payload validated',len(payload) if isinstance(payload,list) else 1,flush=True)
 (root/f"{row['id']}-before.json").write_text(json.dumps({'source_snapshot_json':before,'category_id':row['category_id'],'category_name':row['category_name'],'review_status':row['review_status']},ensure_ascii=False,indent=2))
 plans.append((row,updated,snapshot))
# No marketplace writes. Preserve concurrent edits with an exact snapshot check.
conn=_connect()
try:
 with conn.cursor() as q:
  for old,new,snapshot in plans:
   q.execute('UPDATE erp_mercadolibre_products SET source_snapshot_json=%s, category_id=%s, category_name=%s, review_status=%s WHERE id=%s AND source_snapshot_json=%s',
     (json.dumps(snapshot,ensure_ascii=False),new['category_id'],new['category_name'],'unreviewed',old['id'],old['source_snapshot_json']))
   assert q.rowcount==1, f"Concurrent edit detected: {old['id']}"
 conn.commit()
 print('Restored four drafts; review remains required; nothing published.',flush=True)
except Exception:
 conn.rollback(); raise
finally: conn.close()
