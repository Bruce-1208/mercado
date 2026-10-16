import sys,json
from pathlib import Path
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from erp.mercadolibre_collection_store import _connect_list
from erp.ai_original_images import variation_image_sources,variation_white_image_ready
c=_connect_list()
with c.cursor() as q:
 q.execute("SELECT * FROM erp_mercadolibre_products WHERE source_type='ai_original'")
 rows=q.fetchall()
c.close()
Path(__file__).with_name('before.json').write_text(json.dumps(rows,ensure_ascii=False,default=str))
print(Counter(json.loads(r['source_snapshot_json'] or '{}').get('ai_original',{}).get('status') for r in rows))
for r in rows:
 s=json.loads(r['source_snapshot_json'] or '{}'); a=s.get('ai_original') or {}; o=s.get('original_1688') or {}
 if a.get('status') in ('failed','incomplete') or (a.get('status')=='pending' and a.get('generated_by')):
  print(r['id'],a.get('status'),a.get('failed_step'),a.get('generated_by'),str(a.get('error'))[:120])
  if a.get('failed_step')=='sku_images':
   print('snapshot keys', list(s)); print('original',json.dumps(o,ensure_ascii=False)[:8500]); print('draft sku',json.dumps(a.get('variations'),ensure_ascii=False)[:3000])
