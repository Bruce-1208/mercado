from pathlib import Path
out=Path('artifacts/full-audit-20261012');s=(out/'stress_orders.py').read_text();s=s[:s.index('local=threading.local()')];s=s.replace("MYSQL_POOL_MAX_CONNECTIONS='12'","MYSQL_POOL_MAX_CONNECTIONS='6'")
s+='''
profile='staff'
namespace['get_current_workbench_user']=lambda: {'id':int(request.headers.get('X-Audit-User','1')),'role_key':'member','access_version':1,'organization_key':'synthetic'}
namespace['_authorized_order_store_ids']=lambda: None if profile=='admin' else [((int(request.headers.get('X-Audit-User','1'))-1)%20)+1]
CASES=[('list','/api/orders?page=1&page_size=50&include_summary=0'),('summary','/api/orders?summary_only=1'),('page2','/api/orders?page=2&page_size=50&include_summary=0'),('search','/api/orders?search=Synthetic&include_summary=0')]
def summarize(rows):
 vals=sorted(x['ms'] for x in rows)
 return {'requests':len(rows),'failed':sum(not x['ok'] for x in rows),'error_percent':round(100*sum(not x['ok'] for x in rows)/max(len(rows),1),2),**{'p'+str(p)+'_ms':vals[min(math.ceil(len(vals)*p/100)-1,len(vals)-1)] if vals else 0 for p in (50,95,99)},'max_ms':max(vals,default=0)}
def stage(kind,users,seconds,think=5):
 global profile
 profile=kind;order_read_cache.invalidate();begin=time.monotonic();deadline=begin+seconds;barrier=threading.Barrier(users);records=[];lock=threading.Lock();peak_db=0;peak_flights=0
 def user_run(uid):
  session=requests.Session();session.trust_env=False;session.headers.update({'X-Synthetic-Test':'orders','X-Audit-User':str(uid)});barrier.wait();step=0;rows=[]
  while time.monotonic()<deadline:
   case=1 if step%10==1 else 2 if step%10==5 else 3 if step%10==8 else 0;name,path=CASES[case];start=time.monotonic()
   try:
    r=session.get('http://127.0.0.1:15099'+path,timeout=(3,15));data=r.json();ok=r.status_code==200 and data.get('status')=='success'
    if ok and kind=='staff':
     allowed=((uid-1)%20)+1
     ok=all(int(row.get('store_id',allowed))==allowed for row in data.get('data',{}).get('rows',[]))
    row={'user':uid,'endpoint':name,'ms':round((time.monotonic()-start)*1000,2),'ok':ok,'status':r.status_code}
   except Exception as e:row={'user':uid,'endpoint':name,'ms':round((time.monotonic()-start)*1000,2),'ok':False,'status':type(e).__name__}
   rows.append(row);step+=1;time.sleep(min(think,max(0,deadline-time.monotonic())))
  session.close();return rows
 with ThreadPoolExecutor(users) as pool:
  futures=[pool.submit(user_run,i+1) for i in range(users)]
  while not all(f.done() for f in futures):
   peak_db=max(peak_db,pool_status()['active']);peak_flights=max(peak_flights,sum(x[1] for x in order_read_cache._FLIGHTS.copy().values()));time.sleep(.05)
  for future in futures:records.extend(future.result())
 elapsed=time.monotonic()-begin
 r={'profile':kind,'users':users,'think_seconds':think,'duration_seconds':round(elapsed,2),'actual_rps':round(len(records)/elapsed,2),'peak_db_active':peak_db,'peak_order_requests':peak_flights,**summarize(records),'per_endpoint':{n:summarize([x for x in records if x['endpoint']==n]) for n,_ in CASES},'samples':records,'pool_after':pool_status()}
 print(json.dumps({k:v for k,v in r.items() if k!='samples'}),flush=True);return r
out=Path(__file__).with_name('multiuser.json');results={'scope':'Synthetic users with separate cache scopes. Staff each authorized for one of 20 stores (4000 orders/store); admins authorized for all 80000 orders. Actual current order handler and SQL, simulated identity/authentication SELECT 1, Waitress32 / DB pool6 / isolated MySQL128MiB. Each user waits 5s AFTER response, synchronized cold starts. Request pattern per10: 7 list,1 summary,1 next-page,1 keyword search; short stages may not reach search. No real background jobs or external APIs. Closed-loop model underdelivers target traffic when slow.','stages':[]}
try:
 for kind,users,seconds in [('staff',5,35),('staff',10,35),('staff',20,35),('staff',40,35),('staff',80,35),('admin',5,30),('admin',10,30),('admin',20,30),('staff',5,20)]:
  results['stages'].append(stage(kind,users,seconds));out.write_text(json.dumps(results,indent=2))
finally:
 server.task_dispatcher.shutdown();server.close();thread.join(timeout=3);close_all_pools();out.write_text(json.dumps(results,indent=2))
'''
(out/'multiuser.py').write_text(s);Path('/private/tmp/mercado-full-audit-20261012/artifacts/full-audit-20261012/multiuser.py').write_text(s)
