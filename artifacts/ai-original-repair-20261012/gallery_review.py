import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import requests
from PIL import Image,ImageOps,ImageDraw
out=Path(__file__).resolve().parent
rows=json.loads((out/'before.json').read_text())
for pid in (17318193,17318194,17318196):
 row=next(r for r in rows if r['id']==pid);o=json.loads(row['source_snapshot_json'])['original_1688'];urls=o.get('images') or []
 def download(pair):
  i,u=pair
  try:
   r=requests.get(u,timeout=20);r.raise_for_status(); im=Image.open(BytesIO(r.content)).convert('RGB'); im.save(out/f'{pid}-gallery-{i}.jpg');return i,ImageOps.contain(im,(290,270))
  except Exception:return i,None
 sheet=Image.new('RGB',(1200,310*((len(urls)+3)//4)), 'white');draw=ImageDraw.Draw(sheet)
 with ThreadPoolExecutor(max_workers=4) as pool:
  for i,im in pool.map(download,enumerate(urls)):
   x=(i%4)*300;y=(i//4)*310
   draw.text((x+8,y+6),f'{pid} / image {i}',fill='black')
   if im:sheet.paste(im,(x,y+28))
 sheet.save(out/f'{pid}-gallery.jpg')
 print(pid,len(urls),flush=True)
