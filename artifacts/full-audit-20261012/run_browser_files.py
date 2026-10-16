import concurrent.futures,json,subprocess,sys,time
from pathlib import Path
ROOT=Path('/private/tmp/mercado-full-audit-20261012');OUT=Path(__file__).resolve().parent
files=[]
for p in (ROOT/'tests').glob('test_*.py'):
 s=p.read_text()
 if 'browser' in p.name or 'playwright' in s or 'console_page' in s or 'console_browser' in s:files.append(str(p.relative_to(ROOT)))
(OUT/'browser-files.json').write_text(json.dumps(sorted(files),indent=2));(OUT/'browser-files').mkdir(exist_ok=True)
def run(f):
 stem=Path(f).stem; xml=OUT/'browser-files'/f'{stem}.xml';log=xml.with_suffix('.log');start=time.monotonic()
 with log.open('w') as handle:
  try:r=subprocess.run([sys.executable,'artifacts/full-audit-20261012/run_regression.py','-q',f,'--junitxml='+str(xml),'--tb=short'],cwd=ROOT,stdout=handle,stderr=subprocess.STDOUT,timeout=240);code=r.returncode
  except subprocess.TimeoutExpired:code='timeout'
 return {'file':f,'exit':code,'seconds':round(time.monotonic()-start,2)}
results=[]
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
 for r in pool.map(run,sorted(files)):
  results.append(r);(OUT/'browser-progress.json').write_text(json.dumps(results,indent=2));print(len(results),len(files),r,flush=True)
