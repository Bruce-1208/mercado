"""40 simultaneous cold order reads, then loss/recovery of our test DB socket."""
from pathlib import Path
source=Path(__file__).with_name('stress_orders.py').read_text()
exec(compile(source.split("results={'scope':",1)[0],str(Path(__file__).with_name('stress_orders.py')),'exec'))
namespace['db_list_orders']=db.list_orders
order_read_cache.invalidate()
barrier=threading.Barrier(40)
active=0;peak=0;lock=threading.Lock()
def call(i):
 global active,peak
 barrier.wait(timeout=5)
 with lock:
  active+=1;peak=max(peak,active)
 try:return request_one(i*6)
 finally:
  with lock:active-=1
try:
 start=time.monotonic()
 with ThreadPoolExecutor(40) as executor:rows=list(executor.map(call,range(40)))
 result={'scope':'isolated synthetic orders, 40 simultaneous cold requests across pages 1-3, including cache coalescing','actual_peak_client_inflight':peak,'elapsed_seconds':round(time.monotonic()-start,2),**summarize(rows),'samples':rows}
 # Kill only the test connection we own, not the MySQL service or other clients.
 import pymysql
 connection=get_db_connection()
 with connection.cursor() as q:
  q.execute('SELECT CONNECTION_ID() AS id');connection_id=q.fetchone()['id']
 connection.close()
 killer=pymysql.connect(host='127.0.0.1',port=13307,user='root',database='stress_test')
 with killer.cursor() as q:q.execute('KILL CONNECTION %s',(connection_id,))
 killer.close()
 connection=get_db_connection()
 try:
  with connection.cursor() as q:q.execute('SELECT 1 AS ok');result['idle_socket_recovery']=q.fetchone()['ok']==1
 finally:connection.close()
 result['pool_after']=pool_status()
 Path(__file__).with_name('burst-and-recovery.json').write_text(json.dumps(result,indent=2))
 print(json.dumps({k:v for k,v in result.items() if k!='samples'}),flush=True)
finally:
 server.task_dispatcher.shutdown();server.close();thread.join(timeout=3);close_all_pools()
