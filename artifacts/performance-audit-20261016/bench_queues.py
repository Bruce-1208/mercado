"""Isolated SQLite/control-plane stress; upstream work is a timed stub (20ms).
Measures admission/queue/storage overhead, not marketplace/AI generation speed.
"""
import sys,os,time,json,math,tempfile,threading,socket
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
os.environ['BIT_BACKGROUND_SERVICES_DISABLED']='1'
def no_network(*a,**k):raise PermissionError('Queue benchmark is entirely offline')
socket.socket.connect=no_network
from bit.local_agent_hub import LocalAgentStore
from bit.background_exports import ExportQueue
from bit.sync_capacity import SyncBudget
from mercado_api.request_budget import RequestBudget
from erp.ai_weight_price.service import Service

def summary(rows):
 times=sorted(x['ms'] for x in rows)
 return {'requests':len(rows),'failed':sum(not x['ok'] for x in rows),**{f'p{p}_ms':times[max(0,math.ceil(len(times)*p/100)-1)] for p in (50,95,99)}}
def run(name,fn,workers,seconds=3):
 begin=time.perf_counter();deadline=begin+seconds;rows=[];pending=set();i=0
 def one(index):
  start=time.perf_counter()
  try:fn(index);return {'ms':(time.perf_counter()-start)*1000,'ok':True}
  except Exception as e:return {'ms':(time.perf_counter()-start)*1000,'ok':False,'error':f'{type(e).__name__}: {e}'}
 with ThreadPoolExecutor(workers) as pool:
  while time.perf_counter()<deadline:
   while len(pending)<workers:pending.add(pool.submit(one,i));i+=1
   done,pending=wait(pending,return_when=FIRST_COMPLETED);rows.extend(f.result() for f in done)
  rows.extend(f.result() for f in pending)
 elapsed=time.perf_counter()-begin
 result={'name':name,'concurrency':workers,'duration_s':elapsed,'rps':len(rows)/elapsed,**summary(rows),'samples':rows}
 print(json.dumps({k:v for k,v in result.items() if k!='samples'}),flush=True);return result

def main():
 out=Path(__file__).with_name('queues.json');report={'scope':__doc__,'stages':[]}
 with tempfile.TemporaryDirectory(prefix='mercado-queue-perf-') as tmp:
  root=Path(tmp);hub=LocalAgentStore(root/'hub.sqlite3')
  for i in range(100):
   hub.heartbeat(f'agent{i}',name=f'Synthetic {i}',session_id='session')
   hub.enqueue_job(f'job{i}',f'agent{i}','daily_task',{})
   hub.claim_job(f'agent{i}',session_id='session')
  for i in range(1000):hub.append_event('job0','agent0',content='Synthetic log line '*20)
  service=Service(root/'weight',storage_backend='sqlite',migrate_legacy_state=False)
  service.bind_actor({'id':1})
  with service.store.connect() as db:
   db.executemany('INSERT INTO tasks VALUES(?,?,?,?,?,?)',[(str(i),'pending','collected',json.dumps({'erp_goods_id':str(i),'title':'Synthetic','owner_user_id':1,'padding':'x'*1000}),time.time(),time.time()) for i in range(10000)])
  def status(i):
   service.bind_actor({'id':1});value=service.status();assert isinstance(value,dict) and 'login' in value
  def event(i):hub.append_event(f'job{i%100}',f'agent{i%100}',content='Synthetic event')
  def heartbeat(i):hub.heartbeat(f'agent{i%100}',name=f'Synthetic {i%100}',session_id='session')
  def claim(i):assert hub.claim_job(f'agent{i%100}',session_id='session') is None
  def readlog(i):assert hub.event_page('job0',limit=200)
  def jobs(i):assert len(hub.list_jobs(limit=100))==100
  def progress(i):
   service.bind_actor({'id':1});service.store.record_visual_event(str(i%10000),{'id':f'e{i}','message':'Synthetic step','step':'candidate'})
  def make_budget(kind):
   budget=SyncBudget(8,2) if kind=='store_sync' else RequestBudget(32,8)
   def fn(i):
    slot=budget.slot('orders' if i%4==0 else 'links',i%20,timeout=3) if kind=='store_sync' else budget.slot('Synthetic'+str(i%4),timeout=3)
    with slot:time.sleep(.02)
   return budget,fn
  for name,fn in [('agent.heartbeat',heartbeat),('agent.poll_busy',claim),('agent.log_write',event),('agent.log_read',readlog),('agent.jobs',jobs),('weight_price.status_10k',status),('weight_price.progress',progress)]:
   for workers in (1,8,32):report['stages'].append(run(name,fn,workers));out.write_text(json.dumps(report,indent=2))
  for name in ('store_sync','upstream_budget'):
   budget,fn=make_budget(name)
   for workers in (1,8,32,128):report['stages'].append(run(name+'.20ms_stub',fn,workers));out.write_text(json.dumps(report,indent=2))
   assert budget.active==0
   if name=='store_sync':assert budget.snapshot()['waiting']==0
  exports=ExportQueue(root/'exports')
  def enqueue(i):
   try:exports.enqueue({'id':i+1},'synthetic','/api/reputation/latest/export');return True
   except OverflowError:return False
  with ThreadPoolExecutor(32) as pool:accepted=list(pool.map(enqueue,range(200)))
  with exports.connect() as db:count=db.execute('SELECT COUNT(*) FROM exports').fetchone()[0]
  assert sum(accepted)==100 and count==100
  report['export_admission']={'submissions':200,'concurrency':32,'accepted':100,'rejected_by_capacity':100,'persisted':count}
  processed=0
  while exports.process_one(lambda job:(b'Synthetic export','text/plain','attachment')):processed+=1
  assert processed==100
  report['export_admission']['completed']=processed
 out.write_text(json.dumps(report,indent=2))
if __name__=='__main__':main()
