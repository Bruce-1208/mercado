from pathlib import Path
import requests
from flask import Flask
app=Flask(__name__);app.secret_key=Path('bit/runtime_locks/workbench_secret.key').read_text().strip()
s=requests.Session();s.trust_env=False
s.cookies.set('session',app.session_interface.get_signing_serializer(app).dumps({'workbench_user':{'id':1,'username':'admin'}}))
for path in ['ai-original-products/process','store-links/sync','official-infractions/sync','prohibited-listings/sync','mercado-products/publish','tasks/daily','order-sync','mercado-collection','risk-check','zying-collection']:
 try:
  r=s.get('http://127.0.0.1:5000/api/'+path+'/status',timeout=10)
  d=r.json().get('data',r.json())
  print(path,r.status_code,{k:v for k,v in d.items() if k in ('running','status','message','processed_count','total_count','completed_count','failed_count')})
 except Exception as e: print(path,type(e).__name__)
