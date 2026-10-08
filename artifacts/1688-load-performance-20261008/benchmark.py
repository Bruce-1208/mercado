"""Read-only real-data benchmark; isolated HTTP route, no production login/session."""
import ast,json,logging,runpy,threading,time,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from flask import Flask,jsonify,request
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
from erp import mercadolibre_collection_store as store
from bit.http_performance import install_http_performance
store._schema_ready=True  # read-only audit: never run schema migrations
app=Flask(__name__,static_folder=str(Path('bit/static').resolve()))
install_http_performance(app)
ns=dict(request=request,jsonify=jsonify,logging=logging,
        db_list_mercado_product_items=store.list_product_items,
        _parse_int_param=lambda a,k,d,l,h:max(l,min(h,int(a.get(k,d)))))
node=next(n for n in ast.parse(Path('bit/bit_interface.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='api_1688_products')
node.decorator_list=[]
exec(compile(ast.Module(body=[node],type_ignores=[]),'<real-route>','exec'),ns)
app.add_url_rule('/api/1688-products',view_func=ns['api_1688_products'])
app.add_url_rule('/api/1688-products/options',endpoint='options',view_func=lambda:jsonify(status='success',data=store.get_1688_product_options()))
app.add_url_rule('/api/mercado-management-categories',endpoint='categories',view_func=lambda:jsonify(status='success',data=store.list_management_categories()))
html=runpy.run_path('tests/test_1688_product_management_browser.py')['_product_section_html']()
app.add_url_rule('/',view_func=lambda:html)
logging.getLogger('werkzeug').setLevel(logging.ERROR)
server=make_server('127.0.0.1',0,app,threaded=True)
threading.Thread(target=server.serve_forever,daemon=True).start()
url=f'http://127.0.0.1:{server.server_port}'
report={'scope':'Real MySQL + production route body/gzip + actual product HTML/JS; isolated local HTTP. Excludes production auth/proxy and external image completion.','runs':[]}
with app.test_client() as client:
 t=time.perf_counter(); response=client.get('/api/1688-products?limit=50',headers={'Accept-Encoding':'gzip'})
 report['api']={'seconds':round(time.perf_counter()-t,3),'wire_bytes':len(response.data),'encoding':response.headers.get('Content-Encoding')}
with sync_playwright() as pw:
 browser=pw.chromium.launch(executable_path='/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',headless=True)
 for network in ['local','5Mbps_100ms']:
  for size in [50,100,200]:
   page=browser.new_page(viewport={'width':1440,'height':1000})
   page.route('**/*',lambda r:r.continue_() if r.request.url.startswith(url) else r.abort())
   page.add_init_script('window.aiOriginalDeleteRunning = false;')
   page.goto(url,wait_until='load')
   if network!='local':
    session=page.context.new_cdp_session(page)
    session.send('Network.enable')
    session.send('Network.emulateNetworkConditions',{'offline':False,'latency':100,'downloadThroughput':625000,'uploadThroughput':625000})
   result=page.evaluate('''async size => {
    products1688PageSize=size;
    const start=performance.now();
    await load1688Products();
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    return {seconds:(performance.now()-start)/1000,rows:products1688Rows.length,status:document.getElementById('products-1688-status').textContent};
   }''',size)
   result.update(network=network,page_size=size);report['runs'].append(result)
   assert result['rows']>0 and result['seconds']<3,result
   if size==50 and network=='local':page.screenshot(path='artifacts/1688-load-performance-20261008/products.png')
   page.close()
 browser.close()
server.shutdown()
Path('artifacts/1688-load-performance-20261008/results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False,indent=2))
