from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit
from jinja2 import Environment, FileSystemLoader
import json
ROOT=Path(__file__).resolve().parents[2]
rows=[{'id':101,'title':'便携桌面收纳盒 · 测试商品','review_status':'approved','weight_g':260,'net_proceeds_usd':8.5,'price':199,'currency_id':'MXN','source_type':'collected'}, {'id':102,'title':'旅行收纳袋 · 测试商品','review_status':'unreviewed','weight_g':180,'net_proceeds_usd':6.2,'price':149,'currency_id':'MXN','source_type':'zying'}]
html=Environment(loader=FileSystemLoader(ROOT/'bit/templates')).get_template('index.html').render(current_user={'username':'preview','display_name':'界面预览','is_admin':True},can_change_task_workers=False,runtime_role='server',url_for=lambda endpoint,filename:'/static/'+filename)
html=html.replace('</body>', '<script>switchTab("mercado-products");</script></body>')
class Handler(SimpleHTTPRequestHandler):
 def do_GET(self):
  path=urlsplit(self.path).path
  if path=='/': data=html.encode(); mime='text/html; charset=utf-8'
  elif path.startswith('/static/'):
   file=(ROOT/'bit'/path.lstrip('/')).resolve()
   if not file.is_relative_to(ROOT/'bit/static') or not file.is_file(): self.send_error(404); return
   data=file.read_bytes(); mime=self.guess_type(str(file))
  elif path.startswith('/api/'):
   value={'rows':[],'total':0}
   if path in ['/api/mercado-products','/api/mercado-collection/items']: value={'rows':rows,'total':len(rows)}
   if path=='/api/mercado-tokens': value={'rows':[{'id':1,'display_name':'演示店铺 · 墨西哥','site_id':'MLM','site_settings':[{'site_id':'MLM','group_name':'演示分组'}]}]}
   data=json.dumps({'status':'success','success':True,'data':value,'rows':[],'items':[],'stores':[],'groups':[]}).encode(); mime='application/json'
  else: self.send_error(404); return
  self.send_response(200); self.send_header('Content-Type',mime); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
 def do_POST(self): self.send_error(405,'Preview is read-only')
 def log_message(self,*args): pass
print('Preview http://127.0.0.1:8766',flush=True)
ThreadingHTTPServer(('127.0.0.1',8766),Handler).serve_forever()
