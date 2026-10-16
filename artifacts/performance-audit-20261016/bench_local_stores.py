"""SQLite read pressure with synthetic records; no real business mutations/upstream."""
import os,json,time,tempfile
from pathlib import Path
from datetime import datetime,timezone,timedelta
from bench_queues import run
from erp.mercadolibre_promotion_store import PromotionStore
from erp.mercadolibre_diagnosis_store import DiagnosisStore
from erp.price_competition_store import PriceCompetitionStore
from erp.mercadolibre_size_chart_store import SizeChartStore
from erp import ai_usage_billing as billing

def main():
 out=Path(__file__).with_name('local-stores.json');report={'scope':__doc__,'stages':[],'data':{'promotions':20,'promotion_items':20000,'diagnosis_products':10000,'price_observations':10000,'size_chart_templates':1000,'ai_usage_records':20000}}
 with tempfile.TemporaryDirectory(prefix='mercado-local-store-perf-') as tmp:
  root=Path(tmp);promotions=PromotionStore(root/'promotions.sqlite3')
  for i in range(20):
   pk=promotions.upsert_promotion(token_id=i+1,store_name=f'Synthetic {i}',application_id='synthetic',seller_id=str(i),site_id='MLM',row={'id':f'campaign{i}','type':'DEAL','name':f'Synthetic {i}','status':'started'})
   promotions.replace_items(pk,[{'id':f'fixture{j:08}','title':f'Synthetic product {j}','status':'candidate','price':10,'original_price':20,'currency_id':'MXN'} for j in range(1000)])
  diagnosis=DiagnosisStore(root/'diagnosis.sqlite3');now=datetime.now(timezone.utc).isoformat(timespec='seconds');future=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(timespec='seconds')
  with diagnosis.connect() as db:
   db.executemany('INSERT INTO diagnosis_products VALUES(?,?,?,?,?,?,?,?,?,?)',[(i+1,i%20+1,'MLM',f'fixture{i:08}','healthy',f'Synthetic fixture{i:08}',now,future,1,json.dumps({'link_id':i+1,'token_id':i%20+1,'site_id':'MLM','item_id':f'fixture{i:08}','risk':'healthy','valid_until':future,'checked_at':now,'issues':[]})) for i in range(10000)])
  price=PriceCompetitionStore(root/'prices.sqlite3')
  with price.connection() as db:db.executemany('INSERT INTO observations VALUES(?,?,?,?,?,?)',[(i%20+1,f'fixture{i:08}','{"price":20}','{"cost":10}',time.time(),'') for i in range(10000)])
  charts=SizeChartStore(root/'charts.sqlite3')
  with charts._connect() as db:db.executemany('INSERT INTO size_chart_templates(organization_key,payload_json,created_at,updated_at) VALUES(?,?,?,?)',[('synthetic',json.dumps({'name':f'Synthetic {i}','rows':[]}),now,now) for i in range(1000)])
  os.environ['AI_USAGE_BILLING_PATH']=str(root/'billing.sqlite3')
  db=billing._connect()
  columns='record_id,organization_key,created_at,actor_id,actor_name,salesperson,task_type,task_name,task_id,provider,model,request_id,input_tokens,output_tokens,cached_tokens,total_tokens,usage_known,estimated_cost_cny,payload_json'
  with db:
   db.executemany('INSERT INTO ai_usage_records('+columns+') VALUES ('+','.join(['?']*19)+')',[(f'record{i}','synthetic',now,i%20+1,'Synthetic','Synthetic','copy','Synthetic',str(i),'synthetic','synthetic',f'request{i}',100,50,0,150,1,.001,json.dumps({'record_id':f'record{i}','actor_id':i%20+1,'total_tokens':150,'estimated_cost_cny':.001})) for i in range(20000)])
  db.close()
  def check(fn,minimum=1,key=None):
   def measured(i):
    value=fn(i);rows=value.get(key) if key else value
    assert isinstance(rows,list) and len(rows)>=minimum
   return measured
  cases={
   'promotions.list20':check(lambda i:promotions.list_promotions(),20),
   'promotions.items_page':check(lambda i:promotions.list_items_page(i%20+1),50,'items'),
   'promotions.participation_filter':check(lambda i:promotions.list_promotions(participated='0'),20),
   'diagnosis.page10k':check(lambda i:diagnosis.list_page(list(range(1,21))),50,'rows'),
   'diagnosis.search10k':check(lambda i:diagnosis.list_page(list(range(1,21)),search='fixture00000100'),1,'rows'),
   'prices.read10k':lambda i: price.get(i%20+1,f'fixture{i%10000:08}'),
   'size_charts.list1k':check(lambda i:charts.list_templates('synthetic'),1000),
   'ai_billing.list20k':check(lambda i:billing.ai_usage_billing({'action':'list','organization_key':'synthetic','page_size':50}),50,'items'),
   'ai_billing.user_scope20k':check(lambda i:billing.ai_usage_billing({'action':'list','organization_key':'synthetic','scope_user_ids':[i%20+1],'page_size':50}),50,'items'),
  }
  for name,fn in cases.items():
   try:fn(0)
   except Exception as exc:report.setdefault('validation_failures',[]).append({'name':name,'error':str(exc)});print(name,type(exc).__name__,str(exc),flush=True);continue
   for concurrency in (1,8,32):
    report['stages'].append(run(name,fn,concurrency,seconds=3));out.write_text(json.dumps(report,indent=2))
 out.write_text(json.dumps(report,indent=2))
if __name__=='__main__':main()
