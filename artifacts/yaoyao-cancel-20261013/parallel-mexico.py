import importlib.util,os
from pathlib import Path
p=Path(__file__).with_name('cancel.py');sp=importlib.util.spec_from_file_location('op',p);m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)
os.environ['BIT_PROMOTION_WORKERS']='12'
s=next(x for x in m.data if x['id']==85)
s=dict(s,accounts=[a for a in s['accounts'] if a['site_id']=='MLM'])
r=m.run(s)
(m.out/'mexico-result.json').write_text(m.json.dumps(r,ensure_ascii=False,indent=2))
