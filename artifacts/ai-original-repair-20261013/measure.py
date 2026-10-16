import sys,json,gzip,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from erp.mercadolibre_collection_store import list_product_items
from erp.ai_original_public import compact_product_row
start=time.monotonic();r=list_product_items(source_type='ai_original',include_total=False,include_generator_options=False,limit=50)
query=time.monotonic()-start
for row in r['rows']:row.pop('source_snapshot_json',None)
before=len(json.dumps(r,default=str).encode());r['rows']=[compact_product_row(row) for row in r['rows']];after=json.dumps(r,default=str).encode()
result={'query_seconds':round(query,3),'before_bytes':before,'after_bytes':len(after),'gzip_bytes':len(gzip.compress(after,compresslevel=5))}
Path(__file__).with_suffix('.json').write_text(json.dumps(result,indent=2));print(result)
