import json
from collections import Counter
from bit import bit_mysql
c=bit_mysql.pymysql.connect(**{**bit_mysql.config,'read_timeout':15})
try:
 with c.cursor() as q:
  q.execute('START TRANSACTION READ ONLY')
  q.execute('SELECT id,rule_key,status,started_at,detail_json FROM workbench_automation_runs ORDER BY id DESC LIMIT 300')
  rows=q.fetchall()
  errors=Counter()
  def walk(v):
   if isinstance(v,dict):
    for k,x in v.items():
     if k in ('error','message','errors') and x: errors[str(x)[:800]]+=1
     elif isinstance(x,(dict,list)):walk(x)
   elif isinstance(v,list):
    for x in v:walk(x)
  for r in rows:
   if r['status']=='failed':walk(json.loads(r['detail_json']))
  result={'count':len(rows),'newest':str(rows[0]['started_at']) if rows else None,'oldest':str(rows[-1]['started_at']) if rows else None,'statuses':[(k,n) for k,n in Counter((r['rule_key'],r['status']) for r in rows).items()],'failure_messages':errors.most_common(15),'recent_statuses':[{'id':r['id'],'rule':r['rule_key'],'status':r['status'],'time':str(r['started_at'])} for r in rows[:10]]}
  print(json.dumps(result,ensure_ascii=False,indent=2))
finally:c.rollback();c.close()
