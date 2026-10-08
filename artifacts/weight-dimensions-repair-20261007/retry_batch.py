"""Retry an explicitly selected interrupted batch using the normal workflow."""
import os,sys,json,uuid,argparse
from pathlib import Path
from collections import Counter
sys.path.insert(0,'/Users/a11/mercado')
os.environ['BIT_BACKGROUND_SERVICES_DISABLED']='1'
from bit import bit_mysql as db
from bit import weight_dimensions_records as wdr
from bit.bit_runtime_lock import InterProcessLock
from erp.package_update_weight import package_update_weight_g

parser=argparse.ArgumentParser()
parser.add_argument('--apply',action='store_true')
parser.add_argument('--limit',type=int,default=0)
args=parser.parse_args()
batch='12a73c693cdf4e7ba5622469e9f9c019'
root=Path('/Users/a11/mercado/artifacts/weight-dimensions-repair-20261007')
conn=db.pymysql.connect(**db.config)
try:
 with conn.cursor() as cursor:
  cursor.execute("SELECT order_number,record_json,execution_logs_json FROM zying_weight_dimensions_records WHERE execution_logs_json LIKE %s",('%'+batch+'%',))
  saved=cursor.fetchall()
  cursor.execute('SELECT id,enabled FROM mercado_store_tokens')
  tokens=cursor.fetchall()
finally:conn.close()
rows=[]
for saved_row in saved:
 row=json.loads(saved_row['record_json']); logs=json.loads(saved_row['execution_logs_json'])
 last_entry=next((e for e in reversed(logs) if e.get('execution_id')), {})
 last_execution=last_entry.get('execution_id')
 if (last_execution != batch and last_entry.get('operator') != 'repair:20261007') or row.get('_zeshun_status')=='成功':continue
 if not any(e.get('execution_id')==batch and e.get('stage')=='任务' and 'interpreter shutdown' in e.get('message','') for e in logs):continue
 row['execution_logs']=logs
 if wdr._public_record(row)['can_execute_zeshun']:rows.append(row)
rows.sort(key=lambda row:(row.get('time',''),row['order_number']),reverse=True)
if args.limit:rows=rows[:args.limit]
plan=[{'order_number':r['order_number'],'marketplace_item_id':r['marketplace_item_id'],'global_item_id':r['_global_item_id'],'submitted_weight_g':wdr._marketplace_package_weight_g(package_update_weight_g(r)),'actual_dimensions_cm':r['actual_dimensions_cm']} for r in rows]
print(json.dumps({'apply':args.apply,'batch':batch,'selected':len(rows),'plan':plan},ensure_ascii=False),flush=True)
if not args.apply:raise SystemExit(0)
if not rows:raise SystemExit('No eligible interrupted records remain')
(root/('retry-before-'+str(len(rows))+'.json')).write_text(json.dumps(rows,ensure_ascii=False,default=str,indent=2))
lock=InterProcessLock('weight-dimensions-retry-'+batch,owner='weight-dimensions-repair')
if not lock.acquire(timeout=0):raise SystemExit('This repair batch is already running')
try:
 task_id='repair-'+uuid.uuid4().hex
 task={'task_id':task_id,'owner':'repair:20261007','status':'ready','execute_status':'queued','execute_action':'zeshun','execute_order_numbers':[r['order_number'] for r in rows],'records':rows,'authorized_ids':[int(t['id']) for t in tokens if t['enabled']]}
 wdr._tasks[task_id]=task
 original_log=wdr._log_execution
 def record_log(row,stage,status,message):
  original_log(row,stage,status,message)
  print(json.dumps({'order':row.get('order_number'),'item':row.get('marketplace_item_id'),'stage':stage,'status':status,'message':message},ensure_ascii=False),flush=True)
 wdr._log_execution=record_log
 wdr._run_execute_guarded(task_id,task['owner'],'zeshun',task['execute_order_numbers'],None)
 result={'task_id':task_id,'execute_status':task.get('execute_status'),'message':task.get('execute_message'),'states':dict(Counter(r.get('_zeshun_status','') for r in rows)),'records':[wdr._public_record(r) for r in rows]}
 (root/('retry-result-'+str(len(rows))+'.json')).write_text(json.dumps(result,ensure_ascii=False,default=str,indent=2))
 print(json.dumps({'result':result['states'],'message':result['message']},ensure_ascii=False),flush=True)
finally:lock.release()
