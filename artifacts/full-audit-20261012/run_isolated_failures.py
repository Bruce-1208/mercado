import json,subprocess,sys,time,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path('/private/tmp/mercado-full-audit-20261012');OUT=Path(__file__).resolve().parent
cases=ET.parse(OUT/'python.xml')
files=sorted({c.get('classname','').split('.test_')[0].replace('.','/')+'/test_'+c.get('classname','').split('.test_')[1].split('.')[0]+'.py' for c in cases.iter('testcase') if c.find('failure') is not None and '.test_' in c.get('classname','') and 'browser' not in c.get('classname','')})
results=[]
for index,f in enumerate(files):
 stem=Path(f).stem; xml=OUT/'isolated'/f'{stem}.xml';log=xml.with_suffix('.log');xml.parent.mkdir(exist_ok=True)
 start=time.monotonic()
 with log.open('w') as handle:
  try:r=subprocess.run([sys.executable,'artifacts/full-audit-20261012/run_regression.py','-q',f,'--junitxml='+str(xml),'--tb=short'],cwd=ROOT,stdout=handle,stderr=subprocess.STDOUT,timeout=100);code=r.returncode
  except subprocess.TimeoutExpired:code='timeout'
 results.append({'file':f,'exit':code,'seconds':round(time.monotonic()-start,2)})
 (OUT/'isolated-progress.json').write_text(json.dumps(results,indent=2));print(index+1,len(files),f,code,flush=True)
