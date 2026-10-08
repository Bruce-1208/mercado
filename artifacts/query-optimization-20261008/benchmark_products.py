import sys,time,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'full-functional-20261008'))
from benchmark_empty_search import ReadConnection,store
from bit import product_access
store.ensure_collection_tables=lambda c:None
out=[]
for user in [None,{'access_version':1,'role_key':'super_admin','organization_key':'wuhan-zeshun'},{'access_version':1,'role_key':'employee','id':99999999,'organization_key':'wuhan-zeshun','username':'benchmark'}]:
 with product_access.scope(user):
  for word in ['FunctionalAuditNoMatch20261008','Vestido']:
   t=time.perf_counter();r=store.list_product_items(search=word,limit=100,include_total=False,connection_factory=ReadConnection)
   v={'actor':'unscoped' if user is None else user['role_key'],'word':word,'ms':round(1000*(time.perf_counter()-t),1),'rows':len(r['rows'])};out.append(v);print(v,flush=True)
Path(__file__).with_name('product-benchmark.json').write_text(json.dumps(out,indent=2))
