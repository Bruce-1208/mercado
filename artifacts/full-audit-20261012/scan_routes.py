import importlib.util,json,time
from pathlib import Path
OUT=Path('/Users/a11/mercado/artifacts/full-audit-20261012');ROOT=Path('/private/tmp/mercado-full-audit-20261012')
spec=importlib.util.spec_from_file_location('audit_runner',ROOT/'artifacts/full-audit-20261012/run_regression.py');runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
def scan(*a,**kw):
 from bit import bit_interface
 app=bit_interface.app;rows=[]
 with app.test_client() as client:
  for rule in app.url_map.iter_rules():
   values={key:('00000000-0000-0000-0000-000000000001' if 'UUID' in type(conv).__name__ else 1 if 'Integer' in type(conv).__name__ else 'audit-fixture') for key,conv in rule._converters.items()}
   try:
    built=rule.build(values);path=built[1];start=time.monotonic();r=client.open(path,method='OPTIONS');rows.append({'rule':str(rule),'endpoint':rule.endpoint,'methods':sorted(rule.methods),'options_status':r.status_code,'ms':round((time.monotonic()-start)*1000,2)})
   except Exception as e:rows.append({'rule':str(rule),'endpoint':rule.endpoint,'error':type(e).__name__+': '+str(e)[:300]})
 (OUT/'route-scan.json').write_text(json.dumps({'scope':'All registered routes: OPTIONS only, isolated app bootstrap, no authenticated business operations. This verifies registration/dispatch only, not feature correctness.','routes':rows},ensure_ascii=False,indent=2));print('scanned',len(rows),'routes');return 0
import pytest
pytest.main=scan
runner.main()
