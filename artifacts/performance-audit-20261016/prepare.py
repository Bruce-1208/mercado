import json,shutil,subprocess,hashlib
from pathlib import Path
root=Path.cwd(); out=root/'artifacts/performance-audit-20261016'; dest=Path('/private/tmp/mercado-performance-audit-20261016'); dest.mkdir(exist_ok=True)
files=subprocess.check_output(['rg','--files','--hidden','-g','!.git','-g','!artifacts','-g','!.data','-g','!runtime_logs','-g','!.private','-g','!build','-g','!dist','-g','!.tools','-g','!cache','-g','!node_modules'],text=True).splitlines()
allowed={'.py','.js','.cjs','.mjs','.html','.css','.md','.txt','.toml','.ini','.cfg','.sh','.bat','.ps1','.json','.svg'}
manifest=[]
for f in files:
 p=root/f
 if p.suffix not in allowed or any(x.startswith('.') for x in p.relative_to(root).parts) or p.is_symlink():continue
 if p.suffix=='.json' and not any(x in p.parts for x in ('tests','static','browser_extension')):continue
 t=dest/f;t.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,t)
 manifest.append({'path':f,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
(dest/'artifacts/performance-audit-20261016').mkdir(parents=True,exist_ok=True)
for base in ('bit/static','tests/fixtures','browser_extension/zeshun_collector'):
 for p in (root/base).rglob('*'):
  if p.is_file() and p.suffix.lower() in {'.png','.jpg','.jpeg','.webp','.gif','.woff','.woff2','.xlsx','.csv'}:
   rel=p.relative_to(root);t=dest/rel;t.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,t)
   if not any(x['path']==str(rel) for x in manifest):manifest.append({'path':str(rel),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
for name in ('bit/size-chart-builtins.json','.env.example'):
 p=root/name;t=dest/name;t.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,t)
 manifest.append({'path':name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
for p in out.glob('*.py'):
 shutil.copy2(p,dest/'artifacts/performance-audit-20261016'/p.name)
(out/'source-manifest.json').write_text(json.dumps(manifest,indent=2));(out/'snapshot-path.txt').write_text(str(dest))
print('Snapshot files:',len(manifest),'at',dest)
